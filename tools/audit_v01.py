"""运行 T02 审计。仅替换内存中的 Excel 读取边界，原版文件保持不变。"""
import argparse
import ast
import contextlib
import csv
import hashlib
import io
import json
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import openpyxl
from openpyxl.utils.cell import range_boundaries

ROOT = Path(__file__).resolve().parents[1]


def read_rows(path, sheet, area):
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        left, top, right, bottom = range_boundaries(area)
        rows = [list(row) for row in book.worksheets[sheet].iter_rows(
            min_row=top, max_row=bottom, min_col=left, max_col=right, values_only=True)]
        rows.extend([[None] * (right-left+1) for _ in range(bottom-top+1-len(rows))])
        return rows
    finally:
        book.close()


def load_legacy(entry):
    tree = ast.parse(entry.read_text(encoding='utf-8'), filename=str(entry))
    # 缺失导入无法执行；显式移除唯一的第三方导入与读取函数。
    # 筛选、金额、匹配、统计和输出函数的 AST 保持原样。
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom) and node.module == 'artifact_tool'
        or isinstance(node, ast.FunctionDef) and node.name == 'read_rows')]
    namespace = {'__name__': 'legacy_audit', '__file__': str(entry), 'read_rows': read_rows}
    exec(compile(tree, str(entry), 'exec'), namespace)
    return namespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    if out == ROOT or ROOT not in out.parents:
        parser.error('输出必须位于项目内部的新目录')
    out.mkdir(parents=True, exist_ok=False)
    entries = list((ROOT / 'verifier').rglob('verify.py'))
    if len(entries) != 1:
        raise RuntimeError('V0.1 入口不唯一')
    entry = entries[0]
    source = ROOT / 'samples' / '202606_明细账26.7.241.xlsx'
    template = ROOT / 'samples' / '凭证导入模板2026.6月.xlsx'
    command = [sys.executable, '-B', str(entry), '--source', str(source), '--template', str(template), '--out', str(out / 'original')]
    process = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace')
    (out / 'original_attempt.json').write_text(json.dumps({'command': command, 'returncode': process.returncode, 'stdout': process.stdout, 'stderr': process.stderr}, ensure_ascii=False, indent=2), encoding='utf-8')
    if process.returncode == 0:
        raise RuntimeError('原版已可运行，请按原生依赖路径审计，不使用兼容结果')
    if "No module named 'artifact_tool'" not in process.stderr:
        raise RuntimeError('原版失败原因不是预期依赖缺失，请检查日志')
    namespace = load_legacy(entry)
    original_argv = sys.argv
    captured = io.StringIO()
    try:
        sys.argv = [str(entry), '--source', str(source), '--template', str(template), '--out', str(out / 'compatible')]
        with contextlib.redirect_stdout(captured):
            namespace['main']()
    finally:
        sys.argv = original_argv
    (out / 'compatible_stdout.txt').write_text(captured.getvalue(), encoding='utf-8')
    history = entry.parent / 'results'
    old = json.loads((history / 'summary.json').read_text(encoding='utf-8'))
    new = json.loads((out / 'compatible' / 'summary.json').read_text(encoding='utf-8'))
    comparison = {key: {'historical': old.get(key), 'compatible': new.get(key), 'equal': old.get(key) == new.get(key)} for key in old.keys() | new.keys()}
    csv_changes = {}
    for p in sorted(history.glob('*.csv')):
        def rows(path):
            with path.open(encoding='utf-8-sig', newline='') as handle:
                return list(csv.reader(handle))
        before, after = rows(p), rows(out / 'compatible' / p.name)
        csv_changes[p.name] = {'historical_rows': len(before)-1, 'compatible_rows': len(after)-1, 'equal': before == after, 'different_row_count': sum(a != b for a,b in zip(before, after)) + abs(len(before)-len(after))}
    raw = read_rows(source, 0, 'A1:I750')
    manual = read_rows(source, 1, 'A1:Y210')
    final = read_rows(template, 0, 'A1:X210')
    voucher = namespace['voucher']; subject = namespace['subject']; amount = namespace['amount']
    records = [r for r in raw[2:] if voucher(r[3]) is not None and subject(r[2])]
    curated = [r for r in manual[2:] if voucher(r[4]) is not None and subject(r[3])]
    completed = [r for r in final[1:] if voucher(r[3]) is not None]
    codes = {subject(r[3]) for r in curated}
    result = {'mode': 'openpyxl_read_boundary_compatibility_NOT_native_artifact_tool',
        'python': sys.version, 'openpyxl': openpyxl.__version__,
        'legacy_sha256': hashlib.sha256(entry.read_bytes()).hexdigest(),
        'summary_comparison': comparison, 'csv_comparison': csv_changes,
        'additional_checks': {'final_lines': len(completed), 'manual_vouchers': len({voucher(r[4]) for r in curated}),
            'raw_vouchers': len({voucher(r[3]) for r in records}),
            'candidate_subject_only': sum(subject(r[2]) in codes for r in records),
            'voucher74_rows': [i for i,r in enumerate(raw,1) if voucher(r[3]) == 74],
            'raw_amount_10_cells': sum(amount(r[i]) == 10 for r in records for i in (5,6)),
            'manual_amount_10_cells': sum(amount(r[i]) == 10 for r in curated for i in (9,10)),
            'historical_trace_10_flags': Counter(row['涉及10占位歧义'] for row in csv.DictReader((history/'逐行来源追踪.csv').open(encoding='utf-8-sig')))}}
    (out / 'comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    # 底层 XML 证据与故障注入实验：不是正式读取规则，不能用于转换。
    with zipfile.ZipFile(source) as archive:
        strings = ET.fromstring(archive.read('xl/sharedStrings.xml'))
        shared10 = ''.join(strings[10].itertext())
        xml = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        xml_ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        cells = {c.attrib['r']: ET.tostring(c, encoding='unicode') for c in xml.findall('s:sheetData/s:row/s:c', xml_ns)}
    namespace['read_rows'] = lambda path, sheet, area: [[10 if value == '' else value for value in row] for row in read_rows(path, sheet, area)]
    try:
        sys.argv = [str(entry), '--source', str(source), '--template', str(template), '--out', str(out / 'diagnostic_empty_string_as_10')]
        with contextlib.redirect_stdout(io.StringIO()):
            namespace['main']()
    finally:
        sys.argv = original_argv
    diagnostic = json.loads((out/'diagnostic_empty_string_as_10/summary.json').read_text(encoding='utf-8'))
    evidence = {'shared_string_index_10_resolved': shared10,
        'selected_raw_xml_cells': {key: cells.get(key) for key in ['D3','F3','G3','D60','F60','G60']},
        'subject_rows_without_voucher': sum(bool(subject(r[2])) and voucher(r[3]) is None for r in raw[2:]),
        'diagnostic_only': '故意将已解析的空字符串替换为10，检验历史数字的形成机制；不代表已恢复原读取器。',
        'diagnostic_summary_matches_history': diagnostic == old}
    (out/'reader_evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
