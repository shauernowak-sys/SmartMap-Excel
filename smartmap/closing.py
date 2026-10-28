"""期末损益结转的结构识别；只解释，不授权排除或补填目标科目。"""
from collections import Counter
from .amounts import AmountError, sum_exact, total_rows
from .applicability import applies_to


def classify_closing(rows, profile=None):
    result = {'kind': 'not_evaluated', 'diagnostic_only': True,
              'changes_disposition': False, 'reason_codes': [],
              'member_record_ids': [r['record_id'] for r in rows],
              'member_excel_rows': [r['provenance']['excel_row'] for r in rows]}
    if not profile:
        return result
    if any(not applies_to(profile, period=r['period'],
                         source_sha256=r['provenance']['input_sha256']) for r in rows):
        result['reason_codes'] = ['CLOSING_PROFILE_OUT_OF_SCOPE']
        return result
    selected = [r for r in rows if r['hierarchy']['decision'] == 'retained']
    roles = profile['account_roles']

    def role(code):
        matches = [p for p in roles if code == p or code.startswith(p + '.')]
        return roles[max(matches, key=len)] if matches else 'unknown'

    assigned = [(r, role(r['subject']['code'])) for r in selected]
    hinted = any(r['summary'] in profile['summary_hints'] for r in rows)
    profit = [r for r, value in assigned if value == 'current_profit']
    result.update(kind='possible_closing' if hinted or profit else 'ordinary',
                  selected_record_ids=[r['record_id'] for r in selected],
                  selected_excel_rows=[r['provenance']['excel_row'] for r in selected],
                  hierarchy_excluded_record_ids=[r['record_id'] for r in rows
                                                 if r['hierarchy']['decision'] == 'excluded'],
                  summary_hint=hinted, role_counts=dict(Counter(v for _, v in assigned)),
                  account_roles=[{'record_id': r['record_id'], 'code': r['subject']['code'],
                                  'role': value} for r, value in assigned])
    if result['kind'] == 'ordinary':
        return result
    reasons = []
    if any(r['parse_state'] != 'parsed' or r['hierarchy']['decision'] == 'needs_review' for r in rows):
        reasons.append('CLOSING_UPSTREAM_UNRESOLVED')
    if not profit:
        reasons.append('CLOSING_PROFIT_ACCOUNT_MISSING')
    if not any(v in ('income', 'expense') for _, v in assigned):
        reasons.append('CLOSING_PL_ACCOUNTS_MISSING')
    if any(v not in ('income', 'expense', 'current_profit') for _, v in assigned):
        reasons.append('CLOSING_OTHER_OR_UNKNOWN_ACCOUNTS')
    try:
        totals, _ = total_rows(selected)
        if totals['debit'] != totals['credit']:
            reasons.append('CLOSING_UNBALANCED')
        if not any(totals.values()):
            reasons.append('CLOSING_ZERO_TOTAL')
        for r, value in assigned:
            amounts, _ = total_rows([r])
            if (not any(amounts.values()) or all(amounts.values()) or
                    value == 'income' and amounts['credit'] != 0 or
                    value == 'expense' and amounts['debit'] != 0):
                reasons.append('CLOSING_DIRECTION_OR_ZERO_ENTRY')
        categories = {}
        for value in ('income', 'expense', 'current_profit'):
            amount, _ = total_rows([r for r, v in assigned if v == value])
            categories[value] = {side: str(n) for side, n in amount.items()}
        profit_amounts, _ = total_rows(profit)
        # 只是本次凭证对本年利润的净影响，不表示全年累计余额。
        net = sum_exact([profit_amounts['credit'], -profit_amounts['debit']])
        result.update(totals={side: str(n) for side, n in totals.items()},
                      role_totals=categories, current_profit_net_credit_impact=str(net))
    except AmountError:
        reasons.append('CLOSING_INVALID_AMOUNT')
    result['reason_codes'] = sorted(set(reasons))
    if not reasons:
        result['kind'] = 'closing_structure_verified'
    return result
