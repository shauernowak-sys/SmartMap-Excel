"""SmartMap Excel: read-only June 2026 conversion-rule verifier.
Run: python verify.py --source ../202606_明细账26.7.241.xlsx --template ../凭证导入模板2026.6月.xlsx --out ./results
Never modifies input files; does not create import-ready vouchers.
"""
import argparse, csv, json, re
from collections import defaultdict, Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from artifact_tool import Blob, SpreadsheetFile

def norm(v): return re.sub(r'\s+', '', str(v or '')).strip()
def subject(v):
    m=re.match(r'^\s*(\d+(?:\.\d+)*)',str(v or ''))
    return m.group(1) if m else ''
def voucher(v):
    m=re.search(r'(\d+)',str(v or ''))
    return int(m.group(1)) if m else None
def amount(v):
    # This export uses string '10' as a suspected placeholder. Never silently
    # interpret it as zero: the ambiguity is explicitly tracked in the report.
    if v is None or str(v).strip()=='': return None
    try: return Decimal(str(v).replace(',','').strip())
    except InvalidOperation: return None
def write_csv(path, headers, rows):
    with open(path,'w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(headers);w.writerows(rows)
def read_rows(path, sheet, area):
    wb=SpreadsheetFile.import_xlsx(Blob.load(str(path)))
    return wb.worksheets.get_item_at(sheet).get_range(area).values

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--source',required=True);ap.add_argument('--template',required=True);ap.add_argument('--out',required=True);args=ap.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    raw=read_rows(args.source,0,'A1:I750')
    manual=read_rows(args.source,1,'A1:Y210')
    template=read_rows(args.template,0,'A1:X210')
    records=[]
    for rowno,r in enumerate(raw[2:],3):
        v=voucher(r[3]);s=subject(r[2]);d,c=amount(r[5]),amount(r[6])
        if v is None or not s:continue
        records.append(dict(row=rowno,v=v,sub=s,subtext=str(r[2] or ''),summary=norm(r[4]),debit=d,credit=c))
    expected=[]
    for rowno,r in enumerate(manual[2:],3):
        v=voucher(r[4]);s=subject(r[3]);d,c=amount(r[9]),amount(r[10])
        if v is None or not s:continue
        expected.append(dict(row=rowno,v=v,sub=s,subtext=str(r[3] or ''),summary=norm(r[6]),debit=d,credit=c,target=str(r[7] or ''),name=str(r[8] or ''),aux=tuple(r[14:20])))
    # Historical known-true selection: infer which external subject codes appear
    # in manually curated data. This is an ORACLE benchmark, not an independent
    # production selector. Exact voucher + source subject + non-placeholder side
    # amounts are used to audit source-row traceability.
    selected_subjects={x['sub'] for x in expected}
    selected_vouchers={x['v'] for x in expected}
    candidates=[x for x in records if x['v'] in selected_vouchers and x['sub'] in selected_subjects]
    def same(a,b):
        if a['v']!=b['v'] or a['sub']!=b['sub']:return False
        for side in ('debit','credit'):
            ev=b[side]
            if ev is not None and ev!=10 and a[side]!=ev:return False
        return True
    used=set();audit=[];counts=Counter();mapped=defaultdict(set)
    for e in expected:
        matches=[r for r in records if same(r,e)]
        free=[r for r in matches if r['row'] not in used]
        status='unique' if len(free)==1 else ('ambiguous' if len(free)>1 else 'missing')
        chosen=free[0] if len(free)==1 else None
        if chosen:used.add(chosen['row'])
        counts[status]+=1
        mapped[e['sub']].add((e['target'],e['name']))
        audit.append([e['row'],e['v'],e['sub'],e['subtext'],e['target'],e['name'],str(e['debit']),str(e['credit']),status,chosen['row'] if chosen else '',','.join(str(x['row']) for x in matches), '是' if (e['debit']==10 or e['credit']==10) else '否'])
    # Independent candidate benchmark: compare source-derived set against the
    # curated rows by voucher + subject + meaningful side amount.
    def signature(x):
        return (x['v'],x['sub'],x['debit'] if x['debit']!=10 else None,x['credit'] if x['credit']!=10 else None)
    ref=Counter(map(signature,expected));pred=Counter(map(signature,candidates))
    missing=list((ref-pred).elements());extra=list((pred-ref).elements())
    write_csv(out/'逐行来源追踪.csv',['整理表行号','凭证号','外部科目编码','外部科目原文','内部科目编码','内部科目名称','借方原值','贷方原值','匹配状态','原始表行号','所有匹配原始行号','涉及10占位歧义'],audit)
    write_csv(out/'科目映射候选.csv',['外部科目编码','目标科目编码','目标科目名称','状态'],[[s,t,n,'冲突，必须人工确认' if len(values)>1 else '历史映射待审核'] for s,values in sorted(mapped.items()) for t,n in sorted(values)])
    write_csv(out/'候选差异.csv',['类型','凭证号','外部科目','借方','贷方'],[['缺失',*map(str,x)] for x in missing]+[['多余',*map(str,x)] for x in extra])
    # Validate the real final import sheet's voucher balance, without altering it.
    vouchers=defaultdict(lambda:[Decimal(0),Decimal(0),0])
    for r in template[1:]:
        v=voucher(r[3]);
        if v is None:continue
        for idx,col in enumerate((8,9)):
            a=amount(r[col]);vouchers[v][idx]+=a or 0
        vouchers[v][2]+=1
    balance=[[v,str(vals[0]),str(vals[1]),str(vals[0]-vals[1]),vals[2],'平衡' if vals[0]==vals[1] else '不平衡'] for v,vals in sorted(vouchers.items())]
    write_csv(out/'最终凭证平衡.csv',['凭证号','借方合计','贷方合计','差额','分录数','校验结果'],balance)
    # End-of-month rule, confirmed by the user.
    month='202606';year,mon=int(month[:4]),int(month[4:]);nextmonth=date(year+1,1,1) if mon==12 else date(year,mon+1,1)
    month_end=(nextmonth-timedelta(days=1)).isoformat()
    summary={'raw_voucher_subject_rows':len(records),'manual_lines':len(expected),'source_trace':dict(counts),'candidate_lines_using_historical_subject_dictionary':len(candidates),'candidate_missing':len(missing),'candidate_extra':len(extra),'subject_codes_in_history':len(selected_subjects),'subject_mapping_conflicts':sum(len(x)>1 for x in mapped.values()),'final_voucher_count':len(vouchers),'final_unbalanced_count':sum(x[-1]=='不平衡' for x in balance),'month_end':month_end,'warnings':['历史科目集合来自人工结果，当前候选筛选不是独立的生产算法','原始金额字符串10可能是占位值，也可能是真实金额，尚未证明；禁止自动置空','未证明未来月份上级科目剔除及结转损益排除规则','辅助核算尚未验证，禁止直接导入系统']}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
