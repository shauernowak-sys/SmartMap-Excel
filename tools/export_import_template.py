"""准备空模板或写出经审计的正式候选；禁止以诊断记录替代正式候选。"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from smartmap.template_profile import load_profile, sha
from smartmap.template_writer import prepare_template, write_template
from verify_conversion_run import audit_run


def export_run(run, prepared, out, *, allow_partial=False):
    run, prepared, out = Path(run).resolve(), Path(prepared).resolve(), Path(out).resolve()
    if out.exists() or out in run.parents or run in out.parents or out == run or out in prepared.parents or prepared in out.parents or out == prepared:
        raise ValueError('输出须为转换运行和空模板之外的全新目录')
    if type(allow_partial) is not bool:
        raise ValueError('部分导出须为明确布尔选择')
    audit = audit_run(run)
    if not audit['all_passed'] or not audit['usable_for_evaluation']:
        raise ValueError('转换产物审计未通过')
    def load(name):
        return json.loads((run / name).read_text(encoding='utf-8'))
    manifest = load('run_manifest.json')
    rows = load('candidates.json')['rows']
    blocked = (not rows or not manifest['batch_complete'] and
               not (allow_partial and manifest['allow_partial'] and manifest['partial_export']))
    if blocked:
        result = {'mode': 'template_export', 'status': 'blocked', 'xlsx_created': False,
                  'reason': '批次待确认且未明确选择部分导出，或不存在正式候选；不使用诊断分录补齐',
                  'source_manifest_sha256': audit['manifest_sha256'],
                  'eligible_record_count': manifest.get('eligible_core_candidate_count', 0),
                  'ready_for_system_import': False, 'business_acceptance_passed': False}
        out.mkdir(parents=True)
        (out / '.gitignore').write_text('*\n', encoding='utf-8')
    else:
        result = write_template(rows, prepared, out)
        after_audit = audit_run(run)
        if not after_audit['all_passed'] or sha(run / 'run_manifest.json') != audit['manifest_sha256']:
            (out / 'vouchers.xlsx').unlink(missing_ok=True)
            raise ValueError('写出期间转换运行清单发生变化')
        result.update(status='written_for_review', xlsx_created=True,
                      partial_export=manifest['partial_export'], batch_complete=manifest['batch_complete'],
                      source_manifest_sha256=audit['manifest_sha256'])
    with (out / 'export_manifest.json').open('x', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare', help='按配置清空历史凭证数据，仅生成空模板')
    prep.add_argument('--reference', type=Path, required=True)
    prep.add_argument('--profile', type=Path, required=True)
    prep.add_argument('--out', type=Path, required=True)
    export = sub.add_parser('export', help='只写出正式候选，默认禁止部分导出')
    for name in ['run', 'prepared', 'out']:
        export.add_argument('--' + name, type=Path, required=True)
    export.add_argument('--allow-partial', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            result = prepare_template(args.reference, load_profile(args.profile), args.out)
            print(json.dumps({'status': 'blank_prepared', 'historical_rows_cleared': result['historical_rows_cleared']}, ensure_ascii=False))
            return 0
        result = export_run(args.run, args.prepared, args.out, allow_partial=args.allow_partial)
        print(json.dumps({k: v for k, v in result.items() if k != 'lineage'}, ensure_ascii=False))
        return 3 if result['status'] == 'blocked' else 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
