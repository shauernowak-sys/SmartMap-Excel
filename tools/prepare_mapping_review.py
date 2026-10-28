"""独立核对映射历史证据及目标字典，不改变任何规则状态。"""
import argparse
import hashlib
import json
import sys
from pathlib import Path
from zipfile import BadZipFile

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from smartmap.configuration import load_config
from mapping_review import build_mapping_review
from template_inspection import inspect
from verify_conversion_run import audit_run
from verify_evaluation_report import verify


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def named_manual_rows(path, rows):
    book = openpyxl.load_workbook(path, read_only=True, data_only=False)
    try:
        sheet = book['明细账 (2)']; sheet.reset_dimensions()
        values = {number: value for number, value in enumerate(sheet.iter_rows(values_only=True), 1)}
        if values[1][8] != '科目名称':
            raise ValueError('人工科目名称列不符合历史布局')
        result = []
        for row in rows:
            name = values[row['provenance']['excel_row']][8]
            if not isinstance(name, str) or not name.strip() or name.startswith('='):
                raise ValueError('人工科目名称缺失或为公式')
            result.append({**row, 'target_subject_name': name.strip()})
        return result
    finally:
        book.close()


def prepare(config_path, run, evaluation, template_report, out):
    config_path, run, evaluation, template_report, out = (Path(p).resolve() for p in [config_path, run, evaluation, template_report, out])
    if out.exists() or out.is_symlink() or any(out == p or p in out.parents for p in [run, evaluation, template_report]):
        raise ValueError('输出须为全新目录，且不能位于输入报告目录内')
    paths = {'config': config_path, 'conversion_manifest': run / 'run_manifest.json',
             'evaluation': evaluation / 'evaluation.json', 'template': template_report / 'template_profile.json',
             'references': evaluation / 'references.json'}
    hashes = {k: sha(p) for k, p in paths.items()}
    config, _ = load_config(config_path)
    if not audit_run(run)['usable_for_evaluation'] or not verify(evaluation)['all_passed']:
        raise ValueError('转换或历史验收未通过完整性检查')
    manifest, assessed, template = (read(paths[k]) for k in ['conversion_manifest', 'evaluation', 'template'])
    if manifest['inputs']['config']['sha256'] != hashes['config'] or assessed['inputs']['conversion_manifest']['sha256'] != hashes['conversion_manifest']:
        raise ValueError('配置、转换和历史验收不属于同一版本')
    if sha(Path(template['input']['path'])) != assessed['inputs']['final']['sha256'] or inspect(template['input']['path']) != template:
        raise ValueError('模板画像不对应当前历史成品')
    references = read(paths['references'])
    manual = named_manual_rows(Path(assessed['inputs']['manual']['path']), references['manual']['rows'])
    final_index = {r['excel_row']: r['values'] for r in template['data_rows']}
    final = [{**row, 'target_subject_name': final_index[row['provenance']['excel_row']]['科目名称']} for row in references['final']['rows']]
    packet = build_mapping_review(config, read(run / 'source_rows.json')['rows'],
                                  read(run / 'diagnostic_candidates.json')['rows'], manual, final, template['accounts'])
    if any(sha(paths[k]) != value for k, value in hashes.items()) or not audit_run(run)['usable_for_evaluation'] or not verify(evaluation)['all_passed']:
        raise ValueError('核验期间输入发生变化')
    packet['inputs'] = {k: {'path': str(p), 'sha256': hashes[k]} for k, p in paths.items()}
    out.mkdir(parents=True); (out / '.gitignore').write_text('*\n', encoding='utf-8')
    with (out / 'mapping_review.json').open('x', encoding='utf-8') as stream:
        json.dump(packet, stream, ensure_ascii=False, indent=2, allow_nan=False)
    def cell(v):
        return str(v).replace('|', '\\|').replace('\n', ' ')
    lines = ['# 科目映射逐项核对与待审核表', '',
             '证据一致仅表示历史数据及随附字典一致，不能替代财务确认。本轮没有批准任何映射。',
             f"候选{packet['candidate_mapping_count']}项：证据一致待审核{packet['evidence_consistent_count']}项，阻断{packet['blocked_mapping_count']}项。",
             f"人工及成品核心字段加科目名称多重集合一致，共{len(manual)}条。",
             f"诊断{packet['diagnostic_lines']}条，其中{packet['diagnostic_lines_with_candidate']}条有历史候选，{packet['diagnostic_lines_without_candidate']}条无候选。", '',
             '| 外部编码 | 外部科目名称 | 候选内部编码 | 候选内部名称 | 当前条数 | 人工证据条数 | 核验结论 |',
             '| --- | --- | --- | --- | ---: | ---: | --- |']
    for item in packet['items']:
        verdict = '证据一致，仍待确认' if not item['reason_codes'] else '阻断：' + '、'.join(item['reason_codes'])
        lines.append('| ' + ' | '.join(cell(v) for v in [item['source_code'], '；'.join(item['source_names']),
                     item['proposed_target_code'], item['proposed_target_name'], item['current_diagnostic_count'], item['manual_evidence_count'], verdict]) + ' |')
    lines += ['', '## 需先解决的映射阻断', '']
    for item in packet['items']:
        if item['reason_codes']:
            lines += [f"- {item['source_code']} → {item['proposed_target_code']}：{'、'.join(item['reason_codes'])}。"]
            for ref in item['target_dictionary_evidence']:
                lines += [f"  - 科目备查第{ref['excel_row']}行：{cell(ref['values']['科目名称'])}。"]
    lines += ['', '## 未被本次候选审核覆盖的来源科目', '']
    for item in packet['other_source_accounts']:
        lines += [f"- {item['source_code']}：{item['diagnostic_count']}条；目标未填写，来源行" + '、'.join(str(r['provenance']['excel_row']) for r in item['source_evidence']) + '。']
    lines += ['', '每项来源金额、凭证、原始行、人工行、字典行及同目标成品行见mapping_review.json。',
              '同目标成品行可能来自多个外部科目，不能当成一对一身份关联；全部出现次数均保留。',
              '客户、供应商、员工来源以及结转损益范围仍待确认；没有生成导入Excel。']
    (out / '科目映射待审核表.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    with (out / 'review_manifest.json').open('x', encoding='utf-8') as stream:
        json.dump({'mode': 'mapping_evidence_review', 'status': 'prepared_pending_financial_review',
                   'financial_approval_applied': False, 'ready_for_system_import': False,
                   'artifacts': {name: sha(out / name) for name in ['mapping_review.json', '科目映射待审核表.md', '.gitignore']}},
                  stream, ensure_ascii=False, indent=2)
    return packet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['config', 'run', 'evaluation', 'template-report', 'out']:
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    try:
        p = prepare(args.config, args.run, args.evaluation, args.template_report, args.out)
        print(json.dumps({k: p[k] for k in ['candidate_mapping_count', 'evidence_consistent_count', 'blocked_mapping_count', 'financial_approval_applied']}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError, BadZipFile) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
