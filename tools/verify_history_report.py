"""校验独立分析产物的数量闭环、歧义披露与原件哈希。"""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from analyze_history import ROOT, write_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True,type=Path)
    out=parser.parse_args().report.resolve()
    s=json.loads((out/'summary.json').read_text(encoding='utf-8'))
    with (out/'historical_144_trace.csv').open(encoding='utf-8-sig') as f:
        trace=list(csv.DictReader(f))
    h=s['historical_difference'];leaf=s['structural_leaf_experiment'];mf=s['manual_to_final'];mr=s['raw_to_manual']
    checks={
        'manual_raw_conservation':mr['matched']+mr['manual_missing']==s['stages']['manual']['lines'],
        'raw_conservation':mr['matched']+mr['raw_unmatched']==s['stages']['raw']['lines'],
        'manual_final_all_matched':mf['matched_non_date_fields']==s['stages']['manual']['lines']==s['stages']['final']['lines'] and mf['manual_missing']==mf['final_extra']==0,
        'dates_conserved':mf['date_equal']+mf['date_changed']==s['stages']['final']['lines'],
        'final_month_end':mf['final_month_end_lines']==s['stages']['final']['lines'],
        'no_amount_decoding_anomalies':s['amount_audit']['anomalies']==0,
        'shared_string_10_is_blank':s['amount_audit']['shared_index_10_all_blank'],
        'all_historical_differences_traced':h['reported']==h['traced']==h['reconstructed_extra_count'] and h['unconsumed_extra_rows']==0,
        'trace_consumed_rows_unique':len({t['raw_row'] for t in trace})==len(trace),
        'trace_actual_vouchers_are_empty':all(t['actual_voucher']=='' and t['voucher_xml_decoded']=='' and t['voucher_xml_type']=='s' for t in trace),
        'trace_ambiguities_disclosed':all(
            int(t['raw_row']) in json.loads(t['all_same_signature_raw_rows']) and
            (len(json.loads(t['all_same_signature_raw_rows']))==1 or '不唯一' in t['association_status']) for t in trace),
        'ambiguous_occurrence_count_correct':sum(len(json.loads(t['all_same_signature_raw_rows']))>1 for t in trace)==h['ambiguous_occurrences'],
        'hierarchy_group_totals_equal':s['hierarchy']['groups']==s['hierarchy']['aggregate_equal_groups'],
        'leaf_comparison_conserves_occurrences':leaf['lines']==leaf['manual_matched']+leaf['manual_extra'] and leaf['manual_matched']+leaf['manual_missing']==s['stages']['manual']['lines'],
        'leaf_vouchers_balanced':leaf['vouchers']==leaf['balanced_vouchers'],
        'manual_and_final_balanced':all(s['stages'][stage]['balanced_vouchers']==s['stages'][stage]['vouchers'] for stage in ['manual','final'])}
    records=json.loads((out/'raw_records.json').read_text(encoding='utf-8'))
    nonbusiness=sum(not x['code'] and x['voucher'] is None for x in records)
    checks['raw_parsing_rows_accounted']=s['stages']['raw']['lines']+s['raw_nonvoucher_subject_rows']+nonbusiness==len(records)
    baseline=json.loads((ROOT/'reports/environment/baseline.json').read_text(encoding='utf-8'))
    checks['all_original_files_unchanged']=all(hashlib.sha256((ROOT/x['path']).read_bytes()).hexdigest()==x['sha256'] for x in baseline['files'])
    result={'report_checks':checks,'all_passed':all(checks.values()),'unchanged_original_files':len(baseline['files'])}
    write_json(out/'final_checks.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['all_passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
