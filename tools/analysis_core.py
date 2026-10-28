"""T03—T05 独立分析函数。不导入 V0.1，不包含生产筛选规则。"""
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def blank(value):
    return value is None or isinstance(value, str) and not value.strip()


def money(value):
    """返回状态与 Decimal；空白与数值零分开，格式/非有限值不吞掉。"""
    if blank(value):
        return 'blank', None
    if isinstance(value, bool):
        return 'invalid', None
    text = str(value).strip()
    if not re.fullmatch(r'[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?', text):
        return 'invalid', None
    try:
        result = Decimal(text.replace(',', ''))
    except InvalidOperation:
        return 'invalid', None
    return ('number', result) if result.is_finite() else ('invalid', None)


def voucher(value):
    if blank(value) or isinstance(value, bool):
        return None
    m = re.fullmatch(r'(?:记\s*)?([1-9]\d*)(?:\.0+)?', str(value).strip())
    return int(m.group(1)) if m else None


def subject(value):
    m = re.match(r'^(\d+(?:\.\d+)*)(?=[^\d.]|$)', str(value or '').strip())
    return m.group(1) if m else None


def ancestor(parent, child):
    return bool(parent and child and child.startswith(parent + '.'))


def text(value):
    return '' if blank(value) else str(value).strip()


def day(value):
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    return text(value)


def pair_occurrences(left, right, key):
    """按完整签名一对一消费，保留重复次数；不宣称相同签名有唯一业务身份。"""
    groups = defaultdict(deque)
    for index, item in enumerate(right):
        groups[key(item)].append(index)
    pairs, missing = [], []
    for index, item in enumerate(left):
        queue = groups[key(item)]
        if queue:
            pairs.append((index, queue.popleft()))
        else:
            missing.append(index)
    extra = sorted(index for queue in groups.values() for index in queue)
    return pairs, missing, extra


def xml_sheets(path):
    """直接解码 OOXML，用于与 openpyxl 独立交叉核对。"""
    with zipfile.ZipFile(path) as archive:
        shared = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            for item in ET.fromstring(archive.read('xl/sharedStrings.xml')):
                shared.append(''.join(t.text or '' for t in item.findall('.//s:t', NS)))
        rels = {item.attrib['Id']: item.attrib['Target'] for item in ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))}
        sheets = ET.fromstring(archive.read('xl/workbook.xml')).find('s:sheets', NS)
        result = {}
        for sheet in sheets:
            target = rels[sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']]
            target = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join('xl', target))
            cells = {}
            for cell in ET.fromstring(archive.read(target)).findall('s:sheetData/s:row/s:c', NS):
                typ = cell.attrib.get('t', 'n')
                v = cell.find('s:v', NS)
                raw = v.text if v is not None else None
                value = raw
                if typ == 's':
                    value = shared[int(raw)]
                elif typ == 'inlineStr':
                    value = ''.join(t.text or '' for t in cell.findall('.//s:t', NS))
                cells[cell.attrib['r']] = {'type': typ, 'raw': raw, 'decoded': value,
                    'formula': cell.find('s:f', NS) is not None}
            result[sheet.attrib['name']] = cells
        return result
