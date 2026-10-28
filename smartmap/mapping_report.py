"""T09完整行、映射异常及整凭证筛选证据输出。"""
import csv
import json


def _json(path,value):
    with path.open('x',encoding='utf-8') as handle:json.dump(value,handle,ensure_ascii=False,indent=2,allow_nan=False)


def _csv(path,rows,fields):
    with path.open('x',encoding='utf-8-sig',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader()
        for row in rows:writer.writerow({key:json.dumps(value,ensure_ascii=False) if isinstance(value,(list,dict)) else value for key,value in row.items()})


def write_mapping_report(output,parsed,hierarchy,report):
    if parsed is not None:_json(output/'parse_report.json',parsed)
    if hierarchy is not None:_json(output/'hierarchy_report.json',hierarchy)
    _json(output/'mapping_filter_report.json',report)
    summary={k:v for k,v in report.items() if k not in ['rows','filter_groups','voucher_reviews']}
    _json(output/'summary.json',summary)
    mapping_rows=[];decisions=[]
    for row in report.get('rows',[]):
        mapping=row['account_mapping'];scope=row['voucher_filter'];final=row['mapping_filter']
        entry={'record_id':row['record_id'],'sheet':row['provenance']['sheet'],'excel_row':row['provenance']['excel_row'],
            'period':row['period'],'voucher_category':row['voucher']['category'] if row['voucher'] else None,
            'voucher_number':row['voucher']['number'] if row['voucher'] else None,
            'external_code':row['subject']['code'] if row['subject'] else None,'summary':row['summary'],
            'debit':row['debit']['value'],'credit':row['credit']['value'],'mapping_status':mapping['status'],
            'target_code':mapping['target_code'],'target_name':mapping['target_name'],'mapping_id':mapping['mapping_id'],
            'candidate_mapping_ids':mapping['candidate_mapping_ids'],'rejected_mapping_ids':mapping['rejected_mapping_ids'],
            'decision':final['decision'],'reason_codes':final['reason_codes'],'filter_evidence_id':scope['evidence_id'],
            'matched_filter_rule_ids':scope['matched_rule_ids'],'pending_filter_rule_ids':scope['pending_rule_ids']}
        decisions.append(entry)
        if mapping['status']!='not_evaluated':mapping_rows.append(entry)
    fields=['record_id','sheet','excel_row','period','voucher_category','voucher_number','external_code','summary','debit','credit','mapping_status','target_code','target_name','mapping_id','candidate_mapping_ids','rejected_mapping_ids','decision','reason_codes','filter_evidence_id','matched_filter_rule_ids','pending_filter_rule_ids']
    _csv(output/'row_decisions.csv',decisions,fields)
    _csv(output/'account_mapping.csv',mapping_rows,fields)
    _csv(output/'unmatched_accounts.csv',[r for r in mapping_rows if r['mapping_status']!='matched'],fields)
    filter_rows=[]
    for group in report.get('filter_groups',[]):
        filter_rows.append({'evidence_id':group['evidence_id'],**{k:group['context'][k] for k in ['period','voucher_category','voucher_number']},
            'decision':group['decision'],'source_rows':group['member_excel_rows'],'reason_codes':group['reason_codes'],
            'confirmed_full_rule_ids':group['confirmed_full_rule_ids'],'candidate_full_rule_ids':group['candidate_full_rule_ids'],
            'partial_rule_ids':group['partial_rule_ids'],'evaluations':group['evaluations']})
    _csv(output/'voucher_filters.csv',filter_rows,['evidence_id','period','voucher_category','voucher_number','decision','source_rows','reason_codes','confirmed_full_rule_ids','candidate_full_rule_ids','partial_rule_ids','evaluations'])
    counts=report.get('row_counts',{});m=report.get('mapping_counts',{});f=report.get('filter_counts',{});ex=report.get('exclusion_counts',{})
    lines=['# T09 科目映射与整凭证筛选报告','',f"分析完成：{report['analysis_ok']}；可进入后续阶段：{report['ready_for_next_stage']}。",
        f"全部原始行：{counts.get('total',0)}；阶段保留：{counts.get('retained',0)}；实际排除：{counts.get('excluded',0)}；待确认：{counts.get('needs_review',0)}；非业务：{counts.get('non_business',0)}。",
        f"已确认映射：{m.get('matched',0)}；未确认：{m.get('unconfirmed',0)}；候选冲突：{m.get('candidate_conflict',0)}；未知：{m.get('unknown',0)}；已拒绝：{m.get('rejected',0)}；不需/暂不求映射：{m.get('not_evaluated',0)}。",
        f"完整凭证组：{f.get('voucher_groups',0)}；实际业务排除凭证：{f.get('excluded',0)}；候选完整命中：{f.get('candidate_full_match_vouchers',0)}；部分命中：{f.get('partial_match_vouchers',0)}。",
        f"排除行分类：已确认业务规则{ex.get('voucher_filter',0)}条，已确认层级证据{ex.get('hierarchy',0)}条。",'',
        '映射仅对层级阶段保留的行求值；上游待确认、父级已排除、非业务或整凭证业务排除行不填目标科目。',
        '未知/未确认映射不丢行；同凭证有待确认时，其他分录也保留待确认，避免输出半张凭证。',
        '只使用已确认配置填目标或实际排除；原始金额、辅助核算和日期均保留原值。',
        '当前尚未执行T10借贷平衡、分录序号及月末日期生成，也未生成系统导入文件。',
        '全部原始行及状态见mapping_filter_report.json；映射异常见unmatched_accounts.csv，筛选证据见voucher_filters.csv。',
        '全局错误：'+json.dumps(report.get('fatal_errors',[]),ensure_ascii=False)]
    with (output/'summary.md').open('x',encoding='utf-8') as handle:handle.write('\n'.join(lines)+'\n')
    return summary
