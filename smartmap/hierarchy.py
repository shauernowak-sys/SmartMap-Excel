"""T08 父子科目组证据与配置授权的确定性选择；不进行科目映射。"""
import copy
import hashlib
import json
from collections import Counter, defaultdict
from decimal import Decimal

from .configuration import compile_config
from .grouping import voucher_key as _voucher_key
from .amounts import AmountError, sum_exact as _sum_exact, total_rows


class HierarchyError(ValueError):
    def __init__(self,code,message):
        super().__init__(message);self.code=code


def _totals(rows):
    try:return total_rows(rows)
    except AmountError as error:raise HierarchyError('INVALID_PARSE_REPORT',str(error)) from error


def _validate_input(parsed,config):
    _,fingerprint=compile_config(config)
    if parsed.get('mode')!='source_parse_only' or not parsed.get('read_ok'):
        raise HierarchyError('UPSTREAM_PARSE_BLOCKED','原始读取未通过，不能执行层级选择')
    if (parsed.get('config_id'),parsed.get('config_revision'),parsed.get('effective_config_sha256'))!=(config['config_id'],config['revision'],fingerprint):
        raise HierarchyError('CONFIG_MISMATCH','解析报告与当前配置不一致，请重新解析原始文件')
    rows=parsed.get('rows',[])
    if parsed.get('row_counts',{}).get('total')!=len(rows):raise HierarchyError('INVALID_PARSE_REPORT','解析报告行数不闭环')
    ids=set()
    for row in rows:
        if row['record_id'] in ids:raise HierarchyError('INVALID_PARSE_REPORT','物理行ID重复，不能使用集合丢掉重复记录')
        ids.add(row['record_id'])
        if row['parse_state'] not in ['parsed','non_business','needs_review']:raise HierarchyError('INVALID_PARSE_REPORT','未知原始行解析状态')
        if row['provenance']['input_sha256']!=parsed['input_sha256'] or row['provenance']['sheet']!=parsed['source_sheet']:
            raise HierarchyError('INVALID_PARSE_REPORT','来源文件或工作表不一致')
        if row['parse_state']=='parsed':
            if not row.get('voucher') or not row.get('period') or not row.get('subject') or row.get('issues'):
                raise HierarchyError('INVALID_PARSE_REPORT','解析通过的行缺少凭证、账期、科目或存在异常')
            if row['period']!=parsed.get('period'):
                raise HierarchyError('INVALID_PARSE_REPORT','行账期与已通过的单账期解析报告不一致')
            code=row['subject']['code']
            if row['subject']['segments']!=code.split('.'):
                raise HierarchyError('INVALID_PARSE_REPORT','科目编码与层级片段不一致')
    _totals([row for row in rows if row['parse_state']=='parsed'])
    return fingerprint


def analyze_hierarchy(parsed,config):
    """完整保留原始行；仅已确认规则且整个凭证无层级/解析异常时排除证据充分父级。"""
    fingerprint=_validate_input(parsed,config)
    rule=next(r for r in config['rules'] if r['kind']=='hierarchy')
    scope=rule.get('applicability')
    applicable=scope is None or (scope['period']==parsed['period'] and scope['source_sha256']==parsed['input_sha256'])
    rows=parsed['rows'];valid=[r for r in rows if r['parse_state']=='parsed']
    vouchers=defaultdict(list);contexts=defaultdict(list);blocked=defaultdict(set);block_sources=defaultdict(set)
    for row in rows:
        key=_voucher_key(row)
        if row['parse_state']=='parsed':
            vouchers[key].append(row);contexts[key+(row['summary'],)].append(row)
    for row in rows:
        if row['parse_state']!='needs_review':continue
        key=_voucher_key(row)
        if key is None:
            for target in vouchers:
                blocked[target].add('UNATTRIBUTED_UPSTREAM_ROW');block_sources[target].add(row['record_id'])
        else:
            for target in vouchers:
                if target[:2]==key[:2] and (key[2] is None or target[2]==key[2]) and target[4]==key[4]:
                    # 类别冲突时不猜测是哪一类，保守隔离同号凭证。
                    if target[3]==key[3] or not key[3] or any(i['code']=='VOUCHER_CATEGORY_CONFLICT' for i in row['issues']):
                        blocked[target].add('VOUCHER_HAS_UPSTREAM_ISSUE')
                        block_sources[target].add(row['record_id'])
    # 同一凭证跨摘要缺少父级对应项时，不把它猜成独立父级直接记账或已确认重复。
    for key,entries in vouchers.items():
        codes={tuple(r['subject']['segments']) for r in entries}
        context_codes=defaultdict(set)
        for r in entries:context_codes[r['summary']].add(tuple(r['subject']['segments']))
        for r in entries:
            parts=tuple(r['subject']['segments'])
            if any(parts[:depth] in codes and parts[:depth] not in context_codes[r['summary']] for depth in range(1,len(parts))):
                blocked[key].add('AMBIGUOUS_HIERARCHY_SUMMARY')
                block_sources[key].add(r['record_id'])
                block_sources[key].update(x['record_id'] for x in entries if tuple(x['subject']['segments']) in [parts[:depth] for depth in range(1,len(parts))])
    groups=[];group_parents={}
    for context,entries in contexts.items():
        codes=defaultdict(list)
        for row in entries:codes[tuple(row['subject']['segments'])].append(row)
        children=defaultdict(list)
        for child in codes:
            parents=[child[:depth] for depth in range(1,len(child)) if child[:depth] in codes]
            if parents:children[max(parents,key=len)].append(child)
        for parent in sorted(children):
            parents=codes[parent]
            child_codes=sorted(children[parent])
            descendants=[r for code in child_codes for r in codes[code]]
            parent_total,parent_states=_totals(parents);child_total,child_states=_totals(descendants)
            equal=parent_total==child_total
            nonzero=any(value!=0 for value in parent_total.values())
            reasons=[]
            if not equal:reasons.append('PARENT_CHILD_AMOUNT_MISMATCH')
            if equal and not nonzero:reasons.append('ZERO_TOTAL_HIERARCHY_AMBIGUOUS')
            if reasons:
                blocked[context[:-1]].update(reasons)
                block_sources[context[:-1]].update(r['record_id'] for r in parents+descendants)
            parent_ids=[r['record_id'] for r in parents];child_ids=[r['record_id'] for r in descendants]
            group_id=hashlib.sha256(json.dumps([context,parent,sorted(parent_ids),sorted(child_ids)],ensure_ascii=False).encode('utf-8')).hexdigest()
            group={'evidence_id':group_id,'context':{'input_sha256':context[0],'sheet':context[1],'period':context[2],
                'voucher_category':context[3],'voucher_number':context[4],'summary':context[5]},
                'parent_code':'.'.join(parent),'nearest_child_codes':['.'.join(code) for code in child_codes],
                'parent_record_ids':parent_ids,'child_record_ids':child_ids,
                'parent_excel_rows':[r['provenance']['excel_row'] for r in parents],
                'child_excel_rows':[r['provenance']['excel_row'] for r in descendants],
                'parent_totals':{s:str(v) for s,v in parent_total.items()},'child_totals':{s:str(v) for s,v in child_total.items()},
                'differences':{s:str(_sum_exact([parent_total[s],child_total[s].copy_negate()])) for s in parent_total},
                'parent_amount_states':parent_states,'child_amount_states':child_states,
                'amount_totals_equal':equal,'nonzero_side_total':nonzero,'reason_codes':reasons,
                'rule_id':rule['id'],'rule_status':rule['status'],'rule_applicable':applicable}
            groups.append(group);group_parents[group_id]=parent_ids
    # 任何一组不明，整张凭证禁止自动排除；先构建全部证据再决定，避免处理顺序影响。
    duplicate_parents=set();parent_evidence=defaultdict(list);related_evidence=defaultdict(list)
    for group in groups:
        ctx=group['context'];key=(ctx['input_sha256'],ctx['sheet'],ctx['period'],ctx['voucher_category'],ctx['voucher_number'])
        issues=set(group['reason_codes'])|blocked[key]
        group['reason_codes']=sorted(issues)
        group['structure_verified']=not issues and group['amount_totals_equal'] and group['nonzero_side_total']
        group['analysis_state']='verified_structure' if group['structure_verified'] else 'needs_review'
        for identity in group['parent_record_ids']+group['child_record_ids']:related_evidence[identity].append(group['evidence_id'])
        if group['structure_verified']:
            for identity in group_parents[group['evidence_id']]:
                duplicate_parents.add(identity);parent_evidence[identity].append(group['evidence_id'])
    decisions=[]
    for original in rows:
        row=copy.deepcopy(original);identity=row['record_id'];key=_voucher_key(row)
        if row['parse_state']=='non_business':action='non_business';reasons=[row['reason_code']]
        elif row['parse_state']=='needs_review':action='needs_review';reasons=['UPSTREAM_ROW_REQUIRES_REVIEW']
        elif blocked[key]:action='needs_review';reasons=sorted(blocked[key])
        elif identity in duplicate_parents:
            if rule['status']=='confirmed' and applicable:action='excluded';reasons=['PARENT_CHILD_GROUP_VERIFIED']
            elif rule['status']=='confirmed':action='needs_review';reasons=['HIERARCHY_RULE_OUT_OF_SCOPE']
            else:
                action='needs_review';reasons=['HIERARCHY_POLICY_UNCONFIRMED' if rule['status']=='candidate' else 'HIERARCHY_POLICY_REJECTED']
        else:action='retained';reasons=['NO_OBSERVED_CHILDREN_IN_CONTEXT']
        row['hierarchy']={'decision':action,'reason_codes':reasons,'rule_id':rule['id'] if identity in duplicate_parents else None,
            'parent_evidence_ids':parent_evidence[identity],'related_evidence_ids':related_evidence[identity],
            'duplicate_parent_candidate':identity in duplicate_parents}
        decisions.append(row)
    counts=Counter(row['hierarchy']['decision'] for row in decisions)
    # 诊断投影仅展示结构选择，不作为未经确认的实际排除清单。
    projection=[r['record_id'] for r in valid if r['record_id'] not in duplicate_parents]
    by_id={row['record_id']:row for row in rows}
    blockers=[{'context':{'input_sha256':key[0],'sheet':key[1],'period':key[2],'voucher_category':key[3],'voucher_number':key[4]},
        'reason_codes':sorted(reasons),'related_record_ids':sorted(block_sources[key]),
        'related_excel_rows':sorted(by_id[identity]['provenance']['excel_row'] for identity in block_sources[key])} for key,reasons in blocked.items() if reasons]
    return {'report_version':'0.2','mode':'hierarchy_analysis','hierarchy_version':'0.2-t08',
        'analysis_ok':True,'ready_for_next_stage':not counts['needs_review'],
        'conversion_implemented':False,'ready_for_system_import':False,
        'input_sha256':parsed['input_sha256'],'source_file':parsed['source_file'],'source_sheet':parsed['source_sheet'],'period':parsed['period'],
        'config_id':config['config_id'],'config_revision':config['revision'],'effective_config_sha256':fingerprint,
        'hierarchy_rule':{'id':rule['id'],'status':rule['status'],'policy':rule['policy'],
                          'applicability':copy.deepcopy(scope),'applicable':applicable},
        'row_counts':{'total':len(rows),'retained':counts['retained'],'excluded':counts['excluded'],'needs_review':counts['needs_review'],'non_business':counts['non_business']},
        'group_counts':{'total':len(groups),'verified_structure':sum(g['structure_verified'] for g in groups),
            'needs_review':sum(not g['structure_verified'] for g in groups)},
        'blocked_voucher_count':sum(bool(reasons) for reasons in blocked.values()),
        'review_voucher_count':len({_voucher_key(r) for r in decisions if r['hierarchy']['decision']=='needs_review' and _voucher_key(r) is not None}),
        'review_reason_counts':dict(Counter(reason for row in decisions if row['hierarchy']['decision']=='needs_review' for reason in row['hierarchy']['reason_codes'])),
        'diagnostic_projection':{'diagnostic_only':True,'parsed_business_rows':len(valid),'duplicate_parent_rows':len(duplicate_parents),
            'remaining_rows':len(projection),'remaining_record_ids':projection,'duplicate_parent_record_ids':sorted(duplicate_parents)},
        'blocked_vouchers':blockers,'groups':groups,'rows':decisions}
