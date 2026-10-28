"""逐项核实历史映射证据；证据一致不等于财务批准。"""
import copy
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

from template_inspection import code


def signature(row):
    for side in ['debit', 'credit']:
        value = row[side]
        if value['status'] not in ['blank', 'number'] or (value['status'] == 'blank') != (value['value'] is None):
            raise ValueError('历史金额状态不可靠')
        if value['value'] is not None:
            try:
                number = Decimal(value['value'])
            except (InvalidOperation, TypeError) as error:
                raise ValueError('历史金额不是有效十进制值') from error
            if not number.is_finite():
                raise ValueError('历史金额不能是非有限数值')
    return (row['period'], row['voucher_category'], row['voucher_number'], row['summary'],
            row['target_subject_code'], row['target_subject_name'],
            *[(row[s]['status'], Decimal(row[s]['value']) if row[s]['value'] is not None else None) for s in ['debit', 'credit']])


def build_mapping_review(config, source_rows, diagnostic, manual, final, accounts):
    if not manual or not final:
        raise ValueError('映射证据标准答案不能为空')
    left, right = Counter(signature(r) for r in manual), Counter(signature(r) for r in final)
    if left != right:
        raise ValueError('人工与成品的内部编码、名称或核心业务字段不一致')
    source_index = {r['record_id']: r for r in source_rows}
    if len(source_index) != len(source_rows):
        raise ValueError('来源ID重复')
    selected_ids = [r['record_id'] for r in diagnostic]
    if len(set(selected_ids)) != len(selected_ids) or not set(selected_ids) <= set(source_index):
        raise ValueError('诊断行身份不闭环')
    by_external, historical, dictionary = defaultdict(list), defaultdict(list), defaultdict(list)
    for entry in diagnostic:
        source = source_index[entry['record_id']]
        if source['subject']['code'] != entry['external_subject_code']:
            raise ValueError('诊断行与原始科目不一致')
        by_external[entry['external_subject_code']].append(source)
    for row in manual:
        historical[row['external_subject_code']].append(row)
    for row in accounts:
        dictionary[code(row['values']['科目编码'])].append(row)
    items = []
    for mapping in config['mappings']:
        if mapping['status'] != 'candidate':
            continue
        ext = mapping['source_code']; reference = historical[ext]
        target = (mapping['target_code'], mapping['target_name'])
        observed = sorted({(r['target_subject_code'], r['target_subject_name']) for r in reference})
        matches = dictionary[mapping['target_code']]
        reasons = []
        if not reference:
            reasons.append('NO_MANUAL_MAPPING_EVIDENCE')
        elif observed != [target]:
            reasons.append('HISTORICAL_MAPPING_CONFLICT')
        if not matches:
            reasons.append('TARGET_ACCOUNT_MISSING')
        elif len(matches) != 1:
            reasons.append('TARGET_ACCOUNT_AMBIGUOUS')
        elif matches[0]['values']['科目名称'] != mapping['target_name']:
            reasons.append('TARGET_ACCOUNT_NAME_DIFFERS')
        entries = by_external[ext]
        final_evidence = [r for r in final if r['target_subject_code'] == mapping['target_code'] and r['target_subject_name'] == mapping['target_name']]
        items.append({'mapping_id': mapping['id'], 'financial_review_item_id': 'review-' + mapping['id'], 'decision': 'pending',
                      'evidence_status': 'blocked' if reasons else 'consistent_pending_financial_review',
                      'reason_codes': reasons, 'source_code': ext,
                      'source_names': sorted({r['subject']['name'] for r in entries}),
                      'proposed_target_code': mapping['target_code'], 'proposed_target_name': mapping['target_name'],
                      'current_diagnostic_count': len(entries), 'manual_evidence_count': len(reference),
                      'source_evidence': [{'record_id': r['record_id'], 'provenance': copy.deepcopy(r['provenance']),
                                           'voucher': copy.deepcopy(r['voucher']), 'summary': r['summary'],
                                           'debit': copy.deepcopy(r['debit']), 'credit': copy.deepcopy(r['credit'])} for r in entries],
                      'manual_evidence': copy.deepcopy(reference), 'observed_historical_targets': [list(v) for v in observed],
                      'target_dictionary_evidence': copy.deepcopy(matches),
                      'final_same_target_rows': [copy.deepcopy(r['provenance']) for r in final_evidence],
                      'final_rows_scope': '全部同目标编码及名称行，可能包含其他外部科目；不是一对一关联',
                      'observed_auxiliary_category': matches[0]['values']['核算类别'] if len(matches) == 1 else None,
                      'config_evidence': copy.deepcopy(mapping['evidence'])})
    ids = [i['mapping_id'] for i in items]
    if len(ids) != len(set(ids)):
        raise ValueError('映射ID重复')
    covered_codes = {i['source_code'] for i in items}
    # 其他科目显式列出，包括无候选、已有确认或拒绝状态；不能猜新映射。
    others = [{'source_code': ext, 'proposed_target_code': None, 'decision': 'pending',
               'existing_mapping_statuses': sorted({m['status'] for m in config['mappings'] if m['source_code'] == ext}),
               'diagnostic_count': len(entries),
               'source_evidence': [{'record_id': r['record_id'], 'provenance': copy.deepcopy(r['provenance']),
                                    'voucher': copy.deepcopy(r['voucher']), 'debit': copy.deepcopy(r['debit']),
                                    'credit': copy.deepcopy(r['credit'])} for r in entries]}
              for ext, entries in sorted(by_external.items()) if ext not in covered_codes]
    target_groups = defaultdict(list)
    for item in items:
        target_groups[item['proposed_target_code']].append(item['source_code'])
    return {'mode': 'mapping_evidence_review', 'executable_configuration': False,
            'financial_approval_applied': False, 'ready_for_system_import': False,
            'candidate_mapping_count': len(items),
            'evidence_consistent_count': sum(not i['reason_codes'] for i in items),
            'blocked_mapping_count': sum(bool(i['reason_codes']) for i in items),
            'manual_reference_lines': len(manual), 'final_reference_lines': len(final),
            'manual_final_named_core_multiset_equal': True,
            'diagnostic_lines': len(diagnostic),
            'diagnostic_lines_with_candidate': sum(len(entries) for ext, entries in by_external.items() if ext in covered_codes),
            'diagnostic_lines_without_candidate': sum(len(entries) for ext, entries in by_external.items() if ext not in covered_codes),
            'many_to_one_targets': [{'target_code': target, 'external_codes': sorted(set(ext))} for target, ext in sorted(target_groups.items()) if len(set(ext)) > 1],
            'items': items, 'other_source_accounts': others}
