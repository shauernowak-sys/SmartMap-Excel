"""目标系统适配器到辅助规则的标准化接口；不包含企业科目或人员常量。"""
from collections import defaultdict


def build_catalog(accounts, archives, category_fields):
    """输入标准行code/categories/reference及category/code/name/reference。

    类别映射由调用方显式提供；未知类别和矛盾科目要求拒绝编译，字典重复保留供取值时报告。
    """
    groups=defaultdict(list)
    for row in accounts:
        code=row['code']
        if not isinstance(code,str) or not code.strip():raise ValueError('目标科目编码必须为明确文本')
        categories=row['categories']
        if not isinstance(categories,list):raise ValueError('核算类别必须由适配器明确拆分为列表')
        if any(c not in category_fields for c in categories):raise ValueError('目标科目含尚未适配的核算类别')
        groups[code].append(row)
    requirements=[]
    for code,rows in groups.items():
        choices={tuple(sorted({category_fields[c] for c in row['categories']})) for row in rows}
        if len(choices)!=1:raise ValueError('同一目标科目存在矛盾核算类别，不能首条匹配')
        fields=list(next(iter(choices)))
        if fields:requirements.append({'target_code':code,'fields':fields,'reference':'；'.join(row['reference'] for row in rows)})
    dictionary=[]
    for row in archives:
        if row['category'] not in category_fields:raise ValueError('辅助档案类别尚未适配')
        if any(not isinstance(row[k],str) or not row[k].strip() for k in ['code','name','reference']):raise ValueError('辅助档案编码/名称/来源不完整')
        dictionary.append({'field':category_fields[row['category']],'code':row['code'],'name':row['name'],'reference':row['reference']})
    return {'known_target_codes':list(groups),'account_requirements':requirements,'dictionary':dictionary}
