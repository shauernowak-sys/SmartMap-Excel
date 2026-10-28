"""新三阶段复核器的独立口径边界，不改变企业文件。"""
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from recheck_three_stages import COMMON, core_key, external_key, payload, record
from analysis_core import pair_occurrences


class RecheckTests(unittest.TestCase):
    def test_numeric_code_and_text_code_same(self):
        row=[None]*25;row[4]=61;row[3]='1221.05 原始名称';row[7]=122104;row[8]='住房公积金 （个人）';row[10]=1600
        r=record(row,152,'人工','manual')
        self.assertEqual((r['voucher'],r['code']),(61,'1221.05'))
        self.assertEqual(payload(row,'manual')['target_code'],'122104')

    def test_invalid_money_not_silently_dropped(self):
        row=[None,None,'1221.05', '记61','合成','bad',None]
        with self.assertRaises(ValueError):record(row,101,'原始','raw')

    def test_actual_amount_ten_preserved(self):
        r=record([None,None,'1002','记10','合成',10,None],3,'原始','raw')
        self.assertEqual(str(r['debit'][1]),'10')

    def test_repeated_external_rows_consumed_once(self):
        r=record([None,None,'1002','记1','合成',10,None],3,'原始','raw')
        pairs,missing,extra=pair_occurrences([r,r],[r],external_key)
        self.assertEqual((len(pairs),len(missing),len(extra)),(1,1,0))

    def test_same_amount_other_voucher_distinct(self):
        a=record([None,None,'1002','记1','合成',10,None],3,'原始','raw')
        b=record([None,None,'1002','记2','合成',10,None],4,'原始','raw')
        self.assertNotEqual(external_key(a),external_key(b))

    def test_twenty_one_non_date_fields(self):
        self.assertEqual(len([k for k in COMMON if k!='date']),21)

    def test_auxiliary_customer_value_retained(self):
        row=[None]*25;row[14]='合成客户'
        self.assertEqual(payload(row,'manual')['customer'],'合成客户')

    def test_sequence_empty_not_fabricated(self):
        self.assertEqual(payload([None]*25,'manual')['sequence'],'')


if __name__=='__main__':unittest.main()
