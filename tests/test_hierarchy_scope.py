"""本案例确认不能扩展为其他文件或月份的自动删除许可。"""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(Path(__file__).resolve().parent))
from smartmap.configuration import ConfigurationError, compile_config, load_config, validate_config
from smartmap.hierarchy import analyze_hierarchy
from smartmap.candidate_export import build_exports
from smartmap.conversion_issues import collect_issues
from smartmap.mapping_filter import MappingFilterError, map_and_filter
from smartmap.vouchers import validate_vouchers
from test_hierarchy import row, report


class ScopedHierarchyTests(unittest.TestCase):
    def setUp(self):
        self.config, _ = load_config(ROOT / 'config/example.json')
        self.rule = next(r for r in self.config['rules'] if r['kind'] == 'hierarchy')
        self.rule.update(status='confirmed', applicability={'period': '202606', 'source_sha256': 'a' * 64},
                         review={'reviewed_by': 'synthetic', 'reviewed_on': '2026-10-28', 'reason': '仅合成测试'})
        self.rule['evidence'].append({'kind': 'user_confirmation', 'reference': '仅合成测试'})

    def source(self):
        return report([row('9000', 10), row('9000.01', 10)], self.config)

    def test_matching_case_excludes_only_parent(self):
        r = analyze_hierarchy(self.source(), self.config)
        self.assertEqual(r['row_counts']['excluded'], 1)
        self.assertTrue(r['hierarchy_rule']['applicable'])
        self.assertEqual(r['rows'][0]['hierarchy']['reason_codes'], ['PARENT_CHILD_GROUP_VERIFIED'])

    def test_different_source_hash_never_excludes(self):
        source = self.source(); source['input_sha256'] = 'b' * 64
        for r in source['rows']:
            r['provenance']['input_sha256'] = 'b' * 64
        r = analyze_hierarchy(source, self.config)
        self.assertEqual(r['row_counts']['excluded'], 0)
        self.assertIn('HIERARCHY_RULE_OUT_OF_SCOPE', r['rows'][0]['hierarchy']['reason_codes'])

    def test_different_month_never_excludes(self):
        source = self.source(); source['period'] = '202607'
        for r in source['rows']:
            r['period'] = '202607'
        r = analyze_hierarchy(source, self.config)
        self.assertEqual(r['row_counts']['excluded'], 0)
        self.assertFalse(r['hierarchy_rule']['applicable'])

    def test_out_of_scope_still_retains_diagnostic_evidence(self):
        self.rule['applicability']['source_sha256'] = 'b' * 64
        r = analyze_hierarchy(self.source(), self.config)
        self.assertEqual(r['group_counts']['verified_structure'], 1)
        self.assertEqual(r['diagnostic_projection']['remaining_rows'], 1)
        self.assertEqual(r['row_counts']['needs_review'], 1)

    def test_out_of_scope_full_pipeline_reports_chinese_reason(self):
        self.rule['applicability']['source_sha256'] = 'b' * 64
        source = self.source(); hierarchy = analyze_hierarchy(source, self.config)
        mapped = map_and_filter(hierarchy, self.config)
        voucher = validate_vouchers(mapped, hierarchy, self.config)
        bundle = build_exports(voucher, self.config)
        issues = collect_issues(bundle, {'parse': source, 'hierarchy': hierarchy, 'mapping': mapped, 'voucher': voucher}, self.config)
        self.assertTrue(any(i['code'] == 'HIERARCHY_RULE_OUT_OF_SCOPE' and '案例' in i['message'] for i in issues))
        self.assertEqual(bundle['excluded'], [])

    def test_amount_mismatch_still_blocks_confirmed_rule(self):
        source = report([row('9000', 11), row('9000.01', 10)], self.config)
        r = analyze_hierarchy(source, self.config)
        self.assertEqual(r['row_counts']['excluded'], 0)

    def test_old_configs_without_scope_still_work(self):
        del self.rule['applicability']
        self.assertEqual(analyze_hierarchy(self.source(), self.config)['row_counts']['excluded'], 1)

    def test_invalid_scope_rejected(self):
        for scope in [{'period': '202613', 'source_sha256': 'a' * 64}, {'period': '202606', 'source_sha256': 'bad'},
                      {'period': '202606'}, {}, {'period': '202606', 'source_sha256': 'a' * 64, 'voucher_numbers': [1]}]:
            self.rule['applicability'] = scope
            with self.assertRaises(ConfigurationError):
                validate_config(self.config)

    def test_scope_for_other_rule_kind_rejected(self):
        self.config['rules'][0]['applicability'] = self.rule['applicability']
        with self.assertRaises(ConfigurationError):
            validate_config(self.config)

    def test_scope_changes_effective_fingerprint(self):
        before = compile_config(self.config)[1]
        self.rule['applicability']['period'] = '202607'
        self.assertNotEqual(compile_config(self.config)[1], before)

    def test_downstream_rejects_forged_out_of_scope_exclusion(self):
        self.rule['applicability']['source_sha256'] = 'b' * 64
        hierarchy = analyze_hierarchy(self.source(), self.config)
        hierarchy['rows'][0]['hierarchy']['decision'] = 'excluded'
        hierarchy['row_counts']['excluded'] = 1
        hierarchy['row_counts']['needs_review'] = 0
        with self.assertRaises(MappingFilterError):
            map_and_filter(hierarchy, self.config)

    def test_scoped_candidate_still_cannot_exclude(self):
        self.rule.update(status='candidate', review=None)
        self.assertEqual(analyze_hierarchy(self.source(), self.config)['row_counts']['excluded'], 0)

    def test_source_and_config_not_mutated(self):
        source = self.source(); before = copy.deepcopy((source, self.config))
        analyze_hierarchy(source, self.config)
        self.assertEqual((source, self.config), before)


if __name__ == '__main__':
    unittest.main()
