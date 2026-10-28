"""T10月末、整凭证平衡、精确金额及正式/诊断序号测试。"""
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
from smartmap.configuration import load_config
from smartmap.hierarchy import analyze_hierarchy
from smartmap.mapping_filter import map_and_filter
from smartmap.vouchers import VoucherError, month_end, validate_vouchers


class MonthEndTests(unittest.TestCase):
    def test_normal_and_year_boundaries(self):
        for period,expected in [('202606','2026-06-30'),('202607','2026-07-31'),('202612','2026-12-31'),('202701','2027-01-31'),('999912','9999-12-31')]:
            with self.subTest(period=period):self.assertEqual(month_end(period),expected)

    def test_leap_year_and_century_rules(self):
        for period,expected in [('202402','2024-02-29'),('202702','2027-02-28'),('200002','2000-02-29'),('190002','1900-02-28'),('210002','2100-02-28')]:
            with self.subTest(period=period):self.assertEqual(month_end(period),expected)

    def test_invalid_periods(self):
        for value in [None,True,202606,'2026-06','202600','202613','000001','20261','202606\n']:
            with self.subTest(value=value),self.assertRaises(VoucherError):month_end(value)


class VoucherTests(unittest.TestCase):
    def setUp(self):self.config,_=load_config(ROOT/'config/example.json')

    def confirm(self,item):
        item['status']='confirmed';item['evidence'].append({'kind':'user_confirmation','reference':'仅合成测试，不批准企业数据'})
        item['review']={'reviewed_by':'synthetic-test','reviewed_on':'2026-10-28','reason':'T10合成测试'}
        return item

    def mapping(self,code):
        item={'id':f'map-{len(self.config["mappings"])}','source_code':code,'target_code':'990001','target_name':'合成内部科目',
            'status':'candidate','evidence':[{'kind':'synthetic_example','reference':'test'}],'review':None}
        self.config['mappings'].append(self.confirm(item))

    def inputs(self,rows,period='202606'):
        source=report(rows,self.config);source['period']=period
        hierarchy=analyze_hierarchy(source,self.config)
        return map_and_filter(hierarchy,self.config),hierarchy

    def run_stage(self,rows,period='202606'):
        mapped,hierarchy=self.inputs(rows,period)
        return validate_vouchers(mapped,hierarchy,self.config)

    def known(self):self.mapping('1122.01');self.mapping('1002')

    def test_balanced_complete_voucher_gets_continuous_sequence(self):
        self.known();result=self.run_stage([row('1122.01',100),row('1002',credit=100)])
        self.assertEqual(result['official_summary']['balanced'],1)
        self.assertEqual(result['row_counts']['retained'],2)
        self.assertEqual([r['voucher_validation']['entry_sequence'] for r in result['rows']],[1,2])
        self.assertTrue(all(r['voucher_validation']['posting_date']=='2026-06-30' for r in result['rows']))

    def test_pending_mapping_only_gets_diagnostic_sequence(self):
        result=self.run_stage([row('1122.01',100),row('1002',credit=100)])
        self.assertIsNone(result['official_summary']['balance_rate'])
        self.assertEqual(result['official_summary']['balance_rate_denominator'],0)
        self.assertEqual(result['official_assigned_rows'],0)
        self.assertEqual(result['diagnostic_assigned_rows'],2)
        self.assertTrue(all(r['diagnostic_assignment']['diagnostic_only'] for r in result['rows']))

    def test_pending_parent_is_not_officially_dropped_for_balance(self):
        self.known();result=self.run_stage([row('1122',100),row('1122.01',100),row('1002',credit=100)])
        self.assertEqual(result['official_summary']['needs_review'],1)
        self.assertEqual(result['diagnostic_summary']['balanced'],1)
        self.assertEqual(result['diagnostic_assigned_rows'],2)
        self.assertIsNone(result['rows'][0]['diagnostic_assignment'])

    def test_confirmed_parent_exclusion_balances_terminal_rows(self):
        self.known();self.confirm(next(r for r in self.config['rules'] if r['kind']=='hierarchy'))
        result=self.run_stage([row('1122',100),row('1122.01',100),row('1002',credit=100)])
        self.assertEqual(result['row_counts']['excluded'],1)
        self.assertEqual(result['official_assigned_rows'],2)
        self.assertEqual(result['official_summary']['balanced'],1)
        self.assertIsNone(result['rows'][0]['voucher_validation']['entry_sequence'])

    def test_unbalanced_voucher_quarantines_all_remaining_lines(self):
        self.known();result=self.run_stage([row('1122.01',100),row('1002',credit='99.99')])
        self.assertEqual(result['official_summary']['unbalanced'],1)
        self.assertEqual(result['official_vouchers'][0]['difference'],'0.01')
        self.assertEqual(result['row_counts']['needs_review'],2)
        self.assertEqual(result['official_assigned_rows'],0)
        self.assertEqual(result['diagnostic_assigned_rows'],0)
        self.assertEqual(result['rows'][1]['credit']['value'],'99.99')

    def test_one_unbalanced_voucher_does_not_change_other_voucher(self):
        self.known();result=self.run_stage([row('1122.01',100,number=1),row('1002',credit=99,number=1),row('1122.01',200,number=2),row('1002',credit=200,number=2)])
        self.assertEqual(result['official_summary']['unbalanced'],1)
        self.assertEqual(result['official_summary']['balanced'],1)
        self.assertEqual([r['voucher_validation']['entry_sequence'] for r in result['rows']],[None,None,1,2])

    def test_same_number_different_categories_reset_sequence(self):
        self.known();result=self.run_stage([row('1122.01',100,category='记'),row('1002',credit=100,category='记'),
            row('1122.01',200,category='收'),row('1002',credit=200,category='收')])
        self.assertEqual(result['official_summary']['voucher_groups'],2)
        self.assertEqual([r['voucher_validation']['entry_sequence'] for r in result['rows']],[1,2,1,2])

    def test_duplicate_amount_entries_keep_occurrences_and_sequences(self):
        self.known();result=self.run_stage([row('1122.01',100),row('1122.01',100),row('1002',credit=200)])
        self.assertEqual(result['official_assigned_rows'],3)
        self.assertEqual([r['voucher_validation']['entry_sequence'] for r in result['rows']],[1,2,3])

    def test_sequence_uses_original_physical_order_not_input_list_order(self):
        self.known();mapped,hierarchy=self.inputs([row('1122.01',100),row('1002',credit=100)])
        mapped['rows'].reverse()
        result=validate_vouchers(mapped,hierarchy,self.config)
        assigned={r['provenance']['excel_row']:r['voucher_validation']['entry_sequence'] for r in result['rows']}
        self.assertEqual(assigned,{2:1,3:2})

    def test_large_amount_difference_is_not_rounded(self):
        self.known();result=self.run_stage([row('1122.01','10000000000000000000000000000.01'),row('1002',credit='10000000000000000000000000000.00')])
        self.assertEqual(result['official_vouchers'][0]['difference'],'0.01')
        self.assertEqual(result['official_summary']['unbalanced'],1)

    def test_large_amount_exact_sum(self):
        self.known();result=self.run_stage([row('1122.01','10000000000000000000000000000.00'),row('1122.01','0.01'),row('1002',credit='10000000000000000000000000000.01')])
        self.assertEqual(result['official_summary']['balanced'],1)

    def test_net_zero_with_real_activity_is_still_arithmetically_balanced(self):
        self.known();result=self.run_stage([row('1002','62.11'),row('1122.01','-62.11')])
        self.assertEqual(result['official_summary']['balanced'],1)
        self.assertEqual(result['official_assigned_rows'],2)
        self.assertIn('NET_ZERO_WITH_ACTIVITY',result['official_vouchers'][0]['observations'])
        self.assertEqual(result['rows'][1]['debit']['value'],'-62.11')

    def test_negative_values_not_converted_to_positive(self):
        self.known();result=self.run_stage([row('1122.01','-10.25'),row('1002',credit='-10.25')])
        self.assertEqual(result['official_vouchers'][0]['totals'],{'debit':'-10.25','credit':'-10.25'})
        self.assertEqual(result['official_summary']['balanced'],1)

    def test_all_zero_entries_require_review_but_are_not_deleted(self):
        self.known();result=self.run_stage([row('1122.01',0),row('1002',credit=0)])
        self.assertEqual(result['row_counts']['needs_review'],2)
        self.assertEqual(result['row_counts']['excluded'],0)
        self.assertTrue(result['official_vouchers'][0]['arithmetic_equal'])
        self.assertEqual(result['official_assigned_rows'],0)

    def test_zero_extra_entry_does_not_disappear(self):
        self.known();result=self.run_stage([row('1122.01',100),row('1002',credit=100),row('1002',0)])
        self.assertEqual(len(result['rows']),3)
        self.assertEqual(result['official_summary']['needs_review'],1)

    def test_both_sides_nonzero_needs_review_without_splitting(self):
        self.known();result=self.run_stage([row('1122.01',100,100)])
        self.assertEqual(result['row_counts']['needs_review'],1)
        self.assertTrue(result['official_vouchers'][0]['arithmetic_equal'])
        self.assertEqual(len(result['rows']),1)

    def test_blank_side_not_rewritten_as_zero(self):
        self.known();result=self.run_stage([row('1122.01',100),row('1002',credit=100)])
        self.assertEqual(result['rows'][0]['credit']['status'],'blank')
        self.assertIsNone(result['rows'][0]['credit']['value'])

    def test_original_and_upstream_reports_remain_unchanged(self):
        self.known();mapped,hierarchy=self.inputs([row('1122.01',100),row('1002',credit=100)])
        before=copy.deepcopy((mapped,hierarchy));result=validate_vouchers(mapped,hierarchy,self.config)
        self.assertEqual((mapped,hierarchy),before)
        self.assertTrue(all({k:v for k,v in r.items() if k not in ['voucher_validation','diagnostic_assignment']}==mapped['rows'][i] for i,r in enumerate(result['rows'])))
        self.assertEqual(result['rows'][0]['date'],'2026-06-30')

    def test_original_midmonth_date_is_preserved_and_proposal_is_month_end(self):
        self.known();items=[row('1122.01',100),row('1002',credit=100)]
        for item in items:item['date']='2026-06-03'
        result=self.run_stage(items)
        self.assertEqual(result['rows'][0]['date'],'2026-06-03')
        self.assertEqual(result['rows'][0]['voucher_validation']['posting_date'],'2026-06-30')

    def test_new_month_input_is_not_tied_to_june(self):
        self.known();items=[row('1122.01',100,period='202402'),row('1002',credit=100,period='202402')]
        for item in items:item['date']='2024-02-01'
        result=self.run_stage(items,'202402')
        self.assertEqual(result['month_end'],'2024-02-29')
        self.assertEqual(result['official_assigned_rows'],2)

    def test_excluded_voucher_has_no_posting_assignment(self):
        self.config['rules'].append({'id':'exclude-synthetic','kind':'voucher_filter','status':'candidate','policy':'exclude_voucher',
            'parameters':{'summary_equals':'合成业务','match_scope':'all_lines'},'evidence':[{'kind':'synthetic_example','reference':'test'}],'review':None})
        self.confirm(self.config['rules'][-1])
        result=self.run_stage([row('1122.01',100),row('1002',credit=100)])
        self.assertEqual(result['official_summary']['excluded'],1)
        self.assertEqual(result['official_assigned_rows'],0)
        self.assertEqual(result['diagnostic_assigned_rows'],0)
        self.assertTrue(all(r['voucher_validation']['posting_date'] is None for r in result['rows']))

    def test_invalid_source_amount_is_not_in_diagnostic_balance(self):
        self.known();bad=row('1002','unknown');bad.update(parse_state='needs_review',issues=[{'code':'INVALID_AMOUNT'}])
        result=self.run_stage([row('1122.01',100),bad])
        self.assertEqual(result['diagnostic_summary']['needs_review'],1)
        self.assertIsNone(result['diagnostic_vouchers'][0]['totals'])
        self.assertEqual(result['official_assigned_rows'],0)

    def test_unknown_voucher_row_is_not_given_fake_number(self):
        self.known();bad=row('1002',credit=100);bad.update(voucher=None,parse_state='needs_review',issues=[{'code':'MISSING_VOUCHER'}])
        result=self.run_stage([row('1122.01',100),bad])
        self.assertEqual(len(result['unassigned_record_ids']),1)
        self.assertIsNone(result['rows'][1]['voucher'])
        self.assertEqual(result['diagnostic_assigned_rows'],0)

    def test_invalid_report_and_config_changes_are_rejected(self):
        self.known();mapped,hierarchy=self.inputs([row('1122.01',100),row('1002',credit=100)])
        for field,value in [('analysis_ok',False),('period','202607')]:
            bad=copy.deepcopy(mapped);bad[field]=value
            with self.subTest(field=field),self.assertRaises(VoucherError):validate_vouchers(bad,hierarchy,self.config)
        self.config['revision']+=1
        with self.assertRaises(VoucherError):validate_vouchers(mapped,hierarchy,self.config)

    def test_target_mapping_cannot_be_forged(self):
        self.known();mapped,hierarchy=self.inputs([row('1122.01',100),row('1002',credit=100)])
        mapped['rows'][0]['account_mapping']['target_code']='999999'
        with self.assertRaises(VoucherError):validate_vouchers(mapped,hierarchy,self.config)

    def test_amount_or_original_fields_cannot_be_modified(self):
        self.known();mapped,hierarchy=self.inputs([row('1122.01',100),row('1002',credit=100)])
        mapped['rows'][0]['debit']['value']='99'
        with self.assertRaises(VoucherError):validate_vouchers(mapped,hierarchy,self.config)

    def test_nonfinite_or_numeric_json_amount_cannot_claim_valid(self):
        self.known();mapped,hierarchy=self.inputs([row('1122.01',100),row('1002',credit=100)])
        for value in ['NaN',100.0]:
            a=copy.deepcopy(mapped);b=copy.deepcopy(hierarchy)
            a['rows'][0]['debit']['value']=value;b['rows'][0]['debit']['value']=value
            with self.subTest(value=value),self.assertRaises(VoucherError):validate_vouchers(a,b,self.config)

    def test_duplicate_physical_row_or_duplicate_projection_rejected(self):
        self.known();mapped,hierarchy=self.inputs([row('1122.01',100),row('1002',credit=100)])
        a=copy.deepcopy(mapped);a['rows'][1]['provenance']=a['rows'][0]['provenance'].copy()
        with self.assertRaises(VoucherError):validate_vouchers(a,hierarchy,self.config)
        b=copy.deepcopy(hierarchy);b['diagnostic_projection']['remaining_record_ids'].append(b['diagnostic_projection']['remaining_record_ids'][0])
        with self.assertRaises(VoucherError):validate_vouchers(mapped,b,self.config)


class VoucherCliTests(unittest.TestCase):
    def test_cli_month_end_and_pending_assignment_protection(self):
        with tempfile.TemporaryDirectory(prefix='smartmap-vouchers-') as folder:
            source=Path(folder)/'source.xlsx';output=Path(folder)/'out'
            header=['日期','账期','科目名称','凭证字号','摘要','借方','贷方']
            rows=[['2027-02-03','202702','9000.01虚构','记1','合成',100,None],['2027-02-03','202702','1002虚构','记1','合成',None,100]]
            write_xlsx(source,[('原始',[header]+rows)])
            command=[sys.executable,'-B','-m','smartmap','validate-vouchers','--source',str(source),'--config',str(ROOT/'config/example.json'),'--out',str(output)]
            result=subprocess.run(command,cwd=ROOT,capture_output=True)
            self.assertEqual(result.returncode,3,result.stderr)
            path=output/'voucher_report.json';before=path.read_bytes();data=json.loads(before)
            self.assertEqual(data['month_end'],'2027-02-28')
            self.assertEqual(data['official_assigned_rows'],0)
            self.assertEqual(data['diagnostic_assigned_rows'],2)
            self.assertTrue((output/'row_assignments.csv').is_file())
            self.assertEqual(subprocess.run(command,cwd=ROOT,capture_output=True).returncode,2)
            self.assertEqual(path.read_bytes(),before)

    def test_cli_confirmed_synthetic_complete_voucher(self):
        with tempfile.TemporaryDirectory(prefix='smartmap-vouchers-confirmed-') as folder:
            source=Path(folder)/'source.xlsx';output=Path(folder)/'out';config_path=Path(folder)/'config.json'
            config,_=load_config(ROOT/'config/example.json')
            config['mappings'][0].update(status='confirmed',review={'reviewed_by':'synthetic-test','reviewed_on':'2026-10-28','reason':'仅临时合成测试'})
            config['mappings'][0]['evidence'].append({'kind':'user_confirmation','reference':'不是企业业务批准'})
            config_path.write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
            header=['日期','账期','科目名称','凭证字号','摘要','借方','贷方']
            rows=[['2024-02-01','202402','9000.01虚构','记1','合成',100,None],['2024-02-01','202402','9000.01虚构','记1','合成',None,100]]
            write_xlsx(source,[('原始',[header]+rows)])
            result=subprocess.run([sys.executable,'-B','-m','smartmap','validate-vouchers','--source',str(source),'--config',str(config_path),'--out',str(output)],cwd=ROOT,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            data=json.loads((output/'voucher_report.json').read_text(encoding='utf-8'))
            self.assertEqual(data['official_summary']['balanced'],1)
            self.assertEqual(data['official_assigned_rows'],2)
            self.assertEqual(data['month_end'],'2024-02-29')
            self.assertFalse(data['ready_for_system_import'])


if __name__=='__main__':unittest.main()
