"""结转识别不靠固定摘要、凭证号码、金额或目标答案。"""
import copy
import unittest

from test_hierarchy import ROOT, row, report
from smartmap.closing import classify_closing
from smartmap.configuration import ConfigurationError, load_config, validate_config
from smartmap.hierarchy import analyze_hierarchy
from smartmap.mapping_filter import map_and_filter


class ClosingTests(unittest.TestCase):
    def setUp(self):
        self.profile={'account_roles': {'900': 'current_profit', '700': 'income',
                      '800': 'expense'}, 'summary_hints': ['结转损益'], 'reference': 'synthetic'}
        self.config,_=load_config(ROOT/'config/example.json')

    def rows(self):
        values=[row('700.01',100,number=938,summary='收入结转'),
                row('900',credit=100,number=938,summary='收入归集'),
                row('800.01',credit=80,number=938,summary='费用结转'),
                row('900',80,number=938,summary='费用归集')]
        result=report(values,self.config)['rows']
        for r in result:r['hierarchy']={'decision':'retained'}
        return result

    def test_arbitrary_codes_numbers_and_summaries(self):
        result=classify_closing(self.rows(),self.profile)
        self.assertEqual(result['kind'],'closing_structure_verified')
        self.assertEqual(result['current_profit_net_credit_impact'],'20')
        self.assertFalse(result['changes_disposition'])
        self.assertFalse(result['summary_hint'])

    def test_summary_alone_not_evidence(self):
        rows=self.rows()
        for r in rows:r['subject']['code']='100';r['summary']='结转损益'
        result=classify_closing(rows,self.profile)
        self.assertEqual(result['kind'],'possible_closing')
        self.assertIn('CLOSING_PROFIT_ACCOUNT_MISSING',result['reason_codes'])

    def test_no_summary_no_profit_is_ordinary(self):
        rows=self.rows()
        for r in rows:r['subject']['code']='100'
        self.assertEqual(classify_closing(rows,self.profile)['kind'],'ordinary')

    def test_unbalanced_not_verified(self):
        rows=self.rows();rows[0]['debit']['value']='101'
        self.assertIn('CLOSING_UNBALANCED',classify_closing(rows,self.profile)['reason_codes'])

    def test_unknown_account_and_segment_boundary(self):
        rows=self.rows();rows[0]['subject']['code']='7000.01'
        self.assertIn('CLOSING_OTHER_OR_UNKNOWN_ACCOUNTS',classify_closing(rows,self.profile)['reason_codes'])

    def test_parent_excluded_only_with_upstream_decision(self):
        rows=self.rows();parent=copy.deepcopy(rows[0]);parent['record_id']='parent'
        parent['hierarchy']['decision']='excluded';rows.append(parent)
        result=classify_closing(rows,self.profile)
        self.assertEqual(result['kind'],'closing_structure_verified')
        self.assertEqual(result['hierarchy_excluded_record_ids'],['parent'])
        parent['hierarchy']['decision']='needs_review'
        self.assertIn('CLOSING_UPSTREAM_UNRESOLVED',classify_closing(rows,self.profile)['reason_codes'])

    def test_negative_expense_reversal_supported(self):
        rows=self.rows();rows[2]['credit']['value']='-80';rows[3]['debit']['value']='-80'
        self.assertEqual(classify_closing(rows,self.profile)['current_profit_net_credit_impact'],'180')
        self.assertEqual(classify_closing(rows,self.profile)['kind'],'closing_structure_verified')

    def test_invalid_money_never_raises_or_verifies(self):
        rows=self.rows();rows[0]['debit']={'status':'invalid','value':None}
        self.assertIn('CLOSING_INVALID_AMOUNT',classify_closing(rows,self.profile)['reason_codes'])

    def test_both_sides_not_verified(self):
        rows=self.rows();rows[0]['credit']={'status':'number','value':'1'}
        self.assertIn('CLOSING_DIRECTION_OR_ZERO_ENTRY',classify_closing(rows,self.profile)['reason_codes'])

    def test_wrong_direction_not_verified(self):
        rows=self.rows();rows[0]['debit'],rows[0]['credit']=rows[0]['credit'],rows[0]['debit']
        self.assertIn('CLOSING_DIRECTION_OR_ZERO_ENTRY',classify_closing(rows,self.profile)['reason_codes'])

    def test_out_of_scope_and_other_period(self):
        self.profile['applicability']={'period':'202607','source_sha256':'a'*64}
        self.assertEqual(classify_closing(self.rows(),self.profile)['kind'],'not_evaluated')

    def test_disabled_profile(self):
        self.assertEqual(classify_closing(self.rows())['kind'],'not_evaluated')

    def test_role_validation(self):
        self.config['source_profile']['closing_recognition']=self.profile
        validate_config(self.config)
        self.profile['account_roles']['900']='guess'
        with self.assertRaises(ConfigurationError):validate_config(self.config)

    def test_integration_does_not_change_mapping_dispositions(self):
        values=[row('700.01',100),row('900',credit=100)]
        old=map_and_filter(analyze_hierarchy(report(values,self.config),self.config),self.config)
        self.config['source_profile']['closing_recognition']=self.profile
        new=map_and_filter(analyze_hierarchy(report(values,self.config),self.config),self.config)
        self.assertEqual(old['row_counts'],new['row_counts'])
        self.assertEqual(new['filter_groups'][0]['closing_recognition']['kind'],'closing_structure_verified')
        self.assertEqual(new['row_counts']['excluded'],0)

    def scoped_filter(self):
        self.config['source_profile']['closing_recognition']=self.profile
        rule={'id':'confirmed-closing','kind':'voucher_filter','policy':'exclude_voucher','status':'confirmed',
              'parameters':{'match_scope':'all_lines','summary_equals':'结转损益','classification_equals':'closing_structure_verified'},
              'applicability':{'period':'202606','source_sha256':'a'*64},
              'evidence':[{'kind':'user_confirmation','reference':'合成测试专用'}],
              'review':{'reviewed_by':'synthetic','reviewed_on':'2026-10-28','reason':'仅合成测试'}}
        self.config['rules'].append(rule)
        return rule

    def filtered(self,values):
        return map_and_filter(analyze_hierarchy(report(values,self.config),self.config),self.config)

    def test_confirmed_structured_closing_excludes_whole_voucher(self):
        self.scoped_filter()
        r=self.filtered([row('700.01',100,summary='结转损益'),row('900',credit=100,summary='结转损益')])
        self.assertEqual(r['row_counts']['excluded'],2)
        self.assertEqual(r['row_counts']['needs_review'],0)
        self.assertEqual(r['mapping_counts']['unknown'],0)

    def test_confirmed_keyword_without_structure_never_excludes(self):
        self.scoped_filter()
        r=self.filtered([row('700.01',100,summary='结转损益'),row('100',credit=100,summary='结转损益')])
        self.assertEqual(r['row_counts']['excluded'],0)
        self.assertEqual(r['row_counts']['needs_review'],2)

    def test_other_month_filter_does_not_apply(self):
        self.scoped_filter()['applicability']['period']='202607'
        r=self.filtered([row('700.01',100,summary='结转损益'),row('900',credit=100,summary='结转损益')])
        self.assertEqual(r['filter_groups'][0]['evaluations'][-1]['match'],'out_of_scope')
        self.assertEqual(r['row_counts']['excluded'],0)

    def test_other_source_filter_does_not_apply(self):
        self.scoped_filter()['applicability']['source_sha256']='b'*64
        r=self.filtered([row('700.01',100,summary='结转损益'),row('900',credit=100,summary='结转损益')])
        self.assertEqual(r['row_counts']['excluded'],0)

    def test_partial_summary_still_blocks_whole_closing(self):
        self.scoped_filter()
        r=self.filtered([row('700.01',100,summary='结转损益'),row('900',credit=100,summary='其他')])
        self.assertEqual(r['row_counts']['excluded'],0)
        self.assertEqual(r['row_counts']['needs_review'],2)

    def test_classification_only_filter_does_not_block_ordinary(self):
        rule=self.scoped_filter();del rule['parameters']['summary_equals']
        r=self.filtered([row('100',100),row('200',credit=100)])
        self.assertEqual(r['filter_groups'][0]['decision'],'kept')

    def test_structure_rule_requires_profile(self):
        self.scoped_filter();del self.config['source_profile']['closing_recognition']
        with self.assertRaises(ConfigurationError):validate_config(self.config)


if __name__=='__main__':unittest.main()
