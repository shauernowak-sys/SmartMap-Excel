"""独立复核T12计数及证据；不调用验收匹配函数。"""
import argparse
import calendar
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

from verify_conversion_run import audit_run


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def key(row, code):
    return (row['period'], row['voucher_category'], row['voucher_number'], row.get(code), row['summary'],
            *[(row[s]['status'], Decimal(row[s]['value']) if row[s]['value'] is not None else None) for s in ['debit', 'credit']])


def verify(directory):
    directory = Path(directory).resolve()
    report = read(directory / 'evaluation.json')
    checks = {'completed_evaluation': report['status'] in ['passed', 'differences_found'],
              'no_system_import_claim': report['ready_for_system_import'] is False}
    if not checks['completed_evaluation']:
        return {'checks': checks, 'all_passed': False}
    checks['input_hashes_unchanged'] = all(hashlib.sha256(Path(v['path']).read_bytes()).hexdigest() == v['sha256'] for v in report['inputs'].values())
    run = Path(report['inputs']['conversion_manifest']['path']).parent
    checks['conversion_still_valid'] = audit_run(run)['usable_for_evaluation']
    refs = read(directory / 'references.json')
    # 重新从答案Excel读取，避免只在报告复制件内部自洽。
    from evaluation_reference import load_reference
    checks['reference_snapshots_equal_sources'] = all(refs[k] == load_reference(Path(report['inputs'][k]['path']), k) for k in ['manual', 'final'])
    for view, filename in [('official', 'candidates.json'), ('diagnostic', 'diagnostic_candidates.json')]:
        predicted = read(run / filename)['rows']
        for label, refname, code in [('manual_external', 'manual', 'external_subject_code'), ('final_internal', 'final', 'target_subject_code')]:
            expected = refs[refname]['rows']; result = report['sections'][view][label]
            left = Counter(key(r, code) for r in predicted); right = Counter(key(r, code) for r in expected)
            matched, missing, extra = sum((left & right).values()), sum((right - left).values()), sum((left - right).values())
            prefix = view + '_' + label
            checks[prefix + '_counts'] = (result['predicted_count'], result['expected_count'], result['matched_count'], result['missing_count'], result['extra_count']) == (len(predicted), len(expected), matched, missing, extra)
            checks[prefix + '_unmatched_multisets'] = Counter(key(r, code) for r in result['missing']) == right - left and Counter(key(r, code) for r in result['extra']) == left - right
            pairs = result['pairs']
            checks[prefix + '_pair_occurrences'] = len(pairs) == matched and len({p['predicted_index'] for p in pairs}) == matched and len({p['expected_index'] for p in pairs}) == matched and all(key(predicted[p['predicted_index']], code) == key(expected[p['expected_index']], code) and p['predicted_id'] == predicted[p['predicted_index']]['record_id'] and p['expected_id'] == expected[p['expected_index']]['record_id'] and p['expected_provenance'] == expected[p['expected_index']]['provenance'] and p['predicted_provenance'] == predicted[p['predicted_index']]['provenance'] for p in pairs)
            denominator = sum((Counter(key(r, code)[:5] for r in predicted) & Counter(key(r, code)[:5] for r in expected)).values())
            checks[prefix + '_amount_rate'] = result['amount_consistency'] == {'numerator': matched, 'denominator': denominator, 'value': matched / denominator if denominator else None}
        external = report['sections'][view]['manual_external']
        groups = defaultdict(list)
        for row in predicted:
            groups[key(row, 'external_subject_code')[:3]].append(row)
        with localcontext() as context:
            context.prec = max(64, sum(len(r[s]['value'] or '') for r in predicted for s in ['debit', 'credit']) + 4)
            balanced = sum(sum((Decimal(r['debit']['value'] or '0') for r in members), Decimal(0)) == sum((Decimal(r['credit']['value'] or '0') for r in members), Decimal(0)) for members in groups.values())
        checks[view + '_balance_rate'] = external['balance']['rate'] == {'numerator': balanced, 'denominator': len(groups), 'value': balanced / len(groups) if groups else None}
        mapping_correct = date_correct = 0
        for pair in external['pairs']:
            row = predicted[pair['predicted_index']]; expected = refs['manual']['rows'][pair['expected_index']]
            targets = {r['target_subject_code'] for r in refs['manual']['rows'] if key(r, 'external_subject_code') == key(expected, 'external_subject_code')}
            mapping_correct += len(targets) == 1 and row.get('target_subject_code') in targets
            year, month = int(expected['period'][:4]), int(expected['period'][4:])
            date_correct += row.get('posting_date') == date(year, month, calendar.monthrange(year, month)[1]).isoformat()
        for metric, numerator in [('mapping_accuracy', mapping_correct), ('month_end_date_accuracy', date_correct)]:
            n = len(external['pairs'])
            checks[view + '_' + metric] = external[metric] == {'numerator': numerator, 'denominator': n, 'value': numerator / n if n else None}
    manifest = read(run / 'run_manifest.json')
    checks['unresolved_counts'] = report['unresolved_exception_count'] == manifest['exception_issue_count'] and report['unresolved_affected_record_count'] == manifest['affected_record_count']
    official = report['sections']['official']; a = official['manual_external']; b = official['final_internal']
    expected_pass = not a['missing_count'] and not a['extra_count'] and not b['missing_count'] and not b['extra_count'] and a['mapping_accuracy']['value'] == 1 and a['month_end_date_accuracy']['value'] == 1 and a['balance']['rate']['value'] == 1 and not report['unresolved_exception_count'] and report['reference_final_month_end_dates']['value'] == 1
    checks['acceptance_status_consistent'] = report['business_acceptance_passed'] == expected_pass and report['status'] == ('passed' if expected_pass else 'differences_found') and report['exit_code'] == (0 if expected_pass else 3)
    return {'checks': checks, 'all_passed': all(checks.values()), 'check_count': len(checks)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.report)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['all_passed'] else 1
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        print(json.dumps({'all_passed': False, 'error': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
