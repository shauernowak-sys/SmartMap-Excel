"""T09映射与整凭证筛选。保留全部行，只有已确认规则能产生实际排除。"""
import copy
from collections import Counter, defaultdict

from .account_mapping import MappingIndex, not_evaluated
from .configuration import compile_config
from .grouping import voucher_key
from .voucher_filters import evaluate_voucher
from .closing import classify_closing

MAPPING_REASONS={'unconfirmed':'UNCONFIRMED_ACCOUNT_MAPPING','unknown':'UNMAPPED_ACCOUNT',
    'candidate_conflict':'ACCOUNT_MAPPING_CANDIDATE_CONFLICT','rejected':'ACCOUNT_MAPPING_REJECTED',
    'out_of_scope':'ACCOUNT_MAPPING_OUT_OF_SCOPE'}


class MappingFilterError(ValueError):
    def __init__(self,code,message):
        super().__init__(message);self.code=code


def _validate_input(hierarchy,config):
    _,fingerprint=compile_config(config)
    if hierarchy.get('mode')!='hierarchy_analysis' or not hierarchy.get('analysis_ok'):
        raise MappingFilterError('UPSTREAM_HIERARCHY_BLOCKED','层级分析没有完成，不能继续映射筛选')
    if (hierarchy.get('config_id'),hierarchy.get('config_revision'),hierarchy.get('effective_config_sha256'))!=(config['config_id'],config['revision'],fingerprint):
        raise MappingFilterError('CONFIG_MISMATCH','上游结果与当前配置不一致，请从原始文件重新运行')
    rows=hierarchy['rows'];ids=[row['record_id'] for row in rows]
    if len(ids)!=len(set(ids)) or len(ids)!=hierarchy['row_counts']['total']:
        raise MappingFilterError('INVALID_HIERARCHY_REPORT','上游行ID或行数不闭环')
    counts=Counter(row['hierarchy']['decision'] for row in rows)
    if any(counts[state]!=hierarchy['row_counts'].get(state) for state in ['retained','excluded','needs_review','non_business']):
        raise MappingFilterError('INVALID_HIERARCHY_REPORT','上游各状态计数不闭环')
    rule=next(r for r in config['rules'] if r['kind']=='hierarchy')
    groups={g['evidence_id']:g for g in hierarchy['groups']}
    for row in rows:
        decision=row['hierarchy']['decision']
        if decision not in ['retained','excluded','needs_review','non_business']:
            raise MappingFilterError('INVALID_HIERARCHY_REPORT','未知的层级行状态')
        if row['provenance']['input_sha256']!=hierarchy['input_sha256'] or row['provenance']['sheet']!=hierarchy['source_sheet']:
            raise MappingFilterError('INVALID_HIERARCHY_REPORT','行来源不一致')
        if (row['parse_state']=='non_business')!=(decision=='non_business') or row['parse_state']=='needs_review' and decision!='needs_review':
            raise MappingFilterError('INVALID_HIERARCHY_REPORT','原始解析状态与层级去向冲突')
        if row['parse_state']=='parsed' and row['period']!=hierarchy['period']:
            raise MappingFilterError('INVALID_HIERARCHY_REPORT','上游行账期不一致')
        if decision=='excluded':
            evidence=row['hierarchy']['parent_evidence_ids']
            scope=rule.get('applicability')
            applicable=scope is None or scope=={'period':hierarchy['period'],'source_sha256':hierarchy['input_sha256']}
            if rule['status']!='confirmed' or not applicable or not evidence or any(g not in groups or not groups[g]['structure_verified'] or row['record_id'] not in groups[g]['parent_record_ids'] for g in evidence):
                raise MappingFilterError('INVALID_HIERARCHY_REPORT','上游排除缺少已确认策略或有效父级证据')
    return fingerprint


def map_and_filter(hierarchy,config):
    fingerprint=_validate_input(hierarchy,config)
    index=MappingIndex(config);rows=hierarchy['rows'];vouchers=defaultdict(list)
    for row in rows:
        key=voucher_key(row)
        if row['parse_state']!='non_business' and key:vouchers[key].append(row)
    blocked={tuple(b['context'][name] for name in ['input_sha256','sheet','period','voucher_category','voucher_number']):b['reason_codes'] for b in hierarchy['blocked_vouchers']}
    unassigned=any(row['parse_state']=='needs_review' and voucher_key(row) is None for row in rows)
    rules=[r for r in config['rules'] if r['kind']=='voucher_filter']
    filter_evidence={}
    for key,members in vouchers.items():
        reasons=list(blocked.get(key,[]))
        if any(r['parse_state']=='needs_review' for r in members):reasons.append('VOUCHER_HAS_UPSTREAM_ISSUE')
        if unassigned:reasons.append('UNATTRIBUTED_UPSTREAM_ROW')
        closing=classify_closing(members,config['source_profile'].get('closing_recognition'))
        filter_evidence[key]=evaluate_voucher(key,members,rules,reasons,closing_recognition=closing)
        if config['source_profile'].get('closing_recognition'):
            filter_evidence[key]['closing_recognition']=closing
    mappings={}
    for row in rows:
        key=voucher_key(row);scope=filter_evidence.get(key);upstream=row['hierarchy']['decision']
        if upstream=='non_business':mappings[row['record_id']]=not_evaluated('NON_BUSINESS')
        elif scope and scope['decision']=='excluded':mappings[row['record_id']]=not_evaluated('CONFIRMED_VOUCHER_FILTER')
        elif upstream=='retained':mappings[row['record_id']]=index.resolve(row['subject']['code'],period=row['period'],source_sha256=row['provenance']['input_sha256'])
        else:mappings[row['record_id']]=not_evaluated('UPSTREAM_'+upstream.upper())
    # 映射不完整或上游仍需确认时，不把同凭证其他行伪装成可单独送入后续的完整凭证。
    voucher_reviews={};voucher_review_sources={}
    for key,members in vouchers.items():
        scope=filter_evidence[key];reasons=set();sources=set()
        if scope['decision']=='excluded':
            voucher_reviews[key]=[];voucher_review_sources[key]=[];continue
        if scope['decision']=='needs_review':reasons.update(scope['reason_codes']);sources.update(scope['member_record_ids'])
        for row in members:
            if row['hierarchy']['decision']=='needs_review':reasons.add('VOUCHER_HAS_UPSTREAM_REVIEW');sources.add(row['record_id'])
            status=mappings[row['record_id']]['status']
            if status in MAPPING_REASONS:reasons.add(MAPPING_REASONS[status]);sources.add(row['record_id'])
        voucher_reviews[key]=sorted(reasons);voucher_review_sources[key]=sorted(sources)
    output=[]
    for original in rows:
        row=copy.deepcopy(original);key=voucher_key(row);scope=filter_evidence.get(key);upstream=row['hierarchy']['decision']
        if upstream=='non_business':decision='non_business';reasons=row['hierarchy']['reason_codes'][:]
        elif scope and scope['decision']=='excluded':decision='excluded';reasons=['CONFIRMED_VOUCHER_FILTER']
        elif upstream=='excluded':decision='excluded';reasons=row['hierarchy']['reason_codes'][:]
        elif key is None:decision='needs_review';reasons=['UNATTRIBUTED_UPSTREAM_ROW']
        elif voucher_reviews[key]:decision='needs_review';reasons=voucher_reviews[key][:]
        elif mappings[row['record_id']]['status']=='matched':decision='retained';reasons=['CONFIRMED_ACCOUNT_MAPPING']
        else:decision='needs_review';reasons=['MAPPING_NOT_EVALUATED']
        row['account_mapping']=copy.deepcopy(mappings[row['record_id']])
        row['voucher_filter']={'evidence_id':scope['evidence_id'] if scope else None,'decision':scope['decision'] if scope else 'not_grouped',
            'matched_rule_ids':scope['confirmed_full_rule_ids'][:] if scope else [],
            'pending_rule_ids':scope['candidate_full_rule_ids'][:] if scope else [],'partial_rule_ids':scope['partial_rule_ids'][:] if scope else []}
        row['mapping_filter']={'decision':decision,'reason_codes':reasons}
        output.append(row)
    counts=Counter(r['mapping_filter']['decision'] for r in output)
    statuses=Counter(r['account_mapping']['status'] for r in output)
    evaluated=[r for r in output if r['account_mapping']['status']!='not_evaluated']
    return {'report_version':'0.2','mode':'mapping_filter_analysis','mapping_filter_version':'0.2-t09','analysis_ok':True,
        'ready_for_next_stage':not counts['needs_review'],'conversion_implemented':False,'ready_for_system_import':False,
        'input_sha256':hierarchy['input_sha256'],'source_file':hierarchy['source_file'],'source_sheet':hierarchy['source_sheet'],'period':hierarchy['period'],
        'config_id':config['config_id'],'config_revision':config['revision'],'effective_config_sha256':fingerprint,
        'row_counts':{'total':len(rows),'retained':counts['retained'],'excluded':counts['excluded'],'needs_review':counts['needs_review'],'non_business':counts['non_business']},
        'exclusion_counts':{'voucher_filter':sum(r['mapping_filter']['decision']=='excluded' and r['voucher_filter']['decision']=='excluded' for r in output),
            'hierarchy':sum(r['mapping_filter']['decision']=='excluded' and r['voucher_filter']['decision']!='excluded' for r in output)},
        'mapping_counts':{key:statuses[key] for key in ['matched','unconfirmed','candidate_conflict','unknown','rejected','out_of_scope','not_evaluated']},
        'mapping_scope':{'evaluated_rows':len(evaluated),'evaluated_external_codes':len({r['subject']['code'] for r in evaluated}),
            'unresolved_external_codes':sorted({r['subject']['code'] for r in evaluated if r['account_mapping']['status']!='matched'})},
        'filter_counts':{'voucher_groups':len(filter_evidence),'excluded':sum(v['decision']=='excluded' for v in filter_evidence.values()),
            'needs_review':sum(v['decision']=='needs_review' for v in filter_evidence.values()),
            'candidate_full_match_vouchers':sum(bool(v['candidate_full_rule_ids']) for v in filter_evidence.values()),
            'partial_match_vouchers':sum(bool(v['partial_rule_ids']) for v in filter_evidence.values())},
        'review_reason_counts':dict(Counter(reason for r in output if r['mapping_filter']['decision']=='needs_review' for reason in r['mapping_filter']['reason_codes'])),
        'voucher_reviews':[{'filter_evidence_id':filter_evidence[key]['evidence_id'],'reason_codes':reasons,
            'related_record_ids':voucher_review_sources[key]} for key,reasons in voucher_reviews.items() if reasons],
        'filter_groups':list(filter_evidence.values()),'rows':output}
