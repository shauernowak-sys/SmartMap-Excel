"""T02 审计测试；含本地样本集成检查，缺少企业样本时明确跳过。"""
import sys
import unittest
import zipfile
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from audit_v01 import ROOT, load_legacy, read_rows


class LegacyCharacterization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.legacy = load_legacy(next((ROOT/'verifier').rglob('verify.py')))

    def test_real_ten_is_numeric(self):
        self.assertEqual(self.legacy['amount']('10'), Decimal('10'))

    def test_invalid_amount_currently_collapses_to_blank(self):
        # 记录缺陷，不表示允许 V0.2 继续吞掉非法值。
        self.assertIsNone(self.legacy['amount']('错误金额'))
        self.assertIsNone(self.legacy['amount'](''))

    def test_voucher_parser_is_overly_permissive(self):
        # 证明任意包含数字的字符串也会被接收。
        self.assertEqual(self.legacy['voucher']('not-a-voucher-10'), 10)

    def test_decimal_thousands(self):
        self.assertEqual(self.legacy['amount']('1,354,582.31'), Decimal('1354582.31'))


@unittest.skipUnless((ROOT/'samples/202606_明细账26.7.241.xlsx').exists(), '本地企业样本未提供')
class LocalReaderIntegration(unittest.TestCase):
    def test_shared_string_index_is_not_amount(self):
        with zipfile.ZipFile(ROOT/'samples/202606_明细账26.7.241.xlsx') as archive:
            strings = ET.fromstring(archive.read('xl/sharedStrings.xml'))
        self.assertEqual(''.join(strings[10].itertext()), '')

    def test_padding_and_blank_voucher(self):
        rows = read_rows(ROOT/'samples/202606_明细账26.7.241.xlsx',0,'A1:I750')
        self.assertEqual(len(rows),750)
        self.assertTrue(all(len(row)==9 for row in rows))
        self.assertEqual(rows[2][3],'')
        self.assertTrue(all(value is None for value in rows[-1]))


if __name__ == '__main__':
    unittest.main()
