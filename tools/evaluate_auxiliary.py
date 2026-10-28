"""转换完成后独立核验辅助候选及正式值；标准答案不传回转换引擎。"""
import argparse
import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path

import openpyxl

from analysis_core import day, money, text, voucher
from verify_conversion_run import audit_run

FIELDS={'customer':13,'supplier':14,'department':15,'employee':16,'stock_name':17,'project':20,
        'customer_level':21,'inbound_type':22,'outbound_type':23}
LABELS={'customer':'客户','supplier':'供应商','department':'部门','employee':'员工','stock_name':'存货',
        'project':'项目','customer_level':'客户等级','inbound_type':'其他入库类别','outbound_type':'其他出库类别'}


def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))


def automatic_key(row):
    return (row['period'],row['voucher_number'],row['summary'],row['target_subject_code'],
            money(row['debit']['value']),money(row['credit']['value']))


def evaluate(run,final,out):
    run,final,out=(Path(p).resolve() for p in [run,final,out])
    if out.exists() or out.is_symlink() or run==out or run in out.parents:
        raise ValueError('核验目录必须全新且位于转换目录外')
    audit=audit_run(run)
    if not audit['usable_for_evaluation']:raise ValueError('转换产物无效，不能核验')
    m=read(run/'run_manifest.json');source=read(run/'source_rows.json')['rows'];diag=read(run/'diagnostic_candidates.json')['rows']
    before=hashlib.sha256(final.read_bytes()).hexdigest()
    book=openpyxl.load_workbook(final,read_only=True,data_only=False)
    try:
        sheet=book['凭证模板'];sheet.reset_dimensions();rows=list(sheet.iter_rows(values_only=True))
    finally:book.close()
    if list(rows[0])[13:17]!=['客户','供应商','部门','员工']:raise ValueError('辅助列布局不符合历史样本')
    expected=Counter();expected_rows=[]
    for number,cells in enumerate(rows[1:],2):
        if all(v is None for v in cells):continue
        cells=list(cells)+[None]*24
        actual_date=date.fromisoformat(day(cells[1]))
        period=f'{actual_date.year:04d}{actual_date.month:02d}'
        if period!=m['period'] or voucher(cells[3]) is None:raise ValueError('成品账期或凭证无法关联本次转换')
        debit,credit=money(cells[8]),money(cells[9])
        if 'invalid' in [debit[0],credit[0]]:raise ValueError('成品金额无效')
        key=(period,voucher(cells[3]),text(cells[5]),text(cells[6]),debit,credit)
        for field,column in FIELDS.items():
            value=text(cells[column])
            if value:
                expected[key+(field,value)]+=1
                expected_rows.append({'final_row':number,'field':field,'value':value})
    proposals=Counter();actual=Counter();proposal_rows=[]
    for row in diag:
        key=automatic_key(row)
        for field,p in row.get('auxiliary_candidates',{}).items():
            proposals[key+(field,p['value'])]+=1
            proposal_rows.append({'record_id':row['record_id'],'source_row':row['provenance']['excel_row'],
                                 'field':field,'value':p['value'],'dictionary_status':p['dictionary_status'],
                                 'dictionary_code':p['dictionary_code'],'approved':p['approved']})
        for field,value in row['auxiliary'].items():actual[key+(field,value)]+=1
    stats=lambda values:{'produced':sum(values.values()),'expected':sum(expected.values()),'matched':sum((values&expected).values()),
                         'missing':sum((expected-values).values()),'extra':sum((values-expected).values())}
    result={'mode':'posthoc_auxiliary_evaluation','run_id':m['run_id'],
            'inputs':{'run_manifest_sha256':hashlib.sha256((run/'run_manifest.json').read_bytes()).hexdigest(),
                      'final_path':str(final),'final_sha256':before},
            'proposals_vs_reference':stats(proposals),'approved_values_vs_reference':stats(actual),
            'dictionary_status_counts':dict(Counter(r['dictionary_status'] for r in proposal_rows)),
            'field_counts':dict(Counter(r['field'] for r in proposal_rows)),
            'proposal_rows':proposal_rows,'reference_rows':expected_rows,
            'missing_dictionary_groups':[{'field':field,'value':value,'source_rows':[r['source_row'] for r in proposal_rows if r['field']==field and r['value']==value and r['dictionary_status']=='missing']}
               for field,value in sorted({(r['field'],r['value']) for r in proposal_rows if r['dictionary_status']=='missing'})],
            'formal_candidate_count':m['row_counts']['included'],'ready_for_system_import':False}
    if hashlib.sha256(final.read_bytes()).hexdigest()!=before or not audit_run(run)['usable_for_evaluation']:
        raise ValueError('核验期间输入发生变化')
    out.mkdir(parents=True);(out/'.gitignore').write_text('*\n',encoding='utf-8')
    with (out/'auxiliary_evaluation.json').open('x',encoding='utf-8') as stream:json.dump(result,stream,ensure_ascii=False,indent=2)
    p=result['proposals_vs_reference'];a=result['approved_values_vs_reference']
    lines=['# 原始辅助取值核验', '',
           '候选取值来自原始科目名称及配置；本工具在转换完成后才读取成品作独立核对。',
           f"候选值：提取{p['produced']}，匹配{p['matched']}，漏{p['missing']}，多{p['extra']}。",
           f"正式已批准辅助值：{a['produced']}；仍缺{a['missing']}。正式候选{result['formal_candidate_count']}条。",
           '提取正确、字典匹配、规则批准和系统可导入分别核验。', '', '| 字段 | 原始提取数 |', '| --- | ---: |']
    lines += [f'| {LABELS[field]} | {count} |' for field,count in result['field_counts'].items()]
    lines += ['', '| 备查缺项字段 | 原始名称 | 原始行号 |', '| --- | --- | --- |']
    for group in result['missing_dictionary_groups']:
        lines.append('| '+LABELS[group['field']]+' | '+group['value'].replace('|','\\|')+' | '+'、'.join(map(str,group['source_rows']))+' |')
    lines += ['', '不编造档案编码，不把备查缺项当作源名称缺失；所有来源行及重复次数保留。']
    (out/'辅助核算核验结论.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['run','final','out']:parser.add_argument('--'+name,required=True,type=Path)
    args=parser.parse_args()
    try:
        result=evaluate(args.run,args.final,args.out)
        print(json.dumps({k:result[k] for k in ['proposals_vs_reference','approved_values_vs_reference','dictionary_status_counts']},ensure_ascii=False));return 0
    except (OSError,ValueError,KeyError,TypeError) as error:
        print(json.dumps({'status':'failed','error':str(error)},ensure_ascii=False));return 2


if __name__=='__main__':raise SystemExit(main())
