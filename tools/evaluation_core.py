"""独立验收计算；不导入smartmap转换、映射或筛选模块。"""
import calendar
from collections import Counter, defaultdict, deque
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext


def rate(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'value': numerator / denominator if denominator else None}


def amount(row, side):
    value = row[side]
    if value['status'] == 'blank':
        if value['value'] is not None:
            raise ValueError('空金额不能携带数值')
        return ('blank', None)
    if value['status'] != 'number':
        raise ValueError('验收金额无效，不能按零处理')
    try:
        number = Decimal(value['value'])
    except (InvalidOperation, TypeError) as error:
        raise ValueError('金额不是有效十进制数') from error
    if not number.is_finite():
        raise ValueError('非有限金额')
    return ('number', number)


def identity(row):
    return row['period'], row['voucher_category'], row['voucher_number']


def anchor(row, code):
    return identity(row) + (row.get(code), row['summary'])


def signature(row, code):
    return anchor(row, code) + (amount(row, 'debit'), amount(row, 'credit'))


def match(predicted, expected, code):
    """逐个消费签名；等价重复记录保留次数，不宣称身份唯一。"""
    queues = defaultdict(deque)
    for i, row in enumerate(expected):
        queues[signature(row, code)].append(i)
    pairs, extra = [], []
    counts = Counter(signature(r, code) for r in expected)
    predicted_counts = Counter(signature(r, code) for r in predicted)
    for i, row in enumerate(predicted):
        key = signature(row, code)
        if queues[key]:
            j = queues[key].popleft()
            pairs.append({'predicted_index': i, 'expected_index': j,
                          'predicted_id': row['record_id'], 'expected_id': expected[j]['record_id'],
                          'predicted_provenance': row['provenance'], 'expected_provenance': expected[j]['provenance'],
                          'equivalent_identity_ambiguous': counts[key] > 1 or predicted_counts[key] > 1})
        else:
            extra.append(row)
    missing = [expected[i] for i in sorted(i for q in queues.values() for i in q)]
    return {'predicted_count': len(predicted), 'expected_count': len(expected),
            'matched_count': len(pairs), 'missing_count': len(missing), 'extra_count': len(extra),
            'precision': rate(len(pairs), len(predicted)), 'recall': rate(len(pairs), len(expected)),
            'pairs': pairs, 'missing': missing, 'extra': extra}


def balance(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[identity(row)].append(row)
    results = []
    for key, members in sorted(groups.items()):
        values = [[amount(r, s)[1] or Decimal(0) for r in members] for s in ['debit', 'credit']]
        with localcontext() as context:
            context.prec = max(64, sum(len(str(v)) for side in values for v in side) + 4)
            debit, credit = (sum(side, Decimal(0)) for side in values)
            results.append({'period': key[0], 'voucher_category': key[1], 'voucher_number': key[2],
                            'line_count': len(members), 'debit': str(debit), 'credit': str(credit),
                            'balanced': debit == credit})
    return {'rate': rate(sum(r['balanced'] for r in results), len(results)), 'vouchers': results}


def evaluate(predicted, expected, code):
    result = match(predicted, expected, code)
    # 金额一致率按不含金额的锚点建立总体，再按金额多重集合求交；不任意配对。
    left = Counter(anchor(r, code) for r in predicted)
    right = Counter(anchor(r, code) for r in expected)
    comparable = sum((left & right).values())
    result['amount_consistency'] = rate(result['matched_count'], comparable)
    result['balance'] = balance(predicted)
    return result


def mapping_and_dates(predicted, manual, comparison):
    mapping_correct = mapping_unknown = dates_correct = 0
    for pair in comparison['pairs']:
        row = predicted[pair['predicted_index']]
        expected = manual[pair['expected_index']]
        # 相同外部完整签名对应多个目标科目时，无法确定单条映射身份。
        targets = {r['target_subject_code'] for r in manual
                   if signature(r, 'external_subject_code') == signature(expected, 'external_subject_code')}
        if len(targets) != 1:
            mapping_unknown += 1
        elif row.get('target_subject_code') == next(iter(targets)):
            mapping_correct += 1
        period = expected['period']
        end = date(int(period[:4]), int(period[4:]), calendar.monthrange(int(period[:4]), int(period[4:]))[1]).isoformat()
        dates_correct += row.get('posting_date') == end
    return {'mapping_accuracy': rate(mapping_correct, comparison['matched_count']),
            'mapping_ambiguous_count': mapping_unknown,
            'month_end_date_accuracy': rate(dates_correct, comparison['matched_count'])}
