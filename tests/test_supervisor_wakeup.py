"""A host turn survives compaction without restarting heartbeat admission."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import RunSnapshot
from vibe_guide.supervisor import register_supervisor_address


class WakeupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = ProjectPaths(self.root)
        self.record = self.root / 'rollout.jsonl'
        self.snapshot = RunSnapshot('run-1', 'plan-1', 1, 'running',
            {'n1': {'status': 'review'}}, {},
            authorization_digest='a' * 64, node_contract_digest='b' * 64)
        directory = self.paths.vibe / 'runs/run-1'
        directory.mkdir(parents=True)
        (directory / 'state.json').write_text('{}')
        (self.paths.vibe / 'state.json').write_text('{}')
        self.addCleanup(patch.stopall)
        patch('vibe_guide.supervisor.load_snapshot', return_value=self.snapshot).start()
        patch('vibe_guide.adapters.task_provider.require_entry').start()
        self.append('session_meta', {'id': 'shift-1'})
        self.turn('turn-1')
        self.usage(30000)
        register_supervisor_address(self.paths, 'run-1',
            {'provider': 'codex', 'session_id': 'shift-1', 'host': 'local'},
            session_record=self.record)

    def append(self, kind, payload):
        with self.record.open('a') as stream:
            stream.write(json.dumps({'type': kind, 'payload': payload}) + '\n')

    def turn(self, identity):
        self.append('event_msg', {'type': 'task_started', 'turn_id': identity})
        self.append('turn_context', {'turn_id': identity})

    def usage(self, tokens):
        self.append('event_msg', {'type': 'token_count', 'info': {
            'last_token_usage': {'input_tokens': tokens}}})

    def preflight(self, *extra):
        return run_cli(['supervisor-preflight', '--run-id', 'run-1',
            '--session-record', str(self.record), '--json', *extra], self.root).payload

    def test_compaction_and_process_restart_reuse_completed_preflight(self):
        first = self.preflight()
        self.append('event_msg', {'type': 'context_compacted'})
        self.append('turn_context', {'turn_id': 'turn-1'})
        self.usage(31000)
        second = self.preflight()
        self.assertEqual(first['state'], 'work')
        self.assertFalse(first.get('wakeup', {}).get('reused', True))
        self.assertTrue(second.get('wakeup', {}).get('reused', False))
        self.assertEqual(second['wakeup']['phase'], 'service_mailbox')
        self.assertEqual(second['tokens'], 31000)
        # A separate CLI invocation has no in-memory checkpoint to reuse.
        self.assertTrue(self.preflight()['wakeup']['reused'])

    def test_new_turn_performs_a_new_preflight(self):
        self.preflight()
        self.turn('turn-2')
        result = self.preflight()
        self.assertFalse(result.get('wakeup', {}).get('reused', True))
        self.assertEqual(result['wakeup']['turn_id'], 'turn-2')

    def test_missing_or_wrong_phase_is_recovered_without_an_exception(self):
        path = self.paths.vibe / 'runs/run-1/supervisor-wakeup.json'
        for phase in (None, 'deploy'):
            with self.subTest(phase=phase):
                self.preflight()
                receipt = json.loads(path.read_text())
                if phase is None:
                    receipt.pop('phase')
                else:
                    receipt['phase'] = phase
                path.write_text(json.dumps(receipt))
                result = self.preflight()
                self.assertEqual(result['state'], 'work')
                self.assertFalse(result['wakeup']['reused'])
                self.assertEqual(result['wakeup']['phase'], 'service_mailbox')

    def test_callback_resumes_mailbox_without_repeating_admission(self):
        from vibe_guide.adapters.task_provider import ProviderActionStore
        store = ProviderActionStore(self.paths)
        action = store.request(operation='wait', provider='codex-app-visible',
            run_id='run-1', issue_id='n1', role='reviewer', generation=1,
            native_tool='codex_app__wait_threads', request={'targets': [{'threadId': 'r1'}]})
        self.preflight()
        store.complete(action['action_id'], {'status': 'completed', 'cursor': 'c:1'})
        result = self.preflight()
        self.assertTrue(result.get('wakeup', {}).get('reused', False))
        self.assertEqual(result['wakeup']['phase'], 'service_mailbox')
        self.assertEqual(store.result(action['action_id'])['cursor'], 'c:1')

    def test_live_hard_cap_overrides_reused_work(self):
        self.preflight()
        self.usage(150001)
        result = self.preflight()
        self.assertEqual(result['state'], 'rotate')
        self.assertFalse(result.get('wakeup', {}).get('reused', True))

    def test_changed_authorization_does_not_reuse_admission(self):
        self.preflight()
        self.snapshot.authorization_digest = 'c' * 64
        self.assertFalse(self.preflight().get('wakeup', {}).get('reused', True))

    def test_a_foreign_shift_cannot_create_a_receipt(self):
        register_supervisor_address(self.paths, 'run-1',
            {'provider': 'codex', 'session_id': 'other', 'host': 'local'},
            session_record=self.record)
        result = self.preflight()
        self.assertNotIn('wakeup', result)

    def test_unknown_evidence_can_be_repaired_in_the_same_turn(self):
        self.record.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'shift-1'}}) + '\n')
        self.turn('turn-1')
        self.assertEqual(self.preflight()['state'], 'unknown')
        self.usage(30000)
        result = self.preflight()
        self.assertEqual(result['state'], 'work')
        self.assertFalse(result.get('wakeup', {}).get('reused', True))

    def test_idle_is_rechecked_when_a_delivery_arrives_in_the_same_turn(self):
        from vibe_guide.adapters.task_provider import ProviderActionStore
        self.snapshot.nodes['n1']['status'] = 'running'
        store = ProviderActionStore(self.paths)
        action = store.request(operation='create', provider='codex-app-visible',
            run_id='run-1', issue_id='n1', role='developer', generation=1,
            native_tool='codex_app__create_thread', request={'prompt': 'work'})
        store.complete(action['action_id'], {'binding': {'task_id': 'd1'}})
        self.assertEqual(self.preflight()['state'], 'idle')
        store.record_worker_delivery('run-1', 'n1', 'developer',
            {'delivery_evidence': {'completion_marker': 'DONE', 'delivery_path': 'delivery.md',
                                  'thread_status': 'completed'}}, 1)
        result = self.preflight()
        self.assertEqual(result['state'], 'work')
        self.assertFalse(result.get('wakeup', {}).get('reused', True))

    def test_unrecognized_host_log_keeps_read_only_compatibility(self):
        self.record.write_text(json.dumps({'context_tokens': 30000}))
        self.assertEqual(self.preflight()['state'], 'work')
        self.assertNotIn('wakeup', self.preflight())
        self.assertFalse((self.paths.vibe / 'runs/run-1/supervisor-wakeup.json').exists())
