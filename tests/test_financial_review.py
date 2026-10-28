"""审核准备不能代替业务决定，不丢未知异常，不修改配置。"""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from financial_review import answer_draft, build_packet
from prepare_financial_review import prepare, pointer
from smartmap.configuration import load_config


def fixtures():
    config, _ = load_config(ROOT / 'config/example.json')
    template = {'accounts': [{'excel_row': 3, 'values': {'科目编码': '990001', '科目名称': config['mappings'][0]['target_name'], '核算类别': '客户'}}],
                'crosscheck': {'duplicate_account_codes': [], 'auxiliary_links': []},
                'sheets': [{'sheet': '凭证模板', 'comments': []}], 'sheet_order': ['凭证模板']}
    issues = {'issues': [{'issue_id': 'unknown-1', 'code': 'UNMAPPED_ACCOUNT', 'locator': {'external_code': '9999'},
                         'message': '未知科目', 'source_record_ids': ['row-1'], 'affected_record_ids': ['row-1', 'row-2'], 'rule_ids': []}]}
    return config, template, issues


class PacketTests(unittest.TestCase):
    def test_all_pending_not_executable(self):
        p = build_packet(*fixtures())
        self.assertTrue(all(i['status'] == 'pending' for i in p['items']))
        self.assertFalse(p['executable_configuration'])
        self.assertFalse(p['ready_for_system_import'])

    def test_no_input_mutation(self):
        args = fixtures(); before = copy.deepcopy(args)
        build_packet(*args)
        self.assertEqual(args, before)

    def test_unknown_account_included_no_target_guess(self):
        p = build_packet(*fixtures())
        item = next(i for i in p['items'] if i['kind'] == 'unknown_account')
        self.assertIsNone(item['proposal'])
        self.assertEqual(item['source_record_ids'], ['row-1'])
        self.assertEqual(item['affected_record_ids'], ['row-1', 'row-2'])

    def test_unknown_exception_never_dropped(self):
        c, t, e = fixtures(); e['issues'][0]['code'] = 'FUTURE_UNKNOWN_CODE'
        p = build_packet(c, t, e)
        self.assertEqual(p['covered_exception_ids'], ['unknown-1'])
        self.assertEqual(p['kind_counts']['conversion_exception'], 1)

    def test_mapping_preserved(self):
        c, t, e = fixtures(); p = build_packet(c, t, e)
        m = next(i for i in p['items'] if i['kind'] == 'account_mapping')
        self.assertEqual(m['proposal']['target_code'], c['mappings'][0]['target_code'])
        self.assertFalse(m['template_reference_conflict'])

    def test_target_conflict_blocks_recommendation(self):
        c, t, e = fixtures(); t['accounts'].append(copy.deepcopy(t['accounts'][0]))
        p = build_packet(c, t, e)
        self.assertTrue(next(i for i in p['items'] if i['kind'] == 'account_mapping')['template_reference_conflict'])

    def test_name_conflict_flag(self):
        c, t, e = fixtures(); t['accounts'][0]['values']['科目名称'] = '其他名称'
        p = build_packet(c, t, e)
        self.assertTrue(next(i for i in p['items'] if i['kind'] == 'account_mapping')['template_reference_conflict'])

    def test_duplicate_auxiliary_accounts_keep_conflicting_categories(self):
        c, t, e = fixtures(); duplicate = copy.deepcopy(t['accounts'][0]); duplicate['values']['核算类别'] = '员工'
        t['accounts'].append(duplicate)
        p = build_packet(c, t, e)
        a = next(i for i in p['items'] if i['kind'] == 'account_auxiliary_requirement')
        self.assertTrue(a['conflicting_category_observations'])
        self.assertEqual(a['proposal']['observed_categories'], ['员工', '客户'])
        self.assertEqual(len(a['evidence']), 2)

    def test_missing_target_flag(self):
        c, t, e = fixtures(); t['accounts'] = []
        p = build_packet(c, t, e)
        self.assertTrue(next(i for i in p['items'] if i['kind'] == 'account_mapping')['template_reference_conflict'])

    def test_confirmed_rule_not_reasked(self):
        c, t, e = fixtures(); p = build_packet(c, t, e)
        confirmed = {r['id'] for r in c['rules'] if r['status'] == 'confirmed'}
        self.assertEqual(set(p['existing_confirmed_rule_ids']), confirmed)
        self.assertTrue(all(i.get('config_item_id') not in confirmed for i in p['items']))

    def test_rejected_mapping_not_proposed(self):
        c, t, e = fixtures(); c['mappings'][0]['status'] = 'rejected'
        p = build_packet(c, t, e)
        self.assertNotIn('account_mapping', p['kind_counts'])

    def test_repeated_aux_gaps_grouped_but_occurrences_kept(self):
        c, t, e = fixtures()
        t['crosscheck']['auxiliary_links'] = [{'category': '员工', 'value': '合成人员', 'reference_rows': [], 'data_cell': cell} for cell in ['Q2', 'Q3']]
        p = build_packet(c, t, e)
        item = next(i for i in p['items'] if i['kind'] == 'auxiliary_dictionary_gap')
        self.assertEqual((item['occurrence_count'], item['data_cells']), (2, ['Q2', 'Q3']))

    def test_same_name_different_category_separate(self):
        c, t, e = fixtures()
        t['crosscheck']['auxiliary_links'] = [{'category': cat, 'value': '相同名称', 'reference_rows': [], 'data_cell': 'N2'} for cat in ['客户', '供应商']]
        self.assertEqual(build_packet(c, t, e)['kind_counts']['auxiliary_dictionary_gap'], 2)

    def test_unique_aux_match_not_gap(self):
        c, t, e = fixtures()
        t['crosscheck']['auxiliary_links'] = [{'category': '客户', 'value': '合成', 'reference_rows': [3], 'data_cell': 'N2'}]
        self.assertNotIn('auxiliary_dictionary_gap', build_packet(c, t, e)['kind_counts'])

    def test_all_evidence_pointers_resolve(self):
        c, t, e = fixtures(); sources = {'config': c, 'template': t, 'exceptions': e}
        for item in build_packet(c, t, e)['items']:
            for ref in item['evidence']:
                pointer(sources[ref['input']], ref['pointer'])

    def test_answers_unfilled_and_bound_to_packet_hash(self):
        p = build_packet(*fixtures()); draft = answer_draft(p, 'hash')
        self.assertEqual(draft['packet_sha256'], 'hash')
        self.assertEqual(len(draft['answers']), p['item_count'])
        self.assertTrue(all(a['decision'] == 'pending' and a['reviewed_by'] is None and a['response'] is None for a in draft['answers']))
        self.assertTrue(draft['not_a_runtime_config'])

    def test_answer_edit_does_not_change_packet(self):
        p = build_packet(*fixtures()); before = copy.deepcopy(p)
        draft = answer_draft(p, 'hash'); draft['answers'][0]['decision'] = 'confirmed'
        self.assertEqual(p, before)

    def test_duplicate_item_ids_rejected(self):
        c, t, e = fixtures(); c['mappings'].append(copy.deepcopy(c['mappings'][0]))
        with self.assertRaises(ValueError):
            build_packet(c, t, e)

    def test_known_account_meaning_does_not_approve_mapping(self):
        c,t,e=fixtures();target=c['mappings'][0]['target_code']
        fact={'account_clarification':{'account_code':target,'user_reply':'已说明的科目'}}
        p=build_packet(c,t,e,fact)
        item=next(i for i in p['items'] if i['kind']=='account_mapping')
        self.assertEqual(item['known_current_meaning'],'已说明的科目')
        self.assertEqual(item['status'],'pending')
        self.assertEqual(c['mappings'][0]['status'],'candidate')

    def test_known_meaning_keeps_historical_dictionary_conflict(self):
        c,t,e=fixtures();target=c['mappings'][0]['target_code']
        t['crosscheck']['duplicate_account_codes']=[{'code':target,'rows':[3,4]}]
        p=build_packet(c,t,e,{'account_clarification':{'account_code':target,'user_reply':'合成说明'}})
        item=next(i for i in p['items'] if i['kind']=='account_dictionary_conflict')
        self.assertIn('当前含义已由用户说明',item['title'])
        self.assertFalse(item['current_meaning_confirmation_repeated'])


class SafetyTests(unittest.TestCase):
    def test_existing_output_refused_before_reading(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                prepare('missing', 'missing', 'missing', 'missing', d)

    def test_output_inside_any_sealed_input_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for parent in ['run', 'evaluation', 'template']:
                with self.assertRaises(ValueError):
                    prepare('missing', root / 'run', root / 'evaluation', root / 'template', root / parent / 'new')

    def test_missing_input_no_fake_review_output(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'out'
            with self.assertRaises(OSError):
                prepare('missing', 'missing', 'missing', 'missing', out)
            self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
