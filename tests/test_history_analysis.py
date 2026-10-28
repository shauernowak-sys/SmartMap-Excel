"""独立分析边界测试，不依赖企业数据，不沿用 V0.1 的解析器。"""
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from analysis_core import ancestor, blank, money, pair_occurrences, subject, voucher


class AmountAnalysisTests(unittest.TestCase):
    def test_ten_is_never_placeholder(self):
        for value in [10,10.0,'10','10.00']:
            with self.subTest(value=value):self.assertEqual(money(value),('number',Decimal(10)))

    def test_zero_is_distinct_from_empty(self):
        self.assertEqual(money(0),('number',Decimal(0)))
        for value in [None,'','  ']:
            with self.subTest(value=value):self.assertEqual(money(value),('blank',None))

    def test_valid_amounts(self):
        for value,expected in [('1,354,582.31','1354582.31'),('-1,200.05','-1200.05'),('+10.01','10.01')]:
            with self.subTest(value=value):self.assertEqual(money(value),('number',Decimal(expected)))

    def test_invalid_and_nonfinite(self):
        for value in ['NaN','Infinity','-Infinity',float('nan'),True,'12,34','￥10','error','=1+1']:
            with self.subTest(value=value):self.assertEqual(money(value),('invalid',None))


class AssociationTests(unittest.TestCase):
    def test_duplicate_occurrences_do_not_disappear(self):
        pairs,missing,extra=pair_occurrences(['a','a'],['a'],lambda x:x)
        self.assertEqual((len(pairs),len(missing),len(extra)),(1,1,0))

    def test_extra_duplicate_is_retained(self):
        pairs,missing,extra=pair_occurrences(['a'],['a','a'],lambda x:x)
        self.assertEqual((len(pairs),len(missing),len(extra)),(1,0,1))

    def test_same_amount_different_business(self):
        items=[(1,'1122.01',Decimal(10)),(2,'1122.01',Decimal(10)),(1,'1122.02',Decimal(10))]
        pairs,missing,extra=pair_occurrences(items,list(reversed(items)),lambda x:x)
        self.assertEqual((len(pairs),missing,extra),(3,[],[]))
        self.assertEqual(pairs,[(0,2),(1,1),(2,0)])

    def test_strict_voucher(self):
        self.assertEqual(voucher('记74'),74)
        self.assertEqual(voucher(10),10)
        for value in ['本期合计','abc10','202606_记10',None,'',True,0]:
            with self.subTest(value=value):self.assertIsNone(voucher(value))

    def test_subject_boundaries(self):
        self.assertEqual(subject('2221.01.06应交税费'),'2221.01.06')
        self.assertIsNone(subject('2221..01'))

    def test_hierarchy_is_segment_based(self):
        self.assertTrue(ancestor('1122','1122.07'))
        self.assertTrue(ancestor('2221.01','2221.01.06'))
        self.assertFalse(ancestor('1122.01','1122.010'))
        self.assertFalse(ancestor('1122','11220'))
        self.assertFalse(ancestor('1122','1122'))

    def test_blank_does_not_include_zero(self):
        self.assertFalse(blank(0))
        self.assertTrue(blank(' '))


if __name__=='__main__':unittest.main()
