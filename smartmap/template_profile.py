"""系统模板字段约定。与科目映射、原始账解析分离，不包含历史分录。"""
import hashlib
import json
from pathlib import Path

SOURCES = {'posting_date', 'voucher_category', 'voucher_number', 'entry_sequence',
           'summary', 'target_subject_code', 'target_subject_name', 'debit', 'credit',
           'auxiliary.customer', 'auxiliary.supplier', 'auxiliary.employee',
           'auxiliary.department', 'auxiliary.project', 'auxiliary.stock_name',
           'auxiliary.customer_level', 'auxiliary.inbound_type', 'auxiliary.outbound_type'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_profile(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    validate_profile(value)
    return value


def validate_profile(p):
    required = {'version', 'id', 'sheet_names', 'data_sheet', 'columns',
                'category_mapping', 'date_format', 'filter_policy', 'reference'}
    if set(p) != required or p['version'] != 1:
        raise ValueError('模板配置字段或版本不符合约定')
    names = p['sheet_names']
    if not isinstance(names, list) or len(names) != len(set(names)) or p['data_sheet'] not in names:
        raise ValueError('模板工作表顺序/名称配置无效')
    if p['filter_policy'] != 'data_range':
        raise ValueError('需明确按实际数据范围调整筛选及类别校验')
    columns = p['columns']
    if not columns or len({c['header'] for c in columns}) != len(columns):
        raise ValueError('模板列不能为空或重名')
    seen = set()
    for c in columns:
        if set(c) != {'header', 'source', 'kind', 'required'} or type(c['required']) is not bool:
            raise ValueError('模板列配置字段无效')
        if not isinstance(c['header'], str) or not c['header'].strip():
            raise ValueError('模板列名不能为空')
        if c['kind'] not in ['text', 'date', 'integer', 'money'] or c['source'] not in SOURCES | {None}:
            raise ValueError('未知模板数据来源或类型')
        if c['source'] is None and c['required']:
            raise ValueError('必填列不能配置为空')
        if c['source'] is not None:
            if c['source'] in seen:
                raise ValueError('同一来源不能重复写入多列')
            seen.add(c['source'])
            expected = ('date' if c['source'] == 'posting_date' else
                        'integer' if c['source'] in ['voucher_number', 'entry_sequence'] else
                        'money' if c['source'] in ['debit', 'credit'] else 'text')
            if c['kind'] != expected:
                raise ValueError('来源与模板数据类型不一致')
    if not {'posting_date', 'voucher_category', 'voucher_number', 'summary',
            'target_subject_code', 'debit', 'credit'} <= seen:
        raise ValueError('模板缺少核心凭证字段')
    if not isinstance(p['category_mapping'], dict) or not p['category_mapping'] or any(
            not isinstance(k, str) or not isinstance(v, str) or not k or not v
            for k, v in p['category_mapping'].items()):
        raise ValueError('需配置明确的目标凭证类别')
    for key in ['id', 'date_format', 'reference']:
        if not isinstance(p[key], str) or not p[key].strip():
            raise ValueError('模板标识、日期格式及依据不能为空')
    return p
