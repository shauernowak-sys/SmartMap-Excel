"""T09事后核验：原值、确认状态、整凭证范围及映射依据。"""
import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True,type=Path)
    parser.add_argument('--config',required=True,type=Path)
    args=parser.parse_args();out=args.report.resolve()
    result=json.loads((out/'mapping_filter_report.json').read_text(encoding='utf-8'))
    hierarchy=json.loads((out/'hierarchy_report.json').read_text(encoding='utf-8'))
    parsed=json.loads((out/'parse_report.json').read_text(encoding='utf-8'))
    config=json.loads(args.config.read_text(encoding='utf-8'))
    rows=result['rows'];original={r['record_id']:r for r in hierarchy['rows']};by_id={r['record_id']:r for r in rows}
    approved={m['source_code']:m for m in config['mappings'] if m['status']=='confirmed'}
    candidates=defaultdict(list);rejected=defaultdict(list)
    for m in config['mappings']:
        if m['status']=='candidate':candidates[m['source_code']].append(m)
        elif m['status']=='rejected':rejected[m['source_code']].append(m)
    rules={r['id']:r for r in config['rules'] if r['kind']=='voucher_filter'}
    groups={g['evidence_id']:g for g in result['filter_groups']}
    counts=Counter(r['mapping_filter']['decision'] for r in rows);mapping_counts=Counter(r['account_mapping']['status'] for r in rows)
    checks={'analysis_completed':result['analysis_ok'],
        'original_rows_preserved':Counter(r['record_id'] for r in rows)==Counter(r['record_id'] for r in hierarchy['rows'])==Counter(r['record_id'] for r in parsed['rows']),
        'upstream_fields_unchanged':all({k:v for k,v in r.items() if k not in ['account_mapping','voucher_filter','mapping_filter']}==original[r['record_id']] for r in rows),
        'row_states_conserve_counts':len(rows)==result['row_counts']['total'] and all(counts[s]==result['row_counts'][s] for s in ['retained','excluded','needs_review','non_business']),
        'mapping_states_conserve_counts':sum(mapping_counts.values())==len(rows) and all(mapping_counts[s]==v for s,v in result['mapping_counts'].items()),
        'config_snapshot_matches':hashlib.sha256(args.config.read_bytes()).hexdigest()==result['config_sha256'],
        'no_import_claim':not result['ready_for_system_import'] and not result['conversion_implemented'],
        'unapproved_targets_are_blank':all(r['account_mapping']['target_code'] is None and r['account_mapping']['target_name'] is None and r['account_mapping']['mapping_id'] is None for r in rows if r['account_mapping']['status']!='matched')}
    matches_ok=True;statuses_ok=True
    for r in rows:
        m=r['account_mapping'];status=m['status']
        if status=='not_evaluated':continue
        code=r['subject']['code'];known=approved.get(code)
        if known:
            scope=known.get('applicability')
            expected='matched' if scope is None or scope=={'period':r['period'],'source_sha256':r['provenance']['input_sha256']} else 'out_of_scope'
        elif candidates[code]:expected='candidate_conflict' if len({(x['target_code'],x['target_name']) for x in candidates[code]})>1 else 'unconfirmed'
        elif rejected[code]:expected='rejected'
        else:expected='unknown'
        statuses_ok &= status==expected and m['source_code']==code
        if status=='matched':
            matches_ok &= known is not None and (m['mapping_id'],m['target_code'],m['target_name'])==(known['id'],known['target_code'],known['target_name'])
    checks['mapping_statuses_independently_resolved']=bool(statuses_ok)
    checks['matched_targets_have_confirmed_exact_mapping']=bool(matches_ok)
    membership_ok=True;predicates_ok=True;excluded_ok=True;whole_ok=True
    grouped_ids=[]
    for g in groups.values():
        members=[by_id[i] for i in g['member_record_ids']];grouped_ids.extend(g['member_record_ids'])
        ctx=g['context']
        membership_ok &= g['member_excel_rows']==[r['provenance']['excel_row'] for r in members]
        membership_ok &= all(r['period']==ctx['period'] and r['voucher']['category']==ctx['voucher_category'] and r['voucher']['number']==ctx['voucher_number'] and r['provenance']['input_sha256']==ctx['input_sha256'] and r['provenance']['sheet']==ctx['sheet'] for r in members)
        valid_full=[]
        for e in g['evaluations']:
            rule=rules.get(e['rule_id'])
            if rule is None:predicates_ok=False;continue
            predicates_ok &= rule['status']==e['rule_status'] and rule['parameters']==e['parameters']
            if rule['status']=='rejected':
                predicates_ok &= e['match']=='not_evaluated';continue
            params=rule['parameters'];matched=[];unmatched=[]
            for r in members:
                yes=('summary_equals' not in params or r['summary']==params['summary_equals']) and ('voucher_category_equals' not in params or r['voucher']['category']==params['voucher_category_equals'])
                (matched if yes else unmatched).append(r['record_id'])
            expected='full' if matched and not unmatched else 'partial' if matched else 'none'
            predicates_ok &= expected==e['match'] and Counter(matched)==Counter(e['matched_record_ids']) and Counter(unmatched)==Counter(e['unmatched_record_ids'])
            if rule['status']=='confirmed' and expected=='full':valid_full.append(rule['id'])
        predicates_ok &= Counter(valid_full)==Counter(g['confirmed_full_rule_ids'])
        if g['decision']=='excluded':
            excluded_ok &= bool(valid_full) and all(r['parse_state']=='parsed' for r in members)
            whole_ok &= all(r['mapping_filter']['decision']=='excluded' and r['voucher_filter']['evidence_id']==g['evidence_id'] for r in members)
        kept=[r for r in members if r['mapping_filter']['decision']=='retained']
        if kept:whole_ok &= all(r['mapping_filter']['decision'] in ['retained','excluded'] for r in members)
    expected_ids=[r['record_id'] for r in rows if r['parse_state']!='non_business' and r['voucher'] is not None]
    checks.update({'voucher_membership_conserves_occurrences':Counter(grouped_ids)==Counter(expected_ids),
        'voucher_evidence_scope_matches':bool(membership_ok),'rule_predicates_independently_verified':bool(predicates_ok),
        'filter_exclusions_have_confirmed_full_rule':bool(excluded_ok),'whole_voucher_decisions':bool(whole_ok),
        'every_retained_row_has_confirmed_mapping':all(r['account_mapping']['status']=='matched' for r in rows if r['mapping_filter']['decision']=='retained'),
        'every_exclusion_has_upstream_or_filter_basis':all(r['hierarchy']['decision']=='excluded' or (r['voucher_filter']['evidence_id'] in groups and groups[r['voucher_filter']['evidence_id']]['decision']=='excluded') for r in rows if r['mapping_filter']['decision']=='excluded')})
    baseline=json.loads((ROOT/'reports/environment/baseline.json').read_text(encoding='utf-8'))
    checks['ten_original_files_unchanged']=all(hashlib.sha256((ROOT/x['path']).read_bytes()).hexdigest()==x['sha256'] for x in baseline['files'])
    unknown=[r for r in rows if r['account_mapping']['status']=='unknown']
    final={'checks':checks,'all_passed':all(checks.values()),'original_files_verified':len(baseline['files']),
        'unknown_account_evidence':[{'code':r['subject']['code'],'sheet':r['provenance']['sheet'],'excel_row':r['provenance']['excel_row'],'voucher_number':r['voucher']['number']} for r in unknown]}
    with (out/'final_checks.json').open('x',encoding='utf-8') as handle:json.dump(final,handle,ensure_ascii=False,indent=2)
    print(json.dumps(final,ensure_ascii=False,indent=2))
    return 0 if final['all_passed'] else 1


if __name__=='__main__':raise SystemExit(main())
