"""Cross-node regression coverage for native integration closeout."""
import tempfile
import unittest
from pathlib import Path

from vibe_guide.cli import run_cli
from vibe_guide.decisions import DecisionMailbox
from vibe_guide.paths import ProjectPaths
from vibe_guide.monitor import Monitor, classify_provider_failure
from vibe_guide.models import Plan
from vibe_guide.task_registry import TaskBinding, save_task_binding, load_task_binding
from unittest.mock import patch


class NativeIntegrationTests(unittest.TestCase):
    def test_successor_cannot_bypass_registry_scope_or_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary))
            old = TaskBinding(provider='codex', mode='background', issue_id='n1',
                role='developer', task_id='old', run_id='local-acceptance',
                worktree='.worktrees/n1', branch='node/n1', status='stopped', generation=2)
            save_task_binding(paths, old)
            new = TaskBinding(provider='codex', mode='background', issue_id='n1',
                role='developer', task_id='new', run_id='local-acceptance',
                worktree='.worktrees/other', branch='node/other', status='running',
                generation=1, successor_of='old')
            monitor = Monitor(paths, Plan('p', 1, 'prd.md', [], 'draft'), [])
            with self.assertRaises(ValueError):
                monitor._save_task_binding(new)
            self.assertEqual(load_task_binding(paths, 'n1', 'developer', 'local-acceptance').task_id, 'old')

    def test_failed_provider_result_preserves_external_authorization(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner
        from vibe_guide.adapters.task_provider import ProviderUnavailable
        from types import SimpleNamespace
        result = {'status': 'failed', 'credential_required': True, 'reason': 'timeout'}
        runner = object.__new__(ProviderActionRunner)
        runner.store = SimpleNamespace(result=lambda action: result)
        with patch.object(runner, '_action', return_value={'action_id': 'a'}):
            with self.assertRaises(ProviderUnavailable) as raised:
                runner._require_result({}, 'local-acceptance', 'create', {})
        self.assertEqual(classify_provider_failure(raised.exception)['kind'], 'external_decision')

    def test_decision_does_not_hide_independent_worker_delivery(self):
        from vibe_guide.supervisor import _mailbox_state
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary))
            DecisionMailbox(paths).raise_decision(run_id='local-acceptance',
                node_id='n1', question='Pick', options=['A', 'B'])
            with patch('vibe_guide.adapters.task_provider.ProviderActionStore') as store:
                store.return_value.pending.return_value = []
                store.return_value.unpolled_results.return_value = []
                store.return_value.unconsumed_deliveries.return_value = ['n2']
                result = _mailbox_state(paths, 'local-acceptance', {})
            self.assertEqual(result['state'], 'work')
            self.assertTrue(result['decisions'])

    def test_single_line_beat_preserves_pending_decision_branch(self):
        # A decision-only synthetic project can still expose its pending card.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '.project-root').write_text('local test\n')
            record = root / 'usage.json'
            record.write_text('{"token_count": 30000}')
            DecisionMailbox(ProjectPaths(root)).raise_decision(
                run_id='local-acceptance', node_id='n1',
                question='Pick', options=['A', 'B'])
            for command in ('supervisor-beat', 'supervisor-preflight'):
                with self.subTest(command=command):
                    result = run_cli([command, '--run-id', 'local-acceptance', '--accept-decision',
                                      '--session-record', str(record)], root)
                    self.assertEqual(result.payload['state'], 'decision')
                    self.assertEqual(result.exit_code, 0)
                    self.assertIn('decision', result.text)
                    legacy = run_cli([command, '--run-id', 'local-acceptance',
                                      '--session-record', str(record)], root)
                    self.assertEqual(legacy.payload['state'], 'work')
                    self.assertIs(legacy.payload['decision_only'], True)
                    self.assertEqual(legacy.exit_code, 0)

    def test_decision_does_not_hide_independent_pending_request(self):
        self._independent_work_with_decision(pending=[{'issue_id': 'n2'}])

    def test_decision_does_not_hide_independent_ready_node(self):
        self._independent_work_with_decision(pending=[])

    def _independent_work_with_decision(self, pending):
        from types import SimpleNamespace
        from vibe_guide.supervisor import _mailbox_state
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary))
            DecisionMailbox(paths).raise_decision(run_id='local-acceptance',
                node_id='n1', question='Pick', options=['A', 'B'])
            snapshot = SimpleNamespace(nodes={'n1': {'status': 'blocked_design'},
                'n2': {'status': 'planned'}}, ready_set=['n2'])
            with patch('vibe_guide.adapters.task_provider.ProviderActionStore') as store, \
                 patch('vibe_guide.supervisor.load_snapshot', return_value=snapshot):
                store.return_value.pending.return_value = pending
                store.return_value.unpolled_results.return_value = []
                store.return_value.unconsumed_deliveries.return_value = []
                result = _mailbox_state(paths, 'local-acceptance', {})
            self.assertEqual(result['state'], 'work')
            self.assertTrue(result['decisions'])
