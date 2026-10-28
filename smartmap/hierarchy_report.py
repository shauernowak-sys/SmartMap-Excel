"""T08报告写入：保留所有原始行和证据，不输出系统导入文件。"""
import csv
import json


def _json(path,value):
    with path.open('x',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)


def _csv(path,rows,fields):
    with path.open('x',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        for row in rows:writer.writerow({key:json.dumps(value,ensure_ascii=False) if isinstance(value,(list,dict)) else value for key,value in row.items()})


def write_hierarchy_report(output,parsed,report):
    if parsed is not None:_json(output/'parse_report.json',parsed)
    _json(output/'hierarchy_report.json',report)
    summary={key:value for key,value in report.items() if key not in ['groups','rows']}
    if 'diagnostic_projection' in summary:
        summary['diagnostic_projection']={key:value for key,value in summary['diagnostic_projection'].items() if not key.endswith('_record_ids')}
    _json(output/'summary.json',summary)
    groups=[]
    for group in report.get('groups',[]):
        groups.append({'evidence_id':group['evidence_id'],**{key:group['context'][key] for key in ['sheet','period','voucher_category','voucher_number','summary']},
            'parent_code':group['parent_code'],'nearest_child_codes':group['nearest_child_codes'],
            'parent_rows':group['parent_excel_rows'],'child_rows':group['child_excel_rows'],
            'parent_debit':group['parent_totals']['debit'],'parent_credit':group['parent_totals']['credit'],
            'child_debit':group['child_totals']['debit'],'child_credit':group['child_totals']['credit'],
            'debit_difference':group['differences']['debit'],'credit_difference':group['differences']['credit'],
            'structure_verified':group['structure_verified'],'reason_codes':group['reason_codes'],
            'rule_id':group['rule_id'],'rule_status':group['rule_status']})
    _csv(output/'hierarchy_groups.csv',groups,['evidence_id','sheet','period','voucher_category','voucher_number','summary','parent_code','nearest_child_codes','parent_rows','child_rows','parent_debit','parent_credit','child_debit','child_credit','debit_difference','credit_difference','structure_verified','reason_codes','rule_id','rule_status'])
    decisions=[]
    for row in report.get('rows',[]):
        decisions.append({'record_id':row['record_id'],'sheet':row['provenance']['sheet'],'excel_row':row['provenance']['excel_row'],
            'period':row['period'],'voucher_category':row['voucher']['category'] if row['voucher'] else None,
            'voucher_number':row['voucher']['number'] if row['voucher'] else None,
            'subject_code':row['subject']['code'] if row['subject'] else None,'summary':row['summary'],
            'debit':row['debit']['value'],'debit_status':row['debit']['status'],'credit':row['credit']['value'],'credit_status':row['credit']['status'],
            'decision':row['hierarchy']['decision'],'reason_codes':row['hierarchy']['reason_codes'],
            'parent_evidence_ids':row['hierarchy']['parent_evidence_ids'],'related_evidence_ids':row['hierarchy']['related_evidence_ids']})
    _csv(output/'row_decisions.csv',decisions,['record_id','sheet','excel_row','period','voucher_category','voucher_number','subject_code','summary','debit','debit_status','credit','credit_status','decision','reason_codes','parent_evidence_ids','related_evidence_ids'])
    counts=report.get('row_counts',{});group_counts=report.get('group_counts',{});projection=report.get('diagnostic_projection',{})
    lines=['# T08 父子科目证据与选择报告','',f"分析完成：{report['analysis_ok']}；可进入下一阶段：{report['ready_for_next_stage']}。",
        f"层级规则状态：{report.get('hierarchy_rule',{}).get('status','未执行')}。",
        f"证据组：{group_counts.get('total',0)}；结构核验通过：{group_counts.get('verified_structure',0)}；组异常：{group_counts.get('needs_review',0)}。",
        f"保留：{counts.get('retained',0)}；实际排除：{counts.get('excluded',0)}；待确认：{counts.get('needs_review',0)}；非业务：{counts.get('non_business',0)}。",
        f"诊断投影：父级候选{projection.get('duplicate_parent_rows',0)}条；结构剩余{projection.get('remaining_rows',0)}条。",'',
        '诊断投影不是实际排除。候选或拒绝规则不能自动删除；异常凭证整组保留待确认。',
        '选择过程只使用原始数据和配置；科目映射、凭证筛选和系统模板输出由后续任务完成。',
        '原始解析见parse_report.json；逐行原值和证据见hierarchy_report.json；CSV用于查阅原始行号。',
        '全局错误：'+json.dumps(report.get('fatal_errors',[]),ensure_ascii=False)]
    with (output/'summary.md').open('x',encoding='utf-8') as stream:stream.write('\n'.join(lines)+'\n')
    return summary
