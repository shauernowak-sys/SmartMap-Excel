"""把已核验的证据整理成财务待审核事项，不批准或应用任何决定。"""
import copy
from collections import Counter, defaultdict

from template_inspection import code


def build_packet(config, template, exceptions, clarification=None):
    items = []

    def add(identity, kind, title, proposal, evidence, **extra):
        items.append({'id': identity, 'kind': kind, 'title': title, 'status': 'pending',
                      'proposal': copy.deepcopy(proposal), 'evidence': evidence,
                      'automatically_applied': False, **copy.deepcopy(extra)})

    accounts = defaultdict(list)
    for i, record in enumerate(template['accounts']):
        accounts[code(record['values']['科目编码'])].append((i, record))
    for i, mapping in enumerate(config['mappings']):
        if mapping['status'] != 'candidate':
            continue
        target = accounts.get(mapping['target_code'], [])
        evidence = [{'input': 'config', 'pointer': f'/mappings/{i}'}]
        evidence += [{'input': 'template', 'pointer': f'/accounts/{j}'} for j, _ in target]
        related = [issue['issue_id'] for issue in exceptions['issues']
                   if issue.get('locator', {}).get('external_code') == mapping['source_code']]
        conflict = len(target) != 1 or target[0][1]['values']['科目名称'] != mapping['target_name']
        add('review-' + mapping['id'], 'account_mapping',
            f"外部{mapping['source_code']} → 内部{mapping['target_code']}（{mapping['target_name']}）",
            {k: mapping[k] for k in ['source_code', 'target_code', 'target_name']}, evidence,
            config_item_id=mapping['id'], template_reference_conflict=conflict, issue_ids=related,
            historical_evidence=copy.deepcopy(mapping['evidence']))
    for i, rule in enumerate(config['rules']):
        if rule['status'] == 'candidate':
            add('review-' + rule['id'], 'business_rule', f"业务规则：{rule['kind']} / {rule['policy']}",
                {k: rule[k] for k in ['kind', 'policy', 'parameters']},
                [{'input': 'config', 'pointer': f'/rules/{i}'}], config_item_id=rule['id'],
                historical_evidence=copy.deepcopy(rule['evidence']),
                issue_ids=[e['issue_id'] for e in exceptions['issues'] if rule['id'] in e.get('rule_ids', [])])
    # 未知科目按事实归并，不把父级未评估记录误当新增映射请求。
    for i, issue in enumerate(exceptions['issues']):
        if issue['code'] == 'UNMAPPED_ACCOUNT':
            external = issue['locator']['external_code']
            add('review-unknown-account-' + external.replace('.', '-'), 'unknown_account',
                f'未知外部科目{external}需要何种内部映射', None,
                [{'input': 'exceptions', 'pointer': f'/issues/{i}'}],
                source_record_ids=issue['source_record_ids'], affected_record_ids=issue['affected_record_ids'],
                issue_ids=[issue['issue_id']])
    cross = template['crosscheck']
    for i, conflict in enumerate(cross['duplicate_account_codes']):
        add('review-account-conflict-' + conflict['code'], 'account_dictionary_conflict',
            '目标科目编码' + conflict['code'] + '存在重复备查记录', None,
            [{'input': 'template', 'pointer': f'/crosscheck/duplicate_account_codes/{i}'}],
            reference_rows=conflict['rows'])
    if clarification:
        fact=clarification['account_clarification']
        for item in items:
            if (item['kind']=='account_dictionary_conflict' and item['id']=='review-account-conflict-'+fact['account_code']) or (
                item['kind']=='account_mapping' and item['proposal']['target_code']==fact['account_code']):
                item['known_current_meaning']=fact['user_reply']
                item['current_meaning_confirmation_repeated']=False
                item['evidence'].append({'input':'clarification','pointer':'/account_clarification'})
                if item['kind']=='account_dictionary_conflict':
                    item['title']+='（当前含义已由用户说明；待核对最新字典）'
    gaps = defaultdict(list)
    for i, link in enumerate(cross['auxiliary_links']):
        if len(link['reference_rows']) != 1:
            gaps[(link['category'], link['value'])].append((i, link))
    for number, ((category, value), links) in enumerate(sorted(gaps.items()), 1):
        add(f'review-auxiliary-gap-{number:03d}', 'auxiliary_dictionary_gap',
            f'{category}“{value}”的当前系统档案及来源', None,
            [{'input': 'template', 'pointer': f'/crosscheck/auxiliary_links/{i}'} for i, _ in links],
            category=category, observed_value=value, occurrence_count=len(links),
            data_cells=[link['data_cell'] for _, link in links])
    auxiliary_accounts = defaultdict(list)
    for i, account in enumerate(template['accounts']):
        if account['values']['核算类别']:
            auxiliary_accounts[code(account['values']['科目编码'])].append((i, account))
    for target, records in auxiliary_accounts.items():
        categories = sorted({record['values']['核算类别'] for _, record in records})
        add('review-account-aux-' + target, 'account_auxiliary_requirement',
            f"内部科目{target}是否启用{' / '.join(categories)}核算，以及可靠取值来源",
            {'target_code': target, 'observed_categories': categories},
            [{'input': 'template', 'pointer': f'/accounts/{i}'} for i, _ in records],
            conflicting_category_observations=len(categories) > 1)
    questions = [
        ('current-environment', '当前系统、账套、官方空白模板与字典版本，以及历史文件是否导入成功'),
        ('voucher-identity', '目标凭证类别及凭证号：沿用外部号还是重新编号'),
        ('sequence-policy', '导出分录序号保持空白还是填写内部连续序号（历史批注为非必填）'),
        ('special-values', '零、负数、空值、数量、币别、附件数和文本长度的适用约束'),
        ('auxiliary-source', '客户、供应商、员工等辅助字段可使用的原始来源与已批准关联'),
        ('parent-posting', '存在父级直接记账时的业务识别与保留规则'),
    ]
    for identity, title in questions:
        add('review-' + identity, 'template_policy', title, None,
            [{'input': 'template', 'pointer': '/sheets/0/comments'}]
            if identity == 'sequence-policy' and template['sheets'][0]['sheet'] == '凭证模板' else
            [{'input': 'template', 'pointer': '/sheet_order'}])
    covered = {identity for item in items for identity in item.get('issue_ids', [])}
    for i, issue in enumerate(exceptions['issues']):
        if issue['issue_id'] not in covered:
            add('review-exception-' + issue['issue_id'], 'conversion_exception', issue['message'], None,
                [{'input': 'exceptions', 'pointer': f'/issues/{i}'}], issue_ids=[issue['issue_id']])
    ids = [item['id'] for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError('审核事项ID重复，不能覆盖或丢失证据')
    return {'schema_version': 'review-packet-1', 'mode': 'financial_review_preparation',
            'executable_configuration': False, 'ready_for_system_import': False,
            'all_decisions_pending': True, 'item_count': len(items),
            'covered_exception_ids': sorted({identity for item in items for identity in item.get('issue_ids', [])}),
            'kind_counts': dict(Counter(item['kind'] for item in items)),
            'existing_confirmed_rule_ids': [r['id'] for r in config['rules'] if r['status'] == 'confirmed'],
            'items': items}


def answer_draft(packet, packet_sha256):
    return {'schema_version': 'review-answers-1', 'packet_sha256': packet_sha256,
            'not_a_runtime_config': True, 'automatically_applied': False,
            'answers': [{'item_id': item['id'], 'decision': 'pending', 'response': None,
                         'reviewed_by': None, 'reviewed_on': None, 'reason': None} for item in packet['items']]}
