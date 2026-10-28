"""独立核对实际保存的凭证文件、正式候选及用户指定历史成品；不改写运行清单。"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from smartmap.template_profile import sha, validate_profile
from smartmap.template_writer import sheet_signature
from verify_conversion_run import audit_run


def normalized(value, column):
    if value is None:
        return None
    if column['kind'] == 'date':
        if not isinstance(value, (date, datetime)):
            raise ValueError('日期必须为Excel日期值')
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    if column['source'] == 'target_subject_code':
        return str(value)
    if column['kind'] == 'money':
        return str(Decimal(str(value)).normalize())
    return value


def expected_signature(row, profile):
    values = []
    for column in profile['columns']:
        source = column['source']
        if source is None:
            value = None
        elif source in ['debit', 'credit']:
            amount = row[source]
            value = None if amount['status'] == 'blank' else str(Decimal(amount['value']).normalize())
        elif source.startswith('auxiliary.'):
            value = row['auxiliary'].get(source.split('.')[1])
        elif source == 'voucher_category':
            value = profile['category_mapping'][row[source]]
        else:
            value = row[source]
        values.append(value)
    return tuple(values)


def verify(run, export, prepared, final, out):
    run, export, prepared, final, out = [Path(p).resolve() for p in (run, export, prepared, final, out)]
    if out.exists() or any(out == p or p in out.parents for p in (run, export, prepared)) or out in final.parents:
        raise ValueError('验收报告须写独立的新目录')
    def load(path):
        return json.loads(path.read_text(encoding='utf-8'))
    audit = audit_run(run)
    if not audit['all_passed'] or not audit['usable_for_evaluation']:
        raise ValueError('转换独立审计未通过')
    receipt = load(prepared / 'template_manifest.json')
    profile = validate_profile(receipt['profile'])
    manifest = load(export / 'export_manifest.json')
    write_manifest = load(export / 'write_manifest.json')
    conversion = load(run / 'run_manifest.json')
    candidates = load(run / 'candidates.json')['rows']
    path = export / 'vouchers.xlsx'
    input_paths = [final, path, prepared / 'blank_template.xlsx', prepared / 'template_manifest.json',
                   export / 'export_manifest.json', export / 'write_manifest.json', run / 'run_manifest.json']
    hashes = {str(p): sha(p) for p in input_paths}
    wb = openpyxl.load_workbook(path)
    historical = openpyxl.load_workbook(final)
    checks = {'conversion_audit_passed': audit['all_passed'],
              'complete_batch_without_partial_export': conversion['batch_complete'] and not conversion['partial_export'] and not conversion['row_counts']['needs_review'],
              'export_complete': manifest['status'] == 'written_for_review' and manifest['xlsx_created'] and manifest['batch_complete'] and not manifest['partial_export'],
              'manifest_bound_to_run': manifest['source_manifest_sha256'] == audit['manifest_sha256'],
              'actual_output_hash_matches_both_manifests': sha(path) == manifest['output_sha256'] == write_manifest['output_sha256'],
              'prepared_template_hash_unchanged': sha(prepared / 'blank_template.xlsx') == receipt['blank_sha256'] == manifest['prepared_template_sha256'],
              'sheet_structure': wb.sheetnames == historical.sheetnames == profile['sheet_names'],
              'no_formulas': not any(c.data_type == 'f' for s in wb for row in s for c in row)}
    sheet, reference = wb[profile['data_sheet']], historical[profile['data_sheet']]
    headers = [c['header'] for c in profile['columns']]
    checks['headers_and_order'] = [c.value for c in sheet[1]] == [c.value for c in reference[1]] == headers
    checks['column_count'] = sheet.max_column == reference.max_column == len(headers)
    columns = profile['columns']
    def rows(s):
        return [tuple(normalized(c.value, column) for c, column in zip(row, columns))
                for row in s.iter_rows(min_row=2, max_col=len(columns)) if any(c.value is not None for c in row)]
    actual_rows, historical_rows = rows(sheet), rows(reference)
    actual, reference_counts = Counter(actual_rows), Counter(historical_rows)
    predicted = Counter(expected_signature(r, profile) for r in candidates)
    checks['all_saved_fields_equal_formal_candidates_with_multiplicity'] = actual == predicted
    checks['all_saved_fields_equal_historical_final_with_multiplicity'] = actual == reference_counts
    checks['row_counts'] = sheet.max_row - 1 == len(actual_rows) == len(candidates) == write_manifest['row_count']
    type_ok = True
    balances = defaultdict(lambda: [Decimal(0), Decimal(0), 0])
    source_columns = {c['source']: i for i, c in enumerate(columns) if c['source']}
    for row in sheet.iter_rows(min_row=2, max_col=len(columns)):
        for cell, column in zip(row, columns):
            if cell.value is None:
                type_ok &= not column['required']
                continue
            expected_type = {'text': 's', 'date': 'd', 'money': 'n', 'integer': 'n'}[column['kind']]
            type_ok &= cell.data_type == expected_type
            if column['kind'] == 'integer':
                type_ok &= type(cell.value) is int
            if column['kind'] == 'date':
                type_ok &= cell.number_format == profile['date_format']
        key = tuple(row[source_columns[k]].value for k in ['posting_date', 'voucher_category', 'voucher_number'])
        balance = balances[key]
        for i, side in enumerate(['debit', 'credit']):
            value = row[source_columns[side]].value
            balance[i] += Decimal(str(value)) if value is not None else Decimal(0)
        balance[2] += 1
    checks['saved_types_required_fields_and_date_format'] = bool(type_ok)
    checks['all_saved_vouchers_complete_and_balanced'] = bool(balances) and all(d == c and count >= 2 for d, c, count in balances.values())
    checks['reference_sheets_preserved'] = all(sheet_signature(wb[name]) == receipt['reference_sheet_signatures'][name] == sheet_signature(historical[name])
                                               for name in profile['sheet_names'] if name != profile['data_sheet'])
    lineage = write_manifest['lineage']
    by_id = {r['record_id']: r for r in candidates}
    checks['lineage_is_bijective_and_bound_to_saved_rows'] = (Counter(r['record_id'] for r in lineage) == Counter(by_id.keys()) and
        {r['excel_row'] for r in lineage} == set(range(2, len(actual_rows) + 2)) and
        all(r['provenance'] == by_id[r['record_id']]['provenance'] and
            actual_rows[r['excel_row'] - 2] == expected_signature(by_id[r['record_id']], profile) for r in lineage))
    wb.close()
    historical.close()
    checks['inputs_unchanged_during_verification'] = all(sha(p) == digest for p, digest in hashes.items())
    passed = all(checks.values())
    result = {'mode': 'saved_workbook_reference_acceptance', 'status': 'passed' if passed else 'failed',
              'reference_acceptance_passed': passed, 'system_import_performed': False,
              'scope': {'period': conversion['period'], 'source_sha256': conversion['inputs']['source']['sha256']},
              'inputs': hashes, 'checks': checks, 'check_count': len(checks),
              'row_count': len(actual_rows), 'voucher_count': len(balances),
              'matched': sum((actual & reference_counts).values()), 'missing': sum((reference_counts - actual).values()),
              'extra': sum((actual - reference_counts).values())}
    out.mkdir(parents=True)
    (out / '.gitignore').write_text('*\n', encoding='utf-8')
    (out / 'acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 实际凭证文件最终核对', '', f"本案例按用户指定历史成品验收：{result['status']}。",
             f"实际文件{result['row_count']}条，{result['voucher_count']}组凭证；24列整行匹配{result['matched']}，漏{result['missing']}，多{result['extra']}。",
             f"独立检查{result['check_count']}项。全部通过：{passed}。", '',
             '日期、文本编码、数值金额、空白、重复次数、逐行来源、完整凭证和备查表均检查实际保存文件。',
             '科目编码按标识比较，历史数值编码与同值文本编码等价；不是二进制相同判定。',
             '本验收采用用户指定成品作为基准；未执行用户取消的系统实际导入。', '', '| 检查 | 通过 |', '| --- | --- |']
    lines += [f'| {name} | {value} |' for name, value in checks.items()]
    (out / '最终验收结论.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['run', 'export', 'prepared', 'final', 'out']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.run, args.export, args.prepared, args.final, args.out)
    print(json.dumps({k: result[k] for k in ['status', 'row_count', 'voucher_count', 'matched', 'missing', 'extra', 'check_count']}))
    return 0 if result['reference_acceptance_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
