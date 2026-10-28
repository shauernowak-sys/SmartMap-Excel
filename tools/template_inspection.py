"""T13历史成品模板的只读证据提取；不生成系统导入工作簿。"""
import hashlib
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET

import openpyxl
from openpyxl.utils import get_column_letter

from analysis_core import blank, text


DATA_HEADERS = ['导入结果', '日期', '凭证类别', '凭证号', '分录序号', '摘要', '科目编码', '科目名称',
                '借方金额', '贷方金额', '数量', '币别', '外币金额', '客户', '供应商', '部门', '员工',
                '存货名称', '存货规格', '附件数', '项目', '客户等级', '其他入库类别', '其他出库类别']
ACCOUNT_HEADERS = ['科目编码', '科目名称', '方向', '单位', '币别', '核算类别']
AUX_HEADERS = ['辅助类别', '编码', '名称', '存货规格']
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def encode(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def code(value):
    """仅用于参考字典关联；数值整型与文本等值，禁止截断小数或布尔值。"""
    if blank(value):
        return None
    if isinstance(value, bool):
        raise ValueError('科目/辅助编码不能是布尔值')
    if isinstance(value, (float, Decimal)):
        if not Decimal(str(value)).is_finite() or Decimal(str(value)) != Decimal(str(value)).to_integral_value():
            raise ValueError('数值编码不是有限整数')
        return str(int(value))
    return str(value).strip()


def cell_value(cell):
    return {'cell': cell.coordinate, 'value': encode(cell.value), 'python_type': type(cell.value).__name__,
            'excel_type': cell.data_type, 'number_format': cell.number_format}


def profile_sheet(sheet):
    rows = [list(row) for row in sheet.iter_rows()]
    nonempty = [row for row in rows if any(not blank(c.value) for c in row)]
    fields = []
    for column in range(1, sheet.max_column + 1):
        cells = [sheet.cell(row, column) for row in range(2, sheet.max_row + 1)]
        present = [c for c in cells if not blank(c.value)]
        fields.append({'column': get_column_letter(column), 'header': encode(sheet.cell(1, column).value),
                       'population': 'rows_after_first_row', 'population_count': len(cells),
                       'nonempty_count': len(present), 'blank_count': len(cells) - len(present),
                       'python_types': dict(Counter(type(c.value).__name__ for c in cells)),
                       'excel_types': dict(Counter(c.data_type for c in cells)),
                       'number_formats': dict(Counter(c.number_format for c in cells)),
                       'examples': [cell_value(c) for c in present[:3]]})
    validations = [{k: getattr(v, k) for k in ['type', 'operator', 'formula1', 'formula2', 'allowBlank',
                    'errorStyle', 'showErrorMessage', 'showDropDown', 'promptTitle', 'prompt', 'errorTitle', 'error']}
                   | {'sqref': str(v.sqref)} for v in sheet.data_validations.dataValidation]
    return {'sheet': sheet.title, 'sheet_state': sheet.sheet_state, 'max_row': sheet.max_row,
            'max_column': sheet.max_column, 'nonempty_rows': len(nonempty),
            'merged_ranges': sorted(map(str, sheet.merged_cells.ranges)),
            'freeze_panes': str(sheet.freeze_panes) if sheet.freeze_panes else None,
            'auto_filter': sheet.auto_filter.ref, 'tables': list(sheet.tables),
            'protection_enabled': bool(sheet.protection.sheet), 'validations': validations,
            'conditional_formatting_count': len(sheet.conditional_formatting),
            'formula_cells': [cell_value(c) for row in rows for c in row if c.data_type == 'f'],
            'comments': [{'cell': c.coordinate, 'text': c.comment.text} for row in rows for c in row if c.comment],
            'fields': fields,
            'rows': [{'excel_row': row[0].row, 'cells': [cell_value(c) for c in row]} for row in nonempty]}


def dictionary_records(sheet, headers):
    if [text(sheet.cell(2, i + 1).value) for i in range(len(headers))] != headers:
        raise ValueError(sheet.title + '第2行表头不符合历史模板布局')
    result = []
    for row in sheet.iter_rows(min_row=3):
        if all(blank(c.value) for c in row):
            continue
        result.append({'excel_row': row[0].row, 'values': {label: encode(row[i].value) for i, label in enumerate(headers)}})
    return result


def crosscheck(data_rows, accounts, auxiliary):
    account_index = defaultdict(list)
    aux_codes, aux_names = defaultdict(list), defaultdict(list)
    for r in accounts:
        account_index[code(r['values']['科目编码'])].append(r)
    for r in auxiliary:
        value = r['values']; category = text(value['辅助类别'])
        aux_codes[(category, code(value['编码']))].append(r)
        aux_names[(category, text(value['名称']))].append(r)
    issues, account_links, aux_links, categories = [], [], [], []
    used_accounts = set()
    for row in data_rows:
        values = row['values']; number = row['excel_row']; subject = code(values['科目编码'])
        used_accounts.add(subject); found = account_index.get(subject, [])
        account_links.append({'data_cell': f'G{number}', 'code': subject,
                              'reference_rows': [r['excel_row'] for r in found]})
        if len(found) != 1:
            issues.append({'code': 'ACCOUNT_REFERENCE_MISSING_OR_AMBIGUOUS', 'cell': f'G{number}', 'value': subject})
        elif text(values['科目名称']) != text(found[0]['values']['科目名称']):
            issues.append({'code': 'ACCOUNT_NAME_DIFFERS', 'cell': f'H{number}', 'account_reference_row': found[0]['excel_row']})
        if len(found) == 1 and not blank(found[0]['values']['核算类别']):
            category = text(found[0]['values']['核算类别'])
            supported = category in DATA_HEADERS
            present = supported and not blank(values.get(category))
            categories.append({'data_row': number, 'account_code': subject, 'account_reference_row': found[0]['excel_row'],
                               'category': category, 'column_supported': supported, 'value_present': bool(present),
                               'mandatory_rule_confirmed': False})
            if not present:
                issues.append({'code': 'DECLARED_AUXILIARY_VALUE_ABSENT_OR_UNSUPPORTED', 'data_row': number, 'category': category})
        for category in DATA_HEADERS[13:]:
            # 存货规格、附件数不是独立类别，单独留原值而不猜字典关联。
            if category in ['存货规格', '附件数'] or blank(values[category]):
                continue
            name = text(values[category]); matches = aux_names.get((category, name), [])
            aux_links.append({'data_cell': f'{get_column_letter(DATA_HEADERS.index(category) + 1)}{number}',
                              'category': category, 'value': name, 'reference_rows': [r['excel_row'] for r in matches],
                              'candidate_codes': [code(r['values']['编码']) for r in matches]})
            if len(matches) != 1:
                issues.append({'code': 'AUXILIARY_NAME_MISSING_OR_AMBIGUOUS', 'data_row': number, 'category': category, 'value': name})
    return {'account_record_count': len(accounts), 'auxiliary_record_count': len(auxiliary),
            'used_account_count': len(used_accounts), 'auxiliary_category_counts': dict(Counter(text(r['values']['辅助类别']) for r in auxiliary)),
            'account_auxiliary_category_counts': dict(Counter(text(r['values']['核算类别']) for r in accounts if not blank(r['values']['核算类别']))),
            'duplicate_account_codes': [{'code': k, 'rows': [r['excel_row'] for r in v]} for k, v in account_index.items() if len(v) > 1],
            'duplicate_auxiliary_codes': [{'category': k[0], 'code': k[1], 'rows': [r['excel_row'] for r in v]} for k, v in aux_codes.items() if len(v) > 1],
            'duplicate_auxiliary_names': [{'category': k[0], 'name': k[1], 'rows': [r['excel_row'] for r in v]} for k, v in aux_names.items() if len(v) > 1],
            'account_links': account_links, 'auxiliary_links': aux_links,
            'declared_auxiliary_observations': categories, 'issues': issues,
            'required_fields_confirmed': False, 'ready_for_system_import': False}


def inspect(path):
    path = Path(path).resolve(); before = hashlib.sha256(path.read_bytes()).hexdigest()
    with ZipFile(path) as archive:
        xml_workbook = ET.fromstring(archive.read('xl/workbook.xml'))
        xml_parts = {}
        for name in archive.namelist():
            if name.startswith('xl/worksheets/') and name.endswith('.xml') and '/_rels/' not in name:
                root = ET.fromstring(archive.read(name)); dimension = root.find('s:dimension', NS)
                xml_parts[name] = {'declared_dimension': dimension.get('ref') if dimension is not None else None,
                                   'physical_row_count': len(root.findall('s:sheetData/s:row', NS)),
                                   'physical_cell_count': len(root.findall('s:sheetData/s:row/s:c', NS)),
                                   'formula_count': len(root.findall('s:sheetData/s:row/s:c/s:f', NS))}
        parts = archive.namelist()
        defined_names = xml_workbook.find('s:definedNames', NS)
        names = [{'attributes': dict(item.attrib), 'formula': item.text} for item in defined_names] if defined_names is not None else []
    book = openpyxl.load_workbook(path, data_only=False, keep_links=True)
    try:
        data = book['凭证模板']
        if [text(data.cell(1, i + 1).value) for i in range(len(DATA_HEADERS))] != DATA_HEADERS or data.max_column != len(DATA_HEADERS):
            raise ValueError('凭证模板字段顺序与历史布局不一致')
        rows = [{'excel_row': row[0].row, 'values': {name: encode(row[i].value) for i, name in enumerate(DATA_HEADERS)}}
                for row in data.iter_rows(min_row=2) if any(not blank(c.value) for c in row)]
        accounts = dictionary_records(book['科目备查表'], ACCOUNT_HEADERS)
        auxiliary = dictionary_records(book['辅助核算备查表'], AUX_HEADERS)
        result = {'mode': 'historical_template_inspection', 'profile': 'historical_v1',
                  'input': {'path': str(path), 'sha256': before}, 'read_only_analysis': True,
                  'business_rules_approved': False, 'ready_for_system_import': False,
                  'sheet_order': book.sheetnames, 'epoch': str(book.epoch), 'defined_names': names,
                  'external_link_parts': [p for p in parts if p.startswith('xl/externalLinks/')],
                  'macro_parts': [p for p in parts if 'vbaProject' in p],
                  'xml_worksheets': xml_parts, 'sheets': [profile_sheet(s) for s in book],
                  'data_rows': rows, 'accounts': accounts, 'auxiliary': auxiliary,
                  'crosscheck': crosscheck(rows, accounts, auxiliary)}
    finally:
        book.close()
    if hashlib.sha256(path.read_bytes()).hexdigest() != before:
        raise ValueError('检查期间输入文件发生变化')
    return result
