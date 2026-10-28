"""执行项目固定 schema 的约束；不是面向任意外部 schema 的通用验证器。"""
import json
import re


class ConfigurationError(ValueError):
    """含字段路径的可解释配置错误。"""


SUPPORTED = {'$schema', '$defs', '$ref', 'title', 'description', 'type', 'properties',
             'additionalProperties', 'required', 'items', 'minItems', 'uniqueItems',
             'minLength', 'minimum', 'pattern', 'enum', 'const'}


def _reference(root, value):
    if not value.startswith('#/$defs/') or value.count('/') != 2:
        raise ConfigurationError('schema: 仅支持项目内的 $defs 引用')
    try:
        return root['$defs'][value.split('/')[-1]]
    except KeyError as error:
        raise ConfigurationError('schema: 引用不存在 '+value) from error


def check_schema(schema, root=None):
    """schema新增未实现关键字时拒绝，防止约束被静默忽略。"""
    root = schema if root is None else root
    unknown = set(schema)-SUPPORTED
    if unknown:
        raise ConfigurationError('schema: 未支持的关键字 '+', '.join(sorted(unknown)))
    if '$ref' in schema:
        if set(schema)-{'$ref','title','description'}:
            raise ConfigurationError('schema: $ref旁不得添加可能被忽略的约束')
        _reference(root, schema['$ref'])
    if 'pattern' in schema:
        re.compile(schema['pattern'])
    for keyword in ['properties', '$defs']:
        for child in schema.get(keyword, {}).values():
            check_schema(child, root)
    if 'items' in schema:
        check_schema(schema['items'], root)


def validate(value, schema, root=None, path='$'):
    root = schema if root is None else root
    if '$ref' in schema:
        validate(value, _reference(root, schema['$ref']), root, path)
        return
    types = {'object': lambda x: isinstance(x, dict), 'array': lambda x: isinstance(x, list),
             'string': lambda x: isinstance(x, str), 'integer': lambda x: type(x) is int,
             'null': lambda x: x is None}
    if 'type' in schema:
        choices = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        if not any(types[t](value) for t in choices):
            raise ConfigurationError(f'{path}: 类型必须为 {choices}')
    if 'enum' in schema and not any(type(value) is type(x) and value == x for x in schema['enum']):
        raise ConfigurationError(f'{path}: 值不在允许范围')
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        raise ConfigurationError(f'{path}: 必须为 {schema["const"]!r}')
    if isinstance(value, dict):
        missing = set(schema.get('required', []))-set(value)
        if missing:
            raise ConfigurationError(f'{path}: 缺少字段 {sorted(missing)}')
        properties = schema.get('properties', {})
        if schema.get('additionalProperties') is False and set(value)-set(properties):
            raise ConfigurationError(f'{path}: 未知字段 {sorted(set(value)-set(properties))}')
        for name, child in properties.items():
            if name in value:
                validate(value[name], child, root, f'{path}.{name}')
    elif isinstance(value, list):
        if len(value) < schema.get('minItems', 0):
            raise ConfigurationError(f'{path}: 项目数不足')
        if schema.get('uniqueItems') and len({json.dumps(v,sort_keys=True,ensure_ascii=False) for v in value}) != len(value):
            raise ConfigurationError(f'{path}: 不允许重复项')
        for index, item in enumerate(value):
            if 'items' in schema:
                validate(item, schema['items'], root, f'{path}[{index}]')
    elif isinstance(value, str):
        if len(value.strip()) < schema.get('minLength', 0):
            raise ConfigurationError(f'{path}: 文本不能为空白')
        if 'pattern' in schema and not re.fullmatch(schema['pattern'], value):
            raise ConfigurationError(f'{path}: 格式不符合要求')
    elif type(value) is int and 'minimum' in schema and value < schema['minimum']:
        raise ConfigurationError(f'{path}: 小于最小值 {schema["minimum"]}')
