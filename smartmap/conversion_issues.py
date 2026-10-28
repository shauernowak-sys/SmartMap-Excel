"""T11异常事实聚合：区分事实数、直接记录数和受影响记录数。"""
import hashlib
import json
from collections import defaultdict

from .grouping import voucher_key

MESSAGES={
    'UNCONFIRMED_ACCOUNT_MAPPING':'科目只有历史候选，尚未确认；目标科目保留为空。',
    'UNMAPPED_ACCOUNT':'科目没有可用映射；请确认内部编码，程序不会猜测。',
    'ACCOUNT_MAPPING_CANDIDATE_CONFLICT':'同一外部科目存在冲突候选，需要确认唯一目标。',
    'ACCOUNT_MAPPING_REJECTED':'该科目只有已拒绝映射，保留原始记录待确认。',
    'ACCOUNT_MAPPING_OUT_OF_SCOPE':'科目映射仅在已批准案例生效；当前账期或源文件不在范围内，目标科目留空。',
    'HIERARCHY_POLICY_UNCONFIRMED':'父子科目选择策略尚未确认，重复候选完整保留。',
    'HIERARCHY_POLICY_REJECTED':'层级策略已拒绝，相关记录保留待确认。',
    'HIERARCHY_RULE_OUT_OF_SCOPE':'层级规则仅批准用于指定案例，本次账期或源文件不在范围内，父级保留待确认。',
    'VOUCHER_FILTER_UNCONFIRMED':'候选规则命中完整凭证，但尚未确认，未实际排除。',
    'FILTER_PARTIAL_VOUCHER_MATCH':'规则只命中部分分录，整张凭证保留待确认。',
    'PARENT_CHILD_AMOUNT_MISMATCH':'父子科目借贷合计不一致，整张凭证待确认。',
    'AMBIGUOUS_HIERARCHY_SUMMARY':'父子摘要无法对应，不能判断直接记账或层级展开。',
    'ZERO_TOTAL_HIERARCHY_AMBIGUOUS':'父子合计均为零，证据不足以自动排除。',
    'UNBALANCED_VOUCHER':'凭证借贷不平衡；金额不修改，不补造平衡分录。',
    'ZERO_AMOUNT_ENTRY_REQUIRES_REVIEW':'存在零金额分录，保留并等待确认处理规则。',
    'ZERO_TOTAL_VOUCHER_REQUIRES_REVIEW':'全部金额为零，保留并等待确认。',
    'BOTH_SIDES_NONZERO_REQUIRES_REVIEW':'单行借贷同时非零，保留原值，不擅自拆分。',
    'AUXILIARY_REQUIREMENT_UNKNOWN':'辅助核算要求尚未确认，正式候选暂不输出。',
    'AUXILIARY_PROFILE_UNCONFIRMED':'原始辅助取值策略与按科目要求已配置为候选，尚未批准为正式规则。',
    'AUXILIARY_PROFILE_REJECTED':'辅助取值策略已拒绝，不应用候选值。',
    'AUXILIARY_PROFILE_OUT_OF_SCOPE':'辅助取值策略不在本次案例范围，不提取或填充。',
    'AUXILIARY_DICTIONARY_MISSING':'名称可从原始记录提取，但当前辅助备查没有唯一档案，不能猜编码或新增档案。',
    'AUXILIARY_DICTIONARY_AMBIGUOUS':'辅助名称或编码在备查中重复，不能首条匹配。',
    'AUXILIARY_SOURCE_FORMAT_AMBIGUOUS':'原始名称不符合已配置的两段式结构，不能猜取后缀。',
    'AUXILIARY_MULTIPLE_FIELDS_SOURCE_AMBIGUOUS':'目标科目需多个辅助字段，当前单一后缀来源无法区分。',
    'AUXILIARY_SOURCE_MISSING':'未找到已配置的明确辅助字段或可用名称来源。',
    'AUXILIARY_SOURCE_VALUE_INVALID':'明确辅助字段类型无效，不能回退掩盖原始异常。',
    'AUXILIARY_EXPLICIT_VALUE_CONFLICT':'明确辅助编码与名称相互冲突，整张凭证待确认。',
    'AUXILIARY_TARGET_ACCOUNT_NOT_IN_CATALOG':'目标科目未在当前备查快照中，不能推断为无需辅助核算。',
    'BATCH_REVIEW_BLOCKED':'批次存在待确认凭证，未明确选择部分导出，正常凭证保留待审核。',
    'REQUIRED_AUXILIARY_VALUES_UNAVAILABLE':'已确认必填辅助字段尚无已确认取数规则，不能编造值。',
    'REQUIRES_FINANCIAL_REVIEW':'记录尚未通过全部条件，详见阶段证据。'}


def collect_issues(bundle,stages,config,fatal=None):
    rows=bundle['source_rows'];by_id={r['record_id']:r for r in rows}
    pending={d['record_id'] for d in bundle['row_decisions'] if d['decision']=='needs_review'}
    vouchers=defaultdict(list)
    for row in rows:
        if row['record_id'] in pending and voucher_key(row):vouchers[voucher_key(row)].append(row['record_id'])
    issues=[]
    def add(code,scope,locator,records,rule_ids=(),evidence_ids=(),details=None,severity='needs_confirmation',message=None):
        records=list(dict.fromkeys(records));affected=[]
        for identity in records:
            key=voucher_key(by_id[identity])
            affected.extend(vouchers.get(key,[]) if key else [identity])
        if scope=='run':affected.extend(pending)
        affected=sorted(set(affected)&pending)
        issue_id=hashlib.sha256(json.dumps([code,scope,locator,sorted(records),sorted(rule_ids)],ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()
        issues.append({'issue_id':issue_id,'code':code,'scope':scope,'severity':severity,
            'message':message or MESSAGES.get(code,'原始记录需要核验：'+code),
            'locator':locator,'source_record_ids':records,'affected_record_ids':affected,
            'provenance':[copy_provenance(by_id[i]) for i in records],'rule_ids':list(rule_ids),
            'evidence_ids':list(evidence_ids),'details':details or {}})
    if fatal:
        add(fatal['code'],'run',{'stage':fatal['stage']},[],details=fatal,severity='fatal',message=fatal['message'])
    else:
        for row in rows:
            if row['record_id'] not in pending:continue
            for item in row.get('issues',[]):
                raw=row.get('raw_fields',{}).get(item.get('field'))
                if raw is None and str(item.get('field','')).startswith('column_'):
                    try:column=int(item['field'].split('_')[-1]);raw=next((c for c in row.get('raw_cells',[]) if c['column']==column),None)
                    except ValueError:pass
                add(item['code'],'record',{'record_id':row['record_id'],'field':item.get('field')},[row['record_id']],
                    details={'raw':raw},severity='error',message=item.get('message'))
        mapping_codes={'unconfirmed':'UNCONFIRMED_ACCOUNT_MAPPING','unknown':'UNMAPPED_ACCOUNT',
            'candidate_conflict':'ACCOUNT_MAPPING_CANDIDATE_CONFLICT','rejected':'ACCOUNT_MAPPING_REJECTED',
            'out_of_scope':'ACCOUNT_MAPPING_OUT_OF_SCOPE'}
        accounts=defaultdict(list)
        for row in rows:
            status=row.get('account_mapping',{}).get('status')
            if row['record_id'] in pending and status in mapping_codes:accounts[(row['subject']['code'],status)].append(row)
        for (code,status),entries in accounts.items():
            add(mapping_codes[status],'account',{'external_code':code},[r['record_id'] for r in entries],
                rule_ids=sorted({i for r in entries for i in r['account_mapping']['candidate_mapping_ids']+r['account_mapping']['rejected_mapping_ids']+r['account_mapping'].get('out_of_scope_mapping_ids',[])}))
        hierarchy=stages.get('hierarchy',{})
        for code in ['HIERARCHY_POLICY_UNCONFIRMED','HIERARCHY_POLICY_REJECTED','HIERARCHY_RULE_OUT_OF_SCOPE']:
            entries=[r for r in rows if r['record_id'] in pending and code in r.get('hierarchy',{}).get('reason_codes',[])]
            if entries:
                add(code,'rule',{'rule_id':hierarchy['hierarchy_rule']['id']},[r['record_id'] for r in entries],
                    rule_ids=[hierarchy['hierarchy_rule']['id']],evidence_ids=sorted({i for r in entries for i in r['hierarchy']['parent_evidence_ids']}))
        for group in hierarchy.get('blocked_vouchers',[]):
            relevant=[i for i in group['related_record_ids'] if i in pending]
            if relevant:
                for code in group['reason_codes']:add(code,'voucher',group['context'],relevant,details={'original_excel_rows':group['related_excel_rows']},severity='error')
        for group in stages.get('mapping',{}).get('filter_groups',[]):
            if group['decision']!='needs_review':continue
            for code,rule_ids in [('VOUCHER_FILTER_UNCONFIRMED',group['candidate_full_rule_ids']),('FILTER_PARTIAL_VOUCHER_MATCH',group['partial_rule_ids'])]:
                if rule_ids:add(code,'voucher',group['context'],group['member_record_ids'],rule_ids=rule_ids,evidence_ids=[group['evidence_id']])
        skip={'UPSTREAM_VOUCHER_REQUIRES_REVIEW','UPSTREAM_PARSE_REQUIRES_REVIEW','UNATTRIBUTED_UPSTREAM_ROW'}
        for group in stages.get('voucher',{}).get('official_vouchers',[]):
            if group['status'] not in ['unbalanced','needs_review']:continue
            for code in group['reason_codes']:
                if code not in skip:add(code,'voucher',group['context'],group['selected_record_ids'],evidence_ids=[group['evidence_id']],details={'totals':group['totals'],'difference':group['difference']},severity='error')
        aux=bundle.get('auxiliary_gate')
        batch=bundle.get('batch_gate',{})
        if batch.get('blocked'):add('BATCH_REVIEW_BLOCKED','run',{'allow_partial':False},batch['eligible_record_ids'])
        if aux and aux['reason_code'] and pending:
            add(aux['reason_code'],'run',{'rule_id':aux['rule_id']},[],rule_ids=[aux['rule_id']],details={'required_fields':aux['required_fields']})
        profile=config.get('auxiliary_profile')
        if profile:
            buckets=defaultdict(list)
            for row in rows:
                if row['record_id'] not in pending:continue
                for code in row.get('auxiliary_validation',{}).get('reason_codes',[]):
                    if code=='AUXILIARY_REQUIREMENT_UNKNOWN':continue
                    if code=='AUXILIARY_TARGET_UNRESOLVED':continue
                    proposals=row.get('auxiliary_candidates',{})
                    detail=tuple((field,p['value']) for field,p in sorted(proposals.items())) if code.startswith('AUXILIARY_DICTIONARY_') else ()
                    buckets[(code,detail)].append(row['record_id'])
            for (code,detail),identities in buckets.items():
                add(code,'auxiliary',{'profile_id':profile['id'],'values':list(detail)},identities,rule_ids=[profile['id']])
    covered={i for issue in issues for i in issue['affected_record_ids']}
    for identity in sorted(pending-covered):
        add('REQUIRES_FINANCIAL_REVIEW','record',{'record_id':identity},[identity],details={'reasons':next(d['reason_codes'] for d in bundle['row_decisions'] if d['record_id']==identity)})
    links=defaultdict(list)
    for issue in issues:
        for identity in issue['affected_record_ids']:links[identity].append(issue['issue_id'])
    for decision in bundle['row_decisions']:decision['exception_ids']=links[decision['record_id']]
    return issues


def copy_provenance(row):return dict(row['provenance'])
