"""从原始Excel重新核对三阶段，不用旧分析报告证明新结果正确。"""
import argparse
import calendar
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

import openpyxl

from analysis_core import blank, day, money, pair_occurrences, subject, text, voucher


COMMON = {'date': (1, 1), 'voucher': (4, 3), 'sequence': (5, 4), 'summary': (6, 5),
          'target_code': (7, 6), 'target_name': (8, 7), 'debit': (9, 8), 'credit': (10, 9),
          'quantity': (11, 10), 'currency': (12, 11), 'foreign_amount': (13, 12),
          'customer': (14, 13), 'supplier': (15, 14), 'department': (16, 15), 'employee': (17, 16),
          'stock_name': (18, 17), 'stock_spec': (19, 18), 'attachments': (20, 19), 'project': (21, 20),
          'customer_level': (22, 21), 'inbound_type': (23, 22), 'outbound_type': (24, 23)}
NUMBERS = {'debit', 'credit', 'quantity', 'foreign_amount', 'attachments'}


def read_book(path):
    book = openpyxl.load_workbook(path, read_only=True, data_only=False)
    try:
        result = {}
        for sheet in book:
            sheet.reset_dimensions()
            result[sheet.title] = [list(row) for row in sheet.iter_rows(values_only=True)]
        return result
    finally:
        book.close()


def value(row, column):
    return row[column] if column < len(row) else None


def record(row, number, sheet, layout):
    cols = {'raw': (3, 2, 4, 5, 6), 'manual': (4, 3, 6, 9, 10), 'final': (3, 6, 5, 8, 9)}[layout]
    v, code, summary, debit, credit = (value(row, c) for c in cols)
    d, c = money(debit), money(credit)
    if d[0] == 'invalid' or c[0] == 'invalid':
        raise ValueError(f'{sheet}第{number}行金额非法，不能忽略')
    return {'row': number, 'sheet': sheet, 'voucher': voucher(v), 'code': subject(code),
            'summary': text(summary), 'debit': d, 'credit': c}


def external_key(row):
    return row['voucher'], row['code'], row['summary'], row['debit'], row['credit']


def payload(row, kind):
    result = {}
    for field, cols in COMMON.items():
        raw = value(row, cols[0 if kind == 'manual' else 1])
        result[field] = day(raw) if field == 'date' else voucher(raw) if field == 'voucher' else money(raw) if field in NUMBERS else text(raw)
        if field in NUMBERS and result[field][0] == 'invalid':
            raise ValueError('标准答案有非法金额或数量')
    return result


def core_key(row):
    return tuple(row[k] for k in ['voucher', 'summary', 'target_code', 'target_name', 'debit', 'credit'])


def build_report(source, final_path, config_path, run):
    paths = [Path(p).resolve() for p in [source, final_path, config_path]]
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    books, final_book = read_book(paths[0]), read_book(paths[1])
    raw_rows, manual_rows, final_rows = books['明细账'], books['明细账 (2)'], final_book['凭证模板']
    if value(manual_rows[0], 7) != '科目编码' or value(manual_rows[0], 8) != '科目名称' or value(final_rows[0], 6) != '科目编码':
        raise ValueError('三阶段布局与当前历史样本不符')
    raw_all = [record(r, n, '明细账', 'raw') for n, r in enumerate(raw_rows, 1) if n >= 3]
    raw = [r for r in raw_all if r['voucher'] is not None and r['code']]
    manual = [record(r, n, '明细账 (2)', 'manual') for n, r in enumerate(manual_rows, 1) if n >= 3 and any(not blank(v) for v in r)]
    final = [record(r, n, '凭证模板', 'final') for n, r in enumerate(final_rows, 1) if n >= 2 and any(not blank(v) for v in r)]
    if any(r['voucher'] is None or not r['code'] for r in manual + final):
        raise ValueError('答案有无法解析的非空行')
    manual_values = [payload(manual_rows[r['row'] - 1], 'manual') for r in manual]
    final_values = [payload(final_rows[r['row'] - 1], 'final') for r in final]
    common_key = lambda r: tuple(v for k, v in r.items() if k != 'date')
    pairs, missing_final, extra_final = pair_occurrences(manual_values, final_values, common_key)
    source_pairs, source_missing, _ = pair_occurrences(manual, raw, external_key)
    periods = {text(value(raw_rows[r['row'] - 1], 1)) for r in raw}
    if len(periods) != 1:
        raise ValueError('原始文件不是单账期')
    period = next(iter(periods)); year, month = int(period[:4]), int(period[4:])
    end = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    # 独立按原始凭证/摘要与最近可见子级核平，不用答案选择保留行。
    contexts = defaultdict(list)
    for r in raw:
        contexts[(r['voucher'], r['summary'])].append(r)
    parent_rows, groups = set(), []
    for context, members in contexts.items():
        codes = {r['code'] for r in members}
        for parent in sorted(codes):
            children = [c for c in codes if c.startswith(parent + '.') and not any(c.startswith(mid + '.') and mid.startswith(parent + '.') for mid in codes if mid != parent)]
            if not children:
                continue
            parents = [r for r in members if r['code'] == parent]
            descendants = [r for r in members if r['code'] in children]
            with localcontext() as ctx:
                ctx.prec = 64
                totals = lambda rows: tuple(sum((r[s][1] or Decimal(0) for r in rows), Decimal(0)) for s in ['debit', 'credit'])
                p, c = totals(parents), totals(descendants)
            equal = p == c and any(v != 0 for v in p)
            groups.append({'voucher': context[0], 'summary': context[1], 'parent_code': parent,
                           'parent_rows': [r['row'] for r in parents], 'child_rows': [r['row'] for r in descendants],
                           'parent_totals': p, 'child_totals': c, 'verified': equal})
            if equal:
                parent_rows.update(r['row'] for r in parents)
    blocked_vouchers={g['voucher'] for g in groups if not g['verified']}
    parent_rows={n for g in groups if g['verified'] and g['voucher'] not in blocked_vouchers for n in g['parent_rows']}
    leaves = [r for r in raw if r['row'] not in parent_rows]
    leaf_pairs, leaf_missing, leaf_extra = pair_occurrences(manual, leaves, external_key)
    config = json.loads(paths[2].read_text(encoding='utf-8-sig'))
    approved = {m['source_code']: m for m in config['mappings'] if m['status'] == 'confirmed' and (
        'applicability' not in m or m['applicability'] == {'period': period, 'source_sha256': hashes[str(paths[0])]})}
    predictions = []
    for r in leaves:
        mapping = approved.get(r['code'])
        predictions.append({'voucher': r['voucher'], 'summary': r['summary'], 'target_code': mapping['target_code'] if mapping else '',
                            'target_name': mapping['target_name'] if mapping else '', 'debit': r['debit'], 'credit': r['credit'],
                            'date': end, 'source_row': r['row'], 'external_code': r['code']})
    mapped_pairs, mapped_missing, mapped_extra = pair_occurrences(final_values, predictions, core_key)
    manifest = json.loads((Path(run) / 'run_manifest.json').read_text(encoding='utf-8'))
    if manifest['inputs']['config']['sha256'] != hashes[str(paths[2])] or manifest['inputs']['source']['sha256'] != hashes[str(paths[0])]:
        raise ValueError('生产运行与本次复核输入不一致')
    automatic = json.loads((Path(run) / 'diagnostic_candidates.json').read_text(encoding='utf-8'))['rows']
    auto_index = {r['provenance']['excel_row']: r for r in automatic}
    production_rows=json.loads((Path(run)/'source_rows.json').read_text(encoding='utf-8'))['rows']
    # 生产引擎已通过凭证核验的分录；仅按生产状态选择，标准答案不参与选择。
    core_rows=[]
    for row in production_rows:
        if row['voucher_validation']['decision']!='retained':continue
        core_rows.append({'record_id':row['record_id'],'source_row':row['provenance']['excel_row'],
                          'voucher':row['voucher']['number'],'summary':row['summary'],
                          'external_code':row['subject']['code'], 'target_code':row['account_mapping']['target_code'],
                          'target_name':row['account_mapping']['target_name'],'debit':(row['debit']['status'],Decimal(row['debit']['value']) if row['debit']['value'] is not None else None),
                          'credit':(row['credit']['status'],Decimal(row['credit']['value']) if row['credit']['value'] is not None else None),
                          'date':row['voucher_validation']['posting_date'],'entry_sequence':row['voucher_validation']['entry_sequence'],
                          'auxiliary':{},'review_only':True,'ready_for_system_import':False})
    cp,cm,ce=pair_occurrences(final_values,core_rows,core_key)
    missing_aux = []
    auxiliary_fields = ['customer', 'supplier', 'department', 'employee', 'stock_name', 'stock_spec',
                        'project', 'customer_level', 'inbound_type', 'outbound_type']
    for fi, pi in mapped_pairs:
        ref, pred = final_values[fi], predictions[pi]
        auto = auto_index[pred['source_row']]
        for field in auxiliary_fields:
            expected = ref[field]
            if expected and text(auto.get('auxiliary', {}).get(field)) != expected:
                missing_aux.append({'source_row': pred['source_row'], 'final_row': final[fi]['row'], 'field': field,
                                    'expected': expected, 'actual': auto.get('auxiliary', {}).get(field)})
    # 假设排除摘要全部为结转损益的整凭证，仅用于说明历史范围差异；不应用到配置。
    voucher_groups = defaultdict(list)
    for p in predictions:
        voucher_groups[p['voucher']].append(p)
    closing = {v for v, rows in voucher_groups.items() if all(r['summary'] == '结转损益' for r in rows)}
    hypothetical = [p for p in predictions if p['voucher'] not in closing]
    hp, hm, he = pair_occurrences(final_values, hypothetical, core_key)
    mapping_chain = [{'source_row': raw[j]['row'], 'manual_row': manual[i]['row'], 'manual_values': manual_values[i],
                      'final_rows': [final[fj]['row'] for mi, fj in pairs if mi == i]}
                     for i, j in source_pairs if manual[i]['code'] == '1221.05']
    result = {'mode': 'fresh_three_stage_recheck', 'inputs': hashes, 'period': period,
              'source_business_rows': len(raw), 'source_vouchers': len({r['voucher'] for r in raw}),
              'source_nonvoucher_subject_rows': sum(r['voucher'] is None and r['code'] is not None for r in raw_all),
              'manual_rows': len(manual), 'manual_vouchers': len({r['voucher'] for r in manual}), 'final_rows': len(final),
              'manual_to_raw': {'matched': len(source_pairs), 'missing': len(source_missing)},
              'manual_final_21_nondate_fields': {'matched': len(pairs), 'missing': len(missing_final), 'extra': len(extra_final)},
              'final_month_end_rows': sum(r['date'] == end for r in final_values),
              'verified_hierarchy_groups': sum(g['verified'] for g in groups), 'hierarchy_groups': groups,
              'parent_rows': sorted(parent_rows), 'structural_candidates': len(leaves),
              'external_vs_manual': {'matched': len(leaf_pairs), 'missing': len(leaf_missing), 'extra': len(leaf_extra)},
              'mapped_core_vs_final': {'matched': len(mapped_pairs), 'missing': len(mapped_missing), 'extra': len(mapped_extra)},
              'core_review_rows':core_rows,
              'core_review_vs_final':{'selected_without_gold':True,'count':len(core_rows),'matched':len(cp),'missing':len(cm),'extra':len(ce)},
              'mapped_core_pairs': [{'final_row': final[i]['row'], 'source_row': predictions[j]['source_row']} for i, j in mapped_pairs],
              'extra_business_rows': [leaves[i] for i in leaf_extra], 'mapping_1221_05_chain': mapping_chain,
              'missing_auxiliary_cells': missing_aux, 'missing_auxiliary_cell_count': len(missing_aux),
              'missing_auxiliary_row_count': len({r['final_row'] for r in missing_aux}),
              'hypothetical_closing_exclusion': {'applied': False, 'condition': '整凭证全部摘要为结转损益', 'remaining': len(hypothetical),
                                                'matched_core': len(hp), 'missing_core': len(hm), 'extra_core': len(he)},
              'production_manifest': manifest,
              'business_acceptance_passed': bool(manifest['business_acceptance_passed'] and not missing_aux and not mapped_missing and not mapped_extra),
              'ready_for_system_import': False}
    if any(hashlib.sha256(p.read_bytes()).hexdigest() != hashes[str(p)] for p in paths):
        raise ValueError('复核期间输入发生变化')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['source', 'final', 'config', 'run', 'out']:
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.out.exists() or args.out.is_symlink():
            raise ValueError('复核目录必须全新')
        if any(p.resolve() == args.out.resolve() or p.resolve() in args.out.resolve().parents for p in [args.run, args.source, args.final, args.config]):
            raise ValueError('复核结果不能写入输入或封存转换目录')
        report = build_report(args.source, args.final, args.config, args.run)
        args.out.mkdir(parents=True); (args.out / '.gitignore').write_text('*\n', encoding='utf-8')
        with (args.out / 'recheck.json').open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, default=str)
        with (args.out/'核心审核候选.csv').open('x',encoding='utf-8-sig',newline='') as stream:
            writer=csv.writer(stream)
            writer.writerow(['原始行号','凭证号','原始科目','目标编码','目标名称','摘要','借方','贷方','月末日期','分录序号','辅助核算已验收','可系统导入'])
            for row in report['core_review_rows']:
                values=[row['source_row'],row['voucher'],row['external_code'],row['target_code'],row['target_name'],row['summary'],row['debit'][1],row['credit'][1],row['date'],row['entry_sequence'],'否','否']
                writer.writerow(["'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) and not re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?',v.strip()) else v for v in values])
        manifest=report['production_manifest']; core=report['mapped_core_vs_final']; aux=Counter(r['field'] for r in report['missing_auxiliary_cells'])
        lines=['# 三阶段重新核验结论', '', '本报告直接重新读取原始、人工整理和历史成品，不用旧分析报告作为正确性证明。',
               '', '**正式业务验收尚未通过。**' if not report['business_acceptance_passed'] else '正式清单及本报告均通过业务核对。', '',
               '| 项目 | 本次结果 |', '| --- | --- |',
               f"| 原始有效凭证科目记录 | {report['source_business_rows']}条 |",
               f"| 人工 / 历史成品 | {report['manual_rows']} / {report['final_rows']}条 |",
               f"| 人工追踪原始记录 | 匹配{report['manual_to_raw']['matched']}，漏{report['manual_to_raw']['missing']} |",
               f"| 人工与成品21个非日期字段 | 匹配{report['manual_final_21_nondate_fields']['matched']}，漏{report['manual_final_21_nondate_fields']['missing']}，多{report['manual_final_21_nondate_fields']['extra']} |",
               f"| 层级核平 / 排除父级 | {report['verified_hierarchy_groups']}组 / {len(report['parent_rows'])}条 |",
               f"| 原始结构提取候选 | {report['structural_candidates']}条 |",
               f"| 已映射核心字段对历史成品 | 匹配{core['matched']}，漏{core['missing']}，多{core['extra']} |",
               f"| 生产状态选出的核心审核候选 | {report['core_review_vs_final']['count']}条，匹配{report['core_review_vs_final']['matched']}，漏{report['core_review_vs_final']['missing']}，多{report['core_review_vs_final']['extra']} |",
               f"| 未填辅助值 | {report['missing_auxiliary_cell_count']}处，涉及{report['missing_auxiliary_row_count']}行 |",
               f"| 正式候选输出 | {manifest['row_counts']['included']}条 |", '',
               '## 1221.05明确对应链路', '']
        for chain in report['mapping_1221_05_chain']:
            lines.append(f"原始第{chain['source_row']}行 → 人工第{chain['manual_row']}行 → 成品第{'、'.join(map(str,chain['final_rows']))}行；人工目标编码{chain['manual_values']['target_code']}，名称{chain['manual_values']['target_name']}。")
        lines += ['', '## 剩余差异', '',
                  '多出的业务记录见recheck.json的extra_business_rows；没有因为历史答案未选它们而静默删除。',
                  '核心审核候选.csv按生产引擎凭证核验通过状态选出，没有读取标准答案参与选择；缺辅助字段，只能审核，不能直接导入。其余待确认业务仍保留在生产清单。',
                  '辅助值缺口按字段：'+ '、'.join(f'{k} {n}处' for k,n in aux.items())+'。取数规则尚未实现完成，不能运行时直接抄人工答案充当自动转换。',
                  '若仅作“排除整凭证全部摘要为结转损益”的假设核对：'+str(report['hypothetical_closing_exclusion'])+'。该假设没有应用到配置或正式输出。',
                  '程序测试、历史核心字段匹配、完整业务验收及系统导入是分别核验的结果；本轮没有生成可导入Excel。']
        (args.out/'三阶段复核结论.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
        summary = {k: report[k] for k in ['source_business_rows', 'manual_rows', 'final_rows', 'manual_to_raw',
                    'manual_final_21_nondate_fields', 'verified_hierarchy_groups', 'structural_candidates',
                    'mapped_core_vs_final', 'missing_auxiliary_cell_count', 'hypothetical_closing_exclusion']}
        print(json.dumps(summary, ensure_ascii=False, default=str)); return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False)); return 2


if __name__ == '__main__':
    raise SystemExit(main())
