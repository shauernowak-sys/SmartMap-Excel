"""只读检查历史凭证导入成品与备查表，输出JSON和中文摘要。"""
import argparse
import json
from pathlib import Path
from zipfile import BadZipFile

from template_inspection import inspect


def run(template, out):
    template, out = Path(template).resolve(), Path(out).resolve()
    if out.exists() or out.is_symlink() or out in template.parents:
        raise ValueError('输出须为全新目录，且不能包含输入')
    report = inspect(template)
    out.mkdir(parents=True)
    (out / '.gitignore').write_text('*\n', encoding='utf-8')
    with (out / 'template_profile.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    checks = report['crosscheck']
    lines = ['# 历史系统模板只读检查', '', '该文件为已填写历史成品，不是空白模板。系统导入尚未验收。',
             f"工作表顺序：{'、'.join(report['sheet_order'])}。", f"数据分录：{len(report['data_rows'])}条。",
             f"科目备查：{checks['account_record_count']}条；辅助备查：{checks['auxiliary_record_count']}条。",
             f"使用科目：{checks['used_account_count']}个；实际辅助填值：{len(checks['auxiliary_links'])}处。",
             f"引用或填值观察异常：{len(checks['issues'])}项。必填规则仍未确认。", '',
             '| 列 | 字段 | 非空 | 空白 | Python类型 |', '| --- | --- | ---: | ---: | --- |']
    for field in next(s for s in report['sheets'] if s['sheet'] == '凭证模板')['fields']:
        lines.append(f"| {field['column']} | {field['header']} | {field['nonempty_count']} | {field['blank_count']} | {json.dumps(field['python_types'], ensure_ascii=False)} |")
    lines += ['', '字段样例、单元格类型及格式、字典逐行引用、校验设置、完整来源行见template_profile.json。',
              '观察到填值不等于证明必填；备查核算类别只作为待确认依据。未更改业务配置、输入文件或生成导入Excel。']
    (out / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    try:
        report = run(args.template, args.out)
        print(json.dumps({'status': 'inspected', 'data_rows': len(report['data_rows']),
                          'observation_issues': len(report['crosscheck']['issues']), 'ready_for_system_import': False}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, BadZipFile) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
