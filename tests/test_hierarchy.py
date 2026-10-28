"""T08层级选择的合成业务测试；确认记录仅存在于测试内存或临时目录。"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).resolve().parent))
from smartmap.configuration import compile_config, load_config
from smartmap.hierarchy import HierarchyError, analyze_hierarchy
from smartmap.values import parse_money
from xlsx_fixtures import write_xlsx


def row(code,debit=None,credit=None,number=1,summary='合成业务',category='记',period='202606'):
    return {'parse_state':'parsed','issues':[],'period':period,'summary':summary,'date':'2026-06-30',
        'voucher':{'category':category,'number':number},'subject':{'code':code,'segments':code.split('.'),'name':'虚构科目'},
        'debit':parse_money(debit),'credit':parse_money(credit),'raw_fields':{'source':'synthetic'},'raw_cells':[]}


def report(rows,config):
    rows=copy.deepcopy(rows)
    for index,r in enumerate(rows,2):
        r['record_id']=f'synthetic-row-{index}'
        r['provenance']={'input_sha256':'a'*64,'sheet':'合成原始','excel_row':index}
    return {'mode':'source_parse_only','read_ok':True,'period':'202606','input_sha256':'a'*64,'source_file':'synthetic.xlsx','source_sheet':'合成原始',
        'config_id':config['config_id'],'config_revision':config['revision'],'effective_config_sha256':compile_config(config)[1],
        'row_counts':{'total':len(rows)},'rows':rows}


class HierarchyTests(unittest.TestCase):
    def setUp(self):self.config,_=load_config(ROOT/'config/example.json')

    def confirm(self):
        rule=next(r for r in self.config['rules'] if r['kind']=='hierarchy')
        rule['status']='confirmed';rule['evidence'].append({'kind':'user_confirmation','reference':'仅合成测试确认，不用于真实业务'})
        rule['review']={'reviewed_by':'synthetic-test','reviewed_on':'2026-10-28','reason':'测试已确认状态的执行行为'}
        return rule

    def analyze(self,rows):return analyze_hierarchy(report(rows,self.config),self.config)

    def test_candidate_finds_evidence_but_excludes_nothing(self):
        result=self.analyze([row('1122',100),row('1122.01',100)])
        self.assertEqual(result['row_counts'],{'total':2,'retained':1,'excluded':0,'needs_review':1,'non_business':0})
        self.assertEqual(result['group_counts']['verified_structure'],1)
        self.assertFalse(result['ready_for_next_stage'])

    def test_confirmed_policy_excludes_only_parent_with_evidence(self):
        self.confirm();result=self.analyze([row('1122',100),row('1122.01',100)])
        self.assertEqual(result['row_counts']['excluded'],1)
        parent=result['rows'][0]['hierarchy']
        self.assertEqual(parent['reason_codes'],['PARENT_CHILD_GROUP_VERIFIED'])
        self.assertEqual(parent['parent_evidence_ids'],[result['groups'][0]['evidence_id']])
        self.assertEqual(result['groups'][0]['parent_excel_rows'],[2])

    def test_rejected_policy_never_excludes(self):
        self.confirm()['status']='rejected'
        result=self.analyze([row('1122',100),row('1122.01',100)])
        self.assertEqual(result['row_counts']['excluded'],0)
        self.assertIn('HIERARCHY_POLICY_REJECTED',result['rows'][0]['hierarchy']['reason_codes'])

    def test_no_child_keeps_direct_posting_parent(self):
        self.confirm();result=self.analyze([row('1122',100)])
        self.assertEqual(result['row_counts']['retained'],1)
        self.assertEqual(result['group_counts']['total'],0)

    def test_children_in_different_voucher_do_not_remove_parent(self):
        self.confirm();result=self.analyze([row('1122',100,number=1),row('1122.01',100,number=2)])
        self.assertEqual(result['row_counts']['retained'],2)
        self.assertEqual(result['group_counts']['total'],0)

    def test_same_number_different_categories_do_not_mix(self):
        self.confirm();result=self.analyze([row('1122',100,category='记'),row('1122.01',100,category='收')])
        self.assertEqual(result['row_counts']['retained'],2)

    def test_three_levels_count_only_nearest_children(self):
        self.confirm();result=self.analyze([row('1122',100),row('1122.01',100),row('1122.01.01',100)])
        self.assertEqual(result['row_counts']['excluded'],2)
        top=next(g for g in result['groups'] if g['parent_code']=='1122')
        self.assertEqual(top['nearest_child_codes'],['1122.01'])
        self.assertEqual(top['child_totals']['debit'],'100')

    def test_missing_intermediate_level_is_not_invented(self):
        self.confirm();result=self.analyze([row('1122',100),row('1122.01.06',100)])
        self.assertEqual(result['groups'][0]['nearest_child_codes'],['1122.01.06'])
        self.assertEqual(result['row_counts']['excluded'],1)

    def test_similar_textual_prefix_is_not_a_parent(self):
        self.confirm();result=self.analyze([row('1122.01',100),row('1122.010',100)])
        self.assertEqual(result['group_counts']['total'],0)
        self.assertEqual(result['row_counts']['retained'],2)

    def test_equal_amount_siblings_both_retained(self):
        self.confirm();result=self.analyze([row('1122',200),row('1122.01',100),row('1122.02',100)])
        self.assertEqual(result['row_counts']['retained'],2)
        self.assertEqual(result['row_counts']['excluded'],1)

    def test_identical_child_occurrences_do_not_disappear(self):
        self.confirm();result=self.analyze([row('1122',200),row('1122.01',100),row('1122.01',100)])
        self.assertEqual(result['row_counts']['retained'],2)
        self.assertEqual(len(result['groups'][0]['child_record_ids']),2)

    def test_multiple_parent_rows_compared_as_group(self):
        self.confirm();result=self.analyze([row('1221',4179.84),row('1221',60),row('1221',1600),
            row('1221.02',4179.84),row('1221.02',60),row('1221.03',1600)])
        self.assertEqual(result['row_counts']['excluded'],3)
        self.assertEqual(result['groups'][0]['parent_totals']['debit'],'5839.84')
        self.assertEqual(result['group_counts']['verified_structure'],1)

    def test_identical_parents_and_children_preserve_multiplicity(self):
        self.confirm();result=self.analyze([row('1122',100),row('1122',100),row('1122.01',100),row('1122.01',100)])
        self.assertEqual(result['row_counts']['excluded'],2)
        self.assertEqual(result['row_counts']['retained'],2)

    def test_amount_mismatch_quarantines_whole_voucher(self):
        self.confirm();result=self.analyze([row('1122',150),row('1122.01',100),row('1002',credit=100)])
        self.assertEqual(result['row_counts']['excluded'],0)
        self.assertEqual(result['row_counts']['needs_review'],3)
        self.assertEqual(result['groups'][0]['differences']['debit'],'50')
        self.assertEqual(result['blocked_vouchers'][0]['related_excel_rows'],[2,3])

    def test_net_amount_equality_does_not_replace_side_comparison(self):
        self.confirm();result=self.analyze([row('1122',100),row('1122.01',credit=-100)])
        self.assertEqual(result['row_counts']['excluded'],0)
        self.assertFalse(result['groups'][0]['amount_totals_equal'])

    def test_summary_mismatch_requires_review(self):
        self.confirm();result=self.analyze([row('1122',100,summary='A'),row('1122.01',100,summary='B')])
        self.assertEqual(result['row_counts']['needs_review'],2)
        self.assertIn('AMBIGUOUS_HIERARCHY_SUMMARY',result['blocked_vouchers'][0]['reason_codes'])
        self.assertEqual(result['blocked_vouchers'][0]['related_excel_rows'],[2,3])

    def test_independent_summary_partitions_are_valid(self):
        self.confirm();result=self.analyze([row('5602',100,summary='A'),row('5602.01',100,summary='A'),
            row('5602',200,summary='B'),row('5602.02',200,summary='B')])
        self.assertEqual(result['group_counts']['verified_structure'],2)
        self.assertEqual(result['row_counts']['excluded'],2)

    def test_later_mismatch_blocks_earlier_valid_branch(self):
        self.confirm();result=self.analyze([row('1122',100),row('1122.01',100),row('2221',200),row('2221.01',150)])
        self.assertEqual(result['row_counts']['excluded'],0)
        self.assertEqual(result['row_counts']['needs_review'],4)
        self.assertEqual(result['group_counts']['verified_structure'],0)

    def test_row_order_does_not_change_decisions(self):
        self.confirm();parsed=report([row('1122',100),row('1122.01',100),row('2221',200),row('2221.01',150)],self.config)
        first=analyze_hierarchy(parsed,self.config);parsed['rows'].reverse();second=analyze_hierarchy(parsed,self.config)
        def decisions(result):return {r['record_id']:(r['hierarchy']['decision'],r['hierarchy']['reason_codes']) for r in result['rows']}
        self.assertEqual(decisions(first),decisions(second))

    def test_upstream_invalid_amount_blocks_voucher(self):
        self.confirm();bad=row('9999','unknown');bad.update(parse_state='needs_review',issues=[{'code':'INVALID_AMOUNT'}])
        result=self.analyze([row('1122',100),row('1122.01',100),bad])
        self.assertEqual(result['row_counts']['excluded'],0)
        self.assertEqual(result['row_counts']['needs_review'],3)

    def test_unknown_voucher_error_blocks_batch(self):
        self.confirm();bad=row('9999',10);bad.update(voucher=None,parse_state='needs_review',issues=[{'code':'MISSING_VOUCHER'}])
        result=self.analyze([row('1122',100),row('1122.01',100),row('1002',10,number=2),bad])
        self.assertEqual(result['row_counts']['needs_review'],4)
        self.assertEqual(result['row_counts']['excluded'],0)

    def test_unknown_period_review_row_blocks_same_voucher(self):
        self.confirm();bad=row('9999',10,period=None);bad.update(parse_state='needs_review',issues=[{'code':'INVALID_PERIOD'}])
        result=self.analyze([row('1122',100),row('1122.01',100),bad])
        self.assertEqual(result['row_counts']['excluded'],0)

    def test_zero_only_group_not_sufficient_to_exclude(self):
        self.confirm();result=self.analyze([row('1122',0),row('1122.01',0)])
        self.assertEqual(result['row_counts']['excluded'],0)
        self.assertIn('ZERO_TOTAL_HIERARCHY_AMBIGUOUS',result['groups'][0]['reason_codes'])

    def test_cancelling_nonzero_values_also_require_review(self):
        self.confirm();result=self.analyze([row('1122',100),row('1122',-100),row('1122.01',200),row('1122.01',-200)])
        self.assertEqual(result['row_counts']['excluded'],0)
        self.assertEqual(result['row_counts']['needs_review'],4)

    def test_negative_child_and_positive_child_keep_original_signs(self):
        self.confirm();result=self.analyze([row('5603',credit='-56.61'),row('5603.01',credit='-62.11'),row('5603.02',credit='5.50')])
        self.assertEqual(result['row_counts']['excluded'],1)
        self.assertEqual(result['rows'][1]['credit']['value'],'-62.11')

    def test_long_amount_difference_not_rounded_to_equality(self):
        self.confirm();result=self.analyze([row('1122','10000000000000000000000000000.01'),row('1122.01','10000000000000000000000000000.00')])
        self.assertEqual(result['groups'][0]['differences']['debit'],'0.01')
        self.assertEqual(result['row_counts']['excluded'],0)

    def test_long_amount_exact_sum(self):
        self.confirm();result=self.analyze([row('1122','10000000000000000000000000000.01'),row('1122.01','10000000000000000000000000000.00'),row('1122.02','0.01')])
        self.assertEqual(result['group_counts']['verified_structure'],1)

    def test_blank_and_explicit_zero_are_preserved(self):
        self.confirm();result=self.analyze([row('1122',10),row('1122.01',10,0)])
        self.assertEqual(result['row_counts']['excluded'],1)
        self.assertEqual(result['rows'][0]['credit']['status'],'blank')
        self.assertEqual(result['rows'][1]['credit']['value'],'0')

    def test_input_report_is_not_modified(self):
        parsed=report([row('1122',100),row('1122.01',100)],self.config);before=copy.deepcopy(parsed)
        analyze_hierarchy(parsed,self.config)
        self.assertEqual(parsed,before)

    def test_duplicate_ids_and_foreign_sources_are_rejected(self):
        parsed=report([row('1122',100),row('1122.01',100)],self.config)
        for field,value in [('record_id',parsed['rows'][0]['record_id']),('provenance',{'input_sha256':'other','sheet':'合成原始','excel_row':3})]:
            bad=copy.deepcopy(parsed);bad['rows'][1][field]=value
            with self.subTest(field=field),self.assertRaises(HierarchyError):analyze_hierarchy(bad,self.config)

    def test_input_period_mismatch_is_rejected(self):
        parsed=report([row('1122',100),row('1122.01',100,period='202607')],self.config)
        with self.assertRaises(HierarchyError):analyze_hierarchy(parsed,self.config)

    def test_config_changed_after_parse_requires_reparse(self):
        parsed=report([row('1122',100),row('1122.01',100)],self.config);self.confirm()
        with self.assertRaises(HierarchyError) as caught:analyze_hierarchy(parsed,self.config)
        self.assertEqual(caught.exception.code,'CONFIG_MISMATCH')

    def test_upstream_global_error_is_rejected(self):
        parsed=report([row('1122',100)],self.config);parsed['read_ok']=False
        with self.assertRaises(HierarchyError) as caught:analyze_hierarchy(parsed,self.config)
        self.assertEqual(caught.exception.code,'UPSTREAM_PARSE_BLOCKED')

    def test_nonbusiness_rows_are_preserved_with_reason(self):
        item=row('1002');item.update(parse_state='non_business',reason_code='NON_BUSINESS_OPENING',voucher=None)
        result=self.analyze([item,row('1002',100)])
        self.assertEqual(result['row_counts']['non_business'],1)
        self.assertEqual(result['rows'][0]['hierarchy']['reason_codes'],['NON_BUSINESS_OPENING'])

    def test_unmatched_known_parent_and_unknown_code_are_not_dropped(self):
        self.confirm();result=self.analyze([row('1002',100),row('9999.01',100)])
        self.assertEqual(result['row_counts']['retained'],2)

    def test_falsely_valid_nonfinite_amount_rejected(self):
        parsed=report([row('1122',100)],self.config);parsed['rows'][0]['debit']={'status':'number','value':'NaN'}
        with self.assertRaises(HierarchyError):analyze_hierarchy(parsed,self.config)


class HierarchyCliTests(unittest.TestCase):
    def test_cli_confirmed_synthetic_policy_applies_exclusion(self):
        with tempfile.TemporaryDirectory(prefix='smartmap-hierarchy-confirmed-') as folder:
            source=Path(folder)/'source.xlsx';output=Path(folder)/'out';config_path=Path(folder)/'config.json'
            config,_=load_config(ROOT/'config/example.json')
            rule=next(x for x in config['rules'] if x['kind']=='hierarchy')
            rule.update(status='confirmed',review={'reviewed_by':'synthetic-test','reviewed_on':'2026-10-28','reason':'临时合成测试'})
            rule['evidence'].append({'kind':'user_confirmation','reference':'仅合成测试，不是企业业务批准'})
            config_path.write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
            header=['日期','账期','科目名称','凭证字号','摘要','借方','贷方']
            data=[['2026-06-30','202606',code,'记1','合成业务',100,''] for code in ['9000父级','9000.01子级']]
            write_xlsx(source,[('原始',[header]+data)])
            result=subprocess.run([sys.executable,'-B','-m','smartmap','analyze-hierarchy','--source',str(source),
                '--config',str(config_path),'--out',str(output)],cwd=ROOT,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            report_data=json.loads((output/'hierarchy_report.json').read_text(encoding='utf-8'))
            self.assertEqual(report_data['row_counts']['excluded'],1)
            self.assertFalse(report_data['ready_for_system_import'])

    def test_cli_is_diagnostic_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory(prefix='smartmap-hierarchy-') as folder:
            source=Path(folder)/'source.xlsx';output=Path(folder)/'out'
            header=['日期','账期','科目名称','凭证字号','摘要','借方','贷方']
            data=[['2026-06-30','202606',code,'记1','合成业务',100,''] for code in ['1122父级','1122.01子级']]
            write_xlsx(source,[('原始',[header]+data)])
            command=[sys.executable,'-B','-m','smartmap','analyze-hierarchy','--source',str(source),'--config',str(ROOT/'config/example.json'),'--out',str(output)]
            first=subprocess.run(command,cwd=ROOT,capture_output=True)
            self.assertEqual(first.returncode,3,first.stderr)
            path=output/'hierarchy_report.json';before=path.read_bytes();result=json.loads(before)
            self.assertEqual(result['row_counts']['excluded'],0)
            self.assertEqual(result['diagnostic_projection']['remaining_rows'],1)
            second=subprocess.run(command,cwd=ROOT,capture_output=True)
            self.assertEqual(second.returncode,2)
            self.assertEqual(path.read_bytes(),before)
            self.assertTrue((output/'row_decisions.csv').is_file())


if __name__=='__main__':unittest.main()
