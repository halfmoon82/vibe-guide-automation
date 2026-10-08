"""Regression coverage for bounded supervisor handoffs and CLI output."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from vibe_guide.paths import ProjectPaths
from vibe_guide.supervisor import register_supervisor_address, supervisor_preflight, supervisor_handoff


class ContextBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = ProjectPaths(self.root)
        self.record = self.root / 'session.json'
        self.record.write_text(json.dumps({'context_tokens': 54339}))
        register_supervisor_address(self.paths, 'run-1', {'provider': 'codex', 'session_id': 'shift', 'host': 'local'}, session_record=self.record)

    def preflight(self, tokens, **kwargs):
        self.record.write_text(json.dumps({'context_tokens': tokens}))
        with patch('vibe_guide.supervisor._mailbox_state', return_value={'state': 'work'}):
            return supervisor_preflight(self.paths, 'run-1', self.record, **kwargs)

    def test_observed_context_prepares_without_premature_rotation(self):
        result = self.preflight(130545)
        self.assertEqual(result['state'], 'work')
        self.assertTrue(result['prepare_handoff'])

    def test_hard_cap_cannot_be_lifted_by_growth_setting(self):
        result = self.preflight(150000, token_threshold=300000)
        self.assertEqual(result['state'], 'rotate')

    def test_growth_is_auxiliary_to_absolute_warning(self):
        result = self.preflight(99999, token_threshold=1)
        self.assertEqual(result['state'], 'work')
        self.assertFalse(result['prepare_handoff'])
        self.assertEqual(self.preflight(134339)['state'], 'rotate')

    def test_compact_handoff_contains_bindings_and_verifiable_evidence(self):
        from vibe_guide.state import run_dir
        directory = run_dir(self.paths, 'run-1', create=True)
        snapshot = SimpleNamespace(plan_id='plan-1', run_id='run-1', status='blocked_unknown', authorization_digest='a'*64, node_contract_digest='b'*64, nodes={'n': {'status': 'rework', 'branch': 'node/n', 'worktree': '.worktrees/n', 'reason': 'needs review', 'evidence': ['report'*100000]}})
        (directory/'state.json').write_text('{"status":"blocked_unknown"}')
        (directory/'tasks.json').write_text(json.dumps({'bindings': [{'issue_id':'n', 'role':'developer', 'task_id':'task', 'threadId':'thread', 'hostId':'local', 'cursor':'opaque-cursor', 'generation':1, 'worktree':'.worktrees/n', 'branch':'node/n'}]}))
        with patch('vibe_guide.supervisor.load_snapshot', return_value=snapshot):
            payload, _ = supervisor_handoff(self.paths, 'run-1')
        artifact = Path(payload['handoff_summary']['path'])
        raw = artifact.read_bytes()
        self.assertLessEqual(len(raw), 24576)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), payload['handoff_summary']['sha256'])
        data = json.loads(raw)
        self.assertEqual(data['tasks'][0]['cursor'], 'opaque-cursor')
        self.assertEqual(data['nodes']['n']['worktree'], '.worktrees/n')
        self.assertEqual(data['authorization_digest'], 'a'*64)
        self.assertNotIn('reportreport', raw.decode())
        for evidence in data['evidence']:
            self.assertEqual(hashlib.sha256(Path(evidence['path']).read_bytes()).hexdigest(), evidence['sha256'])

    def test_large_unicode_console_output_is_bounded_and_lossless_on_disk(self):
        from vibe_guide.supervisor import bounded_supervisor_output
        content = {'state': 'work', 'run_id': 'run-1', 'reviewer': '审查'*50000}
        rendered = json.dumps(content, ensure_ascii=False)
        short = bounded_supervisor_output(self.paths, 'run-1', content, rendered)
        self.assertLessEqual(len(short.encode()), 8192)
        ref = json.loads(short)['output_evidence']
        self.assertEqual(Path(ref['path']).read_text(), rendered)
        self.assertEqual(hashlib.sha256(Path(ref['path']).read_bytes()).hexdigest(), ref['sha256'])

    def test_runtime_heartbeat_uses_the_verified_command_prefix(self):
        from vibe_guide.supervisor import heartbeat_prompt
        prefix = "python3 /runtime/bridge.py cli"
        prompt = heartbeat_prompt('plan-1', 'run-1', command_prefix=prefix)
        self.assertIn(prefix + " supervisor-preflight", prompt)
        self.assertIn(prefix + " supervisor-handoff", prompt)
        self.assertIn(prefix + " resume", prompt)
        self.assertNotIn("  vibe supervisor-preflight", prompt)

    def test_cli_caps_resume_output_without_changing_exit_code(self):
        from vibe_guide.cli import main, CLIResult
        import io
        from contextlib import redirect_stdout
        payload = {'command':'resume', 'run_id':'run-1', 'status':'blocked_unknown', 'nodes':['x'*20000]}
        result = CLIResult(3, payload, 'x'*20000, True)
        (self.root / '.vibe').mkdir(exist_ok=True)
        (self.root / 'AGENTS.md').write_text('test project marker')
        subdirectory = self.root / 'nested'
        subdirectory.mkdir()
        output = io.StringIO()
        with patch('vibe_guide.cli.run_cli', return_value=result), patch('vibe_guide.cli.Path.cwd', return_value=subdirectory), redirect_stdout(output):
            code = main(['resume', '--run-id', 'run-1', '--json'])
        self.assertEqual(code, 3)
        short = json.loads(output.getvalue())
        self.assertEqual(short['status'], 'blocked_unknown')
        self.assertLessEqual(len(output.getvalue().encode()),8192)
        self.assertIn('output_evidence',short)
        self.assertTrue(Path(short['output_evidence']['path']).is_relative_to(self.root.resolve() / '.vibe'))
        self.assertFalse((subdirectory / '.vibe').exists())


class HandoffRecoveryTests(unittest.TestCase):
    def start(self):
        from tests.support_v45_authorize import publish_complex_probe
        from vibe_guide.cli import run_cli
        root = publish_complex_probe(self)
        run_cli(['authorize', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE'],root)
        result = run_cli(['monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE'],root)
        return root, result.payload['run_id']

    def test_missing_or_corrupt_primary_preserves_backup_recovery(self):
        from vibe_guide.cli import run_cli
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                root, run_id = self.start()
                directory = root / '.vibe/runs' / run_id
                primary = directory / 'state.json'
                (directory / 'state.previous.json').write_bytes(primary.read_bytes())
                if corrupt:
                    primary.write_text('{invalid')
                else:
                    primary.unlink()
                result = run_cli(['supervisor-handoff', '--run-id', run_id, '--json'],root)
                self.assertEqual(result.exit_code,0,result.payload)
                summary = json.loads(Path(result.payload['handoff_summary']['path']).read_text())
                self.assertTrue(any(Path(e['path']).name == 'state.previous.json' for e in summary['evidence']))

    def test_bridge_prefix_survives_handoff_to_successor(self):
        from vibe_guide.cli import run_cli
        root, run_id = self.start()
        prefix = "python3 '/runtime/bridge with spaces.py' cli"
        result = run_cli(['supervisor-handoff', '--run-id', run_id, '--json'],root, supervisor_command_prefix=prefix)
        self.assertEqual(result.exit_code,0,result.payload)
        self.assertIn(prefix+' supervisor-preflight', result.payload['heartbeat_prompt'])
        summary = json.loads(Path(result.payload['handoff_summary']['path']).read_text())
        self.assertTrue(summary['next_command'].startswith(prefix))
        self.assertTrue(summary['resume_command'].startswith(prefix))

    def test_truncated_handoff_keeps_compact_entry(self):
        from vibe_guide.supervisor import bounded_supervisor_output
        with tempfile.TemporaryDirectory() as root:
            payload={'run_id':'run-1','handoff_summary':{'path':'/small/summary.json','sha256':'a'*64},'big':'x'*20000}
            output=json.loads(bounded_supervisor_output(ProjectPaths(Path(root)),'run-1',payload,json.dumps(payload)))
            self.assertEqual(output['handoff_summary'],payload['handoff_summary'])

    def test_handoff_preparation_race_cannot_clear_hard_stop(self):
        from vibe_guide.cli import run_cli
        with tempfile.TemporaryDirectory() as directory:
            record=Path(directory)/'session.json'
            record.write_text(json.dumps({'context_tokens':150000}))
            with patch('vibe_guide.cli.supervisor_handoff',side_effect=ValueError('handoff source changed; retry at next safe point')):
                result=run_cli(['supervisor-preflight','--run-id','run-1','--session-record',str(record),'--json'],Path(directory))
            self.assertEqual(result.payload['state'],'rotate')
            self.assertEqual(result.payload['handoff_status'],'unknown')
            self.assertNotEqual(result.exit_code,0)
