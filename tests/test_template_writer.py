"""模板写出及阻断边界；测试数据均为临时合成数据。"""
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.worksheet.datavalidation import DataValidation

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from smartmap.template_profile import load_profile, sha, validate_profile
from smartmap.template_writer import prepare_template, write_template, validate_entries
from smartmap.candidate_export import build_exports
from test_conversion import approved_config, base_rows, stages_for
import test_auxiliary
from export_import_template import export_run
from preflight_import_template import preflight
from verify_import_output import verify as verify_saved_output


class TemplateWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = load_profile(ROOT / 'config/template.v1.json')
        self.reference = self.root / 'reference.xlsx'
        self.prepared = self.root / 'prepared'
        wb = openpyxl.Workbook()
        wb.active.title = '凭证模板'
        sheet = wb.active
        sheet.append([c['header'] for c in self.profile['columns']])
        sheet.append(['HISTORICAL_VALUE'] * 24)
        sheet.append(['OLD_LAST_ROW'] * 24)
        sheet['B2'].number_format = self.profile['date_format']
        sheet.auto_filter.ref = 'A1:BS135'
        dv = DataValidation(type='list', formula1='"记账凭证"')
        sheet.add_data_validation(dv)
        dv.add('C2:C3')
        wb.create_sheet('科目备查表').append(['参考编码', '示例名称'])
        wb.create_sheet('辅助核算备查表').append(['类别', '示例名称'])
        wb.save(self.reference)
        wb.close()
        c = approved_config()
        stages = stages_for(base_rows(), c)
        self.entries = build_exports(stages['voucher'], c)['candidates']

    def prepare(self):
        return prepare_template(self.reference, self.profile, self.prepared)

    def test_prepare_clears_all_historical_rows_and_preserves_reference(self):
        before = sha(self.reference)
        receipt = self.prepare()
        self.assertEqual(receipt['historical_rows_cleared'], 2)
        wb = openpyxl.load_workbook(self.prepared / 'blank_template.xlsx')
        self.assertEqual(wb.sheetnames, self.profile['sheet_names'])
        self.assertTrue(all(c.value is None for c in wb['凭证模板'][2]))
        self.assertEqual(wb['凭证模板'].auto_filter.ref, 'A1:X2')
        self.assertEqual(wb['科目备查表']['B1'].value, '示例名称')
        self.assertEqual(sha(self.reference), before)
        wb.close()

    def test_success_roundtrip_has_types_blank_fields_and_lineage(self):
        self.prepare()
        result = write_template(self.entries, self.prepared, self.root / 'export')
        self.assertTrue(result['roundtrip_passed'])
        wb = openpyxl.load_workbook(self.root / 'export/vouchers.xlsx')
        s = wb['凭证模板']
        self.assertEqual(s['B2'].value, datetime(2026, 6, 30))
        self.assertEqual(s['C2'].value, '记账凭证')
        self.assertEqual(s['D2'].data_type, 'n')
        self.assertEqual(s['G2'].data_type, 's')
        self.assertEqual(s['I2'].value, 100)
        self.assertIsNone(s['J2'].value)
        self.assertIsNone(s['E2'].value)
        self.assertEqual(s.auto_filter.ref, 'A1:X3')
        self.assertEqual(str(s.data_validations.dataValidation[0].sqref), 'C2:C3')
        self.assertEqual(len(result['lineage']), 2)
        self.assertFalse(result['ready_for_system_import'])
        wb.close()

    def test_duplicate_amount_entries_are_preserved(self):
        self.prepare()
        more = copy.deepcopy(self.entries)
        for i, r in enumerate(more):
            r['record_id'] += '-second'
            r['entry_sequence'] = i + 3
        result = write_template(self.entries + more, self.prepared, self.root / 'export')
        self.assertEqual(result['row_count'], 4)

    def test_identifier_leading_zeros_and_formula_like_summary_stay_text(self):
        self.prepare()
        for r in self.entries:
            r['target_subject_code'] = '000101'
            r['summary'] = '=SUM(A1:A2)'
        write_template(self.entries, self.prepared, self.root / 'export')
        wb = openpyxl.load_workbook(self.root / 'export/vouchers.xlsx')
        self.assertEqual(wb.active['G2'].value, '000101')
        self.assertEqual(wb.active['F2'].data_type, 's')
        wb.close()

    def test_different_month_uses_dynamic_date(self):
        self.prepare()
        for r in self.entries:
            r['period'] = '202802'
            r['posting_date'] = '2028-02-29'
        write_template(self.entries, self.prepared, self.root / 'export')
        wb = openpyxl.load_workbook(self.root / 'export/vouchers.xlsx')
        self.assertEqual(wb.active['B2'].value, datetime(2028, 2, 29))
        wb.close()

    def test_bad_date_missing_target_invalid_type_block(self):
        for field, value in [('posting_date', '2026-06-29'), ('target_subject_code', None),
                             ('voucher_number', '1'), ('summary', '\x01bad')]:
            rows = copy.deepcopy(self.entries)
            rows[0][field] = value
            with self.assertRaises((ValueError, TypeError)):
                validate_entries(rows, self.profile)

    def test_missing_leg_or_sequence_duplicate_block(self):
        with self.assertRaises(ValueError):
            validate_entries(self.entries[:1], self.profile)
        rows = copy.deepcopy(self.entries)
        rows[1]['entry_sequence'] = 1
        with self.assertRaises(ValueError):
            validate_entries(rows, self.profile)

    def test_unbalanced_and_excess_precision_block(self):
        for number in ['101', '123456789012345.67']:
            rows = copy.deepcopy(self.entries)
            rows[0]['debit']['value'] = number
            if number != '101':
                rows[1]['credit']['value'] = number
            with self.assertRaises(ValueError):
                validate_entries(rows, self.profile)

    def test_no_empty_or_diagnostic_candidates(self):
        with self.assertRaises(ValueError):
            validate_entries([], self.profile)
        self.entries[0]['diagnostic_only'] = True
        with self.assertRaises(ValueError):
            validate_entries(self.entries, self.profile)

    def test_unknown_auxiliary_field_not_silently_dropped(self):
        self.entries[0]['auxiliary']['unsupported'] = 'value'
        with self.assertRaises(ValueError):
            validate_entries(self.entries, self.profile)

    def test_template_hash_change_block(self):
        self.prepare()
        with (self.prepared / 'blank_template.xlsx').open('ab') as f:
            f.write(b'changed')
        with self.assertRaises(ValueError):
            write_template(self.entries, self.prepared, self.root / 'export')

    def test_existing_output_never_overwritten(self):
        self.prepare()
        with self.assertRaises(ValueError):
            self.prepare()
        write_template(self.entries, self.prepared, self.root / 'export')
        with self.assertRaises(ValueError):
            write_template(self.entries, self.prepared, self.root / 'export')

    def test_header_mismatch_rejected(self):
        p = copy.deepcopy(self.profile)
        p['columns'][0]['header'] = 'wrong'
        with self.assertRaises(ValueError):
            prepare_template(self.reference, p, self.prepared)

    def test_source_kind_mismatch_rejected(self):
        self.profile['columns'][1]['kind'] = 'text'
        with self.assertRaises(ValueError):
            validate_profile(self.profile)

    def test_actual_pipeline_complete_synthetic_run_can_write(self):
        self.prepare()
        _, run = test_auxiliary.AuxiliaryRuntimeTests().run_job(self.root, 'confirmed')
        result = export_run(run, self.prepared, self.root / 'export')
        self.assertEqual(result['status'], 'written_for_review')
        self.assertEqual(result['row_count'], 2)
        self.assertTrue(result['batch_complete'])

    def test_preflight_compares_all_fields_without_export(self):
        self.prepare()
        _, run = test_auxiliary.AuxiliaryRuntimeTests().run_job(self.root, 'confirmed')
        export_run(run, self.prepared, self.root / 'export')
        profile_path = self.root / 'profile.json'
        profile_path.write_text(json.dumps(self.profile, ensure_ascii=False), encoding='utf-8')
        result = preflight(run, profile_path, self.root / 'export/vouchers.xlsx', self.root / 'preflight')
        self.assertEqual(result['full_row_multiset'], {'matched': 2, 'missing': 0, 'extra': 0})
        self.assertEqual(len(result['columns']), 24)
        self.assertFalse(result['xlsx_created'])
        self.assertFalse(any((self.root / 'preflight').glob('*.xlsx')))

    def test_preflight_detects_non_core_column_difference(self):
        self.prepare()
        _, run = test_auxiliary.AuxiliaryRuntimeTests().run_job(self.root, 'confirmed')
        export_run(run, self.prepared, self.root / 'export')
        final = self.root / 'export/vouchers.xlsx'
        wb = openpyxl.load_workbook(final)
        wb.active['T2'] = 3
        wb.save(final)
        wb.close()
        profile_path = self.root / 'profile.json'
        profile_path.write_text(json.dumps(self.profile, ensure_ascii=False), encoding='utf-8')
        result = preflight(run, profile_path, final, self.root / 'preflight')
        self.assertEqual(result['full_row_multiset'], {'matched': 1, 'missing': 1, 'extra': 1})
        self.assertEqual(result['columns'][19]['missing'], 1)

    def test_saved_output_independent_acceptance_checks_lineage(self):
        self.prepare()
        _, run = test_auxiliary.AuxiliaryRuntimeTests().run_job(self.root, 'confirmed')
        exported = self.root / 'export'
        export_run(run, self.prepared, exported)
        final = exported / 'vouchers.xlsx'
        result = verify_saved_output(run, exported, self.prepared, final, self.root / 'acceptance')
        self.assertTrue(result['reference_acceptance_passed'])
        self.assertEqual((result['matched'], result['missing'], result['extra']), (2, 0, 0))

    def test_saved_output_detects_lineage_tamper(self):
        self.prepare()
        _, run = test_auxiliary.AuxiliaryRuntimeTests().run_job(self.root, 'confirmed')
        exported = self.root / 'export'
        export_run(run, self.prepared, exported)
        receipt_path = exported / 'write_manifest.json'
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        receipt['lineage'][0]['excel_row'] = 3
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
        result = verify_saved_output(run, exported, self.prepared, exported / 'vouchers.xlsx', self.root / 'acceptance')
        self.assertFalse(result['reference_acceptance_passed'])
        self.assertFalse(result['checks']['lineage_is_bijective_and_bound_to_saved_rows'])

    def test_actual_pipeline_pending_run_does_not_write_xlsx(self):
        self.prepare()
        _, run = test_auxiliary.AuxiliaryRuntimeTests().run_job(self.root, 'candidate')
        result = export_run(run, self.prepared, self.root / 'export')
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(any((self.root / 'export').glob('*.xlsx')))

    def test_partial_requires_both_conversion_and_output_choices(self):
        self.prepare()
        _, run = test_auxiliary.AuxiliaryRuntimeTests().run_job(self.root, 'confirmed', mixed=True, allow_partial=True)
        blocked = export_run(run, self.prepared, self.root / 'blocked')
        self.assertEqual(blocked['status'], 'blocked')
        result = export_run(run, self.prepared, self.root / 'explicit', allow_partial=True)
        self.assertEqual(result['row_count'], 2)
        self.assertTrue(result['partial_export'])
        self.assertFalse(result['batch_complete'])


if __name__ == '__main__':
    unittest.main()
