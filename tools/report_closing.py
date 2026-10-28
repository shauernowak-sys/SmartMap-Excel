"""对已封存运行生成结转凭证说明；不读取人工答案，不改变运行去向。"""
import argparse
import json
from pathlib import Path

from verify_conversion_run import audit_run


def generate(run, out):
    run, out = Path(run).resolve(), Path(out).resolve()
    if out == run or run in out.parents or out.exists():
        raise ValueError('说明报告须写到运行目录外的全新目录')
    audit = audit_run(run)
    if not audit['all_passed'] or not audit['usable_for_evaluation']:
        raise ValueError('运行产物审计未通过')
    def load(name):
        return json.loads((run / name).read_text(encoding='utf-8'))
    stage = load('stage_mapping.json')
    source = load('source_rows.json')['rows']
    by_id = {r['record_id']: r for r in source}
    groups = []
    for group in stage['filter_groups']:
        classification = group.get('closing_recognition', {})
        if classification.get('kind') not in ('possible_closing', 'closing_structure_verified'):
            continue
        groups.append({'context': group['context'], 'filter_decision': group['decision'],
                       'classification': classification,
                       'entries': [{'excel_row': by_id[i]['provenance']['excel_row'],
                                    'subject': by_id[i]['subject'], 'debit': by_id[i]['debit'],
                                    'credit': by_id[i]['credit'], 'record_id': i}
                                   for i in classification['selected_record_ids']]})
    result = {'mode': 'closing_explanation', 'run': str(run),
              'manifest_sha256': audit['manifest_sha256'],
              'changes_disposition': False, 'groups': groups}
    out.mkdir(parents=True)
    (out / '.gitignore').write_text('*\n', encoding='utf-8')
    (out / 'closing.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 结转损益识别说明', '',
             '本报告只解释凭证结构，不批准排除，不生成导入文件。原始文件加配置独立转换，未读取人工整理表或历史成品。', '']
    for g in groups:
        c, context = g['classification'], g['context']
        lines += [f"## {context['period']} {context['voucher_category']}{context['voucher_number']}", '',
                  f"识别状态：{c['kind']}；筛选状态：{g['filter_decision']}。",
                  f"原始展开{len(c['member_record_ids'])}条；层级排除{len(c['hierarchy_excluded_record_ids'])}条；有效结构{len(c['selected_record_ids'])}条。"]
        if 'totals' in c:
            lines += [f"借方合计{c['totals']['debit']}；贷方合计{c['totals']['credit']}。",
                      f"本次对本年利润的净贷方影响：{c['current_profit_net_credit_impact']}。负数表示净借方影响，不代表全年累计余额。"]
        lines += ['', '本年利润分录与损益分录属于同一结转事项；若该事项整体不纳入迁移，则无需仅为它补建目标科目映射。', '',
                  '| 原始Excel行 | 科目编码 | 科目名称 | 借方 | 贷方 |', '| ---: | --- | --- | ---: | ---: |']
        for r in g['entries']:
            name = r['subject']['name'].replace('|', '\\|').replace('\n', ' ')
            lines.append(f"| {r['excel_row']} | {r['subject']['code']} | {name} | {r['debit']['value'] or ''} | {r['credit']['value'] or ''} |")
        if c['reason_codes']:
            lines += ['', '尚需核查：' + '、'.join(c['reason_codes'])]
        lines += ['']
    if not groups:
        lines += ['本次没有识别出结转结构或结转候选；这不证明账套没有结转。']
    (out / '结转损益识别说明.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    generate(args.run, args.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
