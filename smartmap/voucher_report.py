"""T10凭证核验报告；正式序号与诊断序号分列，完整保留原始数据。"""
import csv
import json


def _json(path,value):
    with path.open('x',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)


def _csv(path,rows,fields):
    with path.open('x',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        for row in rows:writer.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in row.items()})


def _balance_rows(groups):
    return [{'evidence_id':g['evidence_id'],'view':g['view'],'diagnostic_only':g['diagnostic_only'],
        **{k:g['context'][k] for k in ['period','voucher_category','voucher_number']},
        'source_excel_rows':g['source_excel_rows'],'selected_excel_rows':g['selected_excel_rows'],
        'status':g['status'],'debit':g['totals']['debit'] if g['totals'] else None,'credit':g['totals']['credit'] if g['totals'] else None,
        'difference':g['difference'],'balanced':g['balanced'],'reason_codes':g['reason_codes'],'posting_date':g['posting_date']} for g in groups]


def write_voucher_report(output,parsed,hierarchy,mapped,report):
    for name,value in [('parse_report',parsed),('hierarchy_report',hierarchy),('mapping_filter_report',mapped)]:
        if value is not None:_json(output/(name+'.json'),value)
    _json(output/'voucher_report.json',report)
    summary={k:v for k,v in report.items() if k not in ['rows','official_vouchers','diagnostic_vouchers','unassigned_record_ids']}
    _json(output/'summary.json',summary)
    balance_fields=['evidence_id','view','diagnostic_only','period','voucher_category','voucher_number','source_excel_rows','selected_excel_rows','status','debit','credit','difference','balanced','reason_codes','posting_date']
    _csv(output/'voucher_balances.csv',_balance_rows(report.get('official_vouchers',[])),balance_fields)
    _csv(output/'diagnostic_voucher_balances.csv',_balance_rows(report.get('diagnostic_vouchers',[])),balance_fields)
    assignments=[]
    for row in report.get('rows',[]):
        v=row['voucher_validation'];diag=row['diagnostic_assignment']
        assignments.append({'record_id':row['record_id'],'sheet':row['provenance']['sheet'],'excel_row':row['provenance']['excel_row'],
            'period':row['period'],'voucher_category':row['voucher']['category'] if row['voucher'] else None,'voucher_number':row['voucher']['number'] if row['voucher'] else None,
            'external_code':row['subject']['code'] if row['subject'] else None,'target_code':row['account_mapping']['target_code'],
            'summary':row['summary'],'debit':row['debit']['value'],'credit':row['credit']['value'],'original_date':row['date'],
            'proposed_posting_date':v['posting_date'],'entry_sequence':v['entry_sequence'],
            'diagnostic_entry_sequence':diag['entry_sequence'] if diag else None,'diagnostic_posting_date':diag['posting_date'] if diag else None,
            'decision':v['decision'],'reason_codes':v['reason_codes'],'evidence_id':v['evidence_id']})
    _csv(output/'row_assignments.csv',assignments,['record_id','sheet','excel_row','period','voucher_category','voucher_number','external_code','target_code','summary','debit','credit','original_date','proposed_posting_date','entry_sequence','diagnostic_entry_sequence','diagnostic_posting_date','decision','reason_codes','evidence_id'])
    official=report.get('official_summary',{});diagnostic=report.get('diagnostic_summary',{})
    lines=['# T10 凭证核验、序号与月末日期报告','',
        f"核验完成：{report['analysis_ok']}；可进入下一阶段：{report['ready_for_next_stage']}。",
        f"账期：{report.get('period','未确定')}；月末：{report.get('month_end','未生成')}。",
        f"正式范围：平衡{official.get('balanced',0)}组，不平衡{official.get('unbalanced',0)}组，待确认{official.get('needs_review',0)}组，排除{official.get('excluded',0)}组。",
        f"诊断范围：平衡{diagnostic.get('balanced',0)}组，不平衡{diagnostic.get('unbalanced',0)}组，待确认{diagnostic.get('needs_review',0)}组。",
        f"正式赋序号{report.get('official_assigned_rows',0)}行；诊断赋序号{report.get('diagnostic_assigned_rows',0)}行。",'',
        '正式核验范围尚未具备条件时，平衡率记为未评价（分母0，JSON值null）。',
        '月末建议值与正式序号分开。未确认记录不因日期或诊断平衡而自动获得批准。',
        '序号按同凭证原始Excel行升序从1连续生成；金额、原始日期和重复次数保留，不自动平账。',
        '全零分录或单行借贷同时非零的特殊情况保留待确认；正负抵消的净零凭证保留原始金额并按实际合计核验。',
        '诊断投影不是正式凭证，也不是系统导入文件；正式平衡见voucher_balances.csv，诊断见diagnostic_voucher_balances.csv。',
        '完整原值与行级证据见voucher_report.json；日期、正式和诊断序号分列见row_assignments.csv。',
        '全局错误：'+json.dumps(report.get('fatal_errors',[]),ensure_ascii=False)]
    with (output/'summary.md').open('x',encoding='utf-8') as stream:stream.write('\n'.join(lines)+'\n')
    return summary
