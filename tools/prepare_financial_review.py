"""生成本地财务确认材料；只读现配置和封存报告，不执行审核或导入。"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from smartmap.configuration import load_config
from financial_review import answer_draft, build_packet
from template_inspection import inspect
from verify_conversion_run import audit_run
from verify_evaluation_report import verify


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def pointer(value, path):
    for part in path.strip('/').split('/'):
        if part:
            value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def prepare(config_path, run, evaluation, template_report, out, *, clarification_path=None):
    config_path, run, evaluation, template_report, out = (Path(p).resolve() for p in [config_path, run, evaluation, template_report, out])
    if out.exists() or out.is_symlink() or any(out == p or p in out.parents for p in [run, evaluation, template_report]):
        raise ValueError('审核输出须为全新目录，且位于所有输入报告目录之外')
    inputs = {'config': config_path, 'conversion_manifest': run / 'run_manifest.json',
              'evaluation': evaluation / 'evaluation.json', 'template': template_report / 'template_profile.json',
              'exceptions': run / 'exceptions.json'}
    if clarification_path:
        inputs['clarification']=Path(clarification_path).resolve()
    hashes = {key: digest(path) for key, path in inputs.items()}
    config, _ = load_config(config_path)
    if not audit_run(run)['usable_for_evaluation'] or not verify(evaluation)['all_passed']:
        raise ValueError('转换或历史验收报告不可靠，拒绝整理审核材料')
    manifest = read(inputs['conversion_manifest']); assessed = read(inputs['evaluation'])
    if manifest['inputs']['config']['sha256'] != hashes['config']:
        raise ValueError('配置已改变，需用新配置重跑转换后再整理')
    if assessed['inputs']['conversion_manifest']['sha256'] != hashes['conversion_manifest']:
        raise ValueError('历史验收与转换不是同一次运行')
    template = read(inputs['template'])
    source_template = Path(template['input']['path'])
    if digest(source_template) != assessed['inputs']['final']['sha256'] or inspect(source_template) != template:
        raise ValueError('模板画像与实际历史成品不一致，需重新盘点')
    exceptions = read(inputs['exceptions'])
    clarification=read(inputs['clarification']) if 'clarification' in inputs else None
    if clarification:
        expected={'period':manifest['period'],'source_sha256':manifest['inputs']['source']['sha256']}
        if clarification['scope']!=expected or clarification['account_clarification']['meaning_confirmed'] is not True or clarification['account_clarification']['mapping_approval_included'] is not False:
            raise ValueError('科目含义说明不属于本案例，或不能与映射批准混用')
    packet = build_packet(config, template, exceptions,clarification)
    evidence_inputs = {'config': config, 'template': template, 'exceptions': exceptions}
    if clarification:evidence_inputs['clarification']=clarification
    for item in packet['items']:
        for ref in item['evidence']:
            pointer(evidence_inputs[ref['input']], ref['pointer'])
    if any(digest(inputs[key]) != value for key, value in hashes.items()):
        raise ValueError('整理期间输入发生变化')
    packet['inputs'] = {k: {'path': str(v), 'sha256': hashes[k]} for k, v in inputs.items()}
    out.mkdir(parents=True)
    (out / '.gitignore').write_text('*\n', encoding='utf-8')
    write(out / 'review_packet.json', packet)
    packet_hash = digest(out / 'review_packet.json')
    write(out / 'review_answers.json', answer_draft(packet, packet_hash))
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ')
    lines = ['# 财务待确认清单', '',
             '所有事项均待确认。本材料不是运行配置；填写答案不会自动批准科目、改变金额或生成导入文件。',
             'review_answers.json为可填写草稿，保留每项ID，填写决定、实际规则、审核人、日期和理由。',
             '已确认的月末日期规则沿用，无需重新确认。先处理字典冲突、业务范围及辅助来源，再逐项审核映射。', '',
             f"共{packet['item_count']}项；这是审核事项数，不等于异常行数或异常事实数。", '',
             '| 类别 | 项数 |', '| --- | ---: |']
    labels = {'account_mapping': '历史科目候选', 'business_rule': '现有候选业务规则', 'unknown_account': '未知科目',
              'account_dictionary_conflict': '科目备查冲突', 'auxiliary_dictionary_gap': '辅助备查缺口',
              'account_auxiliary_requirement': '按科目辅助核算要求', 'template_policy': '系统及来源策略'}
    labels['conversion_exception'] = '其他转换异常（保留，不静默忽略）'
    lines += [f'| {labels[k]} | {n} |' for k, n in packet['kind_counts'].items()]
    if clarification:
        fact=clarification['account_clarification']
        lines += ['',f"用户已说明{fact['account_code']}当前对应“{cell(fact['user_reply'])}”；不重复询问名称。历史备查仍有重复记录，需要核对最新字典；相关未批准映射仍单独待确认。"]
    for kind, label in labels.items():
        lines += ['', '## ' + label, '', '| 审核ID | 事项 | 状态 | 提示 |', '| --- | --- | --- | --- |']
        for item in packet['items']:
            if item['kind'] != kind:
                continue
            hint = '目标字典缺失/冲突或名称不一致，先解决字典' if item.get('template_reference_conflict') else ''
            if item.get('data_cells'):
                hint = '、'.join(item['data_cells'])
            lines.append(f"| {item['id']} | {cell(item['title'])} | 待确认 | {cell(hint)} |")
    lines += ['', '完整证据索引及输入哈希见review_packet.json；决定应限定账套、账期/有效范围，不能用特定凭证号或行号代替业务规则。',
              '未知科目目标、辅助编码、账套版本及审核人均未预填。没有批准任何真实规则。']
    (out / '财务待确认清单.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    write(out / 'review_manifest.json', {'mode': 'financial_review_preparation', 'status': 'prepared_for_review',
          'item_count': packet['item_count'], 'automatic_application_supported': False,
          'immutable_artifacts': {name: digest(out / name) for name in ['review_packet.json', '财务待确认清单.md', '.gitignore']},
          'editable_draft': 'review_answers.json', 'ready_for_system_import': False})
    return packet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['config', 'run', 'evaluation', 'template-report', 'out']:
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--clarification',type=Path,help='可选：与当前案例绑定的用户科目含义说明')
    args = parser.parse_args()
    try:
        packet = prepare(args.config, args.run, args.evaluation, args.template_report, args.out,clarification_path=args.clarification)
        print(json.dumps({'status': 'prepared_for_review', 'item_count': packet['item_count'], 'kind_counts': packet['kind_counts'], 'automatically_applied': False}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
