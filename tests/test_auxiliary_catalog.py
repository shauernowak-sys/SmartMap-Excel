import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).resolve().parent))
from smartmap.auxiliary_catalog import build_catalog
from test_auxiliary import config_for,data,stages_for
from smartmap.auxiliary import analyze_auxiliary


class CatalogTests(unittest.TestCase):
    def accounts(self):return [{'code':'9001','categories':['客类'],'reference':'A1'},{'code':'9002','categories':[],'reference':'A2'}]
    def test_category_mapping_is_external_configuration(self):
        c=build_catalog(self.accounts(),[{'category':'客类','code':'C01','name':'合成','reference':'B1'}],{'客类':'customer'})
        self.assertEqual(c['known_target_codes'],['9001','9002']);self.assertEqual(c['account_requirements'][0]['fields'],['customer'])
    def test_unknown_category_not_silently_omitted(self):
        with self.assertRaises(ValueError):build_catalog(self.accounts(),[],{})
    def test_conflicting_target_requirements_rejected(self):
        with self.assertRaises(ValueError):build_catalog(self.accounts()+[{'code':'9001','categories':[],'reference':'A3'}],[],{'客类':'customer'})
    def test_duplicate_archive_names_preserved_for_ambiguity(self):
        row={'category':'客类','code':'C01','name':'合成','reference':'B1'}
        self.assertEqual(len(build_catalog(self.accounts(),[row,row],{'客类':'customer'})['dictionary']),2)
    def test_no_implicit_splitting_of_category_string(self):
        with self.assertRaises(ValueError):build_catalog([{'code':'9','categories':'客户,部门','reference':'A1'}],[],{})
    def test_empty_archive_code_not_invented(self):
        with self.assertRaises(ValueError):build_catalog(self.accounts(),[{'category':'客类','code':'','name':'合成','reference':'B1'}],{'客类':'customer'})
    def test_missing_target_catalog_is_not_no_requirement(self):
        c=config_for('confirmed');c['auxiliary_profile']['account_requirements']=[];c['auxiliary_profile']['known_target_codes']=['9999']
        r=analyze_auxiliary(stages_for(data(),c)['voucher'],c)
        self.assertFalse(r['rows'][0]['auxiliary_validation']['ready'])
        self.assertIn('AUXILIARY_TARGET_ACCOUNT_NOT_IN_CATALOG',r['rows'][0]['auxiliary_validation']['reason_codes'])
