import unittest
from vibe_guide.models import Plan
from vibe_guide.planner import build_planning_brief

class Issue06PlanningTests(unittest.TestCase):
 def test_complex_plan_preserves_required_context_and_brief_trace(self):
  plan=Plan('p1',1,'prd.md',['n1'],'draft', complexity_band='complex', nodes=[])
  brief=build_planning_brief(plan, goals=[{'id':'g1','user_scenario':'s','code_evidence':['x.py:f'],'spec_ref':'S','issue_ref':'I','dag_nodes':['n1'],'runtime_acceptance':'ok'}], iteration_context={'iteration':'rev6'}, compatibility_scope={'versions':['4.5']}, agentsmd_acceptance_refs=['AGENTS.md#1'], integration_acceptance_contract={'input':'x'}, unverified_or_excluded=['provider'])
  for key in ('iteration_context','compatibility_scope','agentsmd_acceptance_refs','integration_acceptance_contract','unverified_or_excluded','depends_on','integration_after','parallel_group','ready_nodes','goals'):
   self.assertIn(key, brief)
  self.assertEqual(brief['goals'][0]['id'],'g1')
 def test_complex_missing_context_rejected(self):
  with self.assertRaises(ValueError):
   build_planning_brief(Plan('p1',1,'prd.md',['n1'],'draft', complexity_band='complex'))
