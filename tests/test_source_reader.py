"""T07合成工作簿测试，使用临时OOXML文件，不依赖人工标准答案。"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).resolve().parent))
from xlsx_fixtures import write_xlsx
from smartmap.configuration import ConfigurationError, load_config, validate_config
from smartmap.excel_reader import SourceError, digest, parse_source
from smartmap.values import parse_date, parse_money, parse_period, parse_voucher

HEADER=['日期','账期','科目名称','凭证字号','摘要','借方','贷方']
DATA=['2026-06-03','202606','1122.07 应收款','记1','测试业务','1,200.05','']
IMPORT_HEADER=['导入结果','日期','凭证类别','科目名称','凭证号','分录序号','摘要','科目编码','借方金额','贷方金额','客户']


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='smartmap-reader-')
        self.folder=Path(self.temp.name);self.source=self.folder/'source.xlsx'
        self.config,_=load_config(ROOT/'config/example.json')

    def tearDown(self):self.temp.cleanup()

    def run_reader(self,rows=None,sheets=None,period=None,dimension=None):
        write_xlsx(self.source,sheets or [('原始',[HEADER]+(rows if rows is not None else [DATA]))],dimension)
        before=digest(self.source)
        result=parse_source(self.source,self.config,period)
        self.assertEqual(digest(self.source),before)
        return result

    def test_reordered_columns_and_source_provenance(self):
        order=[6,3,5,2,0,4,1]
        result=self.run_reader(sheets=[('任意名称',[[HEADER[i] for i in order],[DATA[i] for i in order]])])
        row=result['rows'][0]
        self.assertEqual(row['subject']['code'],'1122.07')
        self.assertEqual(row['debit']['value'],'1200.05')
        self.assertEqual(row['provenance']['excel_row'],2)
        self.assertEqual(row['provenance']['input_sha256'],digest(self.source))

    def test_title_and_two_line_header(self):
        top=['日期','账期','科目名称','凭证字号','摘要','发生额','']
        bottom=['','','','','','借方','贷方']
        result=self.run_reader(sheets=[('原始',[['合成明细账'],top,bottom,DATA])])
        self.assertEqual(result['header']['header_rows'],[2,3])
        self.assertEqual(result['rows'][0]['provenance']['excel_row'],4)
        self.assertTrue(result['ready_for_next_stage'])

    def test_no_750_row_cutoff_and_wrong_dimension(self):
        rows=[]
        for number in range(1,806):
            item=DATA.copy();item[3]=f'记{number}';rows.append(item)
        result=self.run_reader(rows,dimension='A1:A1')
        self.assertEqual(result['row_counts']['parsed'],805)
        self.assertEqual(result['rows'][-1]['provenance']['excel_row'],806)

    def test_header_is_not_limited_to_first_50_rows(self):
        result=self.run_reader(sheets=[('原始',[['说明'] for _ in range(80)]+[HEADER,DATA])])
        self.assertEqual(result['header']['header_rows'],[81])
        self.assertEqual(result['row_counts']['parsed'],1)

    def test_multiple_ledger_sheets_block_automatic_selection(self):
        with self.assertRaises(SourceError) as caught:self.run_reader(sheets=[('一',[HEADER,DATA]),('二',[HEADER,DATA])])
        self.assertEqual(caught.exception.code,'AMBIGUOUS_SOURCE_SHEET')

    def test_explicit_sheet_never_iterates_other_sheets(self):
        from openpyxl.worksheet._read_only import ReadOnlyWorksheet
        original=ReadOnlyWorksheet.iter_rows
        self.config['source_profile']['worksheet_name']='原始'
        def guarded(sheet,*args,**kwargs):
            if sheet.title=='答案':raise AssertionError('不应读取答案工作表')
            return original(sheet,*args,**kwargs)
        with patch.object(ReadOnlyWorksheet,'iter_rows',guarded):
            report=self.run_reader(sheets=[('答案',[HEADER,DATA]),('原始',[HEADER,DATA])])
        self.assertEqual([x['sheet'] for x in report['sheet_probes']],['原始'])

    def test_automatic_probe_stops_at_import_header(self):
        from openpyxl.worksheet._read_only import ReadOnlyWorksheet
        original=ReadOnlyWorksheet.iter_rows
        def guarded(sheet,*args,**kwargs):
            if sheet.title=='答案':
                yield next(iter(original(sheet,*args,**kwargs)))
                raise AssertionError('不应继续读取人工业务行')
            yield from original(sheet,*args,**kwargs)
        with patch.object(ReadOnlyWorksheet,'iter_rows',guarded):
            result=self.run_reader(sheets=[('答案',[IMPORT_HEADER,HEADER,DATA]),('原始',[HEADER,DATA])])
        self.assertEqual(result['source_sheet'],'原始')
        self.assertEqual(result['sheet_probes'][0]['role'],'import_or_manual')

    def test_reference_sheet_probe_stops_at_incomplete_flat_header(self):
        from openpyxl.worksheet._read_only import ReadOnlyWorksheet
        original=ReadOnlyWorksheet.iter_rows
        def guarded(sheet,*args,**kwargs):
            if sheet.title=='备查':
                yield next(iter(original(sheet,*args,**kwargs)))
                raise AssertionError('不应继续读取备查业务数据')
            yield from original(sheet,*args,**kwargs)
        with patch.object(ReadOnlyWorksheet,'iter_rows',guarded):
            result=self.run_reader(sheets=[('备查',[['科目编码','科目名称','方向'],['1001','示例','借']]),('原始',[HEADER,DATA])])
        self.assertEqual(result['source_sheet'],'原始')

    def test_explicit_manual_sheet_is_rejected(self):
        self.config['source_profile']['worksheet_name']='答案'
        with self.assertRaises(SourceError) as caught:self.run_reader(sheets=[('答案',[IMPORT_HEADER,HEADER,DATA])])
        self.assertEqual(caught.exception.code,'SOURCE_SHEET_ROLE_MISMATCH')

    def test_duplicate_semantic_columns_rejected(self):
        with self.assertRaises(SourceError) as caught:self.run_reader(sheets=[('原始',[HEADER+['借方金额'],DATA+[12]])])
        self.assertEqual(caught.exception.code,'AMBIGUOUS_HEADER')

    def test_custom_alias_and_voucher_prefix(self):
        self.config['source_profile'].update(header_aliases={'voucher':['单据编号']},voucher_prefixes=['JV'])
        head=HEADER.copy();head[3]='单据编号';row=DATA.copy();row[3]='JV-10'
        result=self.run_reader(sheets=[('原始',[head,row])])
        self.assertEqual(result['rows'][0]['voucher'],{'category':'JV','number':10})

    def test_alias_collision_and_numbered_prefix_are_rejected(self):
        self.config['source_profile']['header_aliases']={'voucher':['日期']}
        with self.assertRaises(ConfigurationError):validate_config(self.config)
        self.config['source_profile'].pop('header_aliases');self.config['source_profile']['voucher_prefixes']=['记74']
        with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_known_nonbusiness_rows_and_unknown_rows_are_distinct(self):
        rows=[['','202606','1002银行存款','',label,'',''] for label in ['期初余额','本期合计','本年累计']]
        rows+=[['2026-06-03','202606','1122.07 应收款','','未知无凭证记录',100,''],DATA]
        result=self.run_reader(rows)
        self.assertEqual(result['row_counts'],{'total':5,'parsed':1,'needs_review':1,'non_business':3})
        self.assertIn('MISSING_VOUCHER',result['issue_counts'])

    def test_footer_with_money_must_not_be_silently_discarded(self):
        row=['制作人：测试',None,None,None,'导出时间：测试',100,None]
        result=self.run_reader([DATA,row])
        self.assertEqual(result['rows'][1]['parse_state'],'needs_review')

    def test_same_amount_and_identical_entries_retain_occurrences(self):
        second=DATA.copy();second[3]='记2'
        result=self.run_reader([DATA,DATA,second])
        self.assertEqual(result['row_counts']['parsed'],3)
        self.assertEqual(len({r['record_id'] for r in result['rows']}),3)

    def test_real_ten_zero_and_negative_are_preserved(self):
        row=DATA.copy();row[5]=10;row[6]=0
        other=DATA.copy();other[5]='-62.11'
        result=self.run_reader([row,other])
        self.assertEqual(result['rows'][0]['debit']['value'],'10')
        self.assertEqual(result['rows'][0]['credit']['value'],'0')
        self.assertEqual(result['rows'][1]['debit']['value'],'-62.11')

    def test_invalid_amount_and_formulas_are_quarantined(self):
        a=DATA.copy();a[5]='12,34'
        b=DATA.copy();b[5]={'type':'f','value':'1+1'}
        c=DATA.copy();c[5]={'type':'e','value':'#VALUE!'}
        result=self.run_reader([a,b,c])
        self.assertEqual(result['row_counts']['needs_review'],3)
        self.assertFalse(result['ready_for_next_stage'])
        self.assertTrue({'INVALID_AMOUNT','FORMULA_CELL','ERROR_CELL'}<=set(result['issue_counts']))
        self.assertEqual(result['rows'][1]['debit']['raw']['value'],'=1+1')

    def test_empty_amount_is_not_accepted_as_zero(self):
        row=DATA.copy();row[5]=''
        result=self.run_reader([row])
        self.assertEqual(result['rows'][0]['debit']['status'],'blank')
        self.assertIn('EMPTY_BUSINESS_AMOUNT',result['issue_counts'])

    def test_unknown_subject_is_parsed_without_mapping(self):
        row=DATA.copy();row[2]='9999.01 未知业务'
        result=self.run_reader([row])
        self.assertEqual(result['row_counts']['parsed'],1)
        self.assertEqual(result['rows'][0]['subject']['code'],'9999.01')
        self.assertNotIn('target_code',result['rows'][0])

    def test_separate_subject_code_and_conflict(self):
        head=HEADER+['科目编码'];a=DATA.copy();a[2]='应收款';a+=['001122.07']
        b=DATA.copy();b+=['1122.08']
        result=self.run_reader(sheets=[('原始',[head,a,b])])
        self.assertEqual(result['rows'][0]['subject']['code'],'001122.07')
        self.assertEqual(result['rows'][1]['parse_state'],'needs_review')

    def test_new_month_and_leap_day(self):
        row=DATA.copy();row[:2]=[date(2024,2,29),'202402']
        result=self.run_reader([row])
        self.assertEqual(result['period'],'202402')
        self.assertEqual(result['rows'][0]['date'],'2024-02-29')
        self.assertTrue(result['ready_for_next_stage'])

    def test_invalid_date_and_unformatted_serial(self):
        a=DATA.copy();a[0]='2027-02-29'
        b=DATA.copy();b[0]=46203
        result=self.run_reader([a,b])
        self.assertEqual(result['row_counts']['needs_review'],2)
        self.assertEqual(result['issue_counts']['INVALID_DATE'],2)

    def test_multiple_months_and_request_conflict_block_run(self):
        other=DATA.copy();other[:2]=['2026-07-03','202607']
        self.assertFalse(self.run_reader([DATA,other])['read_ok'])
        result=self.run_reader([DATA],period='202607')
        self.assertEqual(result['fatal_errors'][0]['code'],'PERIOD_CONFLICT')
        self.assertEqual(result['rows'][0]['period'],'202606')

    def test_date_period_conflict_is_review_not_rewrite(self):
        row=DATA.copy();row[0]='2026-07-01'
        result=self.run_reader([row])
        self.assertIn('DATE_PERIOD_MISMATCH',result['issue_counts'])
        self.assertEqual(result['rows'][0]['period'],'202606')

    def test_date_can_provide_period_with_provenance(self):
        order=[0,2,3,4,5,6]
        result=self.run_reader(sheets=[('原始',[[HEADER[i] for i in order],[DATA[i] for i in order]])])
        self.assertEqual(result['period'],'202606')
        self.assertEqual(result['rows'][0]['period_origin'],'source_date')

    def test_request_period_only_used_when_original_blank(self):
        head=HEADER[1:];row=DATA[1:];row[0]=''
        result=self.run_reader(sheets=[('原始',[head,row])],period='202606')
        self.assertEqual(result['rows'][0]['period_origin'],'request')

    def test_repeated_header_and_blank_rows_have_explicit_status(self):
        result=self.run_reader([DATA,HEADER,[None]*7])
        self.assertEqual(result['row_counts']['non_business'],2)
        self.assertEqual(result['rows'][1]['reason_code'],'REPEATED_HEADER')

    def test_cli_refuses_existing_output_and_reports_conflict(self):
        write_xlsx(self.source,[('原始',[HEADER,DATA])])
        output=self.folder/'report'
        command=[sys.executable,'-B','-m','smartmap','parse-source','--source',str(self.source),'--config',str(ROOT/'config/example.json'),'--out',str(output),'--period','202607']
        run=subprocess.run(command,cwd=ROOT,capture_output=True)
        self.assertEqual(run.returncode,2,run.stderr)
        report=json.loads((output/'parse_report.json').read_text(encoding='utf-8'))
        self.assertFalse(report['read_ok'])
        before=(output/'parse_report.json').read_bytes()
        run=subprocess.run(command,cwd=ROOT,capture_output=True)
        self.assertEqual(run.returncode,2)
        self.assertEqual(before,(output/'parse_report.json').read_bytes())

    def test_cli_returns_three_for_row_anomalies(self):
        row=DATA.copy();row[5]='金额未知'
        write_xlsx(self.source,[('原始',[HEADER,row])])
        output=self.folder/'review_report'
        run=subprocess.run([sys.executable,'-B','-m','smartmap','parse-source','--source',str(self.source),
            '--config',str(ROOT/'config/example.json'),'--out',str(output)],cwd=ROOT,capture_output=True)
        self.assertEqual(run.returncode,3,run.stderr)
        self.assertEqual(json.loads((output/'summary.json').read_text(encoding='utf-8'))['row_counts']['needs_review'],1)

    def test_request_period_must_be_exact_yyyymm(self):
        with self.assertRaises(SourceError) as caught:parse_source(self.source,self.config,'2026-06')
        self.assertEqual(caught.exception.code,'INVALID_REQUEST_PERIOD')

    def test_input_hash_change_is_detected(self):
        write_xlsx(self.source,[('原始',[HEADER,DATA])])
        with patch('smartmap.excel_reader.digest',side_effect=['before','after']):
            with self.assertRaises(SourceError) as caught:parse_source(self.source,self.config)
        self.assertEqual(caught.exception.code,'INPUT_CHANGED')

    def test_invalid_workbook_is_explained(self):
        self.source.write_text('不是Excel',encoding='utf-8')
        with self.assertRaises(SourceError) as caught:parse_source(self.source,self.config)
        self.assertEqual(caught.exception.code,'UNREADABLE_WORKBOOK')


class ValueTests(unittest.TestCase):
    def test_invalid_and_ambiguous_periods(self):
        for value in ['20261','202613','2026-00',True,'2026年13月']:
            with self.subTest(value=value),self.assertRaises(ValueError):parse_period(value)
        self.assertEqual(parse_period('2026年6月'),'202606')
        self.assertEqual(parse_period(202606),'202606')

    def test_invalid_voucher_never_extracts_arbitrary_digits(self):
        for value in ['abc10','202606记10','',None]:
            if value in ['',None]:self.assertIsNone(parse_voucher(value,['记']))
            else:
                with self.subTest(value=value),self.assertRaises(ValueError):parse_voucher(value,['记'])

    def test_scientific_numeric_values_and_nonfinite_values(self):
        self.assertEqual(parse_money(1e-7)['value'],'0.0000001')
        for value in [True,float('nan'),float('inf'),'NaN','1e-7']:
            with self.subTest(value=value):self.assertEqual(parse_money(value)['status'],'invalid')

    def test_compact_date_requires_eight_digits(self):
        with self.assertRaises(ValueError):parse_date('2026111')


if __name__=='__main__':unittest.main()
