"""T11运行产物独立审计。审计结果写运行目录之外，保持原清单封存。"""
import argparse
import calendar
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path


def _load(path):return json.loads(path.read_text(encoding='utf-8-sig'))


def _pointer(value,pointer):
    for part in pointer.strip('/').split('/'):
        if part:value=value[int(part)] if isinstance(value,list) else value[part]
    return value


def _closing_structure(rows, config):
    """独立复核结转排除结构，不调用转换引擎的识别函数。"""
    profile=config['source_profile'].get('closing_recognition')
    if not profile:return False
    scope=profile.get('applicability')
    if any(scope is not None and scope!={'period':r['period'],'source_sha256':r['provenance']['input_sha256']} for r in rows):return False
    if any(r['parse_state']!='parsed' or r['hierarchy']['decision']=='needs_review' for r in rows):return False
    selected=[r for r in rows if r['hierarchy']['decision']=='retained']
    roles=[];debits=[];credits=[]
    for row in selected:
        code=row['subject']['code'];matches=[k for k in profile['account_roles'] if code==k or code.startswith(k+'.')]
        role=profile['account_roles'][max(matches,key=len)] if matches else 'unknown'
        if role not in ['income','expense','current_profit']:return False
        values={}
        for side in ['debit','credit']:
            amount=row[side]
            if amount['status']=='blank' and amount['value'] is None:values[side]=Decimal(0)
            elif amount['status']=='number':values[side]=Decimal(amount['value'])
            else:return False
            if not values[side].is_finite():return False
        if not any(values.values()) or all(values.values()):return False
        if role=='income' and values['credit'] or role=='expense' and values['debit']:return False
        roles.append(role);debits.append(values['debit']);credits.append(values['credit'])
    with localcontext() as context:
        context.prec=128
        return ('current_profit' in roles and any(r in ['income','expense'] for r in roles)
                and sum(debits)==sum(credits) and bool(sum(debits) or sum(credits)))


def audit_run(output):
    output=Path(output).resolve();manifest_path=output/'run_manifest.json';m=_load(manifest_path)
    checks={'manifest_mode':m['mode']=='candidate_conversion','no_import_claim':not m['ready_for_system_import'] and not m['business_acceptance_passed']}
    paths_ok=True;hashes_ok=True
    for name,meta in m['artifacts'].items():
        p=output/name
        safe=Path(name).name==name and p.resolve().parent==output and not p.is_symlink()
        paths_ok &= safe
        hashes_ok &= safe and p.is_file() and p.stat().st_size==meta['bytes'] and hashlib.sha256(p.read_bytes()).hexdigest()==meta['sha256']
    checks['artifact_paths_within_run']=bool(paths_ok);checks['artifact_hashes_match']=bool(hashes_ok)
    checks['no_unlisted_artifacts']={p.name for p in output.iterdir()}==set(m['artifacts'])|{'run_manifest.json'}
    if m['status']=='failed':
        checks['failed_status_never_usable']=m['exit_code']==2 and not m['artifacts_complete'] and m['row_counts']['included']==0
        ref=m['row_decision_artifact'];path=output/ref['file']
        if path.is_file() and path.resolve().parent==output:
            decisions=_pointer(_load(path),ref['pointer'])
            checks['failed_decisions_do_not_include_or_exclude']=all(r['decision'] in ['needs_review','non_business'] for r in decisions)
        return {'run_status':m['status'],'usable_for_evaluation':False,'checks':checks,'all_passed':all(checks.values()),
            'manifest_sha256':hashlib.sha256(manifest_path.read_bytes()).hexdigest(),'exit_code':2}
    checks['normal_run_status']=m['status'] in ['completed','review_required'] and m['exit_code']==(0 if m['status']=='completed' else 3) and m['artifacts_complete']
    config=_load(output/'config_snapshot.json');schema_bytes=(output/'schema_snapshot.json').read_bytes()
    checks['config_snapshot_hash_matches_input']=hashlib.sha256((output/'config_snapshot.json').read_bytes()).hexdigest()==m['inputs']['config']['sha256']
    checks['schema_snapshot_matches']=hashlib.sha256(schema_bytes).hexdigest()==m['schema_sha256']
    payload={name:_load(output/(name+'.json')) for name in ['candidates','pending_rows','excluded_rows','diagnostic_candidates','source_rows','row_decisions']}
    checks['artifact_headers_match_run']=all(p['run_id']==m['run_id'] and p['requires_valid_manifest'] and p['row_count']==len(p['rows']) and p['source_data_available'] for p in payload.values())
    source=payload['source_rows']['rows'];decisions=payload['row_decisions']['rows'];by_id={r['record_id']:r for r in source};decisions_by_id={r['record_id']:r for r in decisions}
    checks['source_ids_unique']=len(by_id)==len(source)
    checks['source_provenance_matches_input']=all(r['provenance']['input_sha256']==m['inputs']['source']['sha256'] for r in source)
    checks['all_rows_have_one_decision']=Counter(r['record_id'] for r in decisions)==Counter(r['record_id'] for r in source)
    counts=Counter(r['decision'] for r in decisions)
    checks['terminal_counts_conserve_rows']=m['row_counts']['total']==len(source)==sum(counts.values()) and all(counts[k]==m['row_counts'][k] for k in ['included','excluded','needs_review','non_business'])
    for name,state in [('candidates','included'),('pending_rows','needs_review'),('excluded_rows','excluded')]:
        checks[name+'_matches_decisions']=Counter(r['record_id'] for r in payload[name]['rows'])==Counter(r['record_id'] for r in decisions if r['decision']==state)
    checks['diagnostic_files_separated']=payload['diagnostic_candidates']['diagnostic_only'] and all(r['diagnostic_only'] for r in payload['diagnostic_candidates']['rows']) and not payload['candidates']['diagnostic_only'] and all(not r['diagnostic_only'] for r in payload['candidates']['rows'])
    index=_load(output/'evidence_index.json');refs_ok=True
    for identity,ref in index['evidence'].items():
        path=output/ref['artifact']
        if path.resolve().parent!=output or ref['artifact'] not in m['artifacts']:refs_ok=False;continue
        refs_ok &= _pointer(_load(path),ref['pointer'])['evidence_id']==identity
    for identity,ref in index['rules'].items():
        refs_ok &= ref['artifact']=='config_snapshot.json' and _pointer(config,ref['pointer'])['id']==identity
    issues=_load(output/'exceptions.json')['issues'];issue_index={e['issue_id']:e for e in issues}
    checks['issue_ids_unique']=len(issue_index)==len(issues)
    checks['issue_references_resolve']=all(set(e['source_record_ids'])<=set(by_id) and set(e['affected_record_ids'])<=set(decisions_by_id) and set(e['evidence_ids'])<=set(index['evidence']) and set(e['rule_ids'])<=set(index['rules']) for e in issues)
    checks['review_rows_have_traceable_exceptions']=all(d['exception_ids'] and all(i in issue_index and d['record_id'] in issue_index[i]['affected_record_ids'] for i in d['exception_ids']) for d in decisions if d['decision']=='needs_review')
    checks['all_rule_and_evidence_pointers_resolve']=bool(refs_ok) and all(set(d['rule_ids'])<=set(index['rules']) for d in decisions)
    checks['exception_counts_match']=len(issues)==m['exception_issue_count'] and len({i for e in issues for i in e['affected_record_ids']})==m['affected_record_count']
    checked=payload['candidates']['rows']+payload['pending_rows']['rows']+payload['excluded_rows']['rows']+payload['diagnostic_candidates']['rows']
    checks['amounts_and_original_dates_preserved']=all(r['debit']==by_id[r['record_id']]['debit'] and r['credit']==by_id[r['record_id']]['credit'] and r['original_date']==by_id[r['record_id']]['date'] for r in checked)
    hierarchy_rule=next(r for r in config['rules'] if r['kind']=='hierarchy')
    hierarchy_groups={g['evidence_id']:g for g in _load(output/'stage_hierarchy.json')['groups']}
    hierarchy_exclusions_ok=True
    for row in source:
        if row.get('hierarchy',{}).get('decision')!='excluded':continue
        scope=hierarchy_rule.get('applicability')
        hierarchy_exclusions_ok &= hierarchy_rule['status']=='confirmed' and (scope is None or scope=={'period':m['period'],'source_sha256':m['inputs']['source']['sha256']})
        refs=row['hierarchy']['parent_evidence_ids']
        hierarchy_exclusions_ok &= bool(refs)
        for identity in refs:
            group=hierarchy_groups.get(identity)
            hierarchy_exclusions_ok &= group is not None and group['structure_verified'] and row['record_id'] in group['parent_record_ids'] and all(Decimal(group['parent_totals'][s])==Decimal(group['child_totals'][s]) for s in ['debit','credit'])
    checks['hierarchy_exclusions_have_approved_scoped_evidence']=bool(hierarchy_exclusions_ok)
    filter_rules={r['id']:r for r in config['rules'] if r['kind']=='voucher_filter'}
    filter_ok=True
    for group in _load(output/'stage_mapping.json')['filter_groups']:
        if group['decision']!='excluded':continue
        ctx=group['context']
        members=[r for r in source if r.get('voucher') and r['parse_state']!='non_business' and
                 (r['provenance']['input_sha256'],r['provenance']['sheet'],r['period'],r['voucher']['category'],r['voucher']['number'])==
                 tuple(ctx[k] for k in ['input_sha256','sheet','period','voucher_category','voucher_number'])]
        filter_ok &= bool(members) and Counter(r['record_id'] for r in members)==Counter(group['member_record_ids'])
        filter_ok &= all(decisions_by_id[r['record_id']]['decision']=='excluded' for r in members)
        verified=[]
        for identity in group['confirmed_full_rule_ids']:
            rule=filter_rules.get(identity)
            if not rule or rule['status']!='confirmed':continue
            scope=rule.get('applicability');params=rule['parameters']
            if scope is not None and scope!={'period':ctx['period'],'source_sha256':ctx['input_sha256']}:continue
            if not all(('summary_equals' not in params or r['summary']==params['summary_equals']) and
                       ('voucher_category_equals' not in params or r['voucher']['category']==params['voucher_category_equals']) for r in members):continue
            if 'classification_equals' in params and not _closing_structure(members,config):continue
            verified.append(identity)
        filter_ok &= bool(verified) and set(verified)==set(group['confirmed_full_rule_ids'])
    checks['voucher_exclusions_independently_approved_scoped_complete_and_structured']=bool(filter_ok)
    approved={r['id']:r for r in config['mappings'] if r['status']=='confirmed'}
    scoped_matches_ok=True
    for row in source:
        observed=row.get('account_mapping',{})
        if observed.get('status')!='matched':continue
        mapping=approved.get(observed['mapping_id'])
        if mapping is None:
            scoped_matches_ok=False;continue
        scope=mapping.get('applicability')
        scoped_matches_ok &= (scope is None or scope=={'period':row['period'],'source_sha256':row['provenance']['input_sha256']}) and (row['subject']['code'],observed['target_code'],observed['target_name'])==(mapping['source_code'],mapping['target_code'],mapping['target_name'])
    for row in checked:
        observed=by_id[row['record_id']].get('account_mapping',{})
        expected=(observed.get('target_code'),observed.get('target_name')) if observed.get('status')=='matched' else (None,None)
        scoped_matches_ok &= (row['target_subject_code'],row['target_subject_name'])==expected
    checks['all_matched_targets_respect_approved_case_scope']=bool(scoped_matches_ok)
    candidates=payload['candidates']['rows'];mapped_ok=True;dates_ok=True
    for r in candidates:
        mapping=approved.get(r['mapping_id']);src=by_id[r['record_id']]
        mapped_ok &= mapping is not None and (mapping['source_code'],mapping['target_code'],mapping['target_name'])==(r['external_subject_code'],r['target_subject_code'],r['target_subject_name']) and src['voucher_validation']['decision']=='retained'
        period=r['period'];expected=date(int(period[:4]),int(period[4:]),calendar.monthrange(int(period[:4]),int(period[4:]))[1]).isoformat()
        dates_ok &= r['posting_date']==expected and type(r['entry_sequence']) is int and r['entry_sequence']>0
    checks['candidates_use_only_approved_targets']=bool(mapped_ok);checks['candidate_dates_and_sequence_types_correct']=bool(dates_ok)
    aux=next(r for r in config['rules'] if r['kind']=='auxiliary')
    profile=config.get('auxiliary_profile')
    checks['unresolved_auxiliary_never_exported']=not candidates or aux['status']=='confirmed' and (profile is not None or not aux['parameters']['required_fields'])
    if profile:
        in_scope=profile['applicability']=={'period':m['period'],'source_sha256':m['inputs']['source']['sha256']}
        requirements={r['target_code']:r['fields'] for r in profile['account_requirements']}
        proposal_ok=True;values_ok=True;requirements_by_id={};proposal_keys=[]
        for row in source:
            observed=row.get('account_mapping',{});values=row.get('auxiliary',{});proposals=row.get('auxiliary_candidates',{})
            fields=set(requirements.get(observed.get('target_code'),[]))|set(aux['parameters']['required_fields'])
            requirements_by_id[row['record_id']]=fields
            eligible=row['voucher_validation']['decision'] not in ['excluded','non_business'] and observed.get('status')=='matched' and ('known_target_codes' not in profile or observed['target_code'] in profile['known_target_codes'])
            expected_fields={}
            def raw_explicit(sf):
                raw=row.get('raw_fields',{}).get(sf,{})
                if raw.get('value') is None or not str(raw['value']).strip():return None
                value=str(raw['value']).strip()
                if raw.get('type') not in ['str','int','float','Decimal']:return False
                if sf.endswith('_code') and raw.get('type') in ['float','Decimal']:
                    number=Decimal(value)
                    if not number.is_finite() or number!=number.to_integral_value():return False
                    value=str(int(number))
                return value
            if eligible and in_scope:
                for field in fields:
                    for strategy in profile.get('source_priority',['explicit_code','explicit_name','subject_name']):
                        if strategy=='subject_name':
                            parts=row['subject']['name'].split(profile['delimiter'])
                            if len(fields)==1 and len(parts)==2 and all(p.strip() for p in parts):expected_fields[field]=('subject_name',row['subject']['name'],parts[1].strip(),'name')
                            break
                        sf=field+'_code' if strategy=='explicit_code' else field
                        value=raw_explicit(sf)
                        if value is False:break
                        if value is not None:
                            expected_fields[field]=(sf,value,value,'code' if strategy=='explicit_code' else 'name');break
            proposal_ok &= set(proposals)==set(expected_fields)
            for field,p in proposals.items():
                sf,source_value,lookup_value,lookup=expected_fields.get(field,('',None,None,'name'))
                matches=[d for d in profile['dictionary'] if d['field']==field and d['code' if lookup=='code' else 'name']==lookup_value]
                status='missing' if not matches else 'ambiguous' if len(matches)!=1 or (lookup=='name' and sum(d['field']==field and d['code']==matches[0]['code'] for d in profile['dictionary'])!=1) else 'matched'
                name=matches[0]['name'] if lookup=='code' and status=='matched' else lookup_value if lookup=='name' else ''
                explicit_name=raw_explicit(field)
                conflict=lookup=='code' and status=='matched' and explicit_name is not None and explicit_name!=name
                reference_name=profile.get('name_validation_policy')=='source_name_reference_only' and lookup=='name' and status=='missing'
                approved_value=in_scope and profile['status']=='confirmed' and aux['status']=='confirmed' and (status=='matched' or reference_name) and not conflict
                proposal_ok &= p['value']==name and p['source_field']==sf and p['source_value']==source_value and p['source_provenance']==row['provenance'] and p['dictionary_status']==status and p['dictionary_code']==(matches[0]['code'] if status=='matched' else None) and p['approved']==approved_value
                proposal_keys.append((row['record_id'],field,p['value'],p['dictionary_status'],p['dictionary_code']))
            if values:
                values_ok &= in_scope and profile['status']=='confirmed' and aux['status']=='confirmed' and all(field in proposals and proposals[field]['approved'] and value==proposals[field]['value'] for field,value in values.items())
        checks['auxiliary_proposals_independently_reextracted']=bool(proposal_ok)
        checks['auxiliary_values_require_scope_approval_and_unique_dictionary_or_configured_source_name']=bool(values_ok)
        checks['all_normalized_auxiliary_views_equal_source']=all(r['auxiliary']==by_id[r['record_id']].get('auxiliary',{}) and r.get('auxiliary_candidates',{})==by_id[r['record_id']].get('auxiliary_candidates',{}) for r in checked)
        checks['official_auxiliary_requirements_complete']=all(set(r['auxiliary'])==requirements_by_id[r['record_id']] and profile['status']=='confirmed' and aux['status']=='confirmed' and in_scope and ('known_target_codes' not in profile or r['target_subject_code'] in profile['known_target_codes']) for r in candidates)
        proposal_file=_load(output/'auxiliary_candidates.json')
        checks['auxiliary_proposal_file_occurrences_match']=proposal_file['run_id']==m['run_id'] and proposal_file['row_count']==len(proposal_file['rows']) and Counter(proposal_keys)==Counter((p['record_id'],p['field'],p['value'],p['dictionary_status'],p['dictionary_code']) for p in proposal_file['rows'])
    groups=defaultdict(list)
    for r in candidates:groups[(r['period'],r['voucher_category'],r['voucher_number'])].append(r)
    complete=True;amounts_ok=True
    stage_voucher=_load(output/'stage_voucher.json')
    official={g['evidence_id']:g for g in stage_voucher['official_vouchers']}
    for members in groups.values():
        ids=[r['record_id'] for r in members];evidence_ids={r['voucher_evidence_id'] for r in members}
        g=official.get(next(iter(evidence_ids))) if len(evidence_ids)==1 else None
        complete &= g is not None and g['status']=='balanced' and Counter(ids)==Counter(g['selected_record_ids'])
        order=sorted(members,key=lambda r:(r['provenance']['excel_row'],r['record_id']))
        complete &= [r['entry_sequence'] for r in order]==list(range(1,len(order)+1))
        with localcontext() as context:
            context.prec=max(64,sum(len(r[s]['value'] or '') for r in members for s in ['debit','credit'])+2)
            d=sum((Decimal(r['debit']['value']) if r['debit']['value'] is not None else Decimal(0) for r in members),Decimal(0))
            c=sum((Decimal(r['credit']['value']) if r['credit']['value'] is not None else Decimal(0) for r in members),Decimal(0))
            amounts_ok &= d==c
    checks['candidate_vouchers_complete_and_continuous']=bool(complete);checks['candidate_vouchers_independently_balanced']=bool(amounts_ok)
    expected_diag=[a['record_id'] for g in stage_voucher['diagnostic_vouchers'] for a in g['assignments']]
    checks['diagnostic_occurrences_preserved']=Counter(expected_diag)==Counter(r['record_id'] for r in payload['diagnostic_candidates']['rows']) and len(expected_diag)==m['diagnostic_candidate_count']
    if 'allow_partial' in m:
        expected_partial=bool(candidates and m['row_counts']['needs_review'])
        checks['partial_export_requires_explicit_user_choice']=not expected_partial or m['allow_partial'] is True
        checks['partial_export_never_claims_complete_batch']=m['partial_export']==expected_partial and m['batch_complete']==(m['row_counts']['needs_review']==0) and (not expected_partial or m['status']=='review_required')
    return {'run_status':m['status'],'usable_for_evaluation':all(checks.values()),'checks':checks,'all_passed':all(checks.values()),
        'manifest_sha256':hashlib.sha256(manifest_path.read_bytes()).hexdigest(),'row_counts':m['row_counts'],
        'diagnostic_candidate_count':m['diagnostic_candidate_count'],'exception_issue_count':len(issues),'exit_code':0 if all(checks.values()) else 1}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run',required=True,type=Path)
    parser.add_argument('--audit-out',type=Path,help='可选新审计文件，必须位于运行目录外')
    args=parser.parse_args()
    try:
        if args.audit_out and (args.audit_out.resolve()==args.run.resolve() or args.run.resolve() in args.audit_out.resolve().parents):
            raise ValueError('审计结果必须写运行目录之外，不能改变已封存产物')
        result=audit_run(args.run)
        if args.audit_out:
            args.audit_out.parent.mkdir(parents=True,exist_ok=True)
            with args.audit_out.open('x',encoding='utf-8') as stream:json.dump(result,stream,ensure_ascii=False,indent=2)
        print(json.dumps(result,ensure_ascii=False,indent=2));return result['exit_code']
    except (OSError,ValueError,KeyError,TypeError) as error:
        print(json.dumps({'all_passed':False,'usable_for_evaluation':False,'error':str(error)},ensure_ascii=False));return 2


if __name__=='__main__':raise SystemExit(main())
