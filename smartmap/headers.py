"""原始明细账表头识别。只按字段语义识别，不使用工作表序号或答案行数。"""
import re

DEFAULT_ALIASES={
    'date':['日期','凭证日期','业务日期'], 'period':['账期','会计期间','期间'],
    'subject':['科目名称','科目','会计科目'], 'subject_code':['科目编码','科目代码'],
    'voucher':['凭证字号','凭证号','凭证编号'], 'category':['凭证类别','凭证字'],
    'summary':['摘要'], 'debit':['借方','借方金额'], 'credit':['贷方','贷方金额']}
DEFAULT_PREFIXES=['记','收','付','转']
for _field,_label in [('customer','客户'),('supplier','供应商'),('employee','员工'),('department','部门'),('project','项目'),('stock_name','存货')]:
    DEFAULT_ALIASES[_field]=[_label,_label+'名称']
    DEFAULT_ALIASES[_field+'_code']=[_label+'编码',_label+'代码']


def normalize(value):
    return re.sub(r'\s+','',str(value or '')).strip(':：').lower()


def build_aliases(profile):
    result={key:{normalize(x) for x in values} for key,values in DEFAULT_ALIASES.items()}
    for key,values in profile.get('header_aliases',{}).items():
        result[key].update(normalize(x) for x in values)
    seen={}
    for field,values in result.items():
        for name in values:
            if name in seen and seen[name]!=field:raise ValueError(f'表头别名{ name!r }同时指向不同字段')
            seen[name]=field
    return result


def import_role(row):
    names={normalize(x) for x in row}
    return ('分录序号' in names or '导入结果' in names) and '科目编码' in names and bool(names & {'凭证类别','客户','供应商'})


def locate(row,aliases,previous=None):
    """支持同一行及相邻两行表头。重复语义列拒绝，不取第一列凑数。"""
    hits={}
    for values in ([previous,row] if previous is not None else [row]):
        for index,value in enumerate(values):
            name=normalize(value)
            for field,names in aliases.items():
                if name in names:hits.setdefault(field,set()).add(index)
    complete={'voucher','summary','debit','credit'}<=set(hits) and bool({'subject','subject_code'}&set(hits)) and bool({'date','period'}&set(hits))
    if not complete:return None
    if any(len(columns)!=1 for columns in hits.values()):raise ValueError('同一字段出现多个列，不能自动决定')
    columns={field:next(iter(indices)) for field,indices in hits.items()}
    if len(set(columns.values()))!=len(columns):raise ValueError('同一列同时匹配多个字段，表头层级不明确')
    return columns


def incomplete_flat_header(row,aliases):
    """其他备查/分析表有明确的平面表头时，不继续扫描其业务内容。"""
    names={normalize(x) for x in row}
    fields={field for field,values in aliases.items() if names & values}
    group_labels={'发生额','金额','借贷金额','本币发生额'}
    return len(fields)>=2 and not names & group_labels
