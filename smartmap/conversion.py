"""T11统一候选转换执行器。仅输入原始文件和配置，所有产物写新目录。"""
import hashlib
import json
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import __version__
from .candidate_export import ExportError, build_exports, failed_exports
from .configuration import ConfigurationError, SCHEMA_PATH, compile_config, load_config
from .contracts import ConversionRequest
from .conversion_issues import collect_issues
from .conversion_output import artifact_inventory, stage_snapshot, write_bytes, write_exports, write_json, write_summary


class ConversionError(ValueError):
    def __init__(self,code,message,details=None):
        super().__init__(message);self.code=code;self.details=details or {}


def _sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _now():return datetime.now(timezone.utc).isoformat()


def _check_inputs(manifest,request):
    for name,path in [('source',request.source),('config',request.config)]:
        expected=manifest['inputs'][name].get('sha256')
        if expected is not None and (not path.is_file() or _sha(path)!=expected):
            raise ConversionError('INPUT_CHANGED','运行期间输入或配置发生变化，需重新运行',{'input':name})
    if _sha(SCHEMA_PATH)!=manifest['schema_sha256']:raise ConversionError('SCHEMA_CHANGED','运行期间配置格式定义发生变化')


def run_conversion(request:ConversionRequest):
    from .excel_reader import SourceError, parse_source
    from .hierarchy import HierarchyError, analyze_hierarchy
    from .mapping_filter import MappingFilterError, map_and_filter
    from .vouchers import VoucherError, validate_vouchers
    output=request.output_dir.resolve()
    if request.output_dir.is_symlink() or output.exists():raise ConversionError('OUTPUT_EXISTS','输出目录已存在或是符号链接，请使用全新目录')
    if output in [request.source.resolve(),request.config.resolve()]:raise ConversionError('OUTPUT_OVERLAPS_INPUT','输出不能等于输入路径')
    output.mkdir(parents=True,exist_ok=False)
    write_bytes(output/'.gitignore',b'*\n')
    manifest={'schema_version':'0.2','mode':'candidate_conversion','run_id':str(uuid4()),'program_version':__version__,
        'started_at_utc':_now(),'display_timezone':'Asia/Shanghai','status':'running','exit_code':None,
        'conversion_implemented':True,'output_kind':'review_candidates','ready_for_system_import':False,
        'business_acceptance_passed':False,'last_completed_stage':None,'last_successful_stage':None,'requested_period':request.period,
        'allow_partial':request.allow_partial,'partial_export':False,'batch_complete':False,
        'inputs':{'source':{'path':str(request.source.resolve()),'sha256':None},'config':{'path':str(request.config.resolve()),'sha256':None}},
        'schema_sha256':None,'stage_results':{},'artifacts':{}}
    stages={};config=None;bundle=None;issues=[];stage='configuration';fatal=None;recovery_written=False
    try:
        manifest['schema_sha256']=_sha(SCHEMA_PATH)
        manifest['inputs']['config']['sha256']=_sha(request.config)
        config,config_sha=load_config(request.config)
        snapshot=request.config.read_bytes()
        if config_sha!=manifest['inputs']['config']['sha256'] or hashlib.sha256(snapshot).hexdigest()!=config_sha:
            raise ConversionError('CONFIG_CHANGED','配置读取期间发生变化')
        write_bytes(output/'config_snapshot.json',snapshot)
        write_bytes(output/'schema_snapshot.json',SCHEMA_PATH.read_bytes())
        manifest.update(config_id=config['config_id'],config_revision=config['revision'],effective_config_sha256=compile_config(config)[1])
        manifest['last_completed_stage']='configuration'
        manifest['last_successful_stage']='configuration'
        stage='source_parse';manifest['inputs']['source']['sha256']=_sha(request.source)
        functions=[('parse',lambda:parse_source(request.source,config,request.period)),
            ('hierarchy',lambda:analyze_hierarchy(stages['parse'],config)),
            ('mapping',lambda:map_and_filter(stages['hierarchy'],config)),
            ('voucher',lambda:validate_vouchers(stages['mapping'],stages['hierarchy'],config))]
        for name,operation in functions:
            stage=name;report=operation();report['config_sha256']=config_sha;stages[name]=report
            stage_snapshot(output,name,report,manifest['run_id'])
            manifest['stage_results'][name]={'analysis_ok':report.get('read_ok',report.get('analysis_ok',False)),
                'row_counts':report['row_counts'],'period':report['period']}
            manifest['last_completed_stage']=name
            if not report.get('read_ok',report.get('analysis_ok',False)):
                detail='；'.join(e['code']+'：'+e['message'] for e in report.get('fatal_errors',[]))
                raise ConversionError('SOURCE_PARSE_BLOCKED','原始解析未通过：'+detail,{'fatal_errors':report.get('fatal_errors',[])})
            if report['input_sha256']!=manifest['inputs']['source']['sha256']:
                raise ConversionError('INPUT_CHANGED','源文件在快照与解析之间发生变化')
            manifest['last_successful_stage']=name
        if config.get('auxiliary_profile'):
            from .auxiliary import analyze_auxiliary
            stage='auxiliary';report=analyze_auxiliary(stages['voucher'],config)
            stages['auxiliary']=report
            stage_snapshot(output,'auxiliary',report,manifest['run_id'])
            manifest['stage_results']['auxiliary']={'analysis_ok':True,'row_counts':report['row_counts'],'period':report['period'],'auxiliary_analysis':report['auxiliary_analysis']}
            manifest['last_completed_stage']=manifest['last_successful_stage']='auxiliary'
        stage='candidate_export';_check_inputs(manifest,request)
        bundle=build_exports(stages.get('auxiliary',stages['voucher']),config,allow_partial=request.allow_partial)
        manifest['partial_export']=bundle['batch_gate']['partial_export']
        manifest['batch_complete']=not bundle['pending']
        manifest['eligible_core_candidate_count']=len(bundle['batch_gate']['eligible_record_ids'])
        issues=collect_issues(bundle,stages,config)
        write_exports(output,bundle,issues,stages,config,manifest['run_id'])
        manifest['last_completed_stage']='candidate_export'
        _check_inputs(manifest,request)
        manifest['last_successful_stage']='candidate_export'
    except Exception as error:
        known=isinstance(error,(ConversionError,ConfigurationError,SourceError,HierarchyError,MappingFilterError,VoucherError,ExportError,OSError))
        code=getattr(error,'code','CONFIGURATION_ERROR' if isinstance(error,ConfigurationError) else 'IO_ERROR' if isinstance(error,OSError) else 'INTERNAL_ERROR')
        fatal={'code':code,'message':str(error),'stage':stage,'details':getattr(error,'details',{})}
        latest=next((stages[name] for name in ['voucher','mapping','hierarchy','parse'] if name in stages),None)
        bundle=failed_exports(latest['rows'] if latest else None,fatal)
        issues=collect_issues(bundle,stages,config,fatal)
        # 即使标准产物已部分写入，也先另存完整失败去向，不覆盖或丢掉排错证据。
        try:
            write_json(output/'failure_rows.json',{'run_id':manifest['run_id'],'source_data_available':bundle['source_data_available'],
                'source_rows':bundle['source_rows'],'row_decisions':bundle['row_decisions'],'pending_rows':bundle['pending'],'issues':issues})
            recovery_written=True
        except OSError as secondary:fatal['recovery_write_error']=str(secondary)
        try:
            if not (output/'candidates.json').exists():write_exports(output,bundle,issues,stages,config,manifest['run_id'])
            write_json(output/'failure.json',{'fatal_error':fatal,'traceback':traceback.format_exc() if not known else None})
        except OSError as secondary:fatal['report_write_error']=str(secondary)
    counts=Counter(r['decision'] for r in bundle['row_decisions']) if bundle else Counter()
    review=bool(counts['needs_review'] or any(i['severity'] in ['error','needs_confirmation','fatal'] for i in issues))
    manifest.update(status='failed' if fatal else 'review_required' if review else 'completed',exit_code=2 if fatal else 3 if review else 0,
        completed_at_utc=_now(),period=stages.get('voucher',stages.get('parse',{})).get('period'),
        month_end=stages.get('voucher',{}).get('month_end'),fatal_error=fatal,
        row_counts={'total':len(bundle['row_decisions']) if bundle and bundle['source_data_available'] else None,
            **{key:counts[key] for key in ['included','excluded','needs_review','non_business']}},
        diagnostic_candidate_count=len(bundle['diagnostic_candidates']) if bundle else 0,
        exception_issue_count=len(issues),affected_record_count=len({i for e in issues for i in e['affected_record_ids']}),
        artifacts_complete=not fatal,row_decision_coverage='complete' if bundle and bundle['source_data_available'] and (not fatal or recovery_written) else 'unavailable',
        row_decision_artifact={'file':'failure_rows.json','pointer':'/row_decisions'} if fatal and recovery_written else {'file':'row_decisions.json','pointer':'/rows'},
        source_rows_artifact={'file':'failure_rows.json','pointer':'/source_rows'} if fatal and recovery_written else {'file':'source_rows.json','pointer':'/rows'},
        exceptions_artifact={'file':'failure_rows.json','pointer':'/issues'} if fatal and recovery_written else {'file':'exceptions.json','pointer':'/issues'})
    if fatal:manifest.update(partial_export=False,batch_complete=False)
    try:
        write_summary(output,manifest)
        manifest['artifacts']=artifact_inventory(output)
        write_json(output/'run_manifest.json',manifest)
    except OSError as error:
        manifest.update(status='failed',exit_code=2,manifest_written=False,finalization_error=str(error))
    return manifest
