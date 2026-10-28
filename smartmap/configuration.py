"""配置读取、业务一致性校验和已确认项编译，不读取任何 Excel。"""
import copy
import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path

from .schema_validation import ConfigurationError, check_schema, validate
from .headers import build_aliases
from .applicability import applies_to

SCHEMA_PATH = Path(__file__).resolve().parents[1]/'config/schema.json'
POLICIES = {'date': 'period_month_end', 'hierarchy': 'verified_group_totals',
            'voucher_filter': 'exclude_voucher', 'auxiliary': 'review_requirements'}


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f'JSON重复键: {key}')
        result[key] = value
    return result


def parse_json(content):
    try:
        return json.loads(content, object_pairs_hook=_unique_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ConfigurationError('JSON不允许非有限数值 '+value)))
    except (ValueError, UnicodeError) as error:
        raise ConfigurationError(str(error)) from error


def validate_config(config):
    schema = parse_json(SCHEMA_PATH.read_text(encoding='utf-8'))
    check_schema(schema)
    validate(config, schema)
    try:
        build_aliases(config['source_profile'])
        prefixes=config['source_profile'].get('voucher_prefixes',[])
        if any(not value.isalpha() for value in prefixes):
            raise ValueError('凭证前缀只能含文字，不能夹带凭证编号、空白或符号')
    except ValueError as error:
        raise ConfigurationError(f'$.source_profile: {error}') from error
    closing=config['source_profile'].get('closing_recognition')
    if closing:
        import re
        roles=closing['account_roles']
        if not roles or any(not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', code) or
                role not in ['income','expense','current_profit','other'] for code,role in roles.items()):
            raise ConfigurationError('$.source_profile.closing_recognition.account_roles: 需有效科目编码和明确角色')
        if 'current_profit' not in roles.values():
            raise ConfigurationError('$.source_profile.closing_recognition: 必须配置本年利润科目角色')
    ids = set()
    confirmed_codes = set()
    confirmed_target_names = {}
    for section in ['rules', 'mappings']:
        for index, item in enumerate(config[section]):
            path = f'$.{section}[{index}]'
            if item['id'] in ids:
                raise ConfigurationError(f'{path}.id: 全局重复规则ID')
            ids.add(item['id'])
            if item['status'] == 'candidate' and item['review'] is not None:
                raise ConfigurationError(f'{path}.review: 候选尚未审核，必须为null')
            if item['status'] in ['confirmed', 'rejected']:
                if item['review'] is None or not any(x['kind']=='user_confirmation' for x in item['evidence']):
                    raise ConfigurationError(f'{path}: 已确认/已拒绝项必须有审核记录和用户确认依据')
                try:
                    date.fromisoformat(item['review']['reviewed_on'])
                except ValueError as error:
                    raise ConfigurationError(f'{path}.review.reviewed_on: 日期无效') from error
            if section == 'mappings' and item['status'] == 'confirmed':
                if item['source_code'] in confirmed_codes:
                    raise ConfigurationError(f'{path}.source_code: 一个外部编码只能有一条已确认映射')
                confirmed_codes.add(item['source_code'])
                target=item['target_code']
                if target in confirmed_target_names and confirmed_target_names[target]!=item['target_name']:
                    raise ConfigurationError(f'{path}.target_name: 同一目标编码的已确认名称不一致')
                confirmed_target_names[target]=item['target_name']
            if section == 'rules':
                if 'applicability' in item and item['kind'] not in ['hierarchy','voucher_filter']:
                    raise ConfigurationError(f'{path}.applicability: 当前只支持层级和整凭证筛选规则的案例范围')
                if item['policy'] != POLICIES[item['kind']]:
                    raise ConfigurationError(f'{path}.policy: 与规则类型不匹配')
                params = item['parameters']
                if item['kind'] in ['date', 'hierarchy'] and params:
                    raise ConfigurationError(f'{path}.parameters: 此规则不接受额外参数')
                if item['kind'] == 'voucher_filter':
                    allowed = {'summary_equals','voucher_category_equals','match_scope','classification_equals'}
                    if set(params)-allowed or params.get('match_scope') != 'all_lines' or not (set(params) & (allowed-{'match_scope'})):
                        raise ConfigurationError(f'{path}.parameters: 需完整凭证匹配条件，禁止凭证号或行号筛选')
                    if 'classification_equals' in params and (params['classification_equals']!='closing_structure_verified' or not config['source_profile'].get('closing_recognition')):
                        raise ConfigurationError(f'{path}.parameters: 结转筛选需配置结转识别且使用closing_structure_verified')
                if item['kind'] == 'auxiliary' and set(params) != {'required_fields'}:
                    raise ConfigurationError(f'{path}.parameters: 必须只包含required_fields')
    date_rules = [r for r in config['rules'] if r['kind']=='date']
    if len(date_rules) != 1 or date_rules[0]['status'] != 'confirmed':
        raise ConfigurationError('$.rules: 必须有且只有一条已确认月末日期规则')
    for kind in ['hierarchy','auxiliary']:
        if sum(r['kind']==kind for r in config['rules']) != 1:
            raise ConfigurationError(f'$.rules: 必须有且只有一条{kind}规则（可为候选）')
    profile=config.get('auxiliary_profile')
    if profile:
        priority=profile.get('source_priority',['explicit_code','explicit_name','subject_name'])
        order={'explicit_code':0,'explicit_name':1,'subject_name':2}
        if priority!=sorted(priority,key=order.get):raise ConfigurationError('$.auxiliary_profile.source_priority: 明确编码/字段必须先于名称回退')
        if profile['id'] in ids:raise ConfigurationError('$.auxiliary_profile.id: 全局重复ID')
        if profile['delimiter'].isspace():raise ConfigurationError('$.auxiliary_profile.delimiter: 不能仅为空白')
        if profile['status']=='candidate' and profile['review'] is not None:
            raise ConfigurationError('$.auxiliary_profile.review: 候选必须为null')
        if profile['status'] in ['confirmed','rejected']:
            if profile['review'] is None or not any(e['kind']=='user_confirmation' for e in profile['evidence']):
                raise ConfigurationError('$.auxiliary_profile: 确认/拒绝必须有审核及用户依据')
            try:date.fromisoformat(profile['review']['reviewed_on'])
            except ValueError as error:raise ConfigurationError('$.auxiliary_profile.review: 日期无效') from error
        targets=[r['target_code'] for r in profile['account_requirements']]
        if len(set(targets))!=len(targets):raise ConfigurationError('$.auxiliary_profile.account_requirements: 目标科目重复')
        if 'known_target_codes' in profile and not set(targets)<=set(profile['known_target_codes']):
            raise ConfigurationError('$.auxiliary_profile: 需要辅助的科目不在目标字典范围')
    return config


def compile_config(config):
    """只编译已确认项；返回深拷贝，不让调用方改变原配置。"""
    validate_config(config)
    active = {'schema_version':config['schema_version'],'config_id':config['config_id'],'revision':config['revision'],
        'scope':copy.deepcopy(config['scope']),'source_profile':copy.deepcopy(config['source_profile']),
        'rules':[copy.deepcopy(r) for r in config['rules'] if r['status']=='confirmed'],
        'mappings':[copy.deepcopy(m) for m in config['mappings'] if m['status']=='confirmed']}
    if config.get('auxiliary_profile',{}).get('status')=='confirmed':active['auxiliary_profile']=copy.deepcopy(config['auxiliary_profile'])
    digest = hashlib.sha256(json.dumps(active,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
    return active, digest


def inspect_config(config, raw_sha256=None):
    active, fingerprint = compile_config(config)
    items = config['rules']+config['mappings']
    warnings = []
    candidates = {}
    for mapping in config['mappings']:
        if mapping['status']=='candidate':
            candidates.setdefault(mapping['source_code'],set()).add((mapping['target_code'],mapping['target_name']))
    conflicts = sum(len(values)>1 for values in candidates.values())
    if conflicts:
        warnings.append(f'{conflicts}个外部科目有相互冲突的历史候选，未生效')
    if any(r['kind']=='hierarchy' and r['status']!='confirmed' for r in config['rules']):
        warnings.append('父子科目选择策略尚未确认；后续引擎必须保留待确认记录及证据')
    if any(r['kind']=='auxiliary' and r['status']!='confirmed' for r in config['rules']):
        warnings.append('辅助核算必填规则尚未确认')
    if not active['mappings']:
        warnings.append('没有已确认科目映射；不能输出已完成科目转换的结果')
    pending_filters = sum(r['kind']=='voucher_filter' and r['status']=='candidate' for r in config['rules'])
    if pending_filters:
        warnings.append(f'{pending_filters}条凭证排除规则尚未确认，不生效')
    return {'config_valid':True,'config_id':config['config_id'],'revision':config['revision'],
        'schema_version':config['schema_version'],'config_sha256':raw_sha256,'effective_config_sha256':fingerprint,
        'status_counts':dict(Counter(x['status'] for x in items)),
        'mapping_counts':dict(Counter(x['status'] for x in config['mappings'])),
        'active_mapping_count':len(active['mappings']),
        'active_voucher_filter_count':sum(r['kind']=='voucher_filter' for r in active['rules']),
        'candidate_voucher_filter_count':pending_filters,
        'candidate_mapping_conflict_count':conflicts,'warnings':warnings,
        'conversion_implemented':False,'ready_for_system_import':False}


def load_config(path):
    try:
        content = Path(path).read_bytes()
        config = parse_json(content.decode('utf-8-sig'))
        validate_config(config)
        return config, hashlib.sha256(content).hexdigest()
    except (OSError, UnicodeError) as error:
        raise ConfigurationError(f'无法读取配置：{error}') from error


def resolve_mapping(config, source_code, *, period=None, source_sha256=None):
    """仅精确匹配已确认编码；不继承父级、不截断、不利用候选补齐。"""
    active, _ = compile_config(config)
    if not isinstance(source_code,str):
        raise ConfigurationError('外部科目编码必须为字符串')
    match = next((m for m in active['mappings'] if m['source_code']==source_code),None)
    if match:
        if not applies_to(match,period=period,source_sha256=source_sha256):
            return {'status':'out_of_scope','mapping':None,'out_of_scope_mapping_ids':[match['id']]}
        return {'status':'matched','mapping':match}
    candidates = [m for m in config['mappings'] if m['source_code']==source_code and m['status']=='candidate']
    return {'status':'unconfirmed' if candidates else 'unknown','mapping':None}
