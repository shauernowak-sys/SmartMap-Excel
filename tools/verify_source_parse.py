"""T07解析产物自检；可选独立历史盘点仅用于事后核验，不被读取器导入。"""
import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True,type=Path,help='parse-source产生的新报告目录')
    parser.add_argument('--reference',type=Path,help='可选：独立分析的raw_records.json，只用于事后比较')
    args=parser.parse_args();out=args.report.resolve()
    report=json.loads((out/'parse_report.json').read_text(encoding='utf-8'))
    rows=report['rows'];counts=Counter(r['parse_state'] for r in rows)
    numbers=[r['provenance']['excel_row'] for r in rows]
    checks={'read_completed':report['read_ok'],
        'row_count_conservation':sum(counts.values())==report['row_counts']['total']==len(rows),
        'row_states_match_summary':all(counts[state]==report['row_counts'][state] for state in ['parsed','needs_review','non_business']),
        'unique_occurrence_ids':len({r['record_id'] for r in rows})==len(rows),
        'continuous_original_rows':numbers==list(range(report['header']['header_rows'][-1]+1,report['header']['header_rows'][-1]+1+len(rows))),
        'source_provenance_consistent':all(r['provenance']['sheet']==report['source_sheet'] and r['provenance']['input_sha256']==report['input_sha256'] for r in rows),
        'valid_rows_have_no_issues':all(not r['issues'] for r in rows if r['parse_state']=='parsed'),
        'review_rows_have_reasons':all(r['issues'] for r in rows if r['parse_state']=='needs_review'),
        'nonbusiness_rows_have_reasons':all(r.get('reason_code') for r in rows if r['parse_state']=='non_business'),
        'business_amounts_finite':all(r[side]['status']!='invalid' and (r[side]['value'] is None or Decimal(r[side]['value']).is_finite()) for r in rows if r['parse_state']=='parsed' for side in ['debit','credit']),
        'no_conversion_claim':not report['conversion_implemented'] and not report['ready_for_system_import']}
    reference_comparison=None
    if args.reference:
        reference=json.loads(args.reference.read_text(encoding='utf-8'))
        reference=[r for r in reference if r['voucher'] is not None and r['code']]
        def decimal(v):return None if v is None else Decimal(v)
        expected=Counter((r['row'],r['voucher'],r['code'],r['summary'],r['debit_status'],decimal(r['debit']),r['credit_status'],decimal(r['credit'])) for r in reference)
        actual=Counter((r['provenance']['excel_row'],r['voucher']['number'],r['subject']['code'],r['summary'],r['debit']['status'],decimal(r['debit']['value']),r['credit']['status'],decimal(r['credit']['value'])) for r in rows if r['parse_state']=='parsed')
        reference_comparison={'reference_file':str(args.reference),'matched':sum((expected&actual).values()),
            'missing':sum((expected-actual).values()),'extra':sum((actual-expected).values())}
        checks['independent_raw_reference_matches']=expected==actual
    baseline=json.loads((ROOT/'reports/environment/baseline.json').read_text(encoding='utf-8'))
    checks['original_files_unchanged']=all(hashlib.sha256((ROOT/x['path']).read_bytes()).hexdigest()==x['sha256'] for x in baseline['files'])
    result={'checks':checks,'all_passed':all(checks.values()),'reference_comparison':reference_comparison,
        'original_files_verified':len(baseline['files'])}
    with (out/'final_checks.json').open('x',encoding='utf-8') as handle:json.dump(result,handle,ensure_ascii=False,indent=2)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['all_passed'] else 1


if __name__=='__main__':raise SystemExit(main())
