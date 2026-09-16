import unittest
from vibe_guide.native_loop import NativeLoop
class NativeAcceptanceTests(unittest.TestCase):
 def test_issue_lifecycle_and_dag_ready(self):
  l=NativeLoop([{'id':'ISSUE-05','depends_on':['ISSUE-07','ISSUE-08']},{'id':'ISSUE-07'},{'id':'ISSUE-08'}])
  l.run_issue('ISSUE-07','dev07','rev07'); l.run_issue('ISSUE-08','dev08','rev08')
  self.assertEqual(l.ready(),['ISSUE-05'])
  l.run_issue('ISSUE-05','dev05','rev05'); self.assertEqual(l.nodes['ISSUE-05']['status'],'accepted')
 def test_review_requires_bindings(self):
  l=NativeLoop([{'id':'A'}]); l.bind_writer('A','dev'); l.developer_done('A')
  with self.assertRaises(ValueError): l.review('A')
if __name__=='__main__': unittest.main()
