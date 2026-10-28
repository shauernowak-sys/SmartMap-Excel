"""辅助事后验收要区分候选正确和正式已填，且保留重复次数。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'));sys.path.insert(0,str(Path(__file__).resolve().parent))
from evaluate_auxiliary import evaluate
from template_inspection import DATA_HEADERS
from test_evaluation import item
from xlsx_fixtures import write_xlsx


class AuxiliaryEvaluationTests(unittest.TestCase):
    def fixture(self,root,*,expected='合成客户',proposed='合成客户',duplicates=1,month='06'):
        run=root/'run';run.mkdir();final=root/'final.xlsx'
        row=[None,f'2026-{month}-30','记账凭证',1,None,'合成业务','990001','科目',10,None]
        if expected:
            row += [None,None,None,expected]
        write_xlsx(final,[('凭证模板',[DATA_HEADERS]+[row]*duplicates)])
        entry=item(target_subject_code='990001');entry.update(auxiliary={},auxiliary_candidates={})
        if proposed:
            entry['auxiliary_candidates']={'customer':{'value':proposed,'dictionary_status':'missing','dictionary_code':None,'approved':False}}
        for filename,value in [('run_manifest.json',{'run_id':'synthetic','period':'202606','row_counts':{'included':0}}),
                               ('source_rows.json',{'rows':[]}),('diagnostic_candidates.json',{'rows':[entry]})]:
            (run/filename).write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
        return run,final,root/'out'

    def test_ragged_excel_rows_have_no_index_error(self):
        with tempfile.TemporaryDirectory() as d:
            args=self.fixture(Path(d),expected='',proposed='')
            with patch('evaluate_auxiliary.audit_run',return_value={'usable_for_evaluation':True}):result=evaluate(*args)
            self.assertEqual(result['proposals_vs_reference']['expected'],0)

    def test_candidate_correct_is_not_formal_value(self):
        with tempfile.TemporaryDirectory() as d:
            args=self.fixture(Path(d))
            with patch('evaluate_auxiliary.audit_run',return_value={'usable_for_evaluation':True}):result=evaluate(*args)
            self.assertEqual(result['proposals_vs_reference']['matched'],1)
            self.assertEqual(result['approved_values_vs_reference']['missing'],1)

    def test_duplicate_occurrences_not_set_compared(self):
        with tempfile.TemporaryDirectory() as d:
            args=self.fixture(Path(d),duplicates=2)
            with patch('evaluate_auxiliary.audit_run',return_value={'usable_for_evaluation':True}):result=evaluate(*args)
            self.assertEqual((result['proposals_vs_reference']['matched'],result['proposals_vs_reference']['missing']),(1,1))

    def test_wrong_value_counts_missing_and_extra(self):
        with tempfile.TemporaryDirectory() as d:
            args=self.fixture(Path(d),proposed='另一客户')
            with patch('evaluate_auxiliary.audit_run',return_value={'usable_for_evaluation':True}):result=evaluate(*args)
            self.assertEqual((result['proposals_vs_reference']['matched'],result['proposals_vs_reference']['missing'],result['proposals_vs_reference']['extra']),(0,1,1))

    def test_wrong_reference_period_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            args=self.fixture(Path(d),month='07')
            with patch('evaluate_auxiliary.audit_run',return_value={'usable_for_evaluation':True}),self.assertRaises(ValueError):evaluate(*args)

    def test_existing_output_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            args=list(self.fixture(Path(d)));args[2].mkdir()
            with self.assertRaises(ValueError):evaluate(*args)


if __name__=='__main__':unittest.main()
