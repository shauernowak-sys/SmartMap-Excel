"""仅对正常完整凭证做字段预检并与历史成品核对；绝不生成Excel或改变导出状态。"""
import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from smartmap.candidate_export import normalize_row
from smartmap.template_profile import load_profile, sha
from smartmap.template_writer import check_workbook, validate_entries
from verify_conversion_run import audit_run


def canonical(value, column):
    if value is None:
        return None
    if column['kind'] == 'date':
        if not isinstance(value, (date, datetime)):
            raise ValueError('参考日期不是Excel日期')
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    if column['source'] == 'target_subject_code':
        return str(value)
    if column['kind'] == 'money':
        return str(Decimal(str(value)).normalize())
    return value


def preflight(run, profile_path, final, out):
    run, profile_path, final, out = [Path(p).resolve() for p in (run, profile_path, final, out)]
    if out.exists() or out == run or run in out.parents or out in final.parents:
        raise ValueError('预检须写在独立的新目录，不能位于转换运行内或包含参考原件')
    audit = audit_run(run)
    if not audit['all_passed'] or not audit['usable_for_evaluation']:
        raise ValueError('转换审计未通过')
    hashes = {str(p): sha(p) for p in [run / 'run_manifest.json', profile_path, final]}
    profile = load_profile(profile_path)
    source = json.loads((run / 'source_rows.json').read_text(encoding='utf-8'))['rows']
    selected = []
    for row in source:
        if row['voucher_validation']['decision'] != 'retained':
            continue
        if row.get('auxiliary_validation') and not row['auxiliary_validation']['voucher_ready']:
            continue
        entry = normalize_row(row)
        # 此状态仅用于字段检查，不写入正式候选、不调用写出函数。
        entry['decision'] = 'included'
        selected.append(entry)
    ordered, values = validate_entries(selected, profile)
    wb = openpyxl.load_workbook(final)
    sheet = check_workbook(wb, profile)
    reference = [[c.value for c in row] for row in sheet.iter_rows(min_row=2)
                 if any(c.value is not None for c in row)]
    def signature(row):
        return tuple(canonical(value, column) for value, column in zip(row, profile['columns']))
    predicted, expected = Counter(map(signature, values)), Counter(map(signature, reference))
    matched = sum((predicted & expected).values())
    differences = []
    for index, column in enumerate(profile['columns']):
        a = Counter(canonical(row[index], column) for row in values)
        b = Counter(canonical(row[index], column) for row in reference)
        differences.append({'column': index + 1, 'header': column['header'],
                            'matched': sum((a & b).values()), 'missing': sum((b - a).values()),
                            'extra': sum((a - b).values())})
    wb.close()
    if any(sha(p) != digest for p, digest in hashes.items()):
        raise ValueError('预检期间输入发生变化')
    result = {'mode': 'template_preflight_only', 'xlsx_created': False,
              'business_acceptance_passed': False, 'ready_for_system_import': False,
              'run_manifest_sha256': audit['manifest_sha256'], 'inputs': hashes,
              'normal_eligible_rows': len(values), 'reference_rows': len(reference),
              'full_row_multiset': {'matched': matched, 'missing': sum((expected - predicted).values()),
                                    'extra': sum((predicted - expected).values())},
              'columns': differences,
              'row_lineage': [{'planned_excel_row': i, 'record_id': r['record_id'],
                               'provenance': r['provenance']} for i, r in enumerate(ordered, 2)]}
    out.mkdir(parents=True)
    (out / '.gitignore').write_text('*\n', encoding='utf-8')
    (out / 'preflight.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 模板字段预检（非正式导出）', '',
             f"正常完整凭证{len(values)}条；参考{len(reference)}条；{len(profile['columns'])}列整行多重集合匹配{matched}条。",
             f"漏{result['full_row_multiset']['missing']}；多{result['full_row_multiset']['extra']}。",
             '科目编码按标识比较（历史数值编码与同值文本编码等价）；金额按十进制比较；重复次数保留。',
             '本报告只检查正常完整凭证的字段计划，不改变结转范围，不生成Excel，不代表整批通过。', '',
             '| 列 | 字段 | 匹配 | 漏 | 多 |', '| ---: | --- | ---: | ---: | ---: |']
    lines += [f"| {r['column']} | {r['header']} | {r['matched']} | {r['missing']} | {r['extra']} |" for r in differences]
    (out / '模板字段预检.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['run', 'profile', 'final', 'out']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    r = preflight(args.run, args.profile, args.final, args.out)
    print(json.dumps(r['full_row_multiset']))
    return 0 if not r['full_row_multiset']['missing'] and not r['full_row_multiset']['extra'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
