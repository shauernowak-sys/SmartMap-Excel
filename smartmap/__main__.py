"""统一候选转换及阶段调试命令；独立历史评估后续实现。"""
import argparse
import json
from pathlib import Path

from .configuration import ConfigurationError, inspect_config, load_config


def main():
    parser=argparse.ArgumentParser(description='SmartMap Excel 候选转换与阶段审计（尚未适配系统导入）')
    commands=parser.add_subparsers(dest='command',required=True)
    check=commands.add_parser('config-check',help='只检查配置，不批准规则、不执行转换')
    check.add_argument('--config',required=True,type=Path)
    check.add_argument('--out',type=Path,help='可选：新建JSON报告文件，禁止覆盖')
    parse=commands.add_parser('parse-source',help='只解析原始明细账并保留全部行，不映射、不去重')
    parse.add_argument('--source',required=True,type=Path)
    parse.add_argument('--config',required=True,type=Path)
    parse.add_argument('--out',required=True,type=Path,help='全新报告目录，禁止覆盖')
    parse.add_argument('--period',help='可选YYYYMM，与原始账期冲突则拒绝')
    hierarchy=commands.add_parser('analyze-hierarchy',help='按原始数据生成父子组证据，未确认策略不实际排除')
    hierarchy.add_argument('--source',required=True,type=Path)
    hierarchy.add_argument('--config',required=True,type=Path)
    hierarchy.add_argument('--out',required=True,type=Path,help='全新报告目录，禁止覆盖')
    hierarchy.add_argument('--period',help='可选YYYYMM，与原始账期冲突则拒绝')
    mapping=commands.add_parser('map-and-filter',help='只使用已确认配置映射科目或排除整张凭证')
    mapping.add_argument('--source',required=True,type=Path)
    mapping.add_argument('--config',required=True,type=Path)
    mapping.add_argument('--out',required=True,type=Path,help='全新报告目录，禁止覆盖')
    mapping.add_argument('--period',help='可选YYYYMM，与原始账期冲突则拒绝')
    voucher=commands.add_parser('validate-vouchers',help='完整凭证精确平衡、月末日期及序号；诊断不代替规则确认')
    voucher.add_argument('--source',required=True,type=Path)
    voucher.add_argument('--config',required=True,type=Path)
    voucher.add_argument('--out',required=True,type=Path,help='全新报告目录，禁止覆盖')
    voucher.add_argument('--period',help='可选YYYYMM，与原始账期冲突则拒绝')
    convert=commands.add_parser('convert',help='输出审核候选、待确认、异常和逐行追溯，不生成导入模板')
    convert.add_argument('--source',required=True,type=Path)
    convert.add_argument('--config',required=True,type=Path)
    convert.add_argument('--out',required=True,type=Path,help='全新输出目录，禁止覆盖')
    convert.add_argument('--period',help='可选YYYYMM，与原始账期冲突则拒绝')
    convert.add_argument('--allow-partial',action='store_true',help='明确选择仅输出完整正常凭证；整批仍标记待确认')
    args=parser.parse_args()
    if args.command=='convert':
        from .contracts import ConversionRequest
        from .conversion import ConversionError, run_conversion
        try:
            manifest=run_conversion(ConversionRequest(args.source,args.config,args.out,args.period,args.allow_partial))
            print(json.dumps({k:v for k,v in manifest.items() if k!='artifacts'},ensure_ascii=False,indent=2))
            return manifest['exit_code']
        except (ValueError,OSError) as error:
            print(json.dumps({'status':'failed','error':str(error),'code':getattr(error,'code','INVALID_REQUEST'),'exit_code':2},ensure_ascii=False))
            return 2
    try:
        config,sha=load_config(args.config)
        if args.command=='parse-source':
            return _parse_command(args,config,sha)
        if args.command=='analyze-hierarchy':
            return _hierarchy_command(args,config,sha)
        if args.command=='map-and-filter':
            return _mapping_command(args,config,sha)
        if args.command=='validate-vouchers':
            return _voucher_command(args,config,sha)
        report=inspect_config(config,sha)
        serialized=json.dumps(report,ensure_ascii=False,indent=2)+'\n'
        if args.out is not None:
            args.out.parent.mkdir(parents=True,exist_ok=True)
            with args.out.open('x',encoding='utf-8') as handle:
                handle.write(serialized)
        print(serialized,end='')
        return 0
    except ConfigurationError as error:
        print(json.dumps({'config_valid':False,'error':str(error)},ensure_ascii=False))
        return 2
    except OSError as error:
        print(json.dumps({'config_valid':True,'report_written':False,'error':str(error)},ensure_ascii=False))
        return 2


def _parse_command(args,config,config_sha):
    # 延迟导入：纯配置检查仍不依赖openpyxl。
    from .excel_reader import SourceError, parse_source
    output=args.out.resolve()
    if output in [args.source.resolve(),args.config.resolve()]:
        raise ConfigurationError('输出路径不能等于输入或配置路径')
    if output.exists():raise ConfigurationError('输出目录已存在，请使用新目录')
    output.mkdir(parents=True,exist_ok=False)
    try:report=parse_source(args.source,config,args.period)
    except SourceError as error:
        report={'mode':'source_parse_only','read_ok':False,'ready_for_next_stage':False,
            'conversion_implemented':False,'ready_for_system_import':False,
            'fatal_errors':[{'code':error.code,'message':str(error),'details':error.details}]}
    report['config_sha256']=config_sha
    with (output/'parse_report.json').open('x',encoding='utf-8') as handle:
        json.dump(report,handle,ensure_ascii=False,indent=2,allow_nan=False)
    summary={key:value for key,value in report.items() if key!='rows'}
    with (output/'summary.json').open('x',encoding='utf-8') as handle:
        json.dump(summary,handle,ensure_ascii=False,indent=2,allow_nan=False)
    counts=report.get('row_counts',{})
    lines=['# T07 原始明细账解析报告','',f"读取完成：{report['read_ok']}；可进入后续处理：{report['ready_for_next_stage']}。",
        f"工作表：{report.get('source_sheet','未选择')}；账期：{report.get('period','未确定')}。",
        f"数据行：{counts.get('total',0)}；解析通过：{counts.get('parsed',0)}；非业务：{counts.get('non_business',0)}；待确认：{counts.get('needs_review',0)}。",'',
        '本报告保留原始行，没有执行父子去重、凭证排除、科目映射或系统导入。',
        '原值、单元格类型、行号、解析状态及异常见parse_report.json；全局错误见summary.json。']
    with (output/'summary.md').open('x',encoding='utf-8') as handle:handle.write('\n'.join(lines)+'\n')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 2 if not report['read_ok'] else 3 if counts.get('needs_review') else 0


def _hierarchy_command(args,config,config_sha):
    from .excel_reader import SourceError, parse_source
    from .hierarchy import HierarchyError, analyze_hierarchy
    from .hierarchy_report import write_hierarchy_report
    output=args.out.resolve()
    if output in [args.source.resolve(),args.config.resolve()]:raise ConfigurationError('输出路径不能等于输入或配置路径')
    if output.exists():raise ConfigurationError('输出目录已存在，请使用新目录')
    output.mkdir(parents=True,exist_ok=False)
    parsed=None
    try:
        parsed=parse_source(args.source,config,args.period);parsed['config_sha256']=config_sha
        report=analyze_hierarchy(parsed,config)
    except (SourceError,HierarchyError) as error:
        report={'mode':'hierarchy_analysis','analysis_ok':False,'ready_for_next_stage':False,
            'conversion_implemented':False,'ready_for_system_import':False,'source_file':args.source.name,
            'fatal_errors':[{'code':error.code,'message':str(error)}]}
    report['config_sha256']=config_sha
    summary=write_hierarchy_report(output,parsed,report)
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 2 if not report['analysis_ok'] else 3 if report['row_counts']['needs_review'] else 0


def _mapping_command(args,config,config_sha):
    from .excel_reader import SourceError, parse_source
    from .hierarchy import HierarchyError, analyze_hierarchy
    from .mapping_filter import MappingFilterError, map_and_filter
    from .mapping_report import write_mapping_report
    output=args.out.resolve()
    if output in [args.source.resolve(),args.config.resolve()]:raise ConfigurationError('输出路径不能等于输入或配置路径')
    if output.exists():raise ConfigurationError('输出目录已存在，请使用新目录')
    output.mkdir(parents=True,exist_ok=False)
    parsed=None;hierarchy=None
    try:
        parsed=parse_source(args.source,config,args.period);parsed['config_sha256']=config_sha
        hierarchy=analyze_hierarchy(parsed,config);hierarchy['config_sha256']=config_sha
        report=map_and_filter(hierarchy,config)
    except (SourceError,HierarchyError,MappingFilterError) as error:
        report={'mode':'mapping_filter_analysis','analysis_ok':False,'ready_for_next_stage':False,
            'conversion_implemented':False,'ready_for_system_import':False,'source_file':args.source.name,
            'fatal_errors':[{'code':error.code,'message':str(error)}]}
    report['config_sha256']=config_sha
    summary=write_mapping_report(output,parsed,hierarchy,report)
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 2 if not report['analysis_ok'] else 3 if report['row_counts']['needs_review'] else 0


def _voucher_command(args,config,config_sha):
    from .excel_reader import SourceError, parse_source
    from .hierarchy import HierarchyError, analyze_hierarchy
    from .mapping_filter import MappingFilterError, map_and_filter
    from .vouchers import VoucherError, validate_vouchers
    from .voucher_report import write_voucher_report
    output=args.out.resolve()
    if output in [args.source.resolve(),args.config.resolve()]:raise ConfigurationError('输出路径不能等于输入或配置路径')
    if output.exists():raise ConfigurationError('输出目录已存在，请使用新目录')
    output.mkdir(parents=True,exist_ok=False)
    parsed=None;hierarchy=None;mapped=None
    try:
        parsed=parse_source(args.source,config,args.period);parsed['config_sha256']=config_sha
        hierarchy=analyze_hierarchy(parsed,config);hierarchy['config_sha256']=config_sha
        mapped=map_and_filter(hierarchy,config);mapped['config_sha256']=config_sha
        report=validate_vouchers(mapped,hierarchy,config)
    except (SourceError,HierarchyError,MappingFilterError,VoucherError) as error:
        report={'mode':'voucher_validation','analysis_ok':False,'ready_for_next_stage':False,
            'conversion_implemented':False,'ready_for_system_import':False,'source_file':args.source.name,
            'fatal_errors':[{'code':error.code,'message':str(error)}]}
    report['config_sha256']=config_sha
    summary=write_voucher_report(output,parsed,hierarchy,mapped,report)
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 2 if not report['analysis_ok'] else 3 if report['row_counts']['needs_review'] else 0


if __name__=='__main__':
    raise SystemExit(main())
