"""原始值的确定性解析；不猜科目、不改金额、不使用历史答案。"""
import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


def blank(value):
    return value is None or isinstance(value,str) and not value.strip()


def raw_value(value):
    if value is None:
        return {'type':'null','value':None}
    return {'type':type(value).__name__,'value':value.isoformat() if isinstance(value,(date,datetime)) else str(value)}


def parse_money(value):
    result={'status':'blank','value':None,'raw':raw_value(value)}
    if blank(value):return result
    result['status']='invalid'
    if isinstance(value,bool):return result
    text=str(value).strip()
    if not isinstance(value,(int,float,Decimal)) and not re.fullmatch(r'[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?',text):
        return result
    try:
        amount=Decimal(text.replace(',',''))
        if amount.is_finite():result.update(status='number',value=format(amount,'f'))
    except InvalidOperation:
        pass
    return result


def parse_period(value):
    if blank(value):return None
    if isinstance(value,bool):raise ValueError('账期不能为布尔值')
    if isinstance(value,float):
        if not math.isfinite(value) or not value.is_integer():raise ValueError('账期不是整数')
        value=int(value)
    text=str(value).strip()
    match=re.fullmatch(r'([1-9][0-9]{3})(0[1-9]|1[0-2])',text)
    if not match:match=re.fullmatch(r'([1-9][0-9]{3})[-/年](0?[1-9]|1[0-2])月?',text)
    if not match:raise ValueError('无法识别账期，支持YYYYMM、YYYY-MM或YYYY年M月')
    return f'{int(match[1]):04d}{int(match[2]):02d}'


def parse_date(value):
    if blank(value):return None
    if isinstance(value,datetime):return value.date().isoformat()
    if isinstance(value,date):return value.isoformat()
    if not isinstance(value,str):raise ValueError('无日期格式的数值不能擅自当作Excel日期序列号')
    for pattern in ['%Y-%m-%d','%Y/%m/%d','%Y%m%d']:
        if pattern=='%Y%m%d' and not re.fullmatch(r'[0-9]{8}',value.strip()):continue
        try:return datetime.strptime(value.strip(),pattern).date().isoformat()
        except ValueError:pass
    raise ValueError('日期无效或格式不支持')


def parse_voucher(value,prefixes):
    if blank(value):return None
    if isinstance(value,bool):raise ValueError('凭证号不能为布尔值')
    if isinstance(value,float):
        if not math.isfinite(value) or not value.is_integer():raise ValueError('凭证号不是整数')
        value=int(value)
    text=str(value).strip()
    alternatives='|'.join(re.escape(x) for x in sorted(prefixes,key=len,reverse=True))
    pattern=r'(?:(?P<category>'+alternatives+r')\s*[-－]?\s*)?(?P<number>[0-9]+)'
    match=re.fullmatch(pattern,text)
    if not match or int(match['number'])<=0:raise ValueError('凭证字号无效或前缀尚未配置')
    return {'category':match['category'] or '', 'number':int(match['number'])}


def parse_subject(value,explicit_code=None):
    text='' if blank(value) else str(value).strip()
    match=re.fullmatch(r'([0-9]+(?:\.[0-9]+)*)(?=[^0-9.]|$)\s*(.*)',text)
    inline=match[1] if match else None
    if not blank(explicit_code):
        if not isinstance(explicit_code,str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*',explicit_code.strip()):
            raise ValueError('独立科目编码必须为文本编码，不能猜测数值单元格的前导零')
        code=explicit_code.strip()
        if inline and code!=inline:raise ValueError('独立科目编码与科目原文冲突')
    else:
        if not isinstance(value,str) or inline is None:raise ValueError('无法从科目原文取得文本编码')
        code=inline
    return {'code':code,'segments':code.split('.'),'name':match[2].strip() if inline else text}
