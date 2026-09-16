import hashlib, json, tempfile
from pathlib import Path
import unittest
from vibe_guide.native_loop import materialize_authorized_plan, NativeLoop, validate_remote_git_permissions

class NativeLoopTests(unittest.TestCase):
    def _fixture(self, root):
        src = root/'source.json'; src.write_text(json.dumps({'revision':6,'plan_id':'p','nodes':[{'id':'A','depends_on':[]},{'id':'B','depends_on':['A']}]}, ensure_ascii=False), encoding='utf8')
        digest=hashlib.sha256(src.read_bytes()).hexdigest()
        return src,digest
    def test_materializes_bound_plan_and_nodes_and_rejects_sha_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); src,digest=self._fixture(root)
            card={'plan_id':'p','plan_revision':6,'project_root':str(root),'evidence_refs':{'source.json':digest},'remote_git_actions':'deny','permissions':{}}
            out=materialize_authorized_plan(root, card, {'source.json':src})
            self.assertEqual(json.loads((out/'plan.json').read_text())['plan_id'],'p')
            self.assertEqual([x['id'] for x in json.loads((out/'nodes.json').read_text())],['A','B'])
            with self.assertRaisesRegex(ValueError,'SHA'):
                materialize_authorized_plan(root, {**card,'evidence_refs':{'source.json':'0'*64}}, {'source.json':src})
    def test_permission_switch_is_consistent_and_excludes_deploy(self):
        validate_remote_git_permissions({'remote_git_actions':'deny','permissions':{'commit':False,'push':False,'create_pr':False,'create_mr':False,'merge':False}})
        with self.assertRaises(ValueError):
            validate_remote_git_permissions({'remote_git_actions':'deny','permissions':{'commit':True}})
        with self.assertRaises(ValueError):
            validate_remote_git_permissions({'remote_git_actions':'allow','permissions':{'commit':True,'deploy':True}})
    def test_native_loop_ready_and_unique_writer(self):
        loop=NativeLoop([{'id':'A','depends_on':[]},{'id':'B','depends_on':['A']}])
        self.assertEqual(loop.ready(),['A']); loop.bind_writer('A','dev-A'); self.assertEqual(loop.writer('A'),'dev-A')
        with self.assertRaises(ValueError): loop.bind_writer('A','dev-A-2')
        loop.developer_done('A'); loop.bind_reviewer('A','rev-A'); loop.review('A'); self.assertEqual(loop.ready(),['B'])

    def test_real_rev6_markdown_and_workflow_materialize(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); wf=root/'workflow.json'; wf.write_text(json.dumps({'revision':6,'dag':{'depends_on':{'ISSUE-06':['ISSUE-02'],'ISSUE-07':['ISSUE-06']}}}),encoding='utf8')
            md=root/'prd.md'; md.write_text('# prose evidence\n',encoding='utf8')
            card={'plan_id':'p','plan_revision':6,'project_root':str(root),'evidence_refs':{'prd.md':hashlib.sha256(md.read_bytes()).hexdigest(),'workflow.json':hashlib.sha256(wf.read_bytes()).hexdigest()},'remote_git_actions':'deny','permissions':{}}
            out=materialize_authorized_plan(root,card,{'prd.md':md,'workflow.json':wf})
            self.assertEqual(json.loads((out/'plan.json').read_text())['plan_id'],'p')
            self.assertEqual([n['id'] for n in json.loads((out/'nodes.json').read_text())],['ISSUE-02','ISSUE-06','ISSUE-07'])

if __name__=='__main__': unittest.main()
