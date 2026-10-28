"""T12：独立验收已封存候选产物，不运行转换、不批准业务规则。"""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from zipfile import BadZipFile

from evaluation_core import evaluate, mapping_and_dates, match
from evaluation_reference import load_reference
from verify_conversion_run import audit_run


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write(path, data):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)


def run_evaluation(run, manual, final, out):
    run, manual, final, out = (Path(p).resolve() for p in [run, manual, final, out])
    if out.exists() or out.is_symlink() or run == out or run in out.parents:
        raise ValueError('验收输出须为全新目录，并位于转换目录之外')
    if out in manual.parents or out in final.parents:
        raise ValueError('输出目录不能包含答案输入')
    out.mkdir(parents=True)
    (out / '.gitignore').write_text('*\n', encoding='utf-8')
    report = {'mode': 'independent_historical_evaluation', 'status': 'failed',
              'business_acceptance_passed': False, 'ready_for_system_import': False,
              'created_at_utc': datetime.now(timezone.utc).isoformat(), 'inputs': {}}
    try:
        paths = {'conversion_manifest': run / 'run_manifest.json', 'manual': manual, 'final': final}
        hashes = {key: digest(path) for key, path in paths.items()}
        report['inputs'] = {k: {'path': str(p), 'sha256': hashes[k]} for k, p in paths.items()}
        audit = audit_run(run)
        report['conversion_audit'] = audit
        if not audit['usable_for_evaluation']:
            raise ValueError('转换产物审计未通过或运行失败，拒绝验收')
        manifest = load(run / 'run_manifest.json')
        manual_ref = load_reference(manual, 'manual')
        final_ref = load_reference(final, 'final')
        if manual_ref['rows'] and {r['period'] for r in manual_ref['rows']} != {manifest['period']}:
            raise ValueError('人工标准答案账期与转换不一致')
        if {r['period'] for r in final_ref['rows']} != {manifest['period']}:
            raise ValueError('历史成品账期与转换不一致')
        references = {'manual': manual_ref, 'final': final_ref}
        control = match(manual_ref['rows'], final_ref['rows'], 'target_subject_code')
        report['reference_control'] = control
        if control['missing_count'] or control['extra_count']:
            raise ValueError('人工答案与历史成品非日期核心字段不一致，不能作为一致标准')
        final_dates = mapping_and_dates(final_ref['rows'], final_ref['rows'],
                                       match(final_ref['rows'], final_ref['rows'], 'target_subject_code'))['month_end_date_accuracy']
        report['reference_final_month_end_dates'] = final_dates
        sections = {}
        for name, filename in [('official', 'candidates.json'), ('diagnostic', 'diagnostic_candidates.json')]:
            predicted = load(run / filename)['rows']
            external = evaluate(predicted, manual_ref['rows'], 'external_subject_code')
            external.update(mapping_and_dates(predicted, manual_ref['rows'], external))
            internal = evaluate(predicted, final_ref['rows'], 'target_subject_code')
            sections[name] = {'manual_external': external, 'final_internal': internal}
        report.update(sections=sections,
                      unresolved_exception_count=manifest['exception_issue_count'],
                      unresolved_affected_record_count=manifest['affected_record_count'])
        official = sections['official']['manual_external']
        internal = sections['official']['final_internal']
        passed = (official['matched_count'] == len(manual_ref['rows']) and not official['extra_count']
                  and not internal['missing_count'] and not internal['extra_count']
                  and official['mapping_accuracy']['value'] == 1 and official['month_end_date_accuracy']['value'] == 1
                  and official['balance']['rate']['value'] == 1 and manifest['exception_issue_count'] == 0
                  and final_dates['value'] == 1)
        # 验收期间再次审计全部转换文件和输入，防止混合版本。
        if any(digest(paths[k]) != value for k, value in hashes.items()) or not audit_run(run)['usable_for_evaluation']:
            raise ValueError('验收期间输入或转换产物发生变化')
        report.update(status='passed' if passed else 'differences_found', business_acceptance_passed=passed)
        write(out / 'references.json', references)
    except (OSError, ValueError, KeyError, TypeError, BadZipFile) as error:
        report['error'] = {'type': type(error).__name__, 'message': str(error)}
    report['exit_code'] = 2 if report['status'] == 'failed' else 0 if report['status'] == 'passed' else 3
    write(out / 'evaluation.json', report)
    lines = ['# 独立历史验收报告', '', '状态：' + report['status'] + '；退出码：' + str(report['exit_code']) + '。',
             '系统导入未就绪；正式结果与诊断结果分别统计。', '']
    if report['status'] != 'failed':
        lines += ['| 视图及标准 | 自动条数 | 匹配 | 漏提 | 多提 |', '| --- | ---: | ---: | ---: | ---: |']
        for view, section in report['sections'].items():
            for standard in ['manual_external', 'final_internal']:
                r = section[standard]
                lines.append(f"| {view} / {standard} | {r['predicted_count']} | {r['matched_count']} | {r['missing_count']} | {r['extra_count']} |")
        lines += ['', '| 视图 | 映射正确率 | 金额一致率 | 凭证平衡率 | 月末日期正确率 |',
                  '| --- | --- | --- | --- | --- |']
        def display(value):
            return f"{value['numerator']}/{value['denominator']}（{value['value']:.2%}）" if value['value'] is not None else '未评价（分母0）'
        for view, section in report['sections'].items():
            r = section['manual_external']
            lines.append(f"| {view} | {display(r['mapping_accuracy'])} | {display(r['amount_consistency'])} | {display(r['balance']['rate'])} | {display(r['month_end_date_accuracy'])} |")
        extras = report['sections']['diagnostic']['manual_external']['extra']
        from collections import Counter
        extra_groups = Counter((r['period'], r['voucher_category'], r['voucher_number']) for r in extras)
        lines += ['', '诊断多提凭证分布：' + ('；'.join(f'{k[0]} {k[1]}{k[2]}：{n}条' for k, n in sorted(extra_groups.items())) or '无') + '。']
        lines += ['', f"未解决异常事实：{report['unresolved_exception_count']}。", '',
                  '外部签名包括账期、凭证类别及号码、外部科目、摘要、借贷状态与精确金额。内部签名改用目标科目。',
                  '每个相同签名按出现次数一对一消费；等价重复只证明次数匹配，不证明单条身份唯一。',
                  '诊断目标科目缺失会如实影响内部匹配及映射正确率，不用历史答案补值。',
                  '完整比率分子、分母、逐行匹配、漏提、多提及来源见evaluation.json。']
    else:
        lines += ['验收失败：' + report['error']['message']]
    (out / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['run', 'manual', 'final', 'out']:
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    try:
        result = run_evaluation(args.run, args.manual, args.final, args.out)
        print(json.dumps({'status': result['status'], 'exit_code': result['exit_code'],
                          'business_acceptance_passed': result['business_acceptance_passed']}, ensure_ascii=False))
        return result['exit_code']
    except (OSError, ValueError) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
