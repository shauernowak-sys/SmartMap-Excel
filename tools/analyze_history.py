"""T03/T04/T05：独立盘点、金额类型核验与历史差异逐条追踪；不执行生产转换。"""
import argparse
import calendar
import csv
import hashlib
import json
from collections import Counter, defaultdict, deque
from decimal import Decimal
from pathlib import Path

import openpyxl
from openpyxl.utils.cell import get_column_letter

from analysis_core import ancestor, blank, day, money, pair_occurrences, subject, text, voucher, xml_sheets

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n', encoding='utf-8')


def write_csv(path, rows, fields=None):
    if fields is None:
        fields = list(rows[0]) if rows else ['说明']
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v,ensure_ascii=False,default=str) if isinstance(v,(list,dict,tuple)) else v for k,v in row.items()})


def load(path):
    book = openpyxl.load_workbook(path, read_only=True, data_only=False)
    try:
        return {sheet.title: [list(row) for row in sheet.iter_rows(values_only=True)] for sheet in book}
    finally:
        book.close()


def record(row, number, stage, layout):
    rec = {'stage': stage, 'row': number}
    for field, col in layout.items():
        rec[field+'_raw'] = row[col] if col < len(row) else None
    rec.update(voucher=voucher(rec['voucher_raw']), code=subject(rec['code_raw']),
        summary=text(rec['summary_raw']))
    for side in ['debit','credit']:
        rec[side+'_status'], rec[side] = money(rec[side+'_raw'])
    return rec


def signature(rec):
    return (rec['voucher'],rec['code'],rec['summary'],rec['debit_status'],rec['debit'],rec['credit_status'],rec['credit'])


def statistics(records):
    groups = defaultdict(list)
    for rec in records:
        groups[rec['voucher']].append(rec)
    balances = []
    for v, entries in sorted(groups.items()):
        totals = {side: sum((x[side] or Decimal(0) for x in entries), Decimal(0)) for side in ['debit','credit']}
        valid = all(x[s+'_status'] != 'invalid' for x in entries for s in ['debit','credit'])
        balances.append({'voucher':v,'lines':len(entries),**totals,'valid_amounts':valid,
            'difference':totals['debit']-totals['credit'], 'balanced':valid and totals['debit']==totals['credit']})
    return {'lines':len(records),'vouchers':len(groups),'subjects':len({r['code'] for r in records}),
        'debit':sum((r['debit'] or Decimal(0) for r in records),Decimal(0)),
        'credit':sum((r['credit'] or Decimal(0) for r in records),Decimal(0)),
        'balanced_vouchers':sum(b['balanced'] for b in balances),'balance':balances}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args();out=args.out.resolve()
    if ROOT not in out.parents:
        parser.error('输出必须位于项目内部的新目录')
    out.mkdir(parents=True,exist_ok=False)
    source=ROOT/'samples/202606_明细账26.7.241.xlsx'
    final_path=ROOT/'samples/凭证导入模板2026.6月.xlsx'
    analysis_path=ROOT/'samples/SmartMap_三表逐行核对分析.xlsx'
    baseline=json.loads((ROOT/'reports/environment/baseline.json').read_text(encoding='utf-8'))
    for item in baseline['files']:
        if hashlib.sha256((ROOT/item['path']).read_bytes()).hexdigest()!=item['sha256']:
            raise RuntimeError('接管基线变更：'+item['path'])
    books={p.name:load(p) for p in [source,final_path,analysis_path]}
    raw_rows=books[source.name]['明细账'];manual_rows=books[source.name]['明细账 (2)'];final_rows=books[final_path.name]['凭证模板']
    source_xml=xml_sheets(source)
    raw_all=[record(r,i,'raw',{'voucher':3,'code':2,'summary':4,'debit':5,'credit':6}) for i,r in enumerate(raw_rows,1) if i>=3]
    raw=[r for r in raw_all if r['voucher'] is not None and r['code']]
    manual=[record(r,i,'manual',{'voucher':4,'code':3,'summary':6,'debit':9,'credit':10}) for i,r in enumerate(manual_rows,1) if i>=3]
    manual=[r for r in manual if r['voucher'] is not None and r['code']]
    final=[record(r,i,'final',{'voucher':3,'code':6,'summary':5,'debit':8,'credit':9}) for i,r in enumerate(final_rows,1) if i>=2]
    final=[r for r in final if r['voucher'] is not None and r['code']]
    pairs,missing,unused=pair_occurrences(manual,raw,signature)
    links=[{'manual_row':manual[i]['row'],'raw_row':raw[j]['row'],'signature':signature(manual[i]),
        'equal_signature_raw_count':sum(signature(r)==signature(manual[i]) for r in raw)} for i,j in pairs]
    # 人工→成品按全部共同业务字段关联，显式处理日期类型、金额类型和空白表示。
    # C列在人工第二行是账期，不与成品凭证类别强行等同。
    common={ 'date':(1,1),'voucher':(4,3),'sequence':(5,4),'summary':(6,5),'target_code':(7,6),'target_name':(8,7),
        'debit':(9,8),'credit':(10,9),'quantity':(11,10),'currency':(12,11),'foreign_amount':(13,12),
        'customer':(14,13),'supplier':(15,14),'department':(16,15),'employee':(17,16),'stock_name':(18,17),
        'stock_spec':(19,18),'attachments':(20,19),'project':(21,20),'customer_level':(22,21),'inbound_type':(23,22),'outbound_type':(24,23)}
    def payload(rec,stage):
        row=(manual_rows if stage=='manual' else final_rows)[rec['row']-1]
        result={}
        for field,cols in common.items():
            v=row[cols[0 if stage=='manual' else 1]]
            result[field]=day(v) if field=='date' else voucher(v) if field=='voucher' else money(v) if field in ['debit','credit','quantity','foreign_amount','attachments'] else text(v)
        return result
    def target_signature(rec,stage):
        # 已确认日期统一月末；先用非日期字段建立关联，再单独核验日期变化。
        return tuple(v for k,v in payload(rec,stage).items() if k!='date')
    mapped_manual=[{'record':r,'sig':target_signature(r,'manual')} for r in manual]
    mapped_final=[{'record':r,'sig':target_signature(r,'final')} for r in final]
    target_pairs,target_missing,target_extra=pair_occurrences(mapped_manual,mapped_final,lambda x:x['sig'])
    target_links=[]
    for i,j in target_pairs:
        left=payload(manual[i],'manual');right=payload(final[j],'final')
        target_links.append({'manual_row':manual[i]['row'],'final_row':final[j]['row'],
            'non_date_fields_equal':True,'manual_date':left['date'],'final_date':right['date'],
            'date_equal':left['date']==right['date']})
    # 样本中明确存在账期；正式账期推断实现留给T07。
    periods=Counter(text(r[1]) for r in raw_rows[2:] if len(r)>1 and not blank(r[1]))
    sheet_inventory=[{'file':filename,'sheet':name,'physical_rows':len(rows),'max_columns':max((len(r) for r in rows),default=0)} for filename,sheets in books.items() for name,rows in sheets.items()]
    write_json(out/'inventory.json',sheet_inventory)
    write_json(out/'raw_records.json',raw_all);write_json(out/'manual_records.json',manual);write_json(out/'final_records.json',final)
    write_csv(out/'raw_parsing_decisions.csv',[{'row':r['row'],'voucher_raw':r['voucher_raw'],'code':r['code'],'summary':r['summary'],
        'classification':'凭证科目记录' if r['voucher'] is not None and r['code'] else '无凭证科目行' if r['code'] and blank(r['voucher_raw']) else '非业务尾行' if not r['code'] and blank(r['voucher_raw']) else '解析异常，待确认'} for r in raw_all])
    write_csv(out/'manual_to_raw.csv',links);write_csv(out/'manual_to_final.csv',target_links)
    write_json(out/'unmatched.json',{'manual_without_raw':[manual[i] for i in missing],
        'manual_without_final':[manual[i] for i in target_missing], 'final_without_manual':[final[i] for i in target_extra]})
    # T04 逐个业务金额单元格：底层XML与读取器解码、空白、数值、非法、公式等交叉核验。
    amount_audit=[]
    for p, sheet, columns, first in [(source,'明细账',[5,6],3),(source,'明细账 (2)',[9,10],3),(final_path,'凭证模板',[8,9],2)]:
        cells=(source_xml if p==source else xml_sheets(p))[sheet]
        rows=books[p.name][sheet]
        for number,row in enumerate(rows,1):
            if number<first:continue
            for col in columns:
                address=f'{get_column_letter(col+1)}{number}'
                cell=cells.get(address,{'type':'absent','raw':None,'decoded':None,'formula':False})
                value=row[col] if col<len(row) else None
                a,av=money(value);b,bv=money(cell['decoded'])
                equal=(a,av)==(b,bv)
                amount_audit.append({'file':p.name,'sheet':sheet,'cell':address,**cell,
                    'openpyxl_value':value,'status':a,'amount':av,'xml_status':b,'xml_amount':bv,
                    'decoded_equal':equal,'anomaly':cell['formula'] or cell['type']=='e' or a=='invalid' or not equal})
    write_csv(out/'amount_cells.csv',amount_audit)
    # T05 完整重建旧候选签名；故障模型只用于诊断，不是生产规则。
    subjects={r['code'] for r in manual};vouchers={r['voucher'] for r in manual}
    def old_signature(r,inject=False):
        return (10 if inject and blank(r['voucher_raw']) else r['voucher'],r['code'],
            None if r['debit']==10 else r['debit'],None if r['credit']==10 else r['credit'])
    fault_candidates=[r for r in raw_all if r['code'] in subjects and (10 if blank(r['voucher_raw']) else r['voucher']) in vouchers]
    ref=Counter(old_signature(r) for r in manual)
    pred=Counter(old_signature(r,True) for r in fault_candidates)
    extra=pred-ref
    remaining=ref.copy(); extra_records=[]
    for r in fault_candidates:
        key=old_signature(r,True)
        if remaining[key]:remaining[key]-=1
        else:extra_records.append(r)
    buckets=defaultdict(deque)
    for r in extra_records:buckets[old_signature(r,True)].append(r)
    all_options={key:list(values) for key,values in buckets.items()}
    legacy=next((ROOT/'verifier').rglob('候选差异.csv'))
    with legacy.open(encoding='utf-8-sig',newline='') as stream: historical=list(csv.DictReader(stream))
    traced=[]
    for index,h in enumerate(historical,2):
        def old_amount(value):return None if value=='None' else Decimal(value)
        key=(int(h['凭证号']),h['外部科目'],old_amount(h['借方']),old_amount(h['贷方']))
        alternatives=len(buckets[key]);options=all_options.get(key,[]);r=buckets[key].popleft() if buckets[key] else None
        voucher_xml=source_xml['明细账'].get(f'D{r["row"]}',{}) if r else {}
        traced.append({'historical_csv_line':index,'historical_type':h['类型'],'historical_voucher':h['凭证号'],
            'raw_file':source.name,'raw_sheet':'明细账',
            'raw_row':r['row'] if r else None,'actual_voucher':r['voucher_raw'] if r else None,
            'code':r['code'] if r else h['外部科目'],'summary':r['summary'] if r else None,
            'debit':r['debit'] if r else None,'credit':r['credit'] if r else None,
            'primary_cause':'空字符串解析导致无凭证记录误作凭证10' if r and blank(r['voucher_raw']) else '待确认',
            'secondary_tag':r['summary'] if r else '未定位','remaining_same_signature_options':alternatives,
            'all_same_signature_raw_rows':[x['row'] for x in options],
            'possible_secondary_tags':[x['summary'] for x in options],
            'association_status':'唯一签名定位' if len(options)==1 else '按原始顺序分配重复次数，单条身份不唯一' if options else '未定位',
            'voucher_cell':f'D{r["row"]}' if r else None,'voucher_xml_type':voucher_xml.get('type'),
            'voucher_xml_raw':voucher_xml.get('raw'),'voucher_xml_decoded':voucher_xml.get('decoded')})
    write_csv(out/'historical_144_trace.csv',traced)
    # 每个原始行的人工关联与结构证据；父子关系仅表示候选，不执行排除。
    matched_rows={raw[j]['row'] for _,j in pairs}; raw_explanations=[];hierarchy=[]
    for r in raw:
        descendants=[x for x in raw if x['voucher']==r['voucher'] and x['summary']==r['summary'] and ancestor(r['code'],x['code'])]
        direct=[x for x in descendants if not any(ancestor(r['code'],mid['code']) and ancestor(mid['code'],x['code']) for mid in descendants)]
        equal=bool(direct) and all(sum((x[s] or Decimal(0) for x in direct),Decimal(0))==(r[s] or Decimal(0)) for s in ['debit','credit'])
        if descendants:
            hierarchy.append({'row':r['row'],'voucher':r['voucher'],'code':r['code'],'debit':r['debit'],'credit':r['credit'],
                'immediate_observed_descendant_rows':[x['row'] for x in direct],'side_totals_equal':equal,
                'child_debit_total':sum((x['debit'] or Decimal(0) for x in direct),Decimal(0)),
                'child_credit_total':sum((x['credit'] or Decimal(0) for x in direct),Decimal(0)),
                'same_parent_group_rows':[x['row'] for x in raw if x['voucher']==r['voucher'] and x['summary']==r['summary'] and x['code']==r['code']],
                'qualification':'同凭证同摘要下最近可见子级合计证据；不等于已确认可删除'})
        status='匹配人工分录' if r['row'] in matched_rows else '仅原始存在的凭证，业务取舍待确认' if r['voucher'] not in vouchers else '有父子关系证据，待规则验证' if descendants else '未知原因'
        raw_explanations.append({**r,'audit_status':status,'descendant_rows':[x['row'] for x in descendants],'immediate_descendant_totals_equal':equal})
    write_json(out/'raw_row_disposition.json',raw_explanations);write_csv(out/'hierarchy_evidence.csv',hierarchy)
    hierarchy_groups=[];seen=set();raw_lookup={r['row']:r for r in raw}
    for h in hierarchy:
        parent=raw_lookup[h['row']];key=(parent['voucher'],parent['code'],parent['summary'])
        if key in seen:continue
        seen.add(key)
        parent_rows=[raw_lookup[i] for i in h['same_parent_group_rows']]
        totals={s:sum((x[s] or Decimal(0) for x in parent_rows),Decimal(0)) for s in ['debit','credit']}
        hierarchy_groups.append({'voucher':parent['voucher'],'code':parent['code'],'summary':parent['summary'],
            'parent_rows':h['same_parent_group_rows'],'child_rows':h['immediate_observed_descendant_rows'],
            'parent_debit':totals['debit'],'parent_credit':totals['credit'],
            'child_debit':h['child_debit_total'],'child_credit':h['child_credit_total'],
            'aggregate_equal':totals['debit']==h['child_debit_total'] and totals['credit']==h['child_credit_total']})
    write_csv(out/'hierarchy_group_evidence.csv',hierarchy_groups)
    leaves=[r for r,e in zip(raw,raw_explanations) if not e['descendant_rows']]
    leaf_pairs,leaf_missing,leaf_extra=pair_occurrences(manual,leaves,signature)
    write_json(out/'structural_leaf_experiment.json',{'diagnostic_only':True,
        'rule':'同凭证同摘要中没有可见点分子科目的原始行；仅结构实验，不执行业务删除',
        'rows':[r['row'] for r in leaves],'statistics':statistics(leaves),
        'manual_comparison':{'matched':len(leaf_pairs),'missing':len(leaf_missing),'extra':len(leaf_extra)},
        'outside_manual_voucher_rows':[r for r in leaves if r['voucher'] not in vouchers]})
    duplicates=[]
    for stage,records in [('raw',raw),('manual',manual),('final',final)]:
        groups=defaultdict(list)
        for r in records:
            for side in ['debit','credit']:
                if r[side] is not None:groups[(side,r[side])].append(r)
        for (side,amount),items in groups.items():
            if len(items)>1:
                duplicates.append({'stage':stage,'side':side,'amount':amount,'count':len(items),
                    'rows':[r['row'] for r in items],'vouchers':[r['voucher'] for r in items],'codes':[r['code'] for r in items]})
    write_csv(out/'repeated_amounts.csv',duplicates)
    # 对已有核对工作簿逐行声明进行独立签名核验，不依赖它生成关联。
    prior=books[analysis_path.name];prior_checks=[]
    row_map={x['manual_row']:x['final_row'] for x in target_links}
    for number,row in enumerate(prior['逐行核对_整理对导入'][1:],2):
        if not row or blank(row[0]):continue
        claimed_manual,claimed_final=int(row[0]),int(row[1])
        matched_link=next((x for x in target_links if x['manual_row']==claimed_manual and x['final_row']==claimed_final),None)
        equal=matched_link is not None
        date_claim_ok=bool(matched_link) and ((row[10]=='一致')==matched_link['date_equal'])
        manual_item=next((r for r in manual if r['row']==claimed_manual),None)
        p=payload(manual_item,'manual') if manual_item else {}
        content_claim_ok=manual_item is not None and all([
            voucher(row[2])==p['voucher'],text(row[3])==p['summary'],day(row[4])==p['date'],
            day(row[5])==matched_link['final_date'] if matched_link else False,
            text(row[6])==p['target_code'],money(row[7])==p['debit'],money(row[8])==p['credit']])
        prior_checks.append({'report_row':number,'claimed_manual_row':claimed_manual,'claimed_final_row':claimed_final,
            'independently_verified':equal,'date_claim_verified':date_claim_ok,'content_claim_verified':content_claim_ok,
            'reported_voucher':row[2],'actual_voucher':p.get('voucher')})
    write_csv(out/'prior_report_checks.csv',prior_checks)
    stage_stats={name:statistics(records) for name,records in [('raw',raw),('manual',manual),('final',final)]}
    prior_vouchers=[]
    for number,row in enumerate(prior['凭证级核对'][1:],2):
        if not row or all(blank(x) for x in row):continue
        v=voucher(row[0]);values={k:next((x for x in s['balance'] if x['voucher']==v),None) for k,s in stage_stats.items()}
        if any(x is None for x in values.values()):
            prior_vouchers.append({'report_row':number,'voucher':row[0],'numeric_claims_verified':False,'balance_claim_verified':False,
                'source_presence_claim_verified':False,'issue':'报告凭证号缺失或无法关联，不能按行号补入'})
            continue
        actual=[values['raw']['lines'],values['manual']['lines'],values['final']['lines'],values['manual']['debit'],values['manual']['credit'],values['final']['debit'],values['final']['credit']]
        numeric_claims=[money(x)[1] for x in row[1:8]]
        prior_vouchers.append({'report_row':number,'voucher':v,'numeric_claims_verified':actual==numeric_claims,
            'balance_claim_verified':(row[8]=='平衡')==values['final']['balanced'],'source_presence_claim_verified':(row[9]=='是')==(values['raw'] is not None),'issue':''})
    write_csv(out/'prior_voucher_checks.csv',prior_vouchers,['report_row','voucher','numeric_claims_verified','balance_claim_verified','source_presence_claim_verified','issue'])
    mappings=defaultdict(set)
    for r in manual:
        p=payload(r,'manual');mappings[r['code']].add((p['target_code'],p['target_name']))
    write_json(out/'historical_mapping_evidence.json',[{'external_code':code,'targets':sorted(values),'conflict':len(values)>1,
        'manual_rows':[r['row'] for r in manual if r['code']==code],'status':'历史候选，未作为已确认配置'} for code,values in sorted(mappings.items())])
    summary={'scope':'T03/T04/T05 independent historical analysis, not production conversion',
        'input_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [source,final_path,analysis_path]},
        'periods':dict(periods),'stages':stage_stats,
        'raw_nonvoucher_subject_rows':sum(r['code'] is not None and r['voucher'] is None for r in raw_all),
        'raw_nonvoucher_labels':dict(Counter(r['summary'] for r in raw_all if r['code'] is not None and r['voucher'] is None)),
        'raw_to_manual':{'matched':len(pairs),'manual_missing':len(missing),'raw_unmatched':len(unused),'ambiguous_signature_links':sum(l['equal_signature_raw_count']>1 for l in links)},
        'manual_to_final':{'matched_non_date_fields':len(target_pairs),'date_equal':sum(x['date_equal'] for x in target_links),
            'date_changed':sum(not x['date_equal'] for x in target_links),'manual_missing':len(target_missing),'final_extra':len(target_extra),'compared_fields':list(common),
            'manual_category_is_period':dict(Counter(text(manual_rows[r['row']-1][2]) for r in manual)),
            'final_voucher_category':dict(Counter(text(final_rows[r['row']-1][2]) for r in final))},
        'amount_audit':{'cells':len(amount_audit),'status_counts':dict(Counter(a['status'] for a in amount_audit)),
            'xml_type_counts':dict(Counter(a['type'] for a in amount_audit)),
            'anomalies':sum(a['anomaly'] for a in amount_audit),'number_10_cells':[a for a in amount_audit if a['amount']==10],
            'numeric_zero_cells':sum(a['status']=='number' and a['amount']==0 for a in amount_audit),
            'shared_index_10_cells':sum(a['type']=='s' and a['raw']=='10' for a in amount_audit),
            'shared_index_10_all_blank':all(a['status']=='blank' for a in amount_audit if a['type']=='s' and a['raw']=='10')},
        'historical_difference':{'reported':len(historical),'fault_candidate_count':len(fault_candidates),'reconstructed_extra_count':sum(extra.values()),
            'reconstructed_missing_count':sum((ref-pred).values()),'traced':sum(t['raw_row'] is not None for t in traced),
            'ambiguous_occurrences':sum(len(t['all_same_signature_raw_rows'])>1 for t in traced),
            'ambiguous_signature_groups':sum(len(values)>1 for values in all_options.values()),
            'unconsumed_extra_rows':sum(len(q) for q in buckets.values()),'cause_counts':dict(Counter(t['primary_cause'] for t in traced)),
            'secondary_counts':dict(Counter(t['secondary_tag'] for t in traced))},
        'raw_disposition_counts':dict(Counter(r['audit_status'] for r in raw_explanations)),
        'mapping_evidence':{'external_codes':len(mappings),'target_codes':len({p[0] for values in mappings.values() for p in values}),
            'conflicts':sum(len(values)>1 for values in mappings.values()),'raw_codes_without_historical_mapping':sorted({r['code'] for r in raw}-set(mappings))},
        'hierarchy':{'parents_with_descendants':len(hierarchy),'individual_row_side_totals_equal':sum(h['side_totals_equal'] for h in hierarchy),
            'groups':len(hierarchy_groups),'aggregate_equal_groups':sum(h['aggregate_equal'] for h in hierarchy_groups)},
        'structural_leaf_experiment':{'lines':len(leaves),'vouchers':statistics(leaves)['vouchers'],
            'balanced_vouchers':statistics(leaves)['balanced_vouchers'],'outside_manual_voucher_lines':sum(r['voucher'] not in vouchers for r in leaves),
            'manual_matched':len(leaf_pairs),'manual_missing':len(leaf_missing),'manual_extra':len(leaf_extra)},
        'repeated_amount_groups':dict(Counter(d['stage'] for d in duplicates)),
        'prior_row_report':{'claims':len(prior_checks),'verified':sum(x['independently_verified'] for x in prior_checks),'date_claims_verified':sum(x['date_claim_verified'] for x in prior_checks),
            'content_claims_verified':sum(x['content_claim_verified'] for x in prior_checks)},
        'prior_voucher_report':{'claims':len(prior_vouchers),'verified':sum(all(x[k] for k in ['numeric_claims_verified','balance_claim_verified','source_presence_claim_verified']) for x in prior_vouchers)}}
    period_values=list(periods)
    if len(period_values)==1 and len(period_values[0])==6 and period_values[0].isdigit():
        year,month=int(period_values[0][:4]),int(period_values[0][4:])
        end=f'{year:04d}-{month:02d}-{calendar.monthrange(year,month)[1]:02d}'
        summary['manual_to_final']['expected_month_end']=end
        summary['manual_to_final']['final_month_end_lines']=sum(payload(r,'final')['date']==end for r in final)
    write_json(out/'summary.json',summary)
    report = f'''# T03—T05 独立分析实际运行报告

本报告为历史分析，不是生产转换验收。未导入 V0.1 的解析或筛选函数。

| 阶段 | 有效行 | 凭证组 | 借方合计 | 贷方合计 | 平衡组 |
|---|---:|---:|---:|---:|---:|
'''
    for stage, values in stage_stats.items():
        report += f"| {stage} | {values['lines']} | {values['vouchers']} | {values['debit']} | {values['credit']} | {values['balanced_vouchers']} |\n"
    report += f'''
原始汇总包含层级重复，上表 raw 金额不得视为实际业务总额。

- 人工→原始严格签名匹配：{len(pairs)} 条；未匹配：{len(missing)} 条。
- 人工→成品21个非日期共同字段匹配：{len(target_pairs)} 条；日期变化：{sum(not x['date_equal'] for x in target_links)} 条。
- 金额单元格交叉核验：{len(amount_audit)} 个；异常：{sum(x['anomaly'] for x in amount_audit)} 个。
- 历史差异追溯：{sum(x['raw_row'] is not None for x in traced)}/{len(historical)} 条；{dict(Counter(x['secondary_tag'] for x in traced))}。
- 其中 {summary['historical_difference']['ambiguous_occurrences']} 条历史差异属于 {summary['historical_difference']['ambiguous_signature_groups']} 组等价签名，按原始顺序消费次数；CSV保留全部候选行，不能声称单条身份唯一。
- 父子科目按凭证、摘要和科目分组：{len(hierarchy_groups)} 组，借贷两侧合计相等 {sum(x['aggregate_equal'] for x in hierarchy_groups)} 组。
- 原始数据结构实验保留 {len(leaves)} 条，匹配人工 {len(leaf_pairs)} 条，漏 {len(leaf_missing)} 条，多 {len(leaf_extra)} 条；多出记录归属业务规则尚待确认。
- 旧逐行报告字段内容核验：{sum(x['content_claim_verified'] for x in prior_checks)}/{len(prior_checks)}；旧凭证报告核验：{summary['prior_voucher_report']['verified']}/{len(prior_vouchers)}。

## 明细文件

- inventory.json：全部工作表的实际行列盘点。
- raw_parsing_decisions.csv：原始表逐行解析分类，包含汇总及尾行。
- manual_to_raw.csv、manual_to_final.csv：多重集合一对一关联及原始行号。
- unmatched.json：未匹配明细（不得静默忽略）。
- amount_cells.csv：底层类型、原值、解码结果和解析状态。
- historical_144_trace.csv：旧差异每一条的原始行号、凭证单元格及 XML 证据。
- hierarchy_evidence.csv、hierarchy_group_evidence.csv：逐行及分组父子金额证据。
- raw_row_disposition.json：392条原始凭证科目行的历史关联及结构证据。
- repeated_amounts.csv：相同金额出现次数和不同业务归属。
- historical_mapping_evidence.json：历史映射候选，不代表已审核配置。
- structural_leaf_experiment.json：仅原始结构选择的诊断实验，不能据此决定排除特殊凭证。
- prior_report_checks.csv、prior_voucher_checks.csv：旧分析工作簿的逐项核验。
- summary.json：机器可读统计、定义及输入哈希。

完整业务解释见 docs/02_转换规则与差异分析.md。样本及历史文件在运行前后校验接管哈希，没有变更。
'''
    (out/'summary.md').write_text(report,encoding='utf-8')
    for item in baseline['files']:
        if hashlib.sha256((ROOT/item['path']).read_bytes()).hexdigest()!=item['sha256']:raise RuntimeError('分析后原件发生变更')
    print(json.dumps({k:v for k,v in summary.items() if k!='stages'},ensure_ascii=False,indent=2,default=str))


if __name__=='__main__':
    main()
