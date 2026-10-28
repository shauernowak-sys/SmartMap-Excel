"""历史样本专用的只读答案适配器；布局显式校验，非正式转换入口。"""
import re
from datetime import date, datetime

import openpyxl

from analysis_core import blank, money, subject, text, voucher


def iso_day(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(text(value)).isoformat()


def load_reference(path, kind):
    if kind not in ['manual', 'final']:
        raise ValueError('未知标准答案类型')
    sheet_name = '明细账 (2)' if kind == 'manual' else '凭证模板'
    book = openpyxl.load_workbook(path, read_only=True, data_only=False)
    try:
        sheet = book[sheet_name]
        sheet.reset_dimensions()
        rows = list(sheet.iter_rows(values_only=True))
    finally:
        book.close()
    header = {1: '日期', 4: '凭证号', 7: '科目编码', 9: '借方金额', 10: '贷方金额'} if kind == 'manual' else {
        1: '日期', 3: '凭证号', 6: '科目编码', 8: '借方金额', 9: '贷方金额'}
    if not rows or any(len(rows[0]) <= c or text(rows[0][c]) != label for c, label in header.items()):
        raise ValueError(f'{sheet_name}表头不符合历史验收布局')
    result, ignored = [], []
    first = 3 if kind == 'manual' else 2
    for number, cells in enumerate(rows, 1):
        if number < first:
            continue
        if all(blank(v) for v in cells):
            ignored.append({'excel_row': number, 'reason': 'EMPTY_REFERENCE_ROW'})
            continue
        cells = list(cells) + [None] * 25
        vcol, codecol, scol, dcol, ccol = (4, 7, 6, 9, 10) if kind == 'manual' else (3, 6, 5, 8, 9)
        v = voucher(cells[vcol])
        code = text(cells[codecol])
        if v is None or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', code):
            raise ValueError(f'{sheet_name}第{number}行凭证或科目无效')
        day = iso_day(cells[1])
        period = text(cells[2]) if kind == 'manual' else day[:4] + day[5:7]
        if not re.fullmatch(r'[1-9][0-9]{3}(0[1-9]|1[0-2])', period) or period != day[:4] + day[5:7]:
            raise ValueError(f'{sheet_name}第{number}行账期与日期冲突')
        if kind == 'final' and text(cells[2]) != '记账凭证':
            raise ValueError('历史成品凭证类别不属于已知验收布局')
        external = subject(cells[3]) if kind == 'manual' else None
        if kind == 'manual' and not external:
            raise ValueError(f'人工第{number}行缺外部科目')
        row = {'record_id': f'{kind}:{sheet_name}:{number}',
               'provenance': {'file': str(path), 'sheet': sheet_name, 'excel_row': number},
               'period': period, 'voucher_category': '记', 'voucher_number': v,
               'external_subject_code': external, 'target_subject_code': code,
               'summary': text(cells[scol]), 'posting_date': day}
        for side, col in [('debit', dcol), ('credit', ccol)]:
            status, value = money(cells[col])
            if status == 'invalid':
                raise ValueError(f'{sheet_name}第{number}行{side}金额无效或为公式')
            row[side] = {'status': status, 'value': str(value) if value is not None else None}
        result.append(row)
    if not result:
        raise ValueError('标准答案没有有效记录，拒绝空答案通过')
    return {'profile': 'historical_v1', 'kind': kind, 'sheet': sheet_name, 'rows': result, 'ignored_rows': ignored}
