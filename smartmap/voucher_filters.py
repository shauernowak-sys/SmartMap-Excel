"""T09只按配置评价完整原始凭证；不按凭证白名单或分录行号筛选。"""
import hashlib
import json
from .applicability import applies_to


def evaluate_voucher(key,rows,rules,blocked_reasons=(),closing_recognition=None):
    evaluations=[]
    for rule in rules:
        params=rule['parameters'];status=rule['status']
        in_scope=applies_to(rule,period=key[2],source_sha256=key[0])
        if status=='rejected' or not in_scope:
            evaluations.append({'rule_id':rule['id'],'rule_status':status,'match':'not_evaluated' if in_scope else 'out_of_scope','matched_record_ids':[],
                'unmatched_record_ids':[],'parameters':params.copy()});continue
        matched=[];unmatched=[]
        for row in rows:
            yes=('summary_equals' not in params or row['summary']==params['summary_equals']) and (
                'voucher_category_equals' not in params or row['voucher']['category']==params['voucher_category_equals'])
            (matched if yes else unmatched).append(row['record_id'])
        structure_ok=('classification_equals' not in params or
                      (closing_recognition or {}).get('kind')==params['classification_equals'])
        if not structure_ok and not (set(params)&{'summary_equals','voucher_category_equals'}):
            matched=[];unmatched=[r['record_id'] for r in rows]
        evaluations.append({'rule_id':rule['id'],'rule_status':status,'match':'full' if matched and not unmatched and structure_ok else 'partial' if matched else 'none',
            'classification_verified':structure_ok,
            'matched_record_ids':matched,'unmatched_record_ids':unmatched,'parameters':params.copy()})
    approved=[e['rule_id'] for e in evaluations if e['rule_status']=='confirmed' and e['match']=='full']
    pending=[e['rule_id'] for e in evaluations if e['rule_status']=='candidate' and e['match']=='full']
    partial=[e['rule_id'] for e in evaluations if e['match']=='partial']
    reasons=list(blocked_reasons)
    if reasons:decision='needs_review'
    elif approved:decision='excluded';reasons=['CONFIRMED_VOUCHER_FILTER']
    elif partial:decision='needs_review';reasons=['FILTER_PARTIAL_VOUCHER_MATCH']
    elif pending:decision='needs_review';reasons=['VOUCHER_FILTER_UNCONFIRMED']
    else:decision='kept';reasons=['NO_CONFIRMED_FILTER_MATCH']
    evidence_id=hashlib.sha256(json.dumps([key,sorted(r['record_id'] for r in rows),evaluations],ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()
    return {'evidence_id':evidence_id,'context':{'input_sha256':key[0],'sheet':key[1],'period':key[2],'voucher_category':key[3],'voucher_number':key[4]},
        'member_record_ids':[r['record_id'] for r in rows],'member_excel_rows':[r['provenance']['excel_row'] for r in rows],
        'decision':decision,'reason_codes':sorted(set(reasons)), 'confirmed_full_rule_ids':approved,'candidate_full_rule_ids':pending,
        'partial_rule_ids':partial,'evaluations':evaluations}
