"""多个处理阶段共用的精确金额合计，不改变原始金额。"""
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext
import re


class AmountError(ValueError):
    pass


def sum_exact(values):
    if not values:return Decimal(0)
    integer_digits=max(max(v.adjusted()+1,1) for v in values)
    fractional_digits=max(max(-v.as_tuple().exponent,0) for v in values)
    with localcontext() as context:
        context.prec=max(28,integer_digits+fractional_digits+len(str(len(values)))+2)
        return sum(values,Decimal(0))


def total_rows(rows):
    totals={'debit':Decimal(0),'credit':Decimal(0)};states={}
    for side in totals:
        counts=Counter();values=[]
        for row in rows:
            amount=row[side];counts[amount['status']]+=1
            if amount['status']=='blank':
                if amount['value'] is not None:raise AmountError('空白金额不能包含数值')
                continue
            if amount['status']!='number':raise AmountError('所选记录包含非法金额')
            if not isinstance(amount['value'],str) or not re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?',amount['value']):
                raise AmountError('解析后的金额必须是有限十进制字符串')
            try:value=Decimal(amount['value'])
            except (InvalidOperation,TypeError,ValueError) as error:raise AmountError('金额不是十进制数值') from error
            if not value.is_finite():raise AmountError('金额不能为NaN或Infinity')
            values.append(value)
            if value==0:counts['numeric_zero']+=1
        states[side]=dict(counts);totals[side]=sum_exact(values)
    return totals,states
