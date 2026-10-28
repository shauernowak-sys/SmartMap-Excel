"""只接受经审计的正式候选；空模板准备与历史答案核对是独立阶段。"""
import copy
import json
import os
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import openpyxl
from openpyxl.utils import get_column_letter

from .amounts import total_rows
from .template_profile import sha, validate_profile
from .vouchers import month_end


def sheet_signature(sheet):
    return {'cells': [[c.coordinate, c.value.isoformat() if isinstance(c.value, (date, datetime)) else c.value,
                       c.data_type, c.number_format, c.comment.text if c.comment else None]
                      for row in sheet for c in row if c.value is not None or c.comment],
            'merged': sorted(str(r) for r in sheet.merged_cells.ranges)}


def check_workbook(wb, profile):
    if wb.sheetnames != profile['sheet_names']:
        raise ValueError('工作表名称或顺序与模板配置不同')
    sheet = wb[profile['data_sheet']]
    if sheet.max_column != len(profile['columns']) or [c.value for c in sheet[1]] != [c['header'] for c in profile['columns']]:
        raise ValueError('模板表头/列顺序与配置不同')
    if wb._external_links or wb.vba_archive:
        raise ValueError('本版不支持外部链接或宏模板')
    if sheet.merged_cells.ranges or sheet.tables or sheet._charts or sheet._images:
        raise ValueError('本版凭证数据区不支持合并、表格或图形对象')
    if any(c.data_type == 'f' for s in wb for row in s for c in row):
        raise ValueError('本版模板不应包含公式，不能将历史公式当作空白字段')
    return sheet


def update_ranges(sheet, profile, count):
    end = max(2, count + 1)
    sheet.auto_filter.ref = f'A1:{get_column_letter(len(profile["columns"]))}{end}'
    # openpyxl将FilterDatabase与auto_filter关联序列化，工作表定义名不保留历史末行。
    if '_xlnm._FilterDatabase' in sheet.defined_names:
        del sheet.defined_names['_xlnm._FilterDatabase']
    category_col = next(i for i, c in enumerate(profile['columns'], 1) if c['source'] == 'voucher_category')
    for validation in sheet.data_validations.dataValidation:
        if validation.type != 'list' or str(validation.sqref) != f'{get_column_letter(category_col)}2:{get_column_letter(category_col)}{sheet.max_row}':
            # 调用前保存的历史末行可不同；仅允许单列类别区域。
            ranges = list(validation.sqref.ranges)
            if (validation.type != 'list' or len(ranges) != 1 or
                    ranges[0].min_col != category_col or ranges[0].max_col != category_col or ranges[0].min_row != 2):
                raise ValueError('存在未适配的数据校验，需补充模板策略')
        expected = '"' + ','.join(dict.fromkeys(profile['category_mapping'].values())) + '"'
        if validation.formula1 != expected:
            raise ValueError('模板类别校验选项与配置不同')
        validation.sqref = f'{get_column_letter(category_col)}2:{get_column_letter(category_col)}{end}'


def prepare_template(reference, profile, out):
    validate_profile(profile)
    reference, out = Path(reference).resolve(), Path(out).resolve()
    if out.exists() or out == reference or out in reference.parents:
        raise ValueError('模板准备目录必须为全新路径且不能包含原件')
    reference_hash = sha(reference)
    if reference.suffix.lower() != '.xlsx':
        raise ValueError('本版仅支持xlsx模板')
    with ZipFile(reference) as package:
        if any('vbaproject' in name.lower() or name.startswith('xl/externalLinks/') for name in package.namelist()):
            raise ValueError('本版不支持宏或外部链接模板')
    wb = openpyxl.load_workbook(reference)
    sheet = check_workbook(wb, profile)
    signatures = {s.title: sheet_signature(s) for s in wb if s != sheet}
    data_rows_cleared = sum(any(c.value is not None for c in row) for row in sheet.iter_rows(min_row=2))
    update_ranges(sheet, profile, 0)
    # 不复制任何历史分录值；仅保留第二行样式作为可扩展样式原型。
    if sheet.max_row > 2:
        sheet.delete_rows(3, sheet.max_row - 2)
    for cell in sheet[2]:
        cell.value = None
        cell.comment = None
        cell.hyperlink = None
    out.mkdir(parents=True)
    (out / '.gitignore').write_text('*\n', encoding='utf-8')
    destination = out / 'blank_template.xlsx'
    wb.save(destination)
    wb.close()
    saved = openpyxl.load_workbook(destination)
    check_workbook(saved, profile)
    if any(c.value is not None for row in saved[profile['data_sheet']].iter_rows(min_row=2) for c in row):
        raise ValueError('空模板仍有历史分录残留')
    if signatures != {s.title: sheet_signature(s) for s in saved if s.title != profile['data_sheet']}:
        raise ValueError('备查表在模板准备时发生变化')
    saved.close()
    if sha(reference) != reference_hash:
        raise ValueError('准备期间原始参考文件发生变化')
    receipt = {'mode': 'prepared_blank_template', 'profile': profile,
               'reference_sha256': reference_hash, 'blank_sha256': sha(destination),
               'reference_sheet_signatures': signatures, 'historical_rows_cleared': data_rows_cleared,
               'ready_for_system_import': False}
    with (out / 'template_manifest.json').open('x', encoding='utf-8') as f:
        json.dump(receipt, f, ensure_ascii=False, indent=2)
    return receipt


def extract_value(entry, column, profile):
    source = column['source']
    if source is None:
        value = None
    elif source.startswith('auxiliary.'):
        value = entry['auxiliary'].get(source.split('.')[1])
    elif source == 'voucher_category':
        if entry[source] not in profile['category_mapping']:
            raise ValueError('原始凭证类别没有目标映射')
        value = profile['category_mapping'][entry[source]]
    elif source in ['debit', 'credit']:
        amount = entry[source]
        if amount['status'] == 'blank' and amount['value'] is None:
            value = None
        elif amount['status'] == 'number':
            number = Decimal(amount['value'])
            if not number.is_finite() or len(number.normalize().as_tuple().digits) > 15:
                raise ValueError('金额超过Excel可靠有效数字范围，禁止静默舍入')
            value = float(number)
            if Decimal(str(value)) != number:
                raise ValueError('金额转换为Excel数值时精度变化')
        else:
            raise ValueError('金额状态无效')
    else:
        value = entry.get(source)
    if column['required'] and (value is None or isinstance(value, str) and not value.strip()):
        raise ValueError('模板必填字段缺失：' + column['header'])
    if value is None:
        return None
    if column['kind'] == 'date':
        if value != month_end(entry['period']):
            raise ValueError('导出日期不是账期月末')
        return datetime.combine(date.fromisoformat(value), datetime.min.time())
    if column['kind'] == 'integer':
        if type(value) is not int or value < 1:
            raise ValueError('凭证号/分录序号必须为正整数')
    if column['kind'] == 'text':
        if not isinstance(value, str) or len(value) > 32767 or any(ord(c) < 32 and c not in '\t\n\r' for c in value):
            raise ValueError('模板文本类型、长度或字符无效')
    return value


def validate_entries(entries, profile):
    if not entries:
        raise ValueError('没有正式候选，不能生成凭证文件')
    groups = defaultdict(list)
    ids = set()
    for entry in entries:
        if entry.get('diagnostic_only') or entry.get('decision') != 'included' or entry['record_id'] in ids:
            raise ValueError('禁止诊断分录、待确认分录或重复记录写出')
        ids.add(entry['record_id'])
        amounts, _ = total_rows([entry])
        if not any(amounts.values()) or all(amounts.values()):
            raise ValueError('分录须有单边非零金额')
        groups[(entry['period'], entry['voucher_category'], entry['voucher_number'])].append(entry)
        unknown = set(entry['auxiliary']) - {c['source'].split('.')[1] for c in profile['columns']
                                             if c['source'] and c['source'].startswith('auxiliary.')}
        if unknown:
            raise ValueError('存在未适配的辅助字段，禁止静默丢失')
    target_keys = set()
    for (period, category, number), rows in groups.items():
        if len(rows) < 2 or sorted(r['entry_sequence'] for r in rows) != list(range(1, len(rows) + 1)):
            raise ValueError('凭证分录不完整或序号不连续')
        origins = {(r['provenance']['input_sha256'], r['provenance']['sheet']) for r in rows}
        target_key = (profile['category_mapping'].get(category), number)
        if len(origins) != 1 or target_key in target_keys:
            raise ValueError('目标凭证号发生跨来源、类别或账期碰撞')
        target_keys.add(target_key)
        totals, _ = total_rows(rows)
        if totals['debit'] != totals['credit']:
            raise ValueError('输出凭证借贷不平衡')
    ordered = sorted(entries, key=lambda r: (r['period'], r['voucher_category'], r['voucher_number'], r['entry_sequence']))
    return ordered, [[extract_value(r, c, profile) for c in profile['columns']] for r in ordered]


def write_template(entries, prepared, out):
    """调用者须先完成运行审计；此层再次核验完整分组及所有数据类型。"""
    prepared, out = Path(prepared).resolve(), Path(out).resolve()
    if out.exists() or out == prepared or prepared in out.parents or out in prepared.parents:
        raise ValueError('输出必须为独立的新目录')
    receipt = json.loads((prepared / 'template_manifest.json').read_text(encoding='utf-8'))
    profile = validate_profile(receipt['profile'])
    blank = prepared / 'blank_template.xlsx'
    if sha(blank) != receipt['blank_sha256']:
        raise ValueError('空模板与准备清单哈希不一致')
    ordered, values = validate_entries(entries, profile)
    wb = openpyxl.load_workbook(blank)
    sheet = check_workbook(wb, profile)
    if any(c.value is not None for row in sheet.iter_rows(min_row=2) for c in row):
        raise ValueError('写出输入必须为空模板')
    reference_signature = {s.title: sheet_signature(s) for s in wb if s != sheet}
    if reference_signature != receipt['reference_sheet_signatures']:
        raise ValueError('备查表与准备清单不一致')
    styles = [copy.copy(c._style) for c in sheet[2]]
    for row_index, row in enumerate(values, 2):
        sheet.row_dimensions[row_index].height = sheet.row_dimensions[2].height
        for col_index, value in enumerate(row, 1):
            cell = sheet.cell(row_index, col_index, value)
            cell._style = copy.copy(styles[col_index - 1])
            kind = profile['columns'][col_index - 1]['kind']
            if kind == 'text' and value is not None:
                cell.data_type = 's'  # '=...'等摘要按文字保存，不能变成公式。
            if kind == 'date':
                cell.number_format = profile['date_format']
            if kind == 'text' and profile['columns'][col_index - 1]['source'] == 'target_subject_code':
                cell.number_format = '@'
    update_ranges(sheet, profile, len(values))
    out.mkdir(parents=True)
    (out / '.gitignore').write_text('*\n', encoding='utf-8')
    temporary = out / 'unverified.xlsx'
    destination = out / 'vouchers.xlsx'
    try:
        wb.save(temporary)
        wb.close()
        saved = openpyxl.load_workbook(temporary)
        check_workbook(saved, profile)
        target = saved[profile['data_sheet']]
        if target.max_row != len(values) + 1:
            raise ValueError('往返核验行数不一致')
        for row_index, row in enumerate(values, 2):
            for col_index, expected in enumerate(row, 1):
                cell = target.cell(row_index, col_index)
                if cell.value != expected or cell.data_type == 'f':
                    raise ValueError(f'往返核验值变化：{cell.coordinate}')
                if expected is not None:
                    kind = profile['columns'][col_index - 1]['kind']
                    if kind == 'text' and cell.data_type != 's' or kind in ['money', 'integer'] and cell.data_type != 'n' or kind == 'date' and cell.data_type != 'd':
                        raise ValueError('往返核验数据类型变化')
        if reference_signature != {s.title: sheet_signature(s) for s in saved if s.title != profile['data_sheet']}:
            raise ValueError('备查表往返核验发生变化')
        saved.close()
        if sha(blank) != receipt['blank_sha256']:
            raise ValueError('运行期间空模板变化')
        os.replace(temporary, destination)
        result = {'mode': 'template_write', 'row_count': len(values), 'roundtrip_passed': True,
                  'prepared_template_sha256': receipt['blank_sha256'], 'output_sha256': sha(destination),
                  'lineage': [{'excel_row': i, 'record_id': r['record_id'], 'provenance': r['provenance']}
                              for i, r in enumerate(ordered, 2)],
                  'ready_for_system_import': False, 'business_acceptance_passed': False}
        with (out / 'write_manifest.json').open('x', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return result
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
