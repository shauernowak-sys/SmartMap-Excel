"""本案例映射批准不能用于其他账期、文件或缺失上下文的查询。"""
import copy
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).resolve().parent))
from smartmap.account_mapping import MappingIndex
from smartmap.configuration import ConfigurationError, compile_config, load_config, resolve_mapping, validate_config
from smartmap.candidate_export import ExportError, build_exports
from smartmap.conversion_issues import collect_issues
from test_conversion import base_rows, stages_for, approved_config


class MappingScopeTests(unittest.TestCase):
    def setUp(self):
        self.config=approved_config()
        self.mapping=self.config['mappings'][0]
        self.mapping['applicability']={'period':'202606','source_sha256':'a'*64}

    def resolve(self,**context):
        return MappingIndex(self.config).resolve(self.mapping['source_code'],**context)

    def test_matching_context_uses_approved_target(self):
        r=self.resolve(period='202606',source_sha256='a'*64)
        self.assertEqual((r['status'],r['target_code']),('matched',self.mapping['target_code']))

    def test_different_month_blanks_target(self):
        r=self.resolve(period='202607',source_sha256='a'*64)
        self.assertEqual(r['status'],'out_of_scope');self.assertIsNone(r['target_code'])

    def test_different_file_blanks_target(self):
        r=self.resolve(period='202606',source_sha256='b'*64)
        self.assertEqual(r['status'],'out_of_scope');self.assertIsNone(r['mapping_id'])

    def test_missing_context_does_not_bypass(self):
        self.assertEqual(self.resolve()['status'],'out_of_scope')

    def test_partial_context_does_not_bypass(self):
        self.assertEqual(self.resolve(period='202606')['status'],'out_of_scope')
        self.assertEqual(self.resolve(source_sha256='a'*64)['status'],'out_of_scope')

    def test_single_lookup_also_enforces_scope(self):
        r=resolve_mapping(self.config,self.mapping['source_code'])
        self.assertEqual(r['status'],'out_of_scope');self.assertIsNone(r['mapping'])
        self.assertEqual(resolve_mapping(self.config,self.mapping['source_code'],period='202606',source_sha256='a'*64)['status'],'matched')

    def test_unscoped_existing_mapping_backward_compatible(self):
        del self.mapping['applicability']
        self.assertEqual(self.resolve()['status'],'matched')

    def test_out_of_scope_does_not_fall_back_to_candidate(self):
        alternative=copy.deepcopy(self.mapping);alternative.update(id='alternative',status='candidate',review=None)
        self.config['mappings'].append(alternative)
        self.assertEqual(self.resolve()['status'],'out_of_scope')

    def test_scoped_candidate_still_unconfirmed(self):
        self.mapping.update(status='candidate',review=None)
        self.assertEqual(self.resolve(period='202606',source_sha256='a'*64)['status'],'unconfirmed')

    def test_scoped_rejected_mapping_not_active(self):
        self.mapping['status']='rejected'
        self.assertEqual(self.resolve(period='202606',source_sha256='a'*64)['status'],'rejected')

    def test_scope_strict_fields(self):
        for value in [{}, {'period':'202613','source_sha256':'a'*64}, {'period':'202606','source_sha256':'bad'},
                      {'period':'202606','source_sha256':'a'*64,'voucher_numbers':[1]}]:
            self.mapping['applicability']=value
            with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_scope_fingerprint_changes(self):
        first=compile_config(self.config)[1];self.mapping['applicability']['period']='202607'
        self.assertNotEqual(compile_config(self.config)[1],first)

    def test_full_pipeline_allows_only_matching_source(self):
        stages=stages_for(base_rows(),self.config)
        self.assertEqual(len(build_exports(stages['voucher'],self.config)['candidates']),2)

    def test_full_pipeline_out_of_scope_is_traceable(self):
        self.mapping['applicability']['source_sha256']='b'*64
        stages=stages_for(base_rows(),self.config);bundle=build_exports(stages['voucher'],self.config)
        issues=collect_issues(bundle,stages,self.config)
        self.assertEqual(bundle['candidates'],[])
        self.assertEqual(stages['mapping']['mapping_counts']['out_of_scope'],2)
        self.assertTrue(all(r['target_subject_code'] is None for r in bundle['diagnostic_candidates']))
        issue=next(i for i in issues if i['code']=='ACCOUNT_MAPPING_OUT_OF_SCOPE')
        self.assertEqual(issue['rule_ids'],[self.mapping['id']])

    def test_export_rejects_forged_out_of_scope_match(self):
        self.mapping['applicability']['source_sha256']='b'*64
        stages=stages_for(base_rows(),self.config)
        row=stages['voucher']['rows'][0]
        row['account_mapping'].update(status='matched',mapping_id=self.mapping['id'],target_code=self.mapping['target_code'],target_name=self.mapping['target_name'])
        with self.assertRaises(ExportError):build_exports(stages['voucher'],self.config)

    def test_config_and_result_independent(self):
        before=copy.deepcopy(self.config)
        r=self.resolve(period='202606',source_sha256='a'*64);r['target_name']='changed'
        self.assertEqual(self.config,before)


if __name__=='__main__':unittest.main()
