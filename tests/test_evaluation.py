"""T12独立验收：重复次数、错金额、错科目、未知及失败边界。"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from evaluation_core import amount, balance, evaluate, mapping_and_dates, match
from evaluation_reference import load_reference
from evaluate_conversion import run_evaluation
from xlsx_fixtures import write_xlsx


def item(identifier='one', **changes):
    value = {'record_id': identifier, 'provenance': {'sheet': 'test', 'excel_row': 3},
             'period': '202606', 'voucher_category': '记', 'voucher_number': 1,
             'external_subject_code': '9000.01', 'target_subject_code': '990001', 'summary': '合成业务',
             'debit': {'status': 'number', 'value': '10'}, 'credit': {'status': 'blank', 'value': None},
             'posting_date': '2026-06-30'}
    value.update(changes)
    return value


class MatchingTests(unittest.TestCase):
    def test_duplicate_occurrences_not_set(self):
        result = match([item()], [item('a'), item('b')], 'external_subject_code')
        self.assertEqual((result['matched_count'], result['missing_count'], result['extra_count']), (1, 1, 0))
        self.assertTrue(result['pairs'][0]['equivalent_identity_ambiguous'])

    def test_duplicate_prediction_is_extra(self):
        result = match([item('a'), item('b')], [item()], 'external_subject_code')
        self.assertEqual((result['matched_count'], result['extra_count']), (1, 1))

    def test_same_amount_distinct_voucher(self):
        result = evaluate([item(voucher_number=2)], [item()], 'external_subject_code')
        self.assertEqual(result['matched_count'], 0)

    def test_same_number_different_category(self):
        self.assertEqual(match([item(voucher_category='收')], [item()], 'external_subject_code')['matched_count'], 0)

    def test_month_is_part_of_signature(self):
        self.assertEqual(match([item(period='202607')], [item()], 'external_subject_code')['matched_count'], 0)

    def test_amount_change_not_hidden_by_summary_anchor(self):
        r = evaluate([item(debit={'status': 'number', 'value': '11'})], [item()], 'external_subject_code')
        self.assertEqual((r['matched_count'], r['missing_count'], r['extra_count']), (0, 1, 1))
        self.assertEqual(r['amount_consistency'], {'numerator': 0, 'denominator': 1, 'value': 0})

    def test_blank_and_zero_distinct(self):
        self.assertEqual(match([item(credit={'status': 'number', 'value': '0'})], [item()], 'external_subject_code')['matched_count'], 0)

    def test_decimal_representation_equivalent(self):
        self.assertEqual(match([item(debit={'status': 'number', 'value': '10.00'})], [item()], 'external_subject_code')['matched_count'], 1)

    def test_no_rounding(self):
        self.assertEqual(match([item(debit={'status': 'number', 'value': '10.00001'})], [item()], 'external_subject_code')['matched_count'], 0)

    def test_empty_predictions_report_missing_and_null_rates(self):
        r = evaluate([], [item()], 'external_subject_code')
        self.assertEqual(r['missing_count'], 1)
        self.assertIsNone(r['precision']['value'])
        self.assertIsNone(r['balance']['rate']['value'])

    def test_unknown_target_not_backfilled(self):
        row = item(target_subject_code=None)
        external = evaluate([row], [item()], 'external_subject_code')
        r = mapping_and_dates([row], [item()], external)
        self.assertEqual(r['mapping_accuracy']['value'], 0)
        self.assertEqual(match([row], [item()], 'target_subject_code')['matched_count'], 0)
        self.assertIsNone(row['target_subject_code'])

    def test_ambiguous_target_not_arbitrarily_scored(self):
        refs = [item('a'), item('b', target_subject_code='990002')]
        pred = [item()]
        r = mapping_and_dates(pred, refs, match(pred, refs, 'external_subject_code'))
        self.assertEqual(r['mapping_ambiguous_count'], 1)
        self.assertEqual(r['mapping_accuracy']['value'], 0)

    def test_leap_month_date(self):
        pred = [item(period='202402', posting_date='2024-02-29')]
        refs = [item(period='202402')]
        self.assertEqual(mapping_and_dates(pred, refs, match(pred, refs, 'external_subject_code'))['month_end_date_accuracy']['value'], 1)

    def test_unbalanced(self):
        self.assertEqual(balance([item()])['rate']['value'], 0)

    def test_negative_offset_balance_preserved(self):
        r = balance([item(), item('negative', debit={'status': 'number', 'value': '-10'})])
        self.assertEqual(r['rate']['value'], 1)
        self.assertEqual(r['vouchers'][0]['line_count'], 2)

    def test_invalid_amount_not_zero(self):
        with self.assertRaises(ValueError):
            amount(item(debit={'status': 'invalid', 'value': None}), 'debit')

    def test_blank_carrying_value_rejected(self):
        with self.assertRaises(ValueError):
            amount(item(debit={'status': 'blank', 'value': '0'}), 'debit')

    def test_nonfinite_amount_rejected(self):
        with self.assertRaises(ValueError):
            amount(item(debit={'status': 'number', 'value': 'NaN'}), 'debit')

    def test_malformed_decimal_rejected(self):
        with self.assertRaises(ValueError):
            amount(item(debit={'status': 'number', 'value': 'bad'}), 'debit')

    def test_input_not_mutated(self):
        rows = [item()]; before = copy.deepcopy(rows)
        evaluate(rows, rows, 'external_subject_code')
        self.assertEqual(rows, before)


class ReferenceTests(unittest.TestCase):
    def workbook(self, directory, changes=None):
        header = [None, '日期', '凭证类别', '凭证号', None, '摘要', '科目编码', None, '借方金额', '贷方金额']
        row = [None, '2026-07-31', '记账凭证', 1, None, '合成', '009900', None, 10, None]
        for col, value in (changes or {}).items():
            row[col] = value
        path = Path(directory) / 'final.xlsx'
        write_xlsx(path, [('凭证模板', [header, row])], dimension='A1:A1')
        return path

    def test_reads_full_rows_and_leading_zero(self):
        with tempfile.TemporaryDirectory() as d:
            r = load_reference(self.workbook(d), 'final')['rows'][0]
            self.assertEqual((r['period'], r['target_subject_code']), ('202607', '009900'))

    def test_invalid_reference_amount(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                load_reference(self.workbook(d, {8: '=1+1'}), 'final')

    def test_unknown_category_fails(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                load_reference(self.workbook(d, {2: '其他'}), 'final')

    def test_invalid_reference_date(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                load_reference(self.workbook(d, {1: '2026-02-30'}), 'final')

    def test_unrecognized_nonempty_record_fails(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                load_reference(self.workbook(d, {3: None}), 'final')


class RunnerTests(unittest.TestCase):
    def synthetic_run(self, root, *, pending=0, extra=False, bad_final_date=False, period='202606', changed=False):
        run = root / 'run'; run.mkdir()
        expected = [item(), item('two', debit={'status': 'blank', 'value': None}, credit={'status': 'number', 'value': '10'})]
        predicted = copy.deepcopy(expected)
        if extra:
            predicted.append(item('extra'))
        manifest = {'period': '202606', 'exception_issue_count': pending, 'affected_record_count': pending}
        (run / 'run_manifest.json').write_text(json.dumps(manifest))
        for filename in ['candidates.json', 'diagnostic_candidates.json']:
            (run / filename).write_text(json.dumps({'rows': predicted}))
        reference = root / 'reference'; reference.write_bytes(b'synthetic')
        manual = copy.deepcopy(expected); final = copy.deepcopy(expected)
        for r in manual:
            r['period'] = period
        if bad_final_date:
            final[0]['posting_date'] = '2026-06-29'
        audits = [{'usable_for_evaluation': True}, {'usable_for_evaluation': not changed}]
        with patch('evaluate_conversion.audit_run', side_effect=audits), patch('evaluate_conversion.load_reference', side_effect=[{'rows': manual}, {'rows': final}]):
            return run_evaluation(run, reference, reference, root / 'out')

    def test_complete_synthetic_pass(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.synthetic_run(Path(d))
            self.assertEqual(r['exit_code'], 0)
            self.assertTrue(r['business_acceptance_passed'])
            self.assertFalse(r['ready_for_system_import'])

    def test_extra_cannot_pass(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.synthetic_run(Path(d), extra=True)['exit_code'], 3)

    def test_pending_exception_cannot_pass(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.synthetic_run(Path(d), pending=1)['exit_code'], 3)

    def test_final_wrong_date_cannot_pass(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.synthetic_run(Path(d), bad_final_date=True)['exit_code'], 3)

    def test_period_conflict_fails(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.synthetic_run(Path(d), period='202607')['exit_code'], 2)

    def test_changed_run_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.synthetic_run(Path(d), changed=True)['exit_code'], 2)

    def test_existing_output_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                run_evaluation('missing', 'missing', 'missing', d)

    def test_output_inside_conversion_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                run_evaluation(d, 'missing', 'missing', Path(d) / 'new')

    def test_missing_inputs_write_failed_report(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'out'
            r = run_evaluation('missing', 'missing', 'missing', out)
            self.assertEqual(r['exit_code'], 2)
            self.assertFalse(r['business_acceptance_passed'])
            self.assertTrue((out / 'evaluation.json').is_file())

    def test_failed_conversion_rejected_before_reading_reference(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); run = root / 'run'; run.mkdir()
            (run / 'run_manifest.json').write_text('{}')
            ref = root / 'ref.xlsx'; ref.write_bytes(b'not Excel')
            with patch('evaluate_conversion.audit_run', return_value={'usable_for_evaluation': False}), patch('evaluate_conversion.load_reference') as reader:
                r = run_evaluation(run, ref, ref, root / 'out')
            reader.assert_not_called()
            self.assertEqual(r['exit_code'], 2)


if __name__ == '__main__':
    unittest.main()
