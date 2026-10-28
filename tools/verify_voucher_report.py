"""T10事后独立核验：精确合计、连续序号、月末及原值保留。"""
import argparse
import calendar
import hashlib
import json
from collections import Counter
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True,type=Path)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--reference-dir',type=Path,help='可选：独立历史分析目录，仅用于事后对照')
    args=parser.parse_args();out=args.report.resolve()
    result=json.loads((out/'voucher_report.json').read_text(encoding='utf-8'))
    mapped=json.loads((out/'mapping_filter_report.json').read_text(encoding='utf-8'))
    hierarchy=json.loads((out/'hierarchy_report.json').read_text(encoding='utf-8'))
    config=json.loads(args.config.read_text(encoding='utf-8'))
    rows=result['rows'];by_id={r['record_id']:r for r in rows};old={r['record_id']:r for r in mapped['rows']}
    period=result['period'];expected_end=date(int(period[:4]),int(period[4:]),calendar.monthrange(int(period[:4]),int(period[4:]))[1]).isoformat()
    states=Counter(r['voucher_validation']['decision'] for r in rows)
    date_rule=next(r for r in config['rules'] if r['kind']=='date')
    checks={'analysis_completed':result['analysis_ok'],
        'all_original_occurrences_preserved':Counter(r['record_id'] for r in rows)==Counter(r['record_id'] for r in mapped['rows']),
        'original_fields_unchanged':all({k:v for k,v in r.items() if k not in ['voucher_validation','diagnostic_assignment']}==old[r['record_id']] for r in rows),
        'row_states_conserve_counts':len(rows)==result['row_counts']['total'] and all(states[s]==result['row_counts'][s] for s in ['retained','excluded','needs_review','non_business']),
        'dynamic_month_end_correct':result['month_end']==expected_end,
        'date_rule_is_confirmed':date_rule['status']=='confirmed' and result['date_rule']['id']==date_rule['id'],
        'config_snapshot_matches':result['config_sha256']==hashlib.sha256(args.config.read_bytes()).hexdigest(),
        'posting_dates_follow_known_period':all(r['voucher_validation']['posting_date']==(expected_end if r['parse_state']=='parsed' and r['mapping_filter']['decision'] not in ['excluded','non_business'] else None) for r in rows),
        'no_system_import_claim':not result['ready_for_system_import'] and not result['conversion_implemented']}
    sums_ok=True;sequences_ok=True;scope_ok=True;official_gate=True;stats_ok=True;group_ids=[]
    assignments={'official':{},'diagnostic':{}};official_members=[];diagnostic_selection=[]
    for view in ['official','diagnostic']:
        groups=result[view+'_vouchers']
        for group in groups:
            group_ids.append(group['evidence_id']);members=[by_id[i] for i in group['source_record_ids']]
            selected=[by_id[i] for i in group['selected_record_ids']];ctx=group['context']
            scope_ok &= all(r['period']==ctx['period'] and r['voucher']['category']==ctx['voucher_category'] and r['voucher']['number']==ctx['voucher_number'] for r in members)
            scope_ok &= group['source_excel_rows']==[r['provenance']['excel_row'] for r in members] and group['selected_excel_rows']==[r['provenance']['excel_row'] for r in selected]
            scope_ok &= group['view']==view and group['diagnostic_only']==(view=='diagnostic')
            if view=='official':
                official_members.extend(group['source_record_ids'])
                expected_ids=[r['record_id'] for r in members if r['mapping_filter']['decision']!='excluded']
                scope_ok &= Counter(expected_ids)==Counter(group['selected_record_ids'])
                if group['totals'] is not None:official_gate &= all(r['mapping_filter']['decision']=='retained' for r in selected)
            else:diagnostic_selection.extend(group['selected_record_ids'])
            if group['totals'] is not None:
                with localcontext() as context:
                    context.prec=max(64,sum(len(r[s]['value'] or '') for r in selected for s in ['debit','credit'])+2)
                    d=sum((Decimal(r['debit']['value']) if r['debit']['value'] is not None else Decimal(0) for r in selected),Decimal(0))
                    c=sum((Decimal(r['credit']['value']) if r['credit']['value'] is not None else Decimal(0) for r in selected),Decimal(0))
                    sums_ok &= d==Decimal(group['totals']['debit']) and c==Decimal(group['totals']['credit']) and d-c==Decimal(group['difference'])
                    sums_ok &= group['arithmetic_equal']==(d==c) and group['balanced']==(d==c)
            else:sums_ok &= group['difference'] is None and group['arithmetic_equal'] is None and group['balanced'] is None
            ordered=sorted(selected,key=lambda r:(r['provenance']['excel_row'],r['record_id']))
            expected=[{'record_id':r['record_id'],'entry_sequence':i,'posting_date':expected_end} for i,r in enumerate(ordered,1)] if group['status']=='balanced' else []
            sequences_ok &= group['assignments']==expected
            for a in group['assignments']:
                if a['record_id'] in assignments[view]:sequences_ok=False
                assignments[view][a['record_id']]=dict(a,evidence_id=group['evidence_id'])
        stats=result[view+'_summary'];counts=Counter(g['status'] for g in groups)
        numerator=sum(g['arithmetic_equal'] is True for g in groups);denominator=sum(g['arithmetic_equal'] is not None for g in groups)
        stats_ok &= stats['voucher_groups']==len(groups) and all(counts[s]==stats[s] for s in ['balanced','unbalanced','needs_review','excluded'])
        stats_ok &= (stats['balance_rate_numerator'],stats['balance_rate_denominator'])==(numerator,denominator)
        if not denominator:stats_ok &= stats['balance_rate'] is None
    business_known=[r['record_id'] for r in rows if r['mapping_filter']['decision']!='non_business' and r['voucher'] is not None]
    expected_projection=[i for i in hierarchy['diagnostic_projection']['remaining_record_ids'] if by_id[i]['voucher_filter']['decision']!='excluded']
    checks.update({'voucher_scope_and_source_rows_match':bool(scope_ok),'voucher_evidence_ids_unique':len(group_ids)==len(set(group_ids)),
        'official_groups_cover_known_business_rows':Counter(official_members)==Counter(business_known),
        'diagnostic_selection_matches_upstream_projection':Counter(diagnostic_selection)==Counter(expected_projection),
        'official_math_only_uses_approved_complete_rows':bool(official_gate),'amounts_independently_recalculated':bool(sums_ok),
        'sequences_continuous_in_physical_order':bool(sequences_ok),'balance_rates_have_explicit_correct_denominators':bool(stats_ok),
        'assignment_counts_match':len(assignments['official'])==result['official_assigned_rows'] and len(assignments['diagnostic'])==result['diagnostic_assigned_rows']})
    per_row_ok=True;no_promotion=True
    for r in rows:
        identity=r['record_id'];a=assignments['official'].get(identity);diag=assignments['diagnostic'].get(identity)
        per_row_ok &= r['voucher_validation']['entry_sequence']==(a['entry_sequence'] if a else None)
        per_row_ok &= r['diagnostic_assignment']==(dict(diag,diagnostic_only=True) if diag else None)
        if r['voucher_validation']['decision']=='retained':no_promotion &= r['mapping_filter']['decision']=='retained' and a is not None
        if r['mapping_filter']['decision']=='needs_review':no_promotion &= r['voucher_validation']['decision']=='needs_review' and a is None
        if r['mapping_filter']['decision'] in ['excluded','non_business']:no_promotion &= r['voucher_validation']['decision']==r['mapping_filter']['decision'] and a is None
    checks['row_assignments_match_voucher_evidence']=bool(per_row_ok)
    checks['pending_rows_never_promoted_by_diagnostic_balance']=bool(no_promotion)
    reference_summary=None
    if args.reference_dir:
        reference=json.loads((args.reference_dir/'summary.json').read_text(encoding='utf-8'))
        diag=result['diagnostic_vouchers'];categories={g['context']['voucher_category'] for g in diag}
        if len(categories)!=1:raise ValueError('该历史参考缺少类别，不能用于多类别对照')
        by_number={g['context']['voucher_number']:g for g in diag}
        verified={}
        for stage in ['manual','final']:
            expected=reference['stages'][stage]['balance'];matched=0
            for g in expected:
                found=by_number.get(g['voucher'])
                if found and found['totals'] and Decimal(found['totals']['debit'])==Decimal(g['debit']) and Decimal(found['totals']['credit'])==Decimal(g['credit']) and len(found['selected_record_ids'])==g['lines']:matched+=1
            verified[stage]={'reference_vouchers':len(expected),'matched':matched}
            checks[stage+'_historical_voucher_totals_match']=matched==len(expected)
        reference_summary={'diagnostic_only':True,'stages':verified,
            'additional_diagnostic_vouchers':[v for v in sorted(by_number) if v not in {g['voucher'] for g in reference['stages']['manual']['balance']}]}
    baseline=json.loads((ROOT/'reports/environment/baseline.json').read_text(encoding='utf-8'))
    checks['ten_original_files_unchanged']=all(hashlib.sha256((ROOT/x['path']).read_bytes()).hexdigest()==x['sha256'] for x in baseline['files'])
    final={'checks':checks,'all_passed':all(checks.values()),'reference_comparison':reference_summary,'original_files_verified':len(baseline['files'])}
    with (out/'final_checks.json').open('x',encoding='utf-8') as stream:json.dump(final,stream,ensure_ascii=False,indent=2)
    print(json.dumps(final,ensure_ascii=False,indent=2));return 0 if final['all_passed'] else 1


if __name__=='__main__':raise SystemExit(main())
