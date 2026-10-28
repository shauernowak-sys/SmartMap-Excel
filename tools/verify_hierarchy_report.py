"""T08事后自检及可选历史对照；不参与正式父子选择。"""
import argparse
import csv
import hashlib
import json
from collections import Counter
from decimal import Decimal, localcontext
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True,type=Path)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--reference-dir',type=Path,help='可选：T03—T05独立盘点目录，只用于事后评估')
    args=parser.parse_args();out=args.report.resolve()
    result=json.loads((out/'hierarchy_report.json').read_text(encoding='utf-8'))
    parsed=json.loads((out/'parse_report.json').read_text(encoding='utf-8'))
    config=json.loads(args.config.read_text(encoding='utf-8'))
    rule=next(r for r in config['rules'] if r['kind']=='hierarchy')
    original={r['record_id']:r for r in parsed['rows']};rows=result['rows'];by_id={r['record_id']:r for r in rows}
    groups={g['evidence_id']:g for g in result['groups']}
    states=Counter(r['hierarchy']['decision'] for r in rows)
    duplicates=result['diagnostic_projection']['duplicate_parent_record_ids'];projection=result['diagnostic_projection']['remaining_record_ids']
    actual_business=[r['record_id'] for r in parsed['rows'] if r['parse_state']=='parsed']
    checks={'analysis_completed':result['analysis_ok'],
        'all_original_rows_preserved':Counter(r['record_id'] for r in rows)==Counter(r['record_id'] for r in parsed['rows']),
        'original_fields_unchanged':all({k:v for k,v in r.items() if k!='hierarchy'}==original[r['record_id']] for r in rows),
        'row_decisions_conserve_counts':len(rows)==result['row_counts']['total']==sum(states.values()) and all(states[s]==result['row_counts'][s] for s in ['retained','excluded','needs_review','non_business']),
        'group_ids_unique':len(groups)==len(result['groups']),
        'projection_conserves_occurrences':Counter(duplicates+projection)==Counter(actual_business),
        'projection_counts_match':len(duplicates)==result['diagnostic_projection']['duplicate_parent_rows'] and len(projection)==result['diagnostic_projection']['remaining_rows'],
        'config_sha_matches':hashlib.sha256(args.config.read_bytes()).hexdigest()==result['config_sha256'],
        'rule_status_matches_config':result['hierarchy_rule']['status']==rule['status'],
        'unconfirmed_rules_do_not_exclude':rule['status']=='confirmed' or states['excluded']==0,
        'out_of_scope_rules_do_not_exclude':('applicability' not in rule or
            rule['applicability']=={'period':parsed['period'],'source_sha256':parsed['input_sha256']}) or states['excluded']==0,
        'every_exclusion_has_verified_parent_evidence':all(r['hierarchy']['parent_evidence_ids'] and all(groups[g]['structure_verified'] and r['record_id'] in groups[g]['parent_record_ids'] for g in r['hierarchy']['parent_evidence_ids']) for r in rows if r['hierarchy']['decision']=='excluded'),
        'no_system_import_claim':not result['ready_for_system_import'] and not result['conversion_implemented']}
    amounts_ok=True;scope_ok=True;nearest_ok=True;provenance_ok=True
    for g in groups.values():
        parent=[original[i] for i in g['parent_record_ids']];children=[original[i] for i in g['child_record_ids']]
        entries=parent+children;context=g['context']
        scope_ok &= all(r['period']==context['period'] and r['voucher']['category']==context['voucher_category'] and r['voucher']['number']==context['voucher_number'] and r['summary']==context['summary'] for r in entries)
        provenance_ok &= g['parent_excel_rows']==[r['provenance']['excel_row'] for r in parent] and g['child_excel_rows']==[r['provenance']['excel_row'] for r in children]
        codes={r['subject']['code'] for r in parsed['rows'] if r['parse_state']=='parsed' and r['period']==context['period'] and r['voucher']['category']==context['voucher_category'] and r['voucher']['number']==context['voucher_number'] and r['summary']==context['summary']}
        p=g['parent_code']
        nearest_ok &= all(r['subject']['code']==p for r in parent) and all(c.startswith(p+'.') and not any(x!=p and x.startswith(p+'.') and c.startswith(x+'.') for x in codes) for c in g['nearest_child_codes'])
        with localcontext() as decimal_context:
            decimal_context.prec=max(64,sum(len(r[s]['value'] or '') for r in entries for s in ['debit','credit'])+2)
            for side in ['debit','credit']:
                a=sum((Decimal(r[side]['value']) if r[side]['value'] is not None else Decimal(0) for r in parent),Decimal(0))
                b=sum((Decimal(r[side]['value']) if r[side]['value'] is not None else Decimal(0) for r in children),Decimal(0))
                amounts_ok &= a==Decimal(g['parent_totals'][side]) and b==Decimal(g['child_totals'][side]) and a-b==Decimal(g['differences'][side])
    checks.update({'group_scope_consistent':bool(scope_ok),'nearest_children_only':bool(nearest_ok),
        'evidence_row_numbers_consistent':bool(provenance_ok),'amounts_independently_recalculated':bool(amounts_ok)})
    reference_summary=None
    if args.reference_dir:
        with (args.reference_dir/'hierarchy_group_evidence.csv').open(encoding='utf-8-sig') as stream:reference=list(csv.DictReader(stream))
        def signature(v,code,summary,parents,children,pd,pc,cd,cc):
            return (int(v),code,summary,tuple(sorted(parents)),tuple(sorted(children)),Decimal(pd),Decimal(pc),Decimal(cd),Decimal(cc))
        old=Counter(signature(g['voucher'],g['code'],g['summary'],json.loads(g['parent_rows']),json.loads(g['child_rows']),g['parent_debit'],g['parent_credit'],g['child_debit'],g['child_credit']) for g in reference)
        new=Counter(signature(g['context']['voucher_number'],g['parent_code'],g['context']['summary'],g['parent_excel_rows'],g['child_excel_rows'],g['parent_totals']['debit'],g['parent_totals']['credit'],g['child_totals']['debit'],g['child_totals']['credit']) for g in groups.values())
        checks['independent_group_reference_matches']=old==new
        leaves=json.loads((args.reference_dir/'structural_leaf_experiment.json').read_text(encoding='utf-8'))
        checks['independent_projection_reference_matches']=Counter(original[i]['provenance']['excel_row'] for i in projection)==Counter(leaves['rows'])
        manual=json.loads((args.reference_dir/'manual_records.json').read_text(encoding='utf-8'))
        def number(v):return None if v is None else Decimal(v)
        expected=Counter((r['voucher'],r['code'],r['summary'],r['debit_status'],number(r['debit']),r['credit_status'],number(r['credit'])) for r in manual)
        actual=Counter((by_id[i]['voucher']['number'],by_id[i]['subject']['code'],by_id[i]['summary'],by_id[i]['debit']['status'],number(by_id[i]['debit']['value']),by_id[i]['credit']['status'],number(by_id[i]['credit']['value'])) for i in projection)
        extra=actual-expected
        reference_summary={'diagnostic_only':True,'group_reference_matched':sum((old&new).values()),'projection_manual_matched':sum((actual&expected).values()),
            'projection_manual_missing':sum((expected-actual).values()),'projection_manual_extra':sum(extra.values()),
            'extra_vouchers':dict(Counter({str(v):sum(count for sig,count in extra.items() if sig[0]==v) for v in {sig[0] for sig in extra}}))}
    baseline=json.loads((ROOT/'reports/environment/baseline.json').read_text(encoding='utf-8'))
    checks['original_files_unchanged']=all(hashlib.sha256((ROOT/x['path']).read_bytes()).hexdigest()==x['sha256'] for x in baseline['files'])
    final={'checks':checks,'all_passed':all(checks.values()),'reference_comparison':reference_summary,'original_files_verified':len(baseline['files'])}
    with (out/'final_checks.json').open('x',encoding='utf-8') as stream:json.dump(final,stream,ensure_ascii=False,indent=2)
    print(json.dumps(final,ensure_ascii=False,indent=2))
    return 0 if final['all_passed'] else 1


if __name__=='__main__':raise SystemExit(main())
