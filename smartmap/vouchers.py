"""T10完整凭证核验、月末日期及逐凭证序号；不平账、不修改原值。"""
import calendar
import copy
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal

from .amounts import AmountError, sum_exact, total_rows
from .configuration import compile_config
from .grouping import voucher_key


class VoucherError(ValueError):
    def __init__(self,code,message):
        super().__init__(message);self.code=code


def month_end(period):
    if not isinstance(period,str) or not re.fullmatch(r'[1-9][0-9]{3}(?:0[1-9]|1[0-2])',period):
        raise VoucherError('INVALID_PERIOD','账期必须是有效YYYYMM')
    year,month=int(period[:4]),int(period[4:])
    return date(year,month,calendar.monthrange(year,month)[1]).isoformat()


def _validate_input(mapped,hierarchy,config):
    active,fingerprint=compile_config(config)
    for report,mode in [(mapped,'mapping_filter_analysis'),(hierarchy,'hierarchy_analysis')]:
        if report.get('mode')!=mode or not report.get('analysis_ok'):
            raise VoucherError('UPSTREAM_BLOCKED','上游分析未完成，不能执行凭证核验')
        if (report.get('config_id'),report.get('config_revision'),report.get('effective_config_sha256'))!=(config['config_id'],config['revision'],fingerprint):
            raise VoucherError('CONFIG_MISMATCH','上游与当前配置不一致，请重新运行')
    if (mapped['input_sha256'],mapped['source_sheet'],mapped['period'])!=(hierarchy['input_sha256'],hierarchy['source_sheet'],hierarchy['period']):
        raise VoucherError('INVALID_UPSTREAM_REPORT','上游来源或账期不一致')
    month_end(mapped['period'])
    rows=mapped['rows'];original={r['record_id']:r for r in hierarchy['rows']}
    ids=[r['record_id'] for r in rows]
    if len(ids)!=len(set(ids)) or Counter(ids)!=Counter(r['record_id'] for r in hierarchy['rows']):
        raise VoucherError('INVALID_UPSTREAM_REPORT','行ID重复或上游记录丢失')
    if any(type(r['provenance']['excel_row']) is not int or r['provenance']['excel_row']<1 for r in rows):
        raise VoucherError('INVALID_UPSTREAM_REPORT','Excel行号必须为正整数')
    coordinates={(r['provenance']['input_sha256'],r['provenance']['sheet'],r['provenance']['excel_row']) for r in rows}
    if len(coordinates)!=len(rows):raise VoucherError('INVALID_UPSTREAM_REPORT','同一物理行重复进入处理')
    counts=Counter(r['mapping_filter']['decision'] for r in rows)
    if mapped['row_counts']['total']!=len(rows) or any(counts[s]!=mapped['row_counts'].get(s) for s in ['retained','excluded','needs_review','non_business']):
        raise VoucherError('INVALID_UPSTREAM_REPORT','行状态数量不闭环')
    mappings={m['id']:m for m in active['mappings']}
    filter_groups={g['evidence_id']:g for g in mapped['filter_groups']}
    approved_filters={r['id']:r for r in active['rules'] if r['kind']=='voucher_filter'}
    hierarchy_rule=next(r for r in config['rules'] if r['kind']=='hierarchy')
    for row in rows:
        if {k:v for k,v in row.items() if k not in ['account_mapping','voucher_filter','mapping_filter']}!=original[row['record_id']]:
            raise VoucherError('INVALID_UPSTREAM_REPORT','上游原始字段被改变')
        if row['provenance']['input_sha256']!=mapped['input_sha256'] or row['provenance']['sheet']!=mapped['source_sheet']:
            raise VoucherError('INVALID_UPSTREAM_REPORT','原始行来源不一致')
        decision=row['mapping_filter']['decision']
        if decision not in ['retained','excluded','needs_review','non_business']:raise VoucherError('INVALID_UPSTREAM_REPORT','未知行状态')
        if (row['parse_state']=='non_business')!=(decision=='non_business'):
            raise VoucherError('INVALID_UPSTREAM_REPORT','非业务状态被改变')
        if row['parse_state']=='parsed':
            if row['period']!=mapped['period']:raise VoucherError('INVALID_UPSTREAM_REPORT','行账期不一致')
            try:total_rows([row])
            except AmountError as error:raise VoucherError('INVALID_UPSTREAM_REPORT',str(error)) from error
        if decision=='retained':
            mapping=row['account_mapping'];approved=mappings.get(mapping['mapping_id'])
            if row['parse_state']!='parsed' or not approved or mapping['status']!='matched' or (
                row['subject']['code'],mapping['target_code'],mapping['target_name'])!=(approved['source_code'],approved['target_code'],approved['target_name']):
                raise VoucherError('INVALID_UPSTREAM_REPORT','保留行缺少精确的已确认科目映射')
        if decision=='excluded':
            if row['voucher_filter']['decision']=='excluded':
                group=filter_groups.get(row['voucher_filter']['evidence_id'])
                rules=row['voucher_filter']['matched_rule_ids']
                if not group or group['decision']!='excluded' or row['record_id'] not in group['member_record_ids'] or not rules or any(r not in approved_filters for r in rules):
                    raise VoucherError('INVALID_UPSTREAM_REPORT','凭证排除缺少已确认依据')
            elif row['hierarchy']['decision']!='excluded' or hierarchy_rule['status']!='confirmed' or not row['hierarchy']['parent_evidence_ids']:
                raise VoucherError('INVALID_UPSTREAM_REPORT','父级排除缺少已确认依据')
    projection=hierarchy['diagnostic_projection']['remaining_record_ids']
    if len(projection)!=len(set(projection)) or any(i not in original or original[i]['parse_state']!='parsed' for i in projection):
        raise VoucherError('INVALID_UPSTREAM_REPORT','诊断投影包含重复或无效原始行')
    return fingerprint


def _sort_key(key):return (key[0],key[1],str(key[2] or ''),str(key[3]),key[4])


def _balance_group(key,members,selected,view,end,upstream_reasons=(),excluded=False):
    ordered=sorted(selected,key=lambda r:(r['provenance']['excel_row'],r['record_id']))
    selected_ids=[r['record_id'] for r in ordered]
    identity=hashlib.sha256(json.dumps([view,key,selected_ids,end],ensure_ascii=False).encode('utf-8')).hexdigest()
    reasons=set(upstream_reasons);totals=None;difference=None;states=None;equal=None;negative=[];observations=[]
    if excluded:status='excluded'
    elif reasons:status='needs_review'
    elif not ordered:status='needs_review';reasons.add('EMPTY_VOUCHER_SELECTION')
    else:
        try:
            values,states=total_rows(ordered)
            totals={side:str(value) for side,value in values.items()}
            difference=str(sum_exact([values['debit'],values['credit'].copy_negate()]))
            equal=values['debit']==values['credit']
            has_activity=False
            for row in ordered:
                a=Decimal(row['debit']['value']) if row['debit']['value'] is not None else Decimal(0)
                b=Decimal(row['credit']['value']) if row['credit']['value'] is not None else Decimal(0)
                if a==0 and b==0:reasons.add('ZERO_AMOUNT_ENTRY_REQUIRES_REVIEW')
                if a!=0 or b!=0:has_activity=True
                if a!=0 and b!=0:reasons.add('BOTH_SIDES_NONZERO_REQUIRES_REVIEW')
                if a<0 or b<0:negative.append(row['record_id'])
            if not has_activity:reasons.add('ZERO_TOTAL_VOUCHER_REQUIRES_REVIEW')
            elif not any(v!=0 for v in values.values()):observations.append('NET_ZERO_WITH_ACTIVITY')
            if negative:observations.append('NEGATIVE_AMOUNT_PRESENT')
            if not equal:status='unbalanced';reasons.add('UNBALANCED_VOUCHER')
            elif reasons:status='needs_review'
            else:status='balanced'
        except AmountError:
            status='needs_review';reasons.add('INVALID_AMOUNT')
    return {'evidence_id':identity,'view':view,'diagnostic_only':view=='diagnostic',
        'context':{'input_sha256':key[0],'sheet':key[1],'period':key[2],'voucher_category':key[3],'voucher_number':key[4]},
        'source_record_ids':[r['record_id'] for r in members],'source_excel_rows':[r['provenance']['excel_row'] for r in members],
        'selected_record_ids':selected_ids,'selected_excel_rows':[r['provenance']['excel_row'] for r in ordered],
        'status':status,'totals':totals,'difference':difference,'amount_states':states,'arithmetic_equal':equal,
        'balanced':equal,'reason_codes':sorted(reasons),'observations':observations,'negative_amount_record_ids':negative,
        'posting_date':end,'assignments':[{'record_id':r['record_id'],'entry_sequence':i,'posting_date':end} for i,r in enumerate(ordered,1)] if status=='balanced' else []}


def _stats(groups):
    counts=Counter(g['status'] for g in groups)
    denominator=sum(g['arithmetic_equal'] is not None for g in groups)
    numerator=sum(g['arithmetic_equal'] is True for g in groups)
    return {'voucher_groups':len(groups),'balanced':counts['balanced'],'unbalanced':counts['unbalanced'],
        'needs_review':counts['needs_review'],'excluded':counts['excluded'],
        'arithmetic_checked_groups':denominator,'arithmetically_balanced':numerator,'arithmetically_unbalanced':denominator-numerator,
        'balance_rate_numerator':numerator,'balance_rate_denominator':denominator,
        'balance_rate':str(Decimal(numerator)/Decimal(denominator)) if denominator else None}


def validate_vouchers(mapped,hierarchy,config):
    fingerprint=_validate_input(mapped,hierarchy,config)
    date_rule=next(r for r in config['rules'] if r['kind']=='date');end=month_end(mapped['period'])
    rows=mapped['rows'];vouchers=defaultdict(list)
    for row in rows:
        if row['mapping_filter']['decision']!='non_business' and voucher_key(row):vouchers[voucher_key(row)].append(row)
    projection_ids=set(hierarchy['diagnostic_projection']['remaining_record_ids'])
    structural_blocks={tuple(g['context'][name] for name in ['input_sha256','sheet','period','voucher_category','voucher_number']):g['reason_codes'] for g in hierarchy['blocked_vouchers']}
    unassigned=[r['record_id'] for r in rows if r['mapping_filter']['decision'] not in ['excluded','non_business'] and voucher_key(r) is None]
    official=[];diagnostic=[]
    for key in sorted(vouchers,key=_sort_key):
        members=vouchers[key];active=[r for r in members if r['mapping_filter']['decision']!='excluded']
        excluded=not active
        reasons=[]
        if any(r['mapping_filter']['decision']!='retained' for r in active):reasons.append('UPSTREAM_VOUCHER_REQUIRES_REVIEW')
        if unassigned:reasons.append('UNATTRIBUTED_UPSTREAM_ROW')
        group_end=end if key[2]==mapped['period'] else None
        official.append(_balance_group(key,members,active,'official',group_end,reasons,excluded))
        selected=[r for r in members if r['record_id'] in projection_ids and r['voucher_filter']['decision']!='excluded']
        diag_reasons=list(structural_blocks.get(key,[]))
        if any(r['parse_state']=='needs_review' for r in members):diag_reasons.append('UPSTREAM_PARSE_REQUIRES_REVIEW')
        if unassigned:diag_reasons.append('UNATTRIBUTED_UPSTREAM_ROW')
        scope_excluded=all(r['voucher_filter']['decision']=='excluded' for r in members)
        diagnostic.append(_balance_group(key,members,selected,'diagnostic',group_end,diag_reasons,scope_excluded))
    official_by_key={tuple(g['context'][name] for name in ['input_sha256','sheet','period','voucher_category','voucher_number']):g for g in official}
    official_assignments={a['record_id']:dict(a,evidence_id=g['evidence_id']) for g in official for a in g['assignments']}
    diagnostic_assignments={a['record_id']:dict(a,evidence_id=g['evidence_id'],diagnostic_only=True) for g in diagnostic for a in g['assignments']}
    output=[]
    for original in rows:
        row=copy.deepcopy(original);upstream=row['mapping_filter']['decision'];key=voucher_key(row);group=official_by_key.get(key)
        if upstream in ['excluded','non_business']:decision=upstream;reasons=row['mapping_filter']['reason_codes'][:]
        elif key is None:decision='needs_review';reasons=['UNATTRIBUTED_UPSTREAM_ROW']
        elif group['status']=='balanced':decision='retained';reasons=['VOUCHER_BALANCED']
        else:decision='needs_review';reasons=sorted(set(row['mapping_filter']['reason_codes'])|set(group['reason_codes']))
        assignment=official_assignments.get(row['record_id'])
        row['voucher_validation']={'decision':decision,'reason_codes':reasons,'evidence_id':group['evidence_id'] if group else None,
            'posting_date':end if row['parse_state']=='parsed' and upstream not in ['excluded','non_business'] else None,
            'posting_date_rule_id':date_rule['id'],'entry_sequence':assignment['entry_sequence'] if assignment else None}
        row['diagnostic_assignment']=copy.deepcopy(diagnostic_assignments.get(row['record_id']))
        output.append(row)
    counts=Counter(r['voucher_validation']['decision'] for r in output)
    return {'report_version':'0.2','mode':'voucher_validation','voucher_version':'0.2-t10','analysis_ok':True,
        'ready_for_next_stage':not counts['needs_review'],'ready_for_system_import':False,'conversion_implemented':False,
        'input_sha256':mapped['input_sha256'],'source_file':mapped['source_file'],'source_sheet':mapped['source_sheet'],'period':mapped['period'],
        'config_id':config['config_id'],'config_revision':config['revision'],'effective_config_sha256':fingerprint,
        'date_rule':{'id':date_rule['id'],'status':date_rule['status'],'policy':date_rule['policy']},'month_end':end,
        'row_counts':{'total':len(rows),'retained':counts['retained'],'excluded':counts['excluded'],'needs_review':counts['needs_review'],'non_business':counts['non_business']},
        'official_summary':_stats(official),'diagnostic_summary':dict(_stats(diagnostic),diagnostic_only=True),
        'official_assigned_rows':len(official_assignments),'diagnostic_assigned_rows':len(diagnostic_assignments),
        'proposed_posting_date_rows':sum(r['voucher_validation']['posting_date'] is not None for r in output),
        'unassigned_record_ids':unassigned,'official_vouchers':official,'diagnostic_vouchers':diagnostic,'rows':output}
