"""T07只解析指定原始明细账；不执行层级删除、科目映射或凭证筛选。"""
import hashlib
import zipfile
from collections import Counter
from pathlib import Path

from .configuration import compile_config, validate_config
from .headers import DEFAULT_PREFIXES, build_aliases, import_role, incomplete_flat_header, locate
from .values import blank, parse_date, parse_money, parse_period, parse_subject, parse_voucher, raw_value

NON_BUSINESS={'期初余额':'NON_BUSINESS_OPENING','本期合计':'NON_BUSINESS_PERIOD_TOTAL','本年累计':'NON_BUSINESS_YEAR_TOTAL'}


class SourceError(ValueError):
    def __init__(self,code,message,details=None):
        super().__init__(message);self.code=code;self.details=details or {}


def digest(path):
    hasher=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):hasher.update(chunk)
    return hasher.hexdigest()


def probe(sheet,aliases):
    # 不信任Excel缓存的dimension；真正的数据范围由XML中的行决定。
    sheet.reset_dimensions()
    previous=None
    for number,cells in enumerate(sheet.iter_rows(),1):
        values=[c.value for c in cells]
        if import_role(values):return {'sheet':sheet.title,'role':'import_or_manual','header_row':number}
        try:
            columns=locate(values,aliases)
            if columns:return {'sheet':sheet.title,'role':'ledger','header_rows':[number],'columns':columns}
            columns=locate(values,aliases,previous)
            if columns:return {'sheet':sheet.title,'role':'ledger','header_rows':[number-1,number],'columns':columns}
        except ValueError as error:
            return {'sheet':sheet.title,'role':'ambiguous_header','header_row':number,'message':str(error)}
        if incomplete_flat_header(values,aliases):
            return {'sheet':sheet.title,'role':'unrecognized','header_row':number,'message':'平面表头不符合原始明细账，未扫描业务行'}
        previous=values
    return {'sheet':sheet.title,'role':'unrecognized'}


def _parse_row(cells,number,columns,source_hash,sheet_name,prefixes,requested_period,aliases):
    values=[cell.value for cell in cells]
    def get(name):
        index=columns.get(name)
        return values[index] if index is not None and index<len(values) else None
    raw={name:raw_value(get(name)) for name in columns}
    row={'record_id':hashlib.sha256(f'{source_hash}\0{sheet_name}\0{number}'.encode('utf-8')).hexdigest(),
        'provenance':{'input_sha256':source_hash,'sheet':sheet_name,'excel_row':number},
        'raw_fields':raw,'raw_cells':[{'column':i+1,'data_type':cell.data_type,'raw':raw_value(cell.value)} for i,cell in enumerate(cells)],
        'parse_state':'parsed','issues':[], 'period':None,'period_origin':None,'date':None,'voucher':None,'subject':None,
        'summary':'' if blank(get('summary')) else str(get('summary')).strip(),
        'debit':parse_money(get('debit')),'credit':parse_money(get('credit'))}
    def issue(code,message,field=None):row['issues'].append({'code':code,'message':message,'field':field})
    if all(blank(x) for x in values):
        row.update(parse_state='non_business',reason_code='EMPTY_ROW');return row
    metadata=lambda x:isinstance(x,str) and x.startswith(('制作人：','制作人:','导出时间：','导出时间:'))
    if any(metadata(x) for x in values) and all(blank(x) or metadata(x) for x in values):
        row.update(parse_state='non_business',reason_code='EXPORT_METADATA');return row
    try: repeated=locate(values,aliases)
    except ValueError:repeated=None
    if repeated==columns:
        row.update(parse_state='non_business',reason_code='REPEATED_HEADER');return row
    for field,parser,code in [('date',parse_date,'INVALID_DATE'),('period',parse_period,'INVALID_PERIOD')]:
        try:row[field]=parser(get(field))
        except ValueError as error:issue(code,str(error),field)
    if row['period'] is not None:row['period_origin']='source_period'
    elif blank(get('period')) and row['date']:
        row.update(period=row['date'][:7].replace('-',''),period_origin='source_date')
    elif blank(get('period')) and requested_period:
        row.update(period=requested_period,period_origin='request')
    try:row['voucher']=parse_voucher(get('voucher'),prefixes)
    except ValueError as error:issue('INVALID_VOUCHER',str(error),'voucher')
    try:row['subject']=parse_subject(get('subject'),get('subject_code'))
    except ValueError as error:issue('INVALID_SUBJECT',str(error),'subject')
    if row['voucher'] and not blank(get('category')):
        category=str(get('category')).strip()
        if row['voucher']['category'] and row['voucher']['category']!=category:
            issue('VOUCHER_CATEGORY_CONFLICT','凭证字号前缀与独立类别不一致，不能猜测两者等价','category')
        elif not row['voucher']['category']:row['voucher']['category']=category
    for i,cell in enumerate(cells):
        if cell.data_type in ['f','e']:
            issue('FORMULA_CELL' if cell.data_type=='f' else 'ERROR_CELL','原始公式或Excel错误值须核验，不使用缓存猜金额',f'column_{i+1}')
    for side in ['debit','credit']:
        if row[side]['status']=='invalid':issue('INVALID_AMOUNT','金额格式无效，原值已保留',side)
    if row['period'] and row['date'] and row['period']!=row['date'][:7].replace('-',''):
        issue('DATE_PERIOD_MISMATCH','原始日期月份与账期不同，待确认','date')
    if blank(get('voucher')) and row['subject'] and row['summary'] in NON_BUSINESS:
        row.update(parse_state='non_business',reason_code=NON_BUSINESS[row['summary']])
    elif not row['voucher']:
        issue('MISSING_VOUCHER' if blank(get('voucher')) else 'UNPARSED_VOUCHER','未取得有效凭证归属，不能静默排除','voucher')
    if row['parse_state']!='non_business':
        if row['period'] is None:issue('MISSING_PERIOD','未能从原始账期、日期或请求取得账期','period')
        if all(row[s]['status']=='blank' for s in ['debit','credit']):issue('EMPTY_BUSINESS_AMOUNT','有业务字段但借贷均为空，待确认')
        if blank(get('summary')):issue('MISSING_SUMMARY','业务摘要为空，待确认','summary')
    if row['issues']:row['parse_state']='needs_review'
    return row


def parse_source(source,config,requested_period=None):
    """返回带行级证据的报告；只选择原始表，所有数据行均保留解析终态。"""
    validate_config(config)
    try:
        request_period=parse_period(requested_period)
        if requested_period is not None and (not isinstance(requested_period,str) or request_period!=requested_period):
            raise ValueError('请求账期必须是YYYYMM')
    except ValueError as error:raise SourceError('INVALID_REQUEST_PERIOD',str(error)) from error
    source=Path(source)
    if source.suffix.lower()!='.xlsx':raise SourceError('UNSUPPORTED_SOURCE','当前只读取.xlsx文件')
    try:source_hash=digest(source)
    except OSError as error:raise SourceError('SOURCE_NOT_READABLE',str(error)) from error
    try:
        import openpyxl
    except ImportError as error:raise SourceError('MISSING_DEPENDENCY','请安装requirements.txt中的openpyxl，或使用已带该库的Python') from error
    try:book=openpyxl.load_workbook(source,read_only=True,data_only=False,keep_links=False)
    except (OSError,ValueError,zipfile.BadZipFile,KeyError) as error:
        raise SourceError('UNREADABLE_WORKBOOK',f'无法读取Excel：{error}') from error
    try:
        profile=config['source_profile'];aliases=build_aliases(profile)
        selected=profile['worksheet_name']
        if selected is not None:
            if selected not in book.sheetnames:raise SourceError('SOURCE_SHEET_NOT_FOUND','配置指定的原始工作表不存在',{'requested_sheet':selected})
            probes=[probe(book[selected],aliases)]
        else:
            probes=[probe(sheet,aliases) for sheet in book.worksheets]
        ambiguous=[p for p in probes if p['role']=='ambiguous_header']
        ledgers=[p for p in probes if p['role']=='ledger']
        if ambiguous:raise SourceError('AMBIGUOUS_HEADER','表头存在重复或冲突字段，未自动选择',{'probes':probes})
        if len(ledgers)!=1:
            code='AMBIGUOUS_SOURCE_SHEET' if len(ledgers)>1 else 'SOURCE_SHEET_ROLE_MISMATCH' if selected and probes[0]['role']=='import_or_manual' else 'SOURCE_HEADER_NOT_FOUND'
            raise SourceError(code,'未取得唯一原始明细账；人工整理/导入表不能作为原始表',{'probes':probes})
        header=ledgers[0];sheet=book[header['sheet']];sheet.reset_dimensions()
        start=header['header_rows'][-1]+1
        prefixes=profile.get('voucher_prefixes',DEFAULT_PREFIXES)
        rows=[_parse_row(cells,number,header['columns'],source_hash,sheet.title,prefixes,request_period,aliases)
            for number,cells in enumerate(sheet.iter_rows(min_row=start),start)]
        periods={row['period'] for row in rows if row['period']}
        fatal=[]
        if len(periods)>1:fatal.append({'code':'PERIOD_CONFLICT','message':'原始表包含多个账期，不能擅自选择','periods':sorted(periods)})
        period=next(iter(periods)) if len(periods)==1 else None
        if request_period and any(p!=request_period for p in periods):
            fatal.append({'code':'PERIOD_CONFLICT','message':'请求账期与原始文件冲突','requested_period':request_period,'observed_periods':sorted(periods)})
        if period is None and not periods:fatal.append({'code':'MISSING_PERIOD','message':'无法确定账期'})
        if not any(row['voucher'] for row in rows):fatal.append({'code':'NO_VOUCHER_RECORDS','message':'表头匹配但没有可解析的凭证记录'})
        states=Counter(row['parse_state'] for row in rows)
        _,effective_sha=compile_config(config)
        report={'report_version':'0.2','mode':'source_parse_only','read_ok':not fatal,
            'reader_version':'0.2-t07','openpyxl_version':openpyxl.__version__,
            'ready_for_next_stage':not fatal and not states['needs_review'],
            'conversion_implemented':False,'ready_for_system_import':False,
            'input_sha256':source_hash,'source_file':source.name,'config_id':config['config_id'],'config_revision':config['revision'],
            'effective_config_sha256':effective_sha,'source_sheet':sheet.title,'header':header,'sheet_probes':probes,
            'period':period,'periods':sorted(periods),'fatal_errors':fatal,
            'row_counts':{'total':len(rows),'parsed':states['parsed'],'needs_review':states['needs_review'],'non_business':states['non_business']},
            'non_business_counts':dict(Counter(row.get('reason_code') for row in rows if row['parse_state']=='non_business')),
            'issue_counts':dict(Counter(issue['code'] for row in rows for issue in row['issues'])),
            'parsed_voucher_groups':len({(row['period'],row['voucher']['category'],row['voucher']['number']) for row in rows if row['parse_state']=='parsed' and row['voucher']}),
            'rows':rows}
    finally:book.close()
    try:unchanged=digest(source)==source_hash
    except OSError:unchanged=False
    if not unchanged:raise SourceError('INPUT_CHANGED','读取期间原始文件发生变化，请重新运行')
    return report
