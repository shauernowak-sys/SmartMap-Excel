"""T13只读模板盘点与字典引用；不改企业样本。"""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from template_inspection import code, crosscheck, inspect, DATA_HEADERS, ACCOUNT_HEADERS, AUX_HEADERS
from inspect_import_template import run
from xlsx_fixtures import write_xlsx


def account(number=3, **values):
    return {'excel_row': number, 'values': {'科目编码': '009900', '科目名称': '合成科目', '核算类别': None, **values}}


def aux(number=3, **values):
    return {'excel_row': number, 'values': {'辅助类别': '客户', '编码': 'K01', '名称': '合成客户', **values}}


def data(number=2, **values):
    return {'excel_row': number, 'values': {**dict.fromkeys(DATA_HEADERS), '科目编码': '009900', '科目名称': '合成科目', **values}}


class ReferenceChecks(unittest.TestCase):
    def test_exact_dictionary_match(self):
        result = crosscheck([data(客户='合成客户')], [account(核算类别='客户')], [aux()])
        self.assertEqual(result['issues'], [])
        self.assertEqual(result['auxiliary_links'][0]['reference_rows'], [3])
        self.assertFalse(result['required_fields_confirmed'])

    def test_missing_account_preserved(self):
        r = crosscheck([data()], [], [])
        self.assertEqual(r['issues'][0]['code'], 'ACCOUNT_REFERENCE_MISSING_OR_AMBIGUOUS')

    def test_duplicate_account_not_first_match(self):
        r = crosscheck([data()], [account(), account(4, 科目名称='另一名称')], [])
        self.assertEqual(r['duplicate_account_codes'][0]['rows'], [3, 4])
        self.assertEqual(r['account_links'][0]['reference_rows'], [3, 4])

    def test_name_mismatch(self):
        r = crosscheck([data(科目名称='其他')], [account()], [])
        self.assertEqual(r['issues'][0]['code'], 'ACCOUNT_NAME_DIFFERS')

    def test_unknown_aux_name_no_guess(self):
        r = crosscheck([data(客户='合成客户乙')], [account()], [aux()])
        self.assertEqual(r['auxiliary_links'][0]['candidate_codes'], [])
        self.assertEqual(len(r['issues']), 1)

    def test_same_name_different_categories(self):
        r = crosscheck([data(供应商='合成客户')], [account()], [aux()])
        self.assertEqual(r['auxiliary_links'][0]['reference_rows'], [])

    def test_duplicate_aux_names_ambiguous(self):
        r = crosscheck([data(客户='合成客户')], [account()], [aux(), aux(4, 编码='K02')])
        self.assertEqual(len(r['duplicate_auxiliary_names']), 1)
        self.assertEqual(r['auxiliary_links'][0]['reference_rows'], [3, 4])

    def test_duplicate_aux_codes(self):
        r = crosscheck([], [], [aux(), aux(4, 名称='另一客户')])
        self.assertEqual(len(r['duplicate_auxiliary_codes']), 1)

    def test_declared_aux_missing(self):
        r = crosscheck([data()], [account(核算类别='客户')], [])
        self.assertFalse(r['declared_auxiliary_observations'][0]['value_present'])
        self.assertEqual(r['issues'][0]['code'], 'DECLARED_AUXILIARY_VALUE_ABSENT_OR_UNSUPPORTED')

    def test_unknown_declared_category_not_split(self):
        r = crosscheck([data(客户='合成客户')], [account(核算类别='客户,项目')], [aux()])
        self.assertFalse(r['declared_auxiliary_observations'][0]['column_supported'])

    def test_no_mutation(self):
        rows = [data()]; before = copy.deepcopy(rows)
        crosscheck(rows, [account()], [])
        self.assertEqual(rows, before)

    def test_numeric_code_matches_text(self):
        r = crosscheck([data(科目编码=9900)], [account(科目编码='9900')], [])
        self.assertEqual(r['issues'], [])

    def test_leading_zero_preserved(self):
        self.assertEqual(code('009900'), '009900')
        self.assertEqual(code(9900.0), '9900')

    def test_invalid_numeric_code_rejected(self):
        for value in [True, 99.1, float('nan'), Decimal('Infinity')]:
            with self.assertRaises(ValueError):
                code(value)


class WorkbookChecks(unittest.TestCase):
    def workbook(self, root, *, bad_header=False, reorder=False, formula=False):
        values = data(日期='2026-07-31', 凭证类别='记账凭证', 凭证号=1, 摘要='合成', 借方金额=10)['values']
        if formula:
            values['摘要'] = {'type': 'f', 'value': '1+1'}
        headers = DATA_HEADERS[:]
        if bad_header:
            headers[1] = '未知日期'
        sheets = [('凭证模板', [headers, list(values.values())]),
                  ('科目备查表', [['仅合成参考'], ACCOUNT_HEADERS, ['009900', '合成科目', '借', None, None, None]]),
                  ('辅助核算备查表', [['仅合成参考'], AUX_HEADERS, ['客户', 'K01', '合成客户', None]])]
        if reorder:
            sheets = sheets[1:] + sheets[:1]
        path = Path(root) / 'synthetic.xlsx'; write_xlsx(path, sheets, dimension='A1:A1')
        return path

    def test_entire_data_despite_wrong_dimension(self):
        with tempfile.TemporaryDirectory() as d:
            r = inspect(self.workbook(d))
            self.assertEqual(len(r['data_rows']), 1)
            self.assertEqual(len(r['sheets'][0]['fields']), 24)
            self.assertEqual(r['xml_worksheets']['xl/worksheets/sheet1.xml']['declared_dimension'], 'A1:A1')

    def test_formula_not_evaluated_or_hidden(self):
        with tempfile.TemporaryDirectory() as d:
            r = inspect(self.workbook(d, formula=True))
            self.assertEqual(r['sheets'][0]['formula_cells'][0]['cell'], 'F2')
            self.assertEqual(r['data_rows'][0]['values']['摘要'], '=1+1')

    def test_original_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.workbook(d); before = hashlib.sha256(path.read_bytes()).hexdigest()
            inspect(path)
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_unknown_layout_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                inspect(self.workbook(d, bad_header=True))

    def test_reordered_sheets_keeps_named_data(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'out'
            r = run(self.workbook(d, reorder=True), out)
            self.assertEqual(r['sheet_order'][-1], '凭证模板')
            self.assertIn('| X | 其他出库类别 |', (out / 'summary.md').read_text(encoding='utf-8'))

    def test_report_only_no_xlsx_created(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'out'; run(self.workbook(d), out)
            self.assertEqual({p.name for p in out.iterdir()}, {'.gitignore', 'template_profile.json', 'summary.md'})

    def test_existing_output_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                run(self.workbook(d), d)

    def test_missing_input_does_not_claim_success(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(OSError):
                run(Path(d) / 'missing.xlsx', Path(d) / 'out')
            self.assertFalse((Path(d) / 'out').exists())

    def test_flags_not_approved(self):
        with tempfile.TemporaryDirectory() as d:
            r = inspect(self.workbook(d))
            self.assertTrue(r['read_only_analysis'])
            self.assertFalse(r['business_rules_approved'])
            self.assertFalse(r['ready_for_system_import'])


if __name__ == '__main__':
    unittest.main()
