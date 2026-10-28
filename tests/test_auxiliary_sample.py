"""本地企业样本验收：生产只读原始文件和配置，事后才读取标准答案。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tools'))
from smartmap.contracts import ConversionRequest
from smartmap.conversion import run_conversion
from evaluate_auxiliary import evaluate

SOURCE=ROOT/'samples/202606_明细账26.7.241.xlsx'
FINAL=ROOT/'samples/凭证导入模板2026.6月.xlsx'
CONFIG=ROOT/'config/local/historical-candidates.r7.json'


@unittest.skipUnless(SOURCE.exists() and FINAL.exists() and CONFIG.exists(),'本地业务样本或获批配置未提供')
class AuxiliarySampleTests(unittest.TestCase):
    def test_all_46_values_match_and_missing_archives_block_production(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            manifest=run_conversion(ConversionRequest(SOURCE,CONFIG,root/'run'))
            result=evaluate(root/'run',FINAL,root/'evaluation')
            self.assertEqual(result['proposals_vs_reference'],{'produced':46,'expected':46,'matched':46,'missing':0,'extra':0})
            self.assertEqual(result['dictionary_status_counts'],{'matched':32,'missing':14})
            self.assertEqual(result['approved_values_vs_reference']['matched'],32)
            self.assertEqual(result['approved_values_vs_reference']['missing'],14)
            self.assertEqual(manifest['row_counts']['included'],0)
            self.assertFalse(manifest['allow_partial']);self.assertFalse(manifest['batch_complete'])
            self.assertEqual(manifest['exit_code'],3)


if __name__=='__main__':unittest.main()
