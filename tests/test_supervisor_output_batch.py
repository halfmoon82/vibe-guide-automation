import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.cli import run_cli


class SupervisorOutputBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.vibe').mkdir()
        self.payload = self.root / 'batch.json'

    def output(self, items):
        self.payload.write_text(json.dumps(items, ensure_ascii=False))
        return run_cli(['supervisor-output', '--run-id', 'run-1', '--payload', str(self.payload), '--json'], self.root)

    def test_batch_is_globally_bounded_and_full_results_are_recoverable(self):
        items = [{'name': 'read-'+str(i), 'output': {'stdout': '源码'*5000, 'exit_code': 0}} for i in range(8)]
        result = self.output(items)
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertLessEqual(len(json.dumps(result.payload, ensure_ascii=False).encode()), 8192)
        ref = result.payload['output_evidence']
        raw = Path(ref['path']).read_bytes()
        self.assertEqual(json.loads(raw), items)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), ref['sha256'])

    def test_repeat_only_returns_reference_and_changed_output_is_new(self):
        items = [{'name': 'read', 'output': {'stdout': 'same', 'exit_code': 0}}]
        first = self.output(items)
        second = self.output(items)
        self.assertFalse(first.payload['repeated'])
        self.assertTrue(second.payload['repeated'])
        self.assertNotIn('results', second.payload)
        self.assertEqual(first.payload['output_evidence'], second.payload['output_evidence'])
        items[0]['output']['exit_code'] = 3
        self.assertFalse(self.output(items).payload['repeated'])

    def test_native_failed_and_unknown_are_visible_without_full_messages(self):
        items = [{'name':'wait', 'output': {'polls': [{'cursor':'cursor:9', 'thread':{'id':'t','status':{'type':'idle'}}, 'latestTurn':{'status':'failed','error':{'message':'upstream invalid_argument'}}, 'latestAssistantMessage':{'text':'long'*5000}}]}}, {'name':'scan','output':{'status':'unknown','reason':'timeout'}}]
        result = self.output(items)
        self.assertEqual(result.exit_code, 0, result.payload)
        text = json.dumps(result.payload)
        self.assertIn('failed', text)
        self.assertIn('unknown', text)
        self.assertIn('cursor:9', text)
        self.assertIn('invalid_argument', text)
        self.assertNotIn('long'*100, text)

    def test_symlink_output_directory_cannot_write_outside_run(self):
        directory = self.root / '.vibe/runs/run-1'
        directory.mkdir(parents=True)
        outside = self.root / 'outside'
        outside.mkdir()
        (directory / 'tool-output').symlink_to(outside, target_is_directory=True)
        result = self.output([{'name':'read','output':'x'}])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_many_large_names_and_errors_cannot_exceed_batch_budget(self):
        result = self.output([{'name':'名'*500, 'output':{'status':'failed','error':'错'*5000}} for _ in range(50)])
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertLessEqual(len(json.dumps(result.payload,ensure_ascii=False).encode()),8192)
        self.assertTrue(result.payload['details_omitted'])
        self.assertIn('failed', json.dumps(result.payload))

    def test_mcp_content_envelope_preserves_native_failure(self):
        native = {'content': [{'type': 'text', 'text': json.dumps({'polls': [{'cursor': 'c:2', 'thread': {'id': 't'}, 'latestTurn': {'status': 'failed', 'error': {'message': 'bad model'}}}]})}], 'isError': False}
        result = self.output([{'name': 'wait_threads', 'output': native}])
        self.assertIn('failed', json.dumps(result.payload))
        self.assertIn('bad model', json.dumps(result.payload))

    def test_nested_mcp_error_is_retained(self):
        result = self.output([{'name':'native','output':{'content':[{'type':'text','text':'model validation failed'}],'isError':True}}])
        self.assertIn('model validation failed',json.dumps(result.payload))

    def test_shell_excerpt_is_useful_but_bounded(self):
        result=self.output([{'name':'source','output':{'output':'def selected_function():\n'+('body'*5000),'exit_code':0}}])
        excerpt=result.payload['results'][0]['output']
        self.assertIn('def selected_function()',excerpt)
        self.assertLessEqual(len(excerpt),200)

    def test_malformed_native_poll_is_unknown_instead_of_crashing(self):
        result=self.output([{'name':'wait','output':{'polls':[{'thread':'bad','latestTurn':None},'bad']}}])
        self.assertEqual(result.exit_code,0,result.payload)
        self.assertIn('unknown',json.dumps(result.payload))

    def test_overflow_keeps_failure_unknown_and_poll_error_before_ordinary_states(self):
        items=[{'name':'n'*200,'output':{'status':'active%02d'%i,'stdout':'body'*50}} for i in range(30)]
        items += [{'name':'unknown','output':{'status':'unknown','reason':'timeout'}}, {'name':'mcp','output':{'content':[{'type':'text','text':'provider rejected'}],'isError':True}}, {'name':'poll','output':{'polls':[{'latestTurn':{'status':'completed','error':{'message':'native failure'}},'thread':{'id':'t'}}]}}]
        result=self.output(items)
        text=json.dumps(result.payload)
        for expected in ('unknown','timeout','error','provider rejected','native failure'):
            self.assertIn(expected,text)
        self.assertLessEqual(len(json.dumps(result.payload,ensure_ascii=False).encode()),8192)

    def test_overflow_unknown_timeout_retains_reason(self):
        items=[{'name':'n'*200,'output':{'status':'active%02d'%i,'stdout':'body'*50}} for i in range(30)]
        items.append({'name':'wait','output':{'status':'unknown_timeout','reason':'provider timed out'}})
        result=self.output(items)
        self.assertIn('provider timed out',json.dumps(result.payload))

    def test_public_stdout_including_newline_is_within_budget(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from vibe_guide.cli import main
        items=[{'name':'n'*200,'output':{'status':'active','stdout':'x'*200}} for _ in range(18)]
        items[-1]['name']='n'
        # Exercise the real serialization boundary, including print newline.
        for length in range(140,160):
            items[-1]['output']['stdout']='x'*length
            self.payload.write_text(json.dumps(items))
            stream=io.StringIO()
            with patch('vibe_guide.cli.Path.cwd',return_value=self.root),redirect_stdout(stream):
                code=main(['supervisor-output','--run-id','run-1','--payload',str(self.payload),'--json'])
            self.assertEqual(code,0)
            self.assertLessEqual(len(stream.getvalue().encode()),8192)
