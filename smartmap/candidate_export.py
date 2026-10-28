"""T11候选与终态分流：不批准配置、不合并重复分录、不生成系统模板。"""
import copy
from collections import Counter
from decimal import Decimal

from .amounts import AmountError, total_rows
from .configuration import compile_config
from .grouping import voucher_key
from .vouchers import month_end
from .applicability import applies_to
from .auxiliary import analyze_auxiliary


class ExportError(ValueError):
    def __init__(self,code,message):
        super().__init__(message);self.code=code


def normalize_row(row,diagnostic=False):
    mapping=row.get('account_mapping',{});validation=row.get('voucher_validation',{});diag=row.get('diagnostic_assignment')
    voucher=row.get('voucher') or {};subject=row.get('subject') or {}
    money=lambda side:row.get(side,{'status':'invalid','value':None,'raw':None})
    return {'record_id':row['record_id'],'provenance':copy.deepcopy(row['provenance']),
        'period':row.get('period'),'voucher_category':voucher.get('category'),'voucher_number':voucher.get('number'),
        'entry_sequence':diag['entry_sequence'] if diagnostic and diag else validation.get('entry_sequence'),
        'posting_date':diag['posting_date'] if diagnostic and diag else validation.get('posting_date'),
        'original_date':row.get('date'),'summary':row.get('summary'),
        'external_subject_code':subject.get('code'),'external_subject_name':subject.get('name'),
        'raw_subject':copy.deepcopy(row.get('raw_fields',{}).get('subject',row.get('raw_fields',{}).get('subject_code'))),
        'target_subject_code':mapping.get('target_code') if mapping.get('status')=='matched' else None,
        'target_subject_name':mapping.get('target_name') if mapping.get('status')=='matched' else None,
        'mapping_status':mapping.get('status','not_evaluated'),'mapping_id':mapping.get('mapping_id'),
        'debit':copy.deepcopy(money('debit')),'credit':copy.deepcopy(money('credit')),
        'auxiliary':copy.deepcopy(row.get('auxiliary',{})),
        'auxiliary_candidates':copy.deepcopy(row.get('auxiliary_candidates',{})),'diagnostic_only':diagnostic,
        'voucher_evidence_id':diag['evidence_id'] if diagnostic and diag else validation.get('evidence_id'),
        'hierarchy_evidence_ids':row.get('hierarchy',{}).get('related_evidence_ids',[])[:],
        'filter_evidence_id':row.get('voucher_filter',{}).get('evidence_id')}


def build_exports(voucher_report,config,*,allow_partial=False):
    if type(allow_partial) is not bool:raise ExportError('INVALID_PARTIAL_CHOICE','部分导出必须明确选择')
    if config.get('auxiliary_profile'):voucher_report=analyze_auxiliary(voucher_report,config)
    _,fingerprint=compile_config(config)
    if voucher_report.get('mode')!='voucher_validation' or not voucher_report.get('analysis_ok'):
        raise ExportError('UPSTREAM_VOUCHER_BLOCKED','凭证核验未完成，不能输出候选')
    if (voucher_report.get('config_id'),voucher_report.get('config_revision'),voucher_report.get('effective_config_sha256'))!=(config['config_id'],config['revision'],fingerprint):
        raise ExportError('CONFIG_MISMATCH','配置与核验结果不一致')
    rows=voucher_report['rows'];ids=[r['record_id'] for r in rows]
    if len(ids)!=len(set(ids)) or len(rows)!=voucher_report['row_counts']['total']:
        raise ExportError('INVALID_VOUCHER_REPORT','记录ID或数量不闭环')
    if len({tuple(r['provenance'][k] for k in ['input_sha256','sheet','excel_row']) for r in rows})!=len(rows):
        raise ExportError('INVALID_VOUCHER_REPORT','同一物理行重复')
    counts=Counter(r['voucher_validation']['decision'] for r in rows)
    if any(counts[s]!=voucher_report['row_counts'].get(s) for s in ['retained','excluded','needs_review','non_business']):
        raise ExportError('INVALID_VOUCHER_REPORT','凭证终态数量不闭环')
    approved={m['id']:m for m in config['mappings'] if m['status']=='confirmed'}
    official={};by_id={r['record_id']:r for r in rows}
    for group in voucher_report['official_vouchers']:
        if group['status']!='balanced':
            if group['assignments']:raise ExportError('INVALID_VOUCHER_REPORT','未通过凭证不能赋正式序号')
            continue
        selected=[by_id[i] for i in group['selected_record_ids']]
        key=tuple(group['context'][k] for k in ['input_sha256','sheet','period','voucher_category','voucher_number'])
        ordered=sorted(selected,key=lambda r:(r['provenance']['excel_row'],r['record_id']))
        expected=[{'record_id':r['record_id'],'entry_sequence':i,'posting_date':month_end(r['period'])} for i,r in enumerate(ordered,1)]
        if group['assignments']!=expected or any(r['voucher_validation']['decision']!='retained' or voucher_key(r)!=key for r in selected):
            raise ExportError('INVALID_VOUCHER_REPORT','正式凭证范围或序号不完整，不能输出半张凭证')
        try:totals,_=total_rows(selected)
        except AmountError as error:raise ExportError('INVALID_VOUCHER_REPORT',str(error)) from error
        if totals['debit']!=totals['credit'] or any(totals[s]!=Decimal(group['totals'][s]) for s in totals):
            raise ExportError('INVALID_VOUCHER_REPORT','候选金额与平衡证据不一致')
        for assignment in group['assignments']:
            if assignment['record_id'] in official:raise ExportError('INVALID_VOUCHER_REPORT','正式赋序号记录重复')
            official[assignment['record_id']]=(group,assignment)
    for row in rows:
        state=row['voucher_validation']['decision']
        if state not in ['retained','excluded','needs_review','non_business']:raise ExportError('INVALID_VOUCHER_REPORT','未知终态')
        if row['provenance']['input_sha256']!=voucher_report['input_sha256'] or row['provenance']['sheet']!=voucher_report['source_sheet']:
            raise ExportError('INVALID_VOUCHER_REPORT','来源不一致')
        observed=row.get('account_mapping',{})
        if observed.get('status')=='matched':
            matched=approved.get(observed['mapping_id'])
            if not matched or not applies_to(matched,period=row['period'],source_sha256=row['provenance']['input_sha256']) or (
                row['subject']['code'],observed['target_code'],observed['target_name'])!=(matched['source_code'],matched['target_code'],matched['target_name']):
                raise ExportError('INVALID_VOUCHER_REPORT','已匹配目标缺少相同编码及已批准案例范围证明')
        if state=='retained':
            mapping=row['account_mapping'];confirmed=approved.get(mapping['mapping_id']);pair=official.get(row['record_id'])
            if row['mapping_filter']['decision']!='retained' or not confirmed or mapping['status']!='matched' or (
                row['subject']['code'],mapping['target_code'],mapping['target_name'])!=(confirmed['source_code'],confirmed['target_code'],confirmed['target_name']):
                raise ExportError('INVALID_VOUCHER_REPORT','候选缺少已确认科目映射')
            if not pair or pair[0]['status']!='balanced' or pair[1]['entry_sequence']!=row['voucher_validation']['entry_sequence'] or pair[1]['posting_date']!=row['voucher_validation']['posting_date'] or pair[1]['posting_date']!=month_end(row['period']):
                raise ExportError('INVALID_VOUCHER_REPORT','候选缺少完整平衡、月末日期或连续序号证据')
    aux=next(r for r in config['rules'] if r['kind']=='auxiliary')
    aux_reason=None
    if aux['status']!='confirmed':aux_reason='AUXILIARY_REQUIREMENT_UNKNOWN'
    elif aux['parameters']['required_fields'] and not config.get('auxiliary_profile'):aux_reason='REQUIRED_AUXILIARY_VALUES_UNAVAILABLE'
    candidates=[];pending=[];excluded=[];diagnostic=[];decisions=[]
    for row in rows:
        upstream=row['voucher_validation']['decision'];reason=row['voucher_validation']['reason_codes'][:]
        decision='included' if upstream=='retained' else upstream
        if aux_reason and upstream not in ['excluded','non_business']:
            decision='needs_review';reason=sorted(set(reason)|{aux_reason})
        av=row.get('auxiliary_validation')
        if av and upstream not in ['excluded','non_business'] and not av['voucher_ready']:
            decision='needs_review';reason=sorted(set(reason)|set(av['reason_codes'])|{'AUXILIARY_VOUCHER_REQUIRES_REVIEW'})
        normalized=normalize_row(row)
        normalized.update(decision=decision,reason_codes=reason,auxiliary_status=av['status'] if av else 'unavailable' if aux_reason else 'not_required_by_confirmed_config')
        if decision=='included':candidates.append(normalized)
        elif decision=='needs_review':
            normalized['entry_sequence']=None
            pending.append(normalized)
        elif decision=='excluded':excluded.append(normalized)
        decisions.append({'record_id':row['record_id'],'provenance':copy.deepcopy(row['provenance']),
            'decision':decision,'reason_codes':reason,'exception_ids':[],
            'stage_decisions':{'parse':row['parse_state'],'hierarchy':row['hierarchy']['decision'],
                'mapping':row['account_mapping']['status'],'voucher_filter':row['voucher_filter']['decision'],
                'mapping_filter':row['mapping_filter']['decision'],'voucher_validation':upstream},
            'rule_ids':sorted(set(row['voucher_filter']['matched_rule_ids']+[r for r in [row['hierarchy']['rule_id'],row['account_mapping']['mapping_id'],row['voucher_validation']['posting_date_rule_id'],aux['id'] if upstream not in ['excluded','non_business'] else None] if r]))})
        if row['diagnostic_assignment'] is not None:
            entry=normalize_row(row,diagnostic=True)
            entry.update(decision=decision,reason_codes=reason,auxiliary_status=av['status'] if av else 'unavailable' if aux_reason else 'not_required_by_confirmed_config')
            diagnostic.append(entry)
    eligible_ids=[r['record_id'] for r in candidates]
    batch_blocked=bool(pending and candidates and not allow_partial)
    if batch_blocked:
        eligible=set(eligible_ids)
        for entry in candidates:
            entry.update(decision='needs_review',entry_sequence=None,reason_codes=sorted(set(entry['reason_codes'])|{'BATCH_REVIEW_BLOCKED'}))
        pending.extend(candidates);candidates=[]
        for entry in decisions+diagnostic:
            if entry['record_id'] in eligible:
                entry.update(decision='needs_review',reason_codes=sorted(set(entry['reason_codes'])|{'BATCH_REVIEW_BLOCKED'}))
    # 完整凭证通过才输出；部分导出另须显式选择，不能改变整批未通过状态。
    return {'candidates':candidates,'pending':pending,'excluded':excluded,'diagnostic_candidates':diagnostic,
        'row_decisions':decisions,'source_rows':copy.deepcopy(rows),'source_data_available':True,
        'batch_gate':{'allow_partial':allow_partial,'blocked':batch_blocked,'eligible_record_ids':eligible_ids,
                      'partial_export':bool(allow_partial and candidates and pending)},
        'auxiliary_gate':{'rule_id':aux['id'],'reason_code':aux_reason,'required_fields':aux['parameters']['required_fields'][:]}}


def failed_exports(rows,error):
    pending=[];decisions=[]
    for row in rows or []:
        nonbusiness=row.get('parse_state')=='non_business';decision='non_business' if nonbusiness else 'needs_review'
        reasons=[row.get('reason_code','NON_BUSINESS')] if nonbusiness else ['RUN_FAILED',error['code']]
        entry=normalize_row(row);entry.update(decision=decision,reason_codes=reasons,entry_sequence=None)
        if not nonbusiness:pending.append(entry)
        decisions.append({'record_id':row['record_id'],'provenance':copy.deepcopy(row['provenance']),
            'decision':decision,'reason_codes':reasons,'exception_ids':[],'stage_decisions':{'parse':row.get('parse_state')},'rule_ids':[]})
    return {'candidates':[],'pending':pending,'excluded':[],'diagnostic_candidates':[],'row_decisions':decisions,
        'source_rows':copy.deepcopy(rows or []),'source_data_available':rows is not None,'auxiliary_gate':None}
