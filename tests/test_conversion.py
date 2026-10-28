"""T11候选、异常闭环、运行清单、失败恢复及真实CLI合成测试。"""
import copy
import csv
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.insert(0,str(ROOT/'tools'))
from verify_conversion_run import audit_run
from test_hierarchy import row, report
from xlsx_fixtures import write_xlsx
from smartmap.candidate_export import ExportError, build_exports, failed_exports
from smartmap.configuration import load_config
from smartmap.contracts import ConversionRequest
from smartmap.conversion import ConversionError, run_conversion
from smartmap.conversion_issues import collect_issues
from smartmap.conversion_output import write_csv
from smartmap.hierarchy import analyze_hierarchy
from smartmap.mapping_filter import map_and_filter
from smartmap.vouchers import validate_vouchers


def confirm(item):
    item['status']='confirmed';item['evidence'].append({'kind':'user_confirmation','reference':'仅合成测试，不批准企业数据'})
    item['review']={'reviewed_by':'synthetic-test','reviewed_on':'2026-10-28','reason':'T11临时合成配置'}


def base_rows():return [row('9000.01',100),row('9000.01',credit=100)]


def approved_config(aux=True):
    config,_=load_config(ROOT/'config/example.json');confirm(config['mappings'][0])
    if aux:confirm(next(r for r in config['rules'] if r['kind']=='auxiliary'))
    return config


def stages_for(rows,config):
    source=report(rows,config);hierarchy=analyze_hierarchy(source,config);mapped=map_and_filter(hierarchy,config)
    voucher=validate_vouchers(mapped,hierarchy,config)
    return {'parse':source,'hierarchy':hierarchy,'mapping':mapped,'voucher':voucher}


class ExportTests(unittest.TestCase):
    def test_approved_rows_become_candidates_with_provenance(self):
        config=approved_config();stages=stages_for(base_rows(),config);bundle=build_exports(stages['voucher'],config)
        self.assertEqual(len(bundle['candidates']),2)
        self.assertEqual([r['entry_sequence'] for r in bundle['candidates']],[1,2])
        self.assertTrue(all(r['target_subject_code']=='990001' and not r['diagnostic_only'] for r in bundle['candidates']))
        self.assertEqual([d['decision'] for d in bundle['row_decisions']],['included','included'])

    def test_pending_mapping_never_becomes_official_candidate(self):
        config,_=load_config(ROOT/'config/example.json');stages=stages_for(base_rows(),config)
        bundle=build_exports(stages['voucher'],config);issues=collect_issues(bundle,stages,config)
        self.assertEqual(bundle['candidates'],[])
        self.assertEqual(len(bundle['pending']),2)
        self.assertEqual(len(bundle['diagnostic_candidates']),2)
        self.assertTrue(all(r['target_subject_code'] is None for r in bundle['diagnostic_candidates']))
        self.assertTrue(all(r['exception_ids'] for r in bundle['row_decisions']))
        self.assertEqual(len(issues),2)

    def test_auxiliary_unknown_quarantines_previously_retained_rows(self):
        config=approved_config(aux=False);stages=stages_for(base_rows(),config)
        self.assertEqual(stages['voucher']['row_counts']['retained'],2)
        bundle=build_exports(stages['voucher'],config)
        self.assertEqual(bundle['candidates'],[])
        self.assertTrue(all(r['entry_sequence'] is None for r in bundle['pending']))
        self.assertTrue(all('AUXILIARY_REQUIREMENT_UNKNOWN' in r['reason_codes'] for r in bundle['pending']))

    def test_required_auxiliary_values_not_inferred_from_account_name(self):
        config=approved_config();next(r for r in config['rules'] if r['kind']=='auxiliary')['parameters']['required_fields']=['customer']
        items=base_rows();items[0]['subject']['name']='应收款_虚构单位'
        bundle=build_exports(stages_for(items,config)['voucher'],config)
        self.assertEqual(bundle['candidates'],[])
        self.assertTrue(all(r['auxiliary']=={} for r in bundle['pending']))
        self.assertIn('REQUIRED_AUXILIARY_VALUES_UNAVAILABLE',bundle['pending'][0]['reason_codes'])

    def test_duplicate_occurrences_not_collapsed_by_export(self):
        config=approved_config();items=[row('9000.01',100),row('9000.01',100),row('9000.01',credit=200)]
        bundle=build_exports(stages_for(items,config)['voucher'],config)
        self.assertEqual(len(bundle['candidates']),3)
        self.assertEqual(len({r['record_id'] for r in bundle['candidates']}),3)

    def test_original_report_and_amounts_not_changed(self):
        config=approved_config();stages=stages_for([row('9000.01','-62.11'),row('9000.01',credit='-62.11')],config)
        original=copy.deepcopy(stages['voucher']);bundle=build_exports(original,config)
        self.assertEqual(original,stages['voucher'])
        self.assertEqual(bundle['candidates'][0]['debit']['value'],'-62.11')

    def test_mapping_issue_aggregates_facts_but_keeps_occurrences(self):
        config,_=load_config(ROOT/'config/example.json');confirm(next(r for r in config['rules'] if r['kind']=='auxiliary'))
        stages=stages_for([row('9000.01',100),row('9000.01',credit=100)],config)
        bundle=build_exports(stages['voucher'],config);issues=collect_issues(bundle,stages,config)
        self.assertEqual(len(issues),1)
        self.assertEqual(len(issues[0]['source_record_ids']),2)
        self.assertEqual(len(issues[0]['affected_record_ids']),2)

    def test_mapping_gap_affects_other_rows_without_false_direct_error(self):
        config=approved_config();stages=stages_for([row('9000.01',100),row('9999',credit=100)],config)
        bundle=build_exports(stages['voucher'],config);issues=collect_issues(bundle,stages,config)
        unknown=next(e for e in issues if e['code']=='UNMAPPED_ACCOUNT')
        self.assertEqual(len(unknown['source_record_ids']),1)
        self.assertEqual(len(unknown['affected_record_ids']),2)

    def test_excluded_rows_have_reasons_and_remain_in_sources(self):
        config=approved_config(aux=False)
        rule={'id':'exclude-test','kind':'voucher_filter','status':'candidate','policy':'exclude_voucher',
            'parameters':{'summary_equals':'合成业务','match_scope':'all_lines'},'evidence':[{'kind':'synthetic_example','reference':'test'}],'review':None}
        confirm(rule);config['rules'].append(rule);stages=stages_for(base_rows(),config)
        bundle=build_exports(stages['voucher'],config);issues=collect_issues(bundle,stages,config)
        self.assertEqual(len(bundle['excluded']),2)
        self.assertEqual(len(bundle['source_rows']),2)
        self.assertEqual(bundle['pending'],[])
        self.assertEqual(issues,[])
        self.assertTrue(all(r['reason_codes']==['CONFIRMED_VOUCHER_FILTER'] for r in bundle['excluded']))

    def test_unbalanced_voucher_is_an_exception_not_a_partial_candidate(self):
        config=approved_config();stages=stages_for([row('9000.01',100),row('9000.01',credit='99.99')],config)
        bundle=build_exports(stages['voucher'],config);issues=collect_issues(bundle,stages,config)
        self.assertFalse(bundle['candidates'])
        unbalanced=next(i for i in issues if i['code']=='UNBALANCED_VOUCHER')
        self.assertEqual(unbalanced['details']['difference'],'0.01')
        self.assertEqual(len(unbalanced['affected_record_ids']),2)

    def test_failed_bundle_never_includes_or_excludes_business_rows(self):
        config=approved_config();stages=stages_for(base_rows(),config)
        error={'code':'TEST_FAILURE','message':'synthetic','stage':'test'}
        bundle=failed_exports(stages['voucher']['rows'],error);issues=collect_issues(bundle,stages,config,error)
        self.assertEqual(bundle['candidates'],[])
        self.assertEqual(bundle['excluded'],[])
        self.assertTrue(all(d['decision']=='needs_review' and d['exception_ids'] for d in bundle['row_decisions']))
        self.assertEqual(issues[0]['severity'],'fatal')

    def test_forged_candidate_or_changed_configuration_rejected(self):
        config=approved_config();stages=stages_for(base_rows(),config)
        bad=copy.deepcopy(stages['voucher']);bad['rows'][0]['account_mapping']['target_code']='999999'
        with self.assertRaises(ExportError):build_exports(bad,config)
        config['revision']+=1
        with self.assertRaises(ExportError):build_exports(stages['voucher'],config)

    def test_late_date_amount_or_half_voucher_changes_rejected(self):
        config=approved_config();stages=stages_for(base_rows(),config)
        for field in ['date','amount','half']:
            bad=copy.deepcopy(stages['voucher'])
            if field=='date':bad['rows'][0]['voucher_validation']['posting_date']='2026-06-01'
            elif field=='amount':bad['rows'][0]['debit']['value']='99'
            else:
                bad['rows'][0]['voucher_validation']['decision']='needs_review'
                bad['row_counts']['retained']-=1;bad['row_counts']['needs_review']+=1
            with self.subTest(field=field),self.assertRaises(ExportError):build_exports(bad,config)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='smartmap-conversion-');self.folder=Path(self.temp.name)
        self.source=self.folder/'source.xlsx';self.config_path=self.folder/'config.json';self.output=self.folder/'out'
        self.config=approved_config();self.save_config();self.write_source()

    def tearDown(self):self.temp.cleanup()

    def save_config(self,bom=False):
        self.config_path.write_text(json.dumps(self.config,ensure_ascii=False),encoding='utf-8-sig' if bom else 'utf-8')

    def write_source(self,rows=None):
        header=['日期','账期','科目名称','凭证字号','摘要','借方','贷方']
        data=rows or [['2026-06-03','202606','9000.01虚构','记1','合成业务',100,None],['2026-06-03','202606','9000.01虚构','记1','合成业务',None,100]]
        write_xlsx(self.source,[('原始',[header]+data)])

    def run_job(self):return run_conversion(ConversionRequest(self.source,self.config_path,self.output))

    def load(self,name):return json.loads((self.output/name).read_text(encoding='utf-8-sig'))

    def test_completed_manifest_and_all_artifact_hashes(self):
        before=hashlib.sha256(self.source.read_bytes()).hexdigest();m=self.run_job()
        self.assertEqual(m['status'],'completed');self.assertEqual(m['exit_code'],0)
        self.assertEqual(m['row_counts']['included'],2)
        self.assertTrue(m['artifacts_complete']);self.assertFalse(m['ready_for_system_import'])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),before)
        for name,meta in m['artifacts'].items():
            self.assertEqual(hashlib.sha256((self.output/name).read_bytes()).hexdigest(),meta['sha256'])
        self.assertNotIn('run_manifest.json',m['artifacts'])
        self.assertEqual(self.load('candidates.json')['run_id'],m['run_id'])
        self.assertEqual(self.load('exceptions.json')['issue_count'],0)

    def test_candidates_are_only_review_artifacts(self):
        m=self.run_job();candidates=self.load('candidates.json')
        self.assertEqual(candidates['kind'],'candidates')
        self.assertTrue(candidates['requires_valid_manifest'])
        self.assertFalse(candidates['ready_for_system_import'])
        self.assertEqual(self.load('diagnostic_candidates.json')['row_count'],2)
        self.assertTrue(self.load('diagnostic_candidates.json')['diagnostic_only'])
        self.assertFalse(m['business_acceptance_passed'])

    def test_pending_run_has_explicit_exit_three(self):
        self.config,_=load_config(ROOT/'config/example.json');self.save_config();m=self.run_job()
        self.assertEqual((m['status'],m['exit_code']),('review_required',3))
        self.assertEqual(m['row_counts']['needs_review'],2)
        self.assertEqual(self.load('candidates.json')['row_count'],0)

    def test_existing_output_never_overwritten(self):
        self.output.mkdir();sentinel=self.output/'keep.txt';sentinel.write_text('keep',encoding='utf-8')
        with self.assertRaises(ConversionError):self.run_job()
        self.assertEqual(sentinel.read_text(encoding='utf-8'),'keep')
        self.assertFalse((self.output/'run_manifest.json').exists())

    def test_invalid_config_writes_failed_manifest_without_reading_excel(self):
        self.config_path.write_text('{"broken":',encoding='utf-8')
        with patch('smartmap.excel_reader.parse_source',side_effect=AssertionError('不应读取Excel')):m=self.run_job()
        self.assertEqual((m['status'],m['exit_code']),('failed',2))
        self.assertIsNone(m['row_counts']['total'])
        self.assertFalse(self.load('source_rows.json')['source_data_available'])
        self.assertEqual(self.load('failure_rows.json')['row_decisions'],[])

    def test_missing_source_has_failed_status_not_empty_success(self):
        missing=self.folder/'missing.xlsx'
        m=run_conversion(ConversionRequest(missing,self.config_path,self.output))
        self.assertEqual(m['status'],'failed');self.assertIsNone(m['row_counts']['total'])
        self.assertEqual(m['exit_code'],2)

    def test_corrupt_workbook_has_readable_failure(self):
        self.source.write_text('不是工作簿',encoding='utf-8');m=self.run_job()
        self.assertEqual(m['fatal_error']['code'],'UNREADABLE_WORKBOOK')
        self.assertEqual(m['row_decision_coverage'],'unavailable')

    def test_period_conflict_retains_every_read_row(self):
        self.write_source([['2026-06-03','202606','9000.01虚构','记1','合成业务',100,None],['2026-07-03','202607','9000.01虚构','记2','合成业务',None,100]])
        m=self.run_job();recovery=self.load('failure_rows.json')
        self.assertEqual(m['status'],'failed');self.assertEqual(m['row_counts']['total'],2)
        self.assertEqual(len(recovery['source_rows']),2);self.assertEqual(len(recovery['row_decisions']),2)
        self.assertEqual(m['row_decision_coverage'],'complete')
        self.assertEqual(m['last_successful_stage'],'configuration')
        self.assertIn('PERIOD_CONFLICT',m['fatal_error']['message'])

    def test_post_export_input_change_invalidates_provisional_artifacts(self):
        with patch('smartmap.conversion._check_inputs',side_effect=[None,ConversionError('INPUT_CHANGED','synthetic change')]):m=self.run_job()
        self.assertEqual(m['status'],'failed');self.assertFalse(m['artifacts_complete'])
        self.assertEqual(m['row_counts']['included'],0)
        self.assertEqual(m['row_decision_artifact']['file'],'failure_rows.json')
        self.assertTrue(all(d['decision']=='needs_review' for d in self.load('failure_rows.json')['row_decisions']))
        self.assertTrue(self.load('candidates.json')['requires_valid_manifest'])
        audited=audit_run(self.output)
        self.assertFalse(audited['usable_for_evaluation']);self.assertEqual(audited['exit_code'],2)

    def test_partial_export_io_failure_retains_recovery_ledger(self):
        with patch('smartmap.conversion.write_exports',side_effect=OSError('synthetic output failure')):m=self.run_job()
        self.assertEqual(m['status'],'failed');self.assertEqual(m['fatal_error']['code'],'IO_ERROR')
        self.assertEqual(m['row_counts']['total'],2)
        self.assertEqual(len(self.load('failure_rows.json')['source_rows']),2)
        self.assertEqual(m['row_decision_coverage'],'complete')

    def test_internal_error_keeps_trace_and_last_successful_stage(self):
        with patch('smartmap.vouchers.validate_vouchers',side_effect=RuntimeError('synthetic failure')):m=self.run_job()
        self.assertEqual(m['fatal_error']['code'],'INTERNAL_ERROR')
        self.assertEqual(m['last_successful_stage'],'mapping')
        self.assertIn('RuntimeError',self.load('failure.json')['traceback'])
        self.assertEqual(m['row_counts']['needs_review'],2)

    def test_live_config_change_is_detected(self):
        from smartmap.conversion_output import write_exports as real_write
        def changed(*args,**kwargs):
            real_write(*args,**kwargs)
            with self.config_path.open('a',encoding='utf-8') as stream:stream.write(' ')
        with patch('smartmap.conversion.write_exports',side_effect=changed):m=self.run_job()
        self.assertEqual(m['fatal_error']['code'],'INPUT_CHANGED')
        self.assertEqual(m['status'],'failed')

    def test_bom_configuration_snapshot_keeps_original_bytes(self):
        self.save_config(bom=True);before=self.config_path.read_bytes();m=self.run_job()
        self.assertEqual((self.output/'config_snapshot.json').read_bytes(),before)
        self.assertEqual(m['inputs']['config']['sha256'],hashlib.sha256(before).hexdigest())

    def test_auditor_accepts_valid_run_and_detects_artifact_tampering(self):
        self.run_job();self.assertTrue(audit_run(self.output)['all_passed'])
        with (self.output/'candidates.json').open('a',encoding='utf-8') as stream:stream.write(' ')
        audited=audit_run(self.output)
        self.assertFalse(audited['all_passed']);self.assertFalse(audited['usable_for_evaluation'])
        self.assertFalse(audited['checks']['artifact_hashes_match'])

    def test_auditor_refuses_to_change_sealed_run_directory(self):
        self.run_job();audit_path=self.output/'audit.json'
        command=[sys.executable,'-B','tools/verify_conversion_run.py','--run',str(self.output),'--audit-out',str(audit_path)]
        self.assertEqual(subprocess.run(command,cwd=ROOT,capture_output=True).returncode,2)
        self.assertFalse(audit_path.exists());self.assertTrue(audit_run(self.output)['all_passed'])

    def test_csv_text_does_not_reactivate_quarantined_formulas(self):
        path=self.folder/'text.csv'
        write_csv(path,[{'summary':'=1+1','amount':'-62.11','raw':{'value':'=1+1'}}],['summary','amount','raw'])
        with path.open(encoding='utf-8-sig',newline='') as stream:item=next(csv.DictReader(stream))
        self.assertEqual(item['summary'],"'=1+1")
        self.assertEqual(item['amount'],'-62.11')
        self.assertEqual(json.loads(item['raw'])['value'],'=1+1')

    def test_evidence_and_exception_references_resolve(self):
        self.config,_=load_config(ROOT/'config/example.json');self.save_config();self.run_job()
        index=self.load('evidence_index.json')
        for ref in index['evidence'].values():
            obj=self.load(ref['artifact'])
            for part in ref['pointer'].strip('/').split('/'):obj=obj[int(part)] if isinstance(obj,list) else obj[part]
            self.assertIn('evidence_id',obj)
        for ref in index['rules'].values():
            obj=self.load(ref['artifact'])
            for part in ref['pointer'].strip('/').split('/'):obj=obj[int(part)] if isinstance(obj,list) else obj[part]
            self.assertIn('id',obj)
        issues={i['issue_id'] for i in self.load('exceptions.json')['issues']}
        self.assertTrue(all(set(r['exception_ids'])<=issues for r in self.load('row_decisions.json')['rows']))

    def test_generated_output_is_ignored_even_outside_reports(self):
        self.run_job()
        git=shutil.which('git')
        self.assertIsNotNone(git)
        subprocess.run([git,'init','--quiet',str(self.folder)],check=True)
        for filename in ['config_snapshot.json','candidates.json','source_rows.json','exceptions.csv','run_manifest.json']:
            self.assertEqual(subprocess.run([git,'-C',str(self.folder),'check-ignore','--no-index','--quiet','out/'+filename]).returncode,0)

    def test_cli_forbids_reference_answer_arguments(self):
        command=[sys.executable,'-B','-m','smartmap','convert','--source',str(self.source),'--config',str(self.config_path),'--out',str(self.output),'--manual-workbook','answer.xlsx']
        self.assertEqual(subprocess.run(command,cwd=ROOT,capture_output=True).returncode,2)
        self.assertFalse(self.output.exists())

    def test_cli_completed_exit_and_requested_period_conflict(self):
        command=[sys.executable,'-B','-m','smartmap','convert','--source',str(self.source),'--config',str(self.config_path),'--out',str(self.output)]
        result=subprocess.run(command,cwd=ROOT,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        second=self.folder/'other'
        conflict=command.copy();conflict[-1]=str(second);conflict+=['--period','202607']
        self.assertEqual(subprocess.run(conflict,cwd=ROOT,capture_output=True).returncode,2)
        m=json.loads((second/'run_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(m['status'],'failed');self.assertEqual(m['row_counts']['total'],2)


if __name__=='__main__':unittest.main()
