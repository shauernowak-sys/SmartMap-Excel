"""T09映射、整凭证规则和阶段组合测试；确认仅用于合成数据。"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_hierarchy import row, report
from xlsx_fixtures import write_xlsx
from smartmap.account_mapping import MappingIndex
from smartmap.configuration import ConfigurationError, load_config, validate_config
from smartmap.hierarchy import analyze_hierarchy
from smartmap.mapping_filter import MappingFilterError, map_and_filter


class MappingFilterTests(unittest.TestCase):
    def setUp(self):self.config,_=load_config(ROOT/'config/example.json')

    def review(self,item,status='confirmed'):
        item['status']=status
        item['evidence'].append({'kind':'user_confirmation','reference':'仅合成测试，非企业业务批准'})
        item['review']={'reviewed_by':'synthetic-test','reviewed_on':'2026-10-28','reason':'合成状态测试'}
        return item

    def mapping(self,code,target='990001',status='confirmed',name='合成内部科目'):
        item={'id':f'map-{len(self.config["mappings"])}','source_code':code,'target_code':target,'target_name':name,
            'status':'candidate','evidence':[{'kind':'synthetic_example','reference':'合成数据'}],'review':None}
        if status!='candidate':self.review(item,status)
        self.config['mappings'].append(item);return item

    def filter(self,status='confirmed',**conditions):
        item={'id':f'filter-{len(self.config["rules"])}','kind':'voucher_filter','policy':'exclude_voucher',
            'status':'candidate','parameters':{'match_scope':'all_lines',**conditions},
            'evidence':[{'kind':'synthetic_example','reference':'合成筛选'}],'review':None}
        if status!='candidate':self.review(item,status)
        self.config['rules'].append(item);return item

    def hierarchy(self):self.review(next(r for r in self.config['rules'] if r['kind']=='hierarchy'))

    def run_stage(self,rows):
        upstream=analyze_hierarchy(report(rows,self.config),self.config)
        return map_and_filter(upstream,self.config)

    def test_confirmed_mapping_only_fills_target(self):
        approved=self.mapping('1122.01')
        result=self.run_stage([row('1122.01',100)])
        self.assertEqual(result['rows'][0]['account_mapping']['mapping_id'],approved['id'])
        self.assertEqual(result['rows'][0]['account_mapping']['target_code'],'990001')
        self.assertEqual(result['row_counts']['retained'],1)

    def test_candidate_does_not_fill_target(self):
        self.mapping('1122.01',status='candidate')
        result=self.run_stage([row('1122.01',100)])
        self.assertEqual(result['mapping_counts']['unconfirmed'],1)
        self.assertIsNone(result['rows'][0]['account_mapping']['target_code'])
        self.assertEqual(result['row_counts']['needs_review'],1)

    def test_unknown_account_is_retained_for_review(self):
        result=self.run_stage([row('9999.01',100)])
        self.assertEqual(result['mapping_counts']['unknown'],1)
        self.assertEqual(result['rows'][0]['debit']['value'],'100')
        self.assertEqual(result['row_counts']['excluded'],0)

    def test_rejected_mapping_not_used(self):
        self.mapping('1122.01',status='rejected')
        result=self.run_stage([row('1122.01',100)])
        self.assertEqual(result['mapping_counts']['rejected'],1)
        self.assertIsNone(result['rows'][0]['account_mapping']['target_name'])

    def test_conflicting_candidates_not_used(self):
        self.mapping('1122.01',target='990001',status='candidate');self.mapping('1122.01',target='990002',status='candidate')
        result=self.run_stage([row('1122.01',100)])
        self.assertEqual(result['mapping_counts']['candidate_conflict'],1)
        self.assertIsNone(result['rows'][0]['account_mapping']['target_code'])

    def test_confirmed_mapping_not_overridden_by_new_candidate(self):
        self.mapping('1122.01',target='990001');self.mapping('1122.01',target='990002',status='candidate')
        result=self.run_stage([row('1122.01',100)])
        self.assertEqual(result['mapping_counts']['matched'],1)
        self.assertEqual(result['rows'][0]['account_mapping']['target_code'],'990001')

    def test_many_external_codes_to_one_internal_code_allowed(self):
        self.mapping('1122.01');self.mapping('1122.02')
        result=self.run_stage([row('1122.01',100),row('1122.02',credit=100)])
        self.assertEqual(result['mapping_counts']['matched'],2)

    def test_same_target_code_with_conflicting_confirmed_names_rejected(self):
        self.mapping('1122.01',name='A');self.mapping('1122.02',name='B')
        with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_duplicate_confirmed_source_rejected(self):
        self.mapping('1122.01');self.mapping('1122.01',target='990002')
        with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_leading_zeroes_and_exact_matching(self):
        self.mapping('0010.01',target='000101')
        result=self.run_stage([row('0010.01',100),row('0010.01.02',100,number=2)])
        self.assertEqual(result['rows'][0]['account_mapping']['target_code'],'000101')
        self.assertEqual(result['rows'][1]['account_mapping']['status'],'unknown')

    def test_mapping_index_does_not_expose_mutable_results(self):
        self.mapping('1122.01');index=MappingIndex(self.config)
        first=index.resolve('1122.01');first['target_name']='tampered'
        self.assertEqual(index.resolve('1122.01')['target_name'],'合成内部科目')
        with self.assertRaises(ConfigurationError):index.resolve(1122)

    def test_unresolved_one_line_quarantines_other_mapped_lines(self):
        self.mapping('1122.01')
        result=self.run_stage([row('1122.01',100),row('1002',credit=100)])
        self.assertEqual(result['mapping_counts']['matched'],1)
        self.assertEqual(result['row_counts']['retained'],0)
        self.assertEqual(result['row_counts']['needs_review'],2)

    def test_other_vouchers_not_quarantined_by_mapping_gap(self):
        self.mapping('1122.01')
        result=self.run_stage([row('1122.01',100,number=1),row('9999',100,number=2)])
        self.assertEqual(result['rows'][0]['mapping_filter']['decision'],'retained')
        self.assertEqual(result['rows'][1]['mapping_filter']['decision'],'needs_review')

    def test_candidate_filter_full_match_does_not_exclude(self):
        self.mapping('1122.01');self.filter(status='candidate',summary_equals='合成业务')
        result=self.run_stage([row('1122.01',100)])
        self.assertEqual(result['filter_counts']['candidate_full_match_vouchers'],1)
        self.assertEqual(result['row_counts']['excluded'],0)
        self.assertEqual(result['row_counts']['needs_review'],1)

    def test_confirmed_filter_excludes_entire_voucher_without_mapping(self):
        self.filter(summary_equals='合成业务')
        result=self.run_stage([row('1122.01',100),row('1002',credit=100)])
        self.assertEqual(result['row_counts']['excluded'],2)
        self.assertEqual(result['mapping_counts']['not_evaluated'],2)
        self.assertEqual(result['exclusion_counts']['voucher_filter'],2)
        self.assertEqual(len(result['filter_groups'][0]['member_record_ids']),2)

    def test_rejected_filter_does_not_block_mapped_rows(self):
        self.mapping('1122.01');self.filter(status='rejected',summary_equals='合成业务')
        result=self.run_stage([row('1122.01',100)])
        self.assertEqual(result['row_counts']['retained'],1)
        self.assertEqual(result['filter_groups'][0]['evaluations'][0]['match'],'not_evaluated')

    def test_confirmed_partial_match_never_excludes_half_voucher(self):
        self.mapping('1122.01');self.mapping('1002');self.filter(summary_equals='A')
        result=self.run_stage([row('1122.01',100,summary='A'),row('1002',credit=100,summary='B')])
        self.assertEqual(result['row_counts']['excluded'],0)
        self.assertEqual(result['row_counts']['needs_review'],2)
        self.assertEqual(result['filter_counts']['partial_match_vouchers'],1)

    def test_two_partial_rules_cannot_union_into_full_match(self):
        self.filter(summary_equals='A');self.filter(summary_equals='B')
        result=self.run_stage([row('1122.01',100,summary='A'),row('1002',credit=100,summary='B')])
        self.assertEqual(result['row_counts']['excluded'],0)
        self.assertEqual(len(result['filter_groups'][0]['partial_rule_ids']),2)

    def test_full_confirmed_rule_is_sufficient_despite_other_partial_rule(self):
        full=self.filter(voucher_category_equals='记');self.filter(summary_equals='A')
        result=self.run_stage([row('1122.01',100,summary='A'),row('1002',credit=100,summary='B')])
        self.assertEqual(result['row_counts']['excluded'],2)
        self.assertEqual(result['filter_groups'][0]['confirmed_full_rule_ids'],[full['id']])

    def test_all_full_rule_ids_recorded(self):
        a=self.filter(summary_equals='合成业务');b=self.filter(voucher_category_equals='记')
        result=self.run_stage([row('1122.01',100)])
        self.assertEqual(set(result['rows'][0]['voucher_filter']['matched_rule_ids']),{a['id'],b['id']})

    def test_multiple_conditions_use_and(self):
        self.filter(summary_equals='合成业务',voucher_category_equals='记')
        result=self.run_stage([row('1122.01',100,category='收')])
        self.assertEqual(result['filter_counts']['excluded'],0)
        self.assertEqual(result['filter_groups'][0]['evaluations'][0]['match'],'none')

    def test_summary_matching_is_exact_not_contains(self):
        self.filter(summary_equals='合成')
        result=self.run_stage([row('1122.01',100)])
        self.assertEqual(result['filter_counts']['excluded'],0)

    def test_same_number_different_categories_are_separate(self):
        self.mapping('1122.01');self.filter(voucher_category_equals='记')
        result=self.run_stage([row('1122.01',100,category='记'),row('1122.01',100,category='收')])
        self.assertEqual(result['filter_counts']['voucher_groups'],2)
        self.assertEqual(result['rows'][0]['mapping_filter']['decision'],'excluded')
        self.assertEqual(result['rows'][1]['mapping_filter']['decision'],'retained')

    def test_filter_uses_all_raw_rows_including_hierarchy_parents(self):
        self.hierarchy();self.filter(summary_equals='合成业务')
        result=self.run_stage([row('1122',100),row('1122.01',100),row('1002',credit=100)])
        self.assertEqual(result['row_counts']['excluded'],3)
        self.assertEqual(len(result['filter_groups'][0]['member_record_ids']),3)
        self.assertEqual(result['exclusion_counts'],{'voucher_filter':3,'hierarchy':0})

    def test_confirmed_scope_filter_does_not_need_pending_hierarchy_approval(self):
        self.filter(summary_equals='合成业务')
        result=self.run_stage([row('1122',100),row('1122.01',100)])
        self.assertEqual(result['row_counts']['excluded'],2)

    def test_hierarchy_structural_error_blocks_filter_exclusion(self):
        self.filter(summary_equals='合成业务')
        result=self.run_stage([row('1122',150),row('1122.01',100)])
        self.assertEqual(result['row_counts']['excluded'],0)
        self.assertEqual(result['row_counts']['needs_review'],2)

    def test_upstream_amount_error_blocks_filter_exclusion(self):
        self.filter(summary_equals='合成业务')
        bad=row('1002','unknown');bad.update(parse_state='needs_review',issues=[{'code':'INVALID_AMOUNT'}])
        result=self.run_stage([row('1122.01',100),bad])
        self.assertEqual(result['row_counts']['excluded'],0)

    def test_unattributed_error_blocks_other_vouchers(self):
        self.filter(summary_equals='合成业务')
        bad=row('1002',100);bad.update(voucher=None,parse_state='needs_review',issues=[{'code':'MISSING_VOUCHER'}])
        result=self.run_stage([row('1122.01',100),bad])
        self.assertEqual(result['row_counts']['excluded'],0)

    def test_confirmed_hierarchy_exclusions_need_no_parent_mapping(self):
        self.hierarchy();self.mapping('1122.01');self.mapping('1002')
        result=self.run_stage([row('1122',100),row('1122.01',100),row('1002',credit=100)])
        self.assertEqual(result['row_counts']['excluded'],1)
        self.assertEqual(result['row_counts']['retained'],2)
        self.assertEqual(result['mapping_counts']['matched'],2)
        self.assertEqual(result['rows'][0]['account_mapping']['status'],'not_evaluated')

    def test_pending_hierarchy_quarantines_mapped_children(self):
        self.mapping('1122.01');self.mapping('1002')
        result=self.run_stage([row('1122',100),row('1122.01',100),row('1002',credit=100)])
        self.assertEqual(result['mapping_counts']['matched'],2)
        self.assertEqual(result['row_counts']['retained'],0)
        self.assertEqual(result['row_counts']['needs_review'],3)

    def test_identical_rows_keep_occurrence_count(self):
        self.mapping('1122.01')
        result=self.run_stage([row('1122.01',100),row('1122.01',100)])
        self.assertEqual(result['row_counts']['retained'],2)
        self.assertEqual(len({r['record_id'] for r in result['rows']}),2)

    def test_original_fields_amounts_and_input_report_unchanged(self):
        self.mapping('1122.01')
        upstream=analyze_hierarchy(report([row('1122.01','-62.11',0)],self.config),self.config);before=copy.deepcopy(upstream)
        result=map_and_filter(upstream,self.config)
        self.assertEqual(upstream,before)
        old=upstream['rows'][0];new=result['rows'][0]
        self.assertEqual({k:v for k,v in new.items() if k not in ['account_mapping','voucher_filter','mapping_filter']},old)

    def test_unknown_voucher_number_has_no_special_case(self):
        self.mapping('1122.01')
        result=self.run_stage([row('1122.01',100,number=74,summary='结转损益')])
        self.assertEqual(result['row_counts']['retained'],1)
        self.assertEqual(result['filter_counts']['excluded'],0)

    def test_config_change_requires_fresh_pipeline(self):
        upstream=analyze_hierarchy(report([row('1122.01',100)],self.config),self.config)
        self.mapping('1122.01')
        with self.assertRaises(MappingFilterError) as caught:map_and_filter(upstream,self.config)
        self.assertEqual(caught.exception.code,'CONFIG_MISMATCH')

    def test_upstream_failure_duplicate_id_and_invalid_exclusion_rejected(self):
        upstream=analyze_hierarchy(report([row('1122.01',100),row('1002',credit=100)],self.config),self.config)
        bad=copy.deepcopy(upstream);bad['analysis_ok']=False
        with self.assertRaises(MappingFilterError):map_and_filter(bad,self.config)
        bad=copy.deepcopy(upstream);bad['rows'][1]['record_id']=bad['rows'][0]['record_id']
        with self.assertRaises(MappingFilterError):map_and_filter(bad,self.config)
        bad=copy.deepcopy(upstream);bad['rows'][0]['hierarchy']['decision']='excluded'
        with self.assertRaises(MappingFilterError):map_and_filter(bad,self.config)

    def test_nonbusiness_passthrough_does_not_need_mapping(self):
        item=row('1002');item.update(parse_state='non_business',reason_code='NON_BUSINESS_OPENING',voucher=None)
        result=self.run_stage([item,row('1122.01',100)])
        self.assertEqual(result['rows'][0]['mapping_filter']['decision'],'non_business')
        self.assertEqual(result['rows'][0]['account_mapping']['status'],'not_evaluated')

    def test_forged_upstream_state_counts_are_rejected(self):
        upstream=analyze_hierarchy(report([row('1122.01',100)],self.config),self.config)
        upstream['row_counts']['retained']=0
        with self.assertRaises(MappingFilterError):map_and_filter(upstream,self.config)

    def test_forged_source_state_and_period_are_rejected(self):
        upstream=analyze_hierarchy(report([row('1122.01',100)],self.config),self.config)
        for field,value in [('parse_state','non_business'),('period','202607')]:
            bad=copy.deepcopy(upstream);bad['rows'][0][field]=value
            with self.subTest(field=field),self.assertRaises(MappingFilterError):map_and_filter(bad,self.config)


class MappingCliTests(unittest.TestCase):
    def test_cli_confirmed_filter_excludes_complete_raw_voucher(self):
        with tempfile.TemporaryDirectory(prefix='smartmap-filter-confirmed-') as folder:
            source=Path(folder)/'source.xlsx';output=Path(folder)/'out';config_path=Path(folder)/'config.json'
            config,_=load_config(ROOT/'config/example.json')
            config['rules'].append({'id':'exclude-synthetic','kind':'voucher_filter','status':'confirmed','policy':'exclude_voucher',
                'parameters':{'summary_equals':'合成业务','match_scope':'all_lines'},
                'evidence':[{'kind':'user_confirmation','reference':'仅临时合成测试，不是企业批准'}],
                'review':{'reviewed_by':'synthetic-test','reviewed_on':'2026-10-28','reason':'测试整张原始凭证排除'}})
            config_path.write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
            header=['日期','账期','科目名称','凭证字号','摘要','借方','贷方']
            data=[['2026-06-30','202606',code,'记1','合成业务',d,c] for code,d,c in [('9000父级',100,None),('9000.01子级',100,None),('1002示例',None,100)]]
            write_xlsx(source,[('原始',[header]+data)])
            r=subprocess.run([sys.executable,'-B','-m','smartmap','map-and-filter','--source',str(source),
                '--config',str(config_path),'--out',str(output)],cwd=ROOT,capture_output=True)
            self.assertEqual(r.returncode,0,r.stderr)
            result=json.loads((output/'mapping_filter_report.json').read_text(encoding='utf-8'))
            self.assertEqual(result['row_counts']['excluded'],3)
            self.assertEqual(result['mapping_counts']['not_evaluated'],3)
            self.assertEqual(len(result['rows']),3)

    def test_cli_pending_mapping_and_overwrite_protection(self):
        with tempfile.TemporaryDirectory(prefix='smartmap-map-') as folder:
            source=Path(folder)/'source.xlsx';output=Path(folder)/'out'
            header=['日期','账期','科目名称','凭证字号','摘要','借方','贷方']
            write_xlsx(source,[('原始',[header,['2026-06-30','202606','9000.01虚构科目','记1','合成业务',100,None]])])
            command=[sys.executable,'-B','-m','smartmap','map-and-filter','--source',str(source),'--config',str(ROOT/'config/example.json'),'--out',str(output)]
            r=subprocess.run(command,cwd=ROOT,capture_output=True)
            self.assertEqual(r.returncode,3,r.stderr)
            path=output/'mapping_filter_report.json';before=path.read_bytes();result=json.loads(before)
            self.assertEqual(result['mapping_counts']['unconfirmed'],1)
            self.assertTrue((output/'unmatched_accounts.csv').is_file())
            self.assertEqual(subprocess.run(command,cwd=ROOT,capture_output=True).returncode,2)
            self.assertEqual(path.read_bytes(),before)

    def test_cli_confirmed_synthetic_mapping(self):
        with tempfile.TemporaryDirectory(prefix='smartmap-map-confirmed-') as folder:
            source=Path(folder)/'source.xlsx';output=Path(folder)/'out';config_path=Path(folder)/'config.json'
            config,_=load_config(ROOT/'config/example.json');m=config['mappings'][0]
            m.update(status='confirmed',review={'reviewed_by':'synthetic-test','reviewed_on':'2026-10-28','reason':'仅合成测试'})
            m['evidence'].append({'kind':'user_confirmation','reference':'不用于真实企业数据'})
            config_path.write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
            header=['日期','账期','科目名称','凭证字号','摘要','借方','贷方']
            write_xlsx(source,[('原始',[header,['2026-06-30','202606','9000.01虚构科目','记1','合成业务',100,None]])])
            r=subprocess.run([sys.executable,'-B','-m','smartmap','map-and-filter','--source',str(source),
                '--config',str(config_path),'--out',str(output)],cwd=ROOT,capture_output=True)
            self.assertEqual(r.returncode,0,r.stderr)
            result=json.loads((output/'mapping_filter_report.json').read_text(encoding='utf-8'))
            self.assertEqual(result['rows'][0]['account_mapping']['target_code'],'990001')
            self.assertFalse(result['ready_for_system_import'])


if __name__=='__main__':unittest.main()
