"""辅助取值必须来自原始名称及配置，不抄答案，不按人名猜类别。"""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).resolve().parent))
from smartmap.configuration import ConfigurationError,compile_config,validate_config
from smartmap.auxiliary import AuxiliaryError,analyze_auxiliary
from smartmap.candidate_export import build_exports
from smartmap.conversion_issues import collect_issues
from test_conversion import approved_config,base_rows,stages_for
from smartmap.contracts import ConversionRequest
from smartmap.conversion import run_conversion
from xlsx_fixtures import write_xlsx
sys.path.insert(0,str(ROOT/'tools'))
from verify_conversion_run import audit_run


def config_for(status='candidate'):
    c=approved_config()
    p={'id':'aux-profile','status':status,'applicability':{'period':'202606','source_sha256':'a'*64},
       'source_field':'subject_name','delimiter':'_','strategy':'exact_two_components',
       'account_requirements':[{'target_code':'990001','fields':['customer'],'reference':'synthetic account'}],
       'dictionary':[{'field':'customer','code':'K01','name':'合成客户','reference':'synthetic dictionary'}],
       'evidence':[{'kind':'synthetic_example','reference':'仅合成测试'}],'review':None}
    if status in ['confirmed','rejected']:
        p['evidence'].append({'kind':'user_confirmation','reference':'仅合成测试'})
        p['review']={'reviewed_by':'synthetic','reviewed_on':'2026-10-28','reason':'仅临时测试'}
    c['auxiliary_profile']=p
    return c


def data(name='应收账款_合成客户'):
    rows=base_rows()
    for r in rows:r['subject']['name']=name
    return rows


class AuxiliaryTests(unittest.TestCase):
    def run_aux(self,c,rows=None):
        stages=stages_for(rows or data(),c)
        return analyze_auxiliary(stages['voucher'],c),stages

    def reference_config(self):
        c=config_for('confirmed')
        c['auxiliary_profile']['name_validation_policy']='source_name_reference_only'
        c['auxiliary_profile']['dictionary']=[]
        return c

    def test_reference_only_missing_name_can_fill_without_code(self):
        c=self.reference_config();r,_=self.run_aux(c)
        self.assertEqual(r['rows'][0]['auxiliary'],{'customer':'合成客户'})
        p=r['rows'][0]['auxiliary_candidates']['customer']
        self.assertEqual(p['dictionary_status'],'missing')
        self.assertIsNone(p['dictionary_code'])
        self.assertEqual(p['validation_basis'],'source_name_reference_only')
        self.assertEqual(len(build_exports(r,c)['candidates']),2)

    def test_reference_only_does_not_resolve_unknown_explicit_code(self):
        c=self.reference_config();rows=data()
        rows[0]['raw_fields']['customer_code']={'type':'str','value':'UNKNOWN'}
        r,_=self.run_aux(c,rows)
        self.assertFalse(r['rows'][0]['auxiliary_validation']['ready'])
        self.assertEqual(build_exports(r,c)['candidates'],[])

    def test_reference_only_does_not_suppress_ambiguity(self):
        c=self.reference_config()
        c['auxiliary_profile']['dictionary']=[
            {'field':'customer','code':code,'name':'合成客户','reference':'synthetic'} for code in ['C1','C2']]
        r,_=self.run_aux(c)
        self.assertEqual(r['rows'][0]['auxiliary'],{})
        self.assertIn('AUXILIARY_DICTIONARY_AMBIGUOUS',r['rows'][0]['auxiliary_validation']['reason_codes'])

    def test_reference_only_cross_category_does_not_copy_code(self):
        c=self.reference_config()
        c['auxiliary_profile']['dictionary']=[{'field':'supplier','code':'S1','name':'合成客户','reference':'synthetic'}]
        r,_=self.run_aux(c)
        p=r['rows'][0]['auxiliary_candidates']['customer']
        self.assertIsNone(p['dictionary_code'])
        self.assertEqual(r['rows'][0]['auxiliary'],{'customer':'合成客户'})

    def test_reference_only_still_requires_unambiguous_source(self):
        c=self.reference_config()
        for name in ['无分隔符','科目_两个_后缀']:
            r,_=self.run_aux(c,data(name))
            self.assertEqual(build_exports(r,c)['candidates'],[])

    def test_reference_only_candidate_profile_cannot_fill(self):
        c=self.reference_config();c['auxiliary_profile']['status']='candidate';c['auxiliary_profile']['review']=None
        r,_=self.run_aux(c)
        self.assertEqual(r['rows'][0]['auxiliary'],{})

    def test_reference_only_preserves_voucher_blocking(self):
        c=self.reference_config();rows=data();rows[0]['subject']['name']='不明确'
        r,_=self.run_aux(c,rows)
        self.assertEqual(build_exports(r,c,allow_partial=True)['candidates'],[])

    def test_name_policy_typo_rejected(self):
        c=self.reference_config();c['auxiliary_profile']['name_validation_policy']='anything'
        with self.assertRaises(ConfigurationError):validate_config(c)

    def test_candidate_extracts_but_does_not_fill(self):
        c=config_for();r,_=self.run_aux(c)
        self.assertEqual(r['auxiliary_analysis']['candidate_fields'],2)
        self.assertEqual(r['rows'][0]['auxiliary_candidates']['customer']['value'],'合成客户')
        self.assertEqual(r['rows'][0]['auxiliary'],{})
        self.assertFalse(r['rows'][0]['auxiliary_validation']['ready'])

    def test_confirmed_unique_match_fills(self):
        c=config_for('confirmed');r,_=self.run_aux(c)
        self.assertEqual(r['rows'][0]['auxiliary'],{'customer':'合成客户'})
        self.assertEqual(r['rows'][0]['auxiliary_candidates']['customer']['dictionary_code'],'K01')
        self.assertTrue(r['rows'][0]['auxiliary_validation']['ready'])

    def test_candidate_cannot_output_official_candidates(self):
        c=config_for();r,_=self.run_aux(c)
        self.assertEqual(build_exports(r,c)['candidates'],[])

    def test_fully_confirmed_can_output_complete_voucher(self):
        c=config_for('confirmed');r,_=self.run_aux(c);bundle=build_exports(r,c)
        self.assertEqual(len(bundle['candidates']),2)
        self.assertTrue(all(x['auxiliary']=={'customer':'合成客户'} for x in bundle['candidates']))

    def test_missing_dictionary_keeps_raw_name_no_code(self):
        c=config_for();c['auxiliary_profile']['dictionary']=[];r,_=self.run_aux(c)
        p=r['rows'][0]['auxiliary_candidates']['customer']
        self.assertEqual((p['value'],p['dictionary_status'],p['dictionary_code']),('合成客户','missing',None))

    def test_missing_one_field_blocks_whole_voucher(self):
        c=config_for('confirmed');rows=data();rows[1]['subject']['name']='应收账款_未知客户'
        r,_=self.run_aux(c,rows);bundle=build_exports(r,c)
        self.assertEqual(len(bundle['pending']),2);self.assertEqual(bundle['candidates'],[])

    def test_name_match_in_other_category_not_used(self):
        c=config_for();c['auxiliary_profile']['dictionary'][0]['field']='supplier'
        r,_=self.run_aux(c)
        self.assertEqual(r['rows'][0]['auxiliary_candidates']['customer']['dictionary_status'],'missing')

    def test_repeated_name_ambiguous_not_first_match(self):
        c=config_for();p=c['auxiliary_profile'];p['dictionary'].append({**p['dictionary'][0],'code':'K02'})
        r,_=self.run_aux(c)
        self.assertEqual(r['rows'][0]['auxiliary_candidates']['customer']['dictionary_status'],'ambiguous')

    def test_repeated_code_with_other_name_ambiguous(self):
        c=config_for();p=c['auxiliary_profile'];p['dictionary'].append({**p['dictionary'][0],'name':'另一个客户'})
        r,_=self.run_aux(c)
        self.assertEqual(r['rows'][0]['auxiliary_candidates']['customer']['dictionary_status'],'ambiguous')

    def test_multiple_separators_not_guess_last_component(self):
        c=config_for();r,_=self.run_aux(c,data('应收账款_分组_合成客户'))
        self.assertEqual(r['rows'][0]['auxiliary_candidates'],{})
        self.assertIn('AUXILIARY_SOURCE_FORMAT_AMBIGUOUS',r['rows'][0]['auxiliary_validation']['reason_codes'])

    def test_empty_suffix_not_fabricated(self):
        c=config_for();r,_=self.run_aux(c,data('应收账款_'))
        self.assertEqual(r['rows'][0]['auxiliary_candidates'],{})

    def test_no_separator_not_fabricated(self):
        c=config_for();r,_=self.run_aux(c,data('合成客户'))
        self.assertEqual(r['rows'][0]['auxiliary_candidates'],{})

    def test_trim_boundary_only(self):
        c=config_for();r,_=self.run_aux(c,data('应收账款_ 合成客户 '))
        self.assertEqual(r['rows'][0]['auxiliary_candidates']['customer']['value'],'合成客户')

    def test_multiple_required_fields_not_same_suffix(self):
        c=config_for();c['auxiliary_profile']['account_requirements'][0]['fields'].append('supplier')
        r,_=self.run_aux(c)
        self.assertEqual(r['rows'][0]['auxiliary_candidates'],{})
        self.assertIn('AUXILIARY_MULTIPLE_FIELDS_SOURCE_AMBIGUOUS',r['rows'][0]['auxiliary_validation']['reason_codes'])

    def test_different_month_does_not_extract(self):
        c=config_for('confirmed');c['auxiliary_profile']['applicability']['period']='202607'
        r,_=self.run_aux(c)
        self.assertEqual(r['rows'][0]['auxiliary_candidates'],{})
        self.assertIn('AUXILIARY_PROFILE_OUT_OF_SCOPE',r['rows'][0]['auxiliary_validation']['reason_codes'])

    def test_different_file_does_not_extract(self):
        c=config_for('confirmed');c['auxiliary_profile']['applicability']['source_sha256']='b'*64
        r,_=self.run_aux(c);self.assertEqual(r['auxiliary_analysis']['candidate_fields'],0)

    def test_confirmed_profile_alone_cannot_bypass_requirement_review(self):
        c=config_for('confirmed');aux=next(r for r in c['rules'] if r['kind']=='auxiliary');aux.update(status='candidate',review=None)
        r,_=self.run_aux(c);self.assertEqual(r['rows'][0]['auxiliary'],{})

    def test_rejected_profile_never_fills(self):
        c=config_for('rejected');r,_=self.run_aux(c)
        self.assertEqual(r['rows'][0]['auxiliary'],{})

    def test_unrequired_subject_does_not_extract_by_name(self):
        c=config_for('confirmed');c['auxiliary_profile']['account_requirements']=[]
        r,_=self.run_aux(c);self.assertEqual(r['rows'][0]['auxiliary'],{})
        self.assertTrue(r['rows'][0]['auxiliary_validation']['ready'])

    def test_global_required_field_is_not_ignored(self):
        c=config_for('confirmed');c['auxiliary_profile']['account_requirements']=[]
        next(r for r in c['rules'] if r['kind']=='auxiliary')['parameters']['required_fields']=['customer']
        r,_=self.run_aux(c);self.assertEqual(len(build_exports(r,c)['candidates']),2)

    def test_input_and_config_unchanged(self):
        c=config_for();stages=stages_for(data(),c);before=copy.deepcopy((c,stages))
        analyze_auxiliary(stages['voucher'],c)
        self.assertEqual((c,stages),before)

    def test_stale_configuration_report_rejected(self):
        c=config_for();stages=stages_for(data(),c);c['revision']+=1
        with self.assertRaises(AuxiliaryError):analyze_auxiliary(stages['voucher'],c)

    def test_missing_dictionary_fact_has_source_and_voucher(self):
        c=config_for();c['auxiliary_profile']['dictionary']=[];r,stages=self.run_aux(c)
        issues=collect_issues(build_exports(r,c),stages,c)
        issue=next(i for i in issues if i['code']=='AUXILIARY_DICTIONARY_MISSING')
        self.assertEqual(len(issue['source_record_ids']),2);self.assertEqual(len(issue['affected_record_ids']),2)

    def test_duplicate_required_target_rejected(self):
        c=config_for();p=c['auxiliary_profile'];p['account_requirements'].append(copy.deepcopy(p['account_requirements'][0]))
        with self.assertRaises(ConfigurationError):validate_config(c)

    def test_confirmation_requires_user_evidence(self):
        c=config_for();c['auxiliary_profile']['status']='confirmed'
        with self.assertRaises(ConfigurationError):validate_config(c)

    def test_unknown_profile_fields_rejected(self):
        c=config_for();c['auxiliary_profile']['voucher_whitelist']=[1]
        with self.assertRaises(ConfigurationError):validate_config(c)

    def test_wrong_source_field_rejected(self):
        c=config_for();c['auxiliary_profile']['source_field']='manual_answer'
        with self.assertRaises(ConfigurationError):validate_config(c)

    def test_confirmed_dictionary_changes_fingerprint(self):
        c=config_for('confirmed');before=compile_config(c)[1]
        c['auxiliary_profile']['dictionary'][0]['name']='另一个名称'
        self.assertNotEqual(compile_config(c)[1],before)

    def test_explicit_code_precedes_subject_suffix(self):
        c=config_for('confirmed');rows=data('应收账款_错误后缀')
        for row in rows:row['raw_fields']['customer_code']={'type':'str','value':'K01'}
        r,_=self.run_aux(c,rows)
        self.assertEqual(r['rows'][0]['auxiliary'],{'customer':'合成客户'})
        self.assertEqual(r['rows'][0]['auxiliary_candidates']['customer']['source_field'],'customer_code')

    def test_explicit_name_precedes_suffix(self):
        c=config_for('confirmed');rows=data('应收账款_错误后缀')
        for row in rows:row['raw_fields']['customer']={'type':'str','value':'合成客户'}
        r,_=self.run_aux(c,rows);self.assertEqual(r['rows'][0]['auxiliary'],{'customer':'合成客户'})

    def test_bad_explicit_code_not_hidden_by_valid_suffix(self):
        c=config_for('confirmed');rows=data()
        for row in rows:row['raw_fields']['customer_code']={'type':'str','value':'UNKNOWN'}
        r,_=self.run_aux(c,rows)
        self.assertEqual(r['rows'][0]['auxiliary'],{})
        self.assertIn('AUXILIARY_DICTIONARY_MISSING',r['rows'][0]['auxiliary_validation']['reason_codes'])

    def test_explicit_code_name_conflict_blocks(self):
        c=config_for('confirmed');rows=data()
        for row in rows:row['raw_fields'].update(customer_code={'type':'str','value':'K01'},customer={'type':'str','value':'另一客户'})
        r,_=self.run_aux(c,rows)
        self.assertEqual(r['rows'][0]['auxiliary'],{})
        self.assertIn('AUXILIARY_EXPLICIT_VALUE_CONFLICT',r['rows'][0]['auxiliary_validation']['reason_codes'])

    def test_unique_code_can_resolve_same_name(self):
        c=config_for('confirmed');p=c['auxiliary_profile'];p['dictionary'].append({**p['dictionary'][0],'code':'K02'})
        rows=data()
        for row in rows:row['raw_fields']['customer_code']={'type':'str','value':'K01'}
        r,_=self.run_aux(c,rows);self.assertTrue(r['rows'][0]['auxiliary_validation']['ready'])

    def test_suffix_strategy_can_be_disabled(self):
        c=config_for('confirmed');c['auxiliary_profile']['source_priority']=['explicit_code','explicit_name']
        r,_=self.run_aux(c)
        self.assertEqual(r['auxiliary_analysis']['candidate_fields'],0)
        self.assertIn('AUXILIARY_SOURCE_MISSING',r['rows'][0]['auxiliary_validation']['reason_codes'])

    def test_priority_cannot_put_fallback_first(self):
        c=config_for();c['auxiliary_profile']['source_priority']=['subject_name','explicit_code']
        with self.assertRaises(ConfigurationError):validate_config(c)

    def test_explicit_fields_can_support_multiple_categories(self):
        c=config_for('confirmed');p=c['auxiliary_profile'];p['account_requirements'][0]['fields'].append('supplier')
        p['dictionary'].append({'field':'supplier','code':'S01','name':'合成供应商','reference':'synthetic'})
        rows=data('无分隔符')
        for row in rows:row['raw_fields'].update(customer={'type':'str','value':'合成客户'},supplier_code={'type':'str','value':'S01'})
        r,_=self.run_aux(c,rows)
        self.assertEqual(r['rows'][0]['auxiliary'],{'customer':'合成客户','supplier':'合成供应商'})

    def test_bad_boolean_explicit_source_does_not_fallback(self):
        c=config_for('confirmed');rows=data()
        for row in rows:row['raw_fields']['customer_code']={'type':'bool','value':'True'}
        r,_=self.run_aux(c,rows);self.assertEqual(r['rows'][0]['auxiliary'],{})

    def mixed(self):
        rows=data()+data('应收账款_未知客户')
        for row in rows[2:]:row['voucher']['number']=2
        return rows

    def test_default_batch_block_even_when_one_voucher_ready(self):
        c=config_for('confirmed');r,_=self.run_aux(c,self.mixed());b=build_exports(r,c)
        self.assertEqual(b['candidates'],[]);self.assertEqual(len(b['pending']),4)
        self.assertEqual(len(b['batch_gate']['eligible_record_ids']),2)

    def test_partial_opt_in_keeps_complete_normal_voucher_only(self):
        c=config_for('confirmed');r,_=self.run_aux(c,self.mixed());b=build_exports(r,c,allow_partial=True)
        self.assertEqual(len(b['candidates']),2);self.assertEqual(len(b['pending']),2)
        self.assertEqual({x['voucher_number'] for x in b['candidates']},{1})
        self.assertTrue(b['batch_gate']['partial_export'])

    def test_partial_selection_must_be_boolean(self):
        c=config_for('confirmed');r,_=self.run_aux(c)
        with self.assertRaises(ValueError):build_exports(r,c,allow_partial='yes')


class AuxiliaryRuntimeTests(unittest.TestCase):
    def run_job(self,root,status='candidate',missing=False,explicit=False,mixed=False,allow_partial=False):
        source=root/'source.xlsx';config_path=root/'config.json';output=root/'run'
        header=['日期','账期','科目名称','凭证字号','摘要','借方','贷方']
        rows=[['2026-06-03','202606','9000.01应收账款_合成客户','记1','合成',10,None],
              ['2026-06-03','202606','9000.01应收账款_合成客户','记1','合成',None,10]]
        if explicit:
            header.append('客户编码')
            for row in rows:row[2]='9000.01无后缀';row.append('K01')
        if mixed:
            rows += [['2026-06-03','202606','9000.01应收账款_未知客户','记2','合成',10,None],
                     ['2026-06-03','202606','9000.01应收账款_未知客户','记2','合成',None,10]]
        write_xlsx(source,[('原始',[header]+rows)])
        c=config_for(status);c['auxiliary_profile']['applicability']['source_sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
        if missing:c['auxiliary_profile']['dictionary']=[]
        config_path.write_text(json.dumps(c,ensure_ascii=False),encoding='utf-8')
        return run_conversion(ConversionRequest(source,config_path,output,allow_partial=allow_partial)),output

    def test_candidate_real_runtime_has_review_files_and_passes_independent_audit(self):
        with tempfile.TemporaryDirectory() as d:
            m,out=self.run_job(Path(d))
            self.assertEqual(m['exit_code'],3)
            self.assertTrue((out/'辅助核算取值核验.csv').is_file())
            self.assertTrue((out/'stage_auxiliary.json').is_file())
            self.assertTrue(audit_run(out)['all_passed'])

    def test_confirmed_real_runtime_outputs_two_complete_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            m,out=self.run_job(Path(d),'confirmed')
            self.assertEqual(m['exit_code'],0)
            self.assertEqual(m['row_counts']['included'],2)
            self.assertTrue(audit_run(out)['all_passed'])

    def test_missing_dictionary_real_runtime_blocks_and_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            m,out=self.run_job(Path(d),'confirmed',True)
            self.assertEqual((m['exit_code'],m['row_counts']['needs_review']),(3,2))
            self.assertTrue(audit_run(out)['all_passed'])

    def test_explicit_column_is_read_from_excel_and_audited(self):
        with tempfile.TemporaryDirectory() as d:
            m,out=self.run_job(Path(d),'confirmed',explicit=True)
            self.assertEqual(m['row_counts']['included'],2)
            self.assertTrue(audit_run(out)['all_passed'])

    def test_runtime_default_never_partially_exports(self):
        with tempfile.TemporaryDirectory() as d:
            m,out=self.run_job(Path(d),'confirmed',mixed=True)
            self.assertEqual((m['row_counts']['included'],m['eligible_core_candidate_count']),(0,2))
            self.assertFalse(m['batch_complete']);self.assertFalse(m['partial_export'])
            self.assertTrue(audit_run(out)['all_passed'])

    def test_runtime_explicit_partial_still_review_required(self):
        with tempfile.TemporaryDirectory() as d:
            m,out=self.run_job(Path(d),'confirmed',mixed=True,allow_partial=True)
            self.assertEqual((m['exit_code'],m['row_counts']['included']),(3,2))
            self.assertFalse(m['batch_complete']);self.assertTrue(m['partial_export'])
            self.assertTrue(audit_run(out)['all_passed'])


if __name__=='__main__':unittest.main()
