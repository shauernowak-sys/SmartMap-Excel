"""案例审核范围只约束何时可执行，不参与凭证或行号选择。"""


def applies_to(item, *, period=None, source_sha256=None):
    scope = item.get('applicability')
    return scope is None or (scope['period'] == period and scope['source_sha256'] == source_sha256)
