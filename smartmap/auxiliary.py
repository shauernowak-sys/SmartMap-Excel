"""按明确科目要求及原始名称分隔规则取辅助候选；不查人工答案。"""
import copy
import hashlib
import json
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

from .applicability import applies_to
from .configuration import compile_config
from .grouping import voucher_key


class AuxiliaryError(ValueError):
    pass


def explicit_value(row,field):
    raw=row.get('raw_fields',{}).get(field)
    if not raw or raw.get('value') is None or not str(raw['value']).strip():return None
    if raw.get('type') not in ['str','int','float','Decimal']:return ('invalid',str(raw['value']))
    value=str(raw['value']).strip()
    if field.endswith('_code') and raw.get('type') in ['float','Decimal']:
        try:
            number=Decimal(value)
            if not number.is_finite() or number!=number.to_integral_value():return ('invalid',value)
            value=str(int(number))
        except InvalidOperation:return ('invalid',value)
    return ('valid',value)


def analyze_auxiliary(voucher_report, config):
    _,fingerprint=compile_config(config)
    if voucher_report.get('mode')!='voucher_validation' or not voucher_report.get('analysis_ok') or (
        voucher_report.get('config_id'),voucher_report.get('config_revision'),voucher_report.get('effective_config_sha256'))!=(config['config_id'],config['revision'],fingerprint):
        raise AuxiliaryError('辅助取值的上游状态或配置不一致')
    profile=config.get('auxiliary_profile')
    if not profile:return copy.deepcopy(voucher_report)
    report=copy.deepcopy(voucher_report)
    requirements={r['target_code']:r['fields'] for r in profile['account_requirements']}
    by_name=defaultdict(list);by_code=defaultdict(list)
    for entry in profile['dictionary']:
        by_name[(entry['field'],entry['name'])].append(entry)
        by_code[(entry['field'],entry['code'])].append(entry)
    rule=next(r for r in config['rules'] if r['kind']=='auxiliary')
    applicable=applies_to(profile,period=report['period'],source_sha256=report['input_sha256'])
    approved=profile['status']=='confirmed' and rule['status']=='confirmed' and applicable
    for row in report['rows']:
        row['auxiliary']={};row['auxiliary_candidates']={}
        if row['voucher_validation']['decision'] in ['excluded','non_business']:
            row['auxiliary_validation']={'ready':True,'status':'not_evaluated','required_fields':[],'reason_codes':[]};continue
        mapping=row.get('account_mapping',{})
        if mapping.get('status')!='matched':
            row['auxiliary_validation']={'ready':False,'status':'mapping_unresolved','required_fields':[],'reason_codes':['AUXILIARY_TARGET_UNRESOLVED']};continue
        if 'known_target_codes' in profile and mapping['target_code'] not in profile['known_target_codes']:
            row['auxiliary_validation']={'ready':False,'status':'catalog_unresolved','required_fields':[],'reason_codes':['AUXILIARY_TARGET_ACCOUNT_NOT_IN_CATALOG']};continue
        fields=sorted(set(requirements.get(mapping['target_code'],[]))|set(rule['parameters']['required_fields']))
        reasons=[]
        if not applicable:reasons.append('AUXILIARY_PROFILE_OUT_OF_SCOPE')
        if profile['status']!='confirmed':reasons.append('AUXILIARY_PROFILE_UNCONFIRMED' if profile['status']=='candidate' else 'AUXILIARY_PROFILE_REJECTED')
        if rule['status']!='confirmed':reasons.append('AUXILIARY_REQUIREMENT_UNKNOWN')
        for field in fields:
            if not applicable:continue
            selected=None;lookup='name';source_field=None
            priority=profile.get('source_priority',['explicit_code','explicit_name','subject_name'])
            for strategy in priority:
                if strategy in ['explicit_code','explicit_name']:
                    sf=field+'_code' if strategy=='explicit_code' else field
                    explicit=explicit_value(row,sf)
                    if explicit is not None:
                        if explicit[0]=='invalid':reasons.append('AUXILIARY_SOURCE_VALUE_INVALID');selected=False;break
                        selected=explicit[1];source_field=sf;lookup='code' if strategy=='explicit_code' else 'name';break
                else:
                    if len(fields)!=1:reasons.append('AUXILIARY_MULTIPLE_FIELDS_SOURCE_AMBIGUOUS');selected=False;break
                    parts=row['subject']['name'].split(profile['delimiter'])
                    if len(parts)!=2 or not all(p.strip() for p in parts):
                        reasons.append('AUXILIARY_SOURCE_FORMAT_AMBIGUOUS');selected=False;break
                    selected=parts[1].strip();source_field='subject_name';break
            if selected is None:reasons.append('AUXILIARY_SOURCE_MISSING');continue
            if selected is False:continue
            matches=by_code[(field,selected)] if lookup=='code' else by_name[(field,selected)]
            status='missing' if not matches else 'ambiguous' if len(matches)!=1 or (lookup=='name' and len(by_code[(field,matches[0]['code'])])!=1) else 'matched'
            extracted=matches[0]['name'] if lookup=='code' and status=='matched' else selected if lookup=='name' else ''
            conflict=False
            provided_name=explicit_value(row,field)
            if lookup=='code' and status=='matched' and provided_name and (provided_name[0]!='valid' or provided_name[1]!=extracted):
                conflict=True;reasons.append('AUXILIARY_EXPLICIT_VALUE_CONFLICT')
            reference_name = (profile.get('name_validation_policy')=='source_name_reference_only'
                              and lookup=='name' and status=='missing')
            resolved = status=='matched' or reference_name
            proposal={'field':field,'value':extracted,'source_field':source_field,'lookup_kind':lookup,
                          'source_value':row['subject']['name'] if source_field=='subject_name' else selected,
                          'source_provenance':copy.deepcopy(row['provenance']),'profile_id':profile['id'],
                          'dictionary_status':status,'dictionary_code':matches[0]['code'] if status=='matched' else None,
                          'dictionary_references':[e['reference'] for e in matches], 'candidate_names':[e['name'] for e in matches],
                          'validation_basis':'source_name_reference_only' if reference_name else 'unique_dictionary',
                          'approved':approved and resolved and not conflict}
            row['auxiliary_candidates'][field]=proposal
            if not resolved:reasons.append('AUXILIARY_DICTIONARY_MISSING' if status=='missing' else 'AUXILIARY_DICTIONARY_AMBIGUOUS')
            elif approved and not conflict:row['auxiliary'][field]=extracted
        ready=approved and not reasons and len(row['auxiliary'])==len(fields)
        row['auxiliary_validation']={'ready':ready,'status':'ready' if ready else 'needs_review','required_fields':fields,'reason_codes':sorted(set(reasons))}
    # 任何必需辅助值缺失，整凭证阻断；候选不视为正式值。
    blocked={voucher_key(r) for r in report['rows'] if r['voucher_validation']['decision'] not in ['excluded','non_business'] and not r['auxiliary_validation']['ready']}
    for row in report['rows']:
        if row['voucher_validation']['decision'] not in ['excluded','non_business']:
            row['auxiliary_validation']['voucher_ready']=voucher_key(row) not in blocked
    proposals=[v for r in report['rows'] for v in r['auxiliary_candidates'].values()]
    report['auxiliary_analysis']={'profile_id':profile['id'],'profile_status':profile['status'],'applicable':applicable,
        'profile_sha256':hashlib.sha256(json.dumps(profile,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest(),
        'candidate_fields':len(proposals),'dictionary_status_counts':dict(Counter(v['dictionary_status'] for v in proposals)),
        'approved_fields':sum(len(r['auxiliary']) for r in report['rows']),'blocked_voucher_count':len(blocked)}
    return report
