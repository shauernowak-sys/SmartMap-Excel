"""T06：候选隔离、严格配置校验及入口边界的合成测试。"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from smartmap.configuration import (ConfigurationError, compile_config, inspect_config,
    load_config, parse_json, resolve_mapping, validate_config)
from smartmap.contracts import ConversionRequest, EvaluationRequest
from smartmap.schema_validation import check_schema


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.config,_=load_config(ROOT/'config/example.json')

    def confirm(self,item):
        item['status']='confirmed'
        item['evidence'].append({'kind':'user_confirmation','reference':'合成测试确认，不涉及真实财务规则'})
        item['review']={'reviewed_by':'synthetic-test','reviewed_on':'2026-10-28','reason':'仅验证审核状态校验'}
        return item

    def test_example_is_valid_but_not_conversion(self):
        report=inspect_config(self.config)
        self.assertTrue(report['config_valid'])
        self.assertFalse(report['conversion_implemented'])
        self.assertFalse(report['ready_for_system_import'])

    def test_candidates_are_not_active(self):
        active,_=compile_config(self.config)
        self.assertEqual(active['mappings'],[])
        self.assertEqual([x['kind'] for x in active['rules']],['date'])

    def test_confirmed_requires_human_evidence_and_review(self):
        self.config['mappings'][0]['status']='confirmed'
        with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_review_without_confirmation_evidence_is_invalid(self):
        m=self.confirm(self.config['mappings'][0]);m['evidence']=[{'kind':'historical_analysis','reference':'历史不等于确认'}]
        with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_candidate_must_not_claim_review(self):
        self.config['mappings'][0]['review']=self.config['rules'][0]['review']
        with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_invalid_review_date(self):
        m=self.confirm(self.config['mappings'][0]);m['review']['reviewed_on']='2027-02-29'
        with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_confirmed_mapping_exact_and_preserves_zeroes(self):
        m=self.confirm(self.config['mappings'][0]);m.update(source_code='0010.01',target_code='000101')
        result=resolve_mapping(self.config,'0010.01')
        self.assertEqual(result['status'],'matched')
        self.assertEqual(result['mapping']['target_code'],'000101')
        self.assertEqual(resolve_mapping(self.config,'0010.01.02')['status'],'unknown')

    def test_candidate_and_unknown_are_separate(self):
        self.assertEqual(resolve_mapping(self.config,'9000.01')['status'],'unconfirmed')
        self.assertEqual(resolve_mapping(self.config,'9999')['status'],'unknown')

    def test_rejected_mapping_never_active(self):
        m=self.confirm(self.config['mappings'][0]);m['status']='rejected'
        self.assertEqual(compile_config(self.config)[0]['mappings'],[])

    def test_duplicate_confirmed_source_even_same_target_is_error(self):
        self.confirm(self.config['mappings'][0])
        m=copy.deepcopy(self.config['mappings'][0]);m['id']='other';self.config['mappings'].append(m)
        with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_conflicting_candidates_are_reported_not_used(self):
        m=copy.deepcopy(self.config['mappings'][0]);m.update(id='other',target_code='990002');self.config['mappings'].append(m)
        report=inspect_config(self.config)
        self.assertEqual(report['candidate_mapping_conflict_count'],1)
        self.assertEqual(report['active_mapping_count'],0)

    def test_numeric_code_rejected(self):
        self.config['mappings'][0]['source_code']=9000
        with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_mapping_id_cannot_duplicate_rule_id(self):
        self.config['mappings'][0]['id']=self.config['rules'][0]['id']
        with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_unknown_keys_cannot_embed_answers(self):
        for key,value in [('expected_lines',176),('manual_workbook','answer.xlsx'),('voucher_whitelist',[1,2])]:
            c=copy.deepcopy(self.config);c[key]=value
            with self.subTest(key=key),self.assertRaises(ConfigurationError):validate_config(c)

    def test_filter_cannot_use_row_numbers_or_voucher_whitelist(self):
        for field in ['excel_rows','voucher_numbers']:
            c=copy.deepcopy(self.config)
            c['rules'].append({'id':'bad-filter','kind':'voucher_filter','status':'candidate','policy':'exclude_voucher',
                'parameters':{field:[74],'match_scope':'all_lines'},'evidence':[{'kind':'synthetic_example','reference':'test'}],'review':None})
            with self.subTest(field=field),self.assertRaises(ConfigurationError):validate_config(c)

    def test_candidate_filter_never_active(self):
        self.config['rules'].append({'id':'review-filter','kind':'voucher_filter','status':'candidate','policy':'exclude_voucher',
            'parameters':{'summary_equals':'合成测试业务','match_scope':'all_lines'},'evidence':[{'kind':'synthetic_example','reference':'test'}],'review':None})
        self.assertEqual(inspect_config(self.config)['active_voucher_filter_count'],0)

    def test_filter_requires_whole_voucher_scope(self):
        self.config['rules'].append({'id':'bad-filter','kind':'voucher_filter','status':'candidate','policy':'exclude_voucher',
            'parameters':{'summary_equals':'test'},'evidence':[{'kind':'synthetic_example','reference':'test'}],'review':None})
        with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_kind_and_policy_must_agree(self):
        self.config['rules'][1]['policy']='exclude_voucher'
        with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_hierarchy_cannot_smuggle_filter_parameters(self):
        self.config['rules'][1]['parameters']={'summary_equals':'test'}
        with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_month_end_rule_must_remain_confirmed(self):
        self.config['rules'][0]['status']='candidate';self.config['rules'][0]['review']=None
        with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_unsupported_schema_keyword_fails_closed(self):
        with self.assertRaises(ConfigurationError):check_schema({'type':'string','maxLength':1})
        with self.assertRaises(ConfigurationError):check_schema({'$defs':{'s':{'type':'string'}},'properties':{'s':{'$ref':'#/$defs/s','minLength':1}}})

    def test_duplicate_json_keys_rejected_at_any_depth(self):
        for value in ['{"id":1,"id":2}','{"inner":{"id":1,"id":2}}']:
            with self.subTest(value=value),self.assertRaises(ConfigurationError):parse_json(value)

    def test_nonfinite_json_rejected(self):
        for value in ['NaN','Infinity','-Infinity']:
            with self.subTest(value=value),self.assertRaises(ConfigurationError):parse_json('{"value":'+value+'}')

    def test_unsupported_schema_version(self):
        self.config['schema_version']='0.3'
        with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_candidate_change_does_not_change_effective_fingerprint(self):
        _,before=compile_config(self.config)
        self.config['mappings'][0]['target_code']='990003'
        self.assertEqual(compile_config(self.config)[1],before)

    def test_compiled_result_does_not_mutate_config(self):
        active,_=compile_config(self.config);active['rules'][0]['policy']='tampered'
        self.assertEqual(self.config['rules'][0]['policy'],'period_month_end')

    def test_strict_revision_and_code_syntax(self):
        for value in [True,0,'1']:
            c=copy.deepcopy(self.config);c['revision']=value
            with self.subTest(value=value),self.assertRaises(ConfigurationError):validate_config(c)
        self.config['mappings'][0]['source_code']='9000.01\n'
        with self.assertRaises(ConfigurationError):validate_config(self.config)


class InterfaceTests(unittest.TestCase):
    def test_conversion_contract_has_no_reference_answer(self):
        self.assertEqual({x.name for x in fields(ConversionRequest)},{'source','config','output_dir','period','allow_partial'})
        self.assertIn('manual_workbook',{x.name for x in fields(EvaluationRequest)})

    def test_invalid_period_or_overlapping_path_rejected(self):
        for period in ['202613','202600','2026-06',202606]:
            with self.subTest(period=period),self.assertRaises(ValueError):ConversionRequest(Path('raw.xlsx'),Path('config.json'),Path('out'),period)
        with self.assertRaises(ValueError):ConversionRequest(Path('same'),Path('same'),Path('out'))
        self.assertEqual(ConversionRequest(Path('raw'),Path('config'),Path('out'),'202702').period,'202702')

    def test_cli_does_not_read_excel_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'report.json'
            command=[sys.executable,'-B','-m','smartmap','config-check','--config',str(ROOT/'config/example.json'),'--out',str(output)]
            first=subprocess.run(command,cwd=ROOT,capture_output=True)
            self.assertEqual(first.returncode,0,first.stderr)
            original=output.read_bytes()
            second=subprocess.run(command,cwd=ROOT,capture_output=True)
            self.assertEqual(second.returncode,2)
            self.assertEqual(output.read_bytes(),original)


if __name__=='__main__':unittest.main()
