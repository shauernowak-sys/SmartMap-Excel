"""逐项证据核对不能消除重复次数或代替映射批准。"""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'tools'))
from smartmap.configuration import load_config
from mapping_review import build_mapping_review
from prepare_mapping_review import prepare


def fixtures():
    config, _ = load_config(ROOT / 'config/example.json')
    mapping = config['mappings'][0]
    row = {'record_id': 'gold-1', 'provenance': {'sheet': '人工', 'excel_row': 3},
           'period': '202606', 'voucher_category': '记', 'voucher_number': 1, 'summary': '合成',
           'external_subject_code': mapping['source_code'], 'target_subject_code': mapping['target_code'],
           'target_subject_name': mapping['target_name'], 'debit': {'status': 'number', 'value': '10'},
           'credit': {'status': 'blank', 'value': None}}
    manual, final = [copy.deepcopy(row)], [copy.deepcopy(row)]
    source = [{'record_id': 'src-1', 'subject': {'code': mapping['source_code'], 'name': '外部合成'},
               'provenance': {'sheet': '原始', 'excel_row': 10}, 'voucher': {'number': 1, 'category': '记'},
               'summary': '合成', 'debit': row['debit'].copy(), 'credit': row['credit'].copy()}]
    diagnostic = [{'record_id': 'src-1', 'external_subject_code': mapping['source_code']}]
    accounts = [{'excel_row': 3, 'values': {'科目编码': mapping['target_code'], '科目名称': mapping['target_name'], '核算类别': None}}]
    return config, source, diagnostic, manual, final, accounts


class MappingEvidenceTests(unittest.TestCase):
    def test_consistent_evidence_still_pending(self):
        p = build_mapping_review(*fixtures())
        self.assertEqual(p['evidence_consistent_count'], 1)
        self.assertFalse(p['financial_approval_applied'])
        self.assertFalse(p['executable_configuration'])
        self.assertEqual(p['items'][0]['decision'], 'pending')

    def test_inputs_unchanged(self):
        args = fixtures(); before = copy.deepcopy(args)
        build_mapping_review(*args)
        self.assertEqual(args, before)

    def test_duplicate_account_code_not_first_match(self):
        args = list(fixtures()); args[-1].append(copy.deepcopy(args[-1][0]))
        p = build_mapping_review(*args)
        self.assertEqual(p['blocked_mapping_count'], 1)
        self.assertIn('TARGET_ACCOUNT_AMBIGUOUS', p['items'][0]['reason_codes'])

    def test_wrong_dictionary_name_blocks(self):
        args = list(fixtures()); args[-1][0]['values']['科目名称'] = '其他'
        self.assertIn('TARGET_ACCOUNT_NAME_DIFFERS', build_mapping_review(*args)['items'][0]['reason_codes'])

    def test_missing_target_blocks(self):
        args = list(fixtures()); args[-1] = []
        self.assertIn('TARGET_ACCOUNT_MISSING', build_mapping_review(*args)['items'][0]['reason_codes'])

    def test_manual_final_name_mismatch_fails(self):
        args = list(fixtures()); args[4][0]['target_subject_name'] = '其他'
        with self.assertRaises(ValueError):
            build_mapping_review(*args)

    def test_duplicate_gold_occurrences_counted(self):
        args = list(fixtures()); args[3].append(copy.deepcopy(args[3][0]))
        with self.assertRaises(ValueError):
            build_mapping_review(*args)

    def test_same_amount_other_voucher_not_equal(self):
        args = list(fixtures()); args[4][0]['voucher_number'] = 2
        with self.assertRaises(ValueError):
            build_mapping_review(*args)

    def test_empty_gold_not_passed(self):
        args = list(fixtures()); args[3] = []
        with self.assertRaises(ValueError):
            build_mapping_review(*args)

    def test_invalid_money_not_zero(self):
        args = list(fixtures()); args[3][0]['debit'] = {'status': 'invalid', 'value': None}
        with self.assertRaises(ValueError):
            build_mapping_review(*args)

    def test_nonfinite_money_rejected(self):
        args = list(fixtures()); args[3][0]['debit'] = {'status': 'number', 'value': 'NaN'}
        with self.assertRaises(ValueError):
            build_mapping_review(*args)

    def test_bad_decimal_rejected(self):
        args = list(fixtures()); args[3][0]['debit'] = {'status': 'number', 'value': 'bad'}
        with self.assertRaises(ValueError):
            build_mapping_review(*args)

    def test_unknown_source_no_target_guessed(self):
        args = list(fixtures()); args[1][0]['subject']['code'] = '9999'; args[2][0]['external_subject_code'] = '9999'
        p = build_mapping_review(*args)
        self.assertEqual(p['diagnostic_lines_without_candidate'], 1)
        self.assertIsNone(p['other_source_accounts'][0]['proposed_target_code'])

    def test_historical_candidate_disagrees(self):
        args = list(fixtures()); args[0]['mappings'][0]['target_code'] = '999999'
        self.assertIn('HISTORICAL_MAPPING_CONFLICT', build_mapping_review(*args)['items'][0]['reason_codes'])

    def test_missing_manual_evidence_blocks(self):
        args = list(fixtures()); args[3][0]['external_subject_code'] = '9999'
        self.assertIn('NO_MANUAL_MAPPING_EVIDENCE', build_mapping_review(*args)['items'][0]['reason_codes'])

    def test_duplicated_source_id_fails(self):
        args = list(fixtures()); args[1].append(copy.deepcopy(args[1][0]))
        with self.assertRaises(ValueError):
            build_mapping_review(*args)

    def test_diagnostic_subject_mismatch_fails(self):
        args = list(fixtures()); args[2][0]['external_subject_code'] = '9999'
        with self.assertRaises(ValueError):
            build_mapping_review(*args)

    def test_diagnostic_duplicate_not_dropped(self):
        args = list(fixtures()); args[2].append(copy.deepcopy(args[2][0]))
        with self.assertRaises(ValueError):
            build_mapping_review(*args)

    def test_many_to_one_explicit_not_dedup(self):
        args = list(fixtures()); second = copy.deepcopy(args[0]['mappings'][0]); second.update(id='second', source_code='9000.02')
        args[0]['mappings'].append(second)
        p = build_mapping_review(*args)
        self.assertEqual(p['candidate_mapping_count'], 2)
        self.assertEqual(len(p['many_to_one_targets'][0]['external_codes']), 2)

    def test_original_source_amount_in_evidence(self):
        args = fixtures(); p = build_mapping_review(*args)
        self.assertEqual(p['items'][0]['source_evidence'][0]['debit'], args[1][0]['debit'])


class OutputSafetyTests(unittest.TestCase):
    def test_existing_output_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                prepare('missing', 'missing', 'missing', 'missing', d)

    def test_output_under_sealed_report_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                prepare('missing', d, 'missing', 'missing', Path(d) / 'new')

    def test_missing_inputs_no_success_files(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'out'
            with self.assertRaises(OSError):
                prepare('missing', 'missing', 'missing', 'missing', out)
            self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
