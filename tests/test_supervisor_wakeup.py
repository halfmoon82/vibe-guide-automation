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
        self.usage(200001)
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


class HeartbeatIntervalTests(unittest.TestCase):
    """N1: --accept-decision gates the decision state; 10/30/60 minute cadence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = ProjectPaths(self.root)
        self.record = self.root / 'usage.json'
        self.record.write_text(json.dumps({'context_tokens': 30000}))
        directory = self.paths.vibe / 'runs/run-1'
        directory.mkdir(parents=True)
        (directory / 'state.json').write_text('{}')
        (self.paths.vibe / 'state.json').write_text('{}')
        self.addCleanup(patch.stopall)
        self.snapshot = RunSnapshot('run-1', 'plan-1', 1, 'running',
            {'n1': {'status': 'blocked_design'}}, {},
            authorization_digest='a' * 64, node_contract_digest='b' * 64)
        patch('vibe_guide.supervisor.load_snapshot', return_value=self.snapshot).start()
        patch('vibe_guide.adapters.task_provider.require_entry').start()
        self.clock = lambda: 1000.0

    def raise_decision(self):
        from vibe_guide.decisions import DecisionMailbox
        return DecisionMailbox(self.paths).raise_decision(
            run_id='run-1', node_id='n1', question='Pick', options=['A', 'B'])

    def register(self, provider='codex'):
        register_supervisor_address(self.paths, 'run-1',
            {'provider': provider, 'session_id': 's-1', 'host': 'local'})

    def write_verification(self, content):
        path = self.root / '.vibe/push-wake-verification.json'
        path.write_text(content if isinstance(content, str) else json.dumps(content))

    def evidence(self, **kinds):
        return {'platforms': {'codex': {k: [{'evidence_ref': v}] for k, v in kinds.items()}}}

    def test_decision_only_is_work_without_flag_and_decision_with_it(self):
        from vibe_guide.supervisor import supervisor_preflight
        self.raise_decision()
        legacy = supervisor_preflight(self.paths, 'run-1', self.record, single_shift=False)
        self.assertEqual(legacy['state'], 'work')
        self.assertTrue(legacy['decision_only'])
        self.assertTrue(legacy['decisions'])
        new = supervisor_preflight(self.paths, 'run-1', self.record,
                                   single_shift=False, accept_decision=True)
        self.assertEqual(new['state'], 'decision')
        self.assertTrue(new['decision_only'])

    def test_cli_preflight_flag_gates_decision(self):
        self.raise_decision()
        args = ['--run-id', 'run-1', '--session-record', str(self.record), '--json']
        legacy = run_cli(['supervisor-preflight', *args], self.root).payload
        flagged = run_cli(['supervisor-preflight', '--accept-decision', *args], self.root).payload
        self.assertEqual(legacy['state'], 'work')
        self.assertEqual(flagged['state'], 'decision')
        self.assertEqual(flagged['heartbeat_interval']['minutes'], 60)
        # preflight is read-only: it never writes the interval state.
        self.assertFalse((self.paths.vibe / 'runs/run-1/heartbeat-interval.json').exists())

    def test_interval_idle_decision_and_push_verified(self):
        from vibe_guide.supervisor import heartbeat_interval
        self.register()
        idle = heartbeat_interval(self.paths, 'run-1', decision_only=False)
        self.assertEqual((idle['minutes'], idle['mode']), (10, 'default'))
        waiting = heartbeat_interval(self.paths, 'run-1', decision_only=True)
        self.assertEqual((waiting['minutes'], waiting['mode']), (60, 'decision'))
        self.write_verification(self.evidence(success='logs/ok.txt', failure='logs/fail.txt'))
        pushed = heartbeat_interval(self.paths, 'run-1', decision_only=False)
        self.assertEqual((pushed['minutes'], pushed['mode']), (30, 'push_verified'))
        self.assertEqual(heartbeat_interval(self.paths, 'run-1', decision_only=True)['minutes'], 60)

    def test_interval_unverified_push_falls_back_with_reason(self):
        from vibe_guide.supervisor import heartbeat_interval
        self.register()
        self.write_verification('{not json')
        broken = heartbeat_interval(self.paths, 'run-1', decision_only=False)
        self.assertEqual(broken['minutes'], 10)
        self.assertTrue(broken['reason'].startswith('push wake verification unreadable'))
        self.write_verification(self.evidence(success='logs/ok.txt'))
        partial = heartbeat_interval(self.paths, 'run-1', decision_only=False)
        self.assertEqual(partial['minutes'], 10)
        self.assertIn('lacks a failure evidence', partial['reason'])
        self.write_verification(self.evidence(success='logs/ok.txt', failure=' '))
        self.assertEqual(heartbeat_interval(self.paths, 'run-1', decision_only=False)['minutes'], 10)

    def test_interval_without_registered_supervisor_is_default_with_reason(self):
        from vibe_guide.supervisor import heartbeat_interval
        result = heartbeat_interval(self.paths, 'run-1', decision_only=False)
        self.assertEqual(result['minutes'], 10)
        self.assertIn('no supervisor registered', result['reason'])

    def test_record_keeps_since_while_mode_is_unchanged(self):
        from vibe_guide.supervisor import (heartbeat_interval, read_heartbeat_interval,
                                           record_heartbeat_interval)
        self.assertIsNone(read_heartbeat_interval(self.paths, 'run-1'))
        waiting = heartbeat_interval(self.paths, 'run-1', decision_only=True)
        first = record_heartbeat_interval(self.paths, 'run-1', waiting, clock=lambda: 1000.0)
        self.assertEqual((first['since'], first['minutes']), (1000.0, 60))
        again = record_heartbeat_interval(self.paths, 'run-1', waiting, clock=lambda: 2000.0)
        self.assertEqual((again['since'], again['updated_at']), (1000.0, 2000.0))
        idle = heartbeat_interval(self.paths, 'run-1', decision_only=False)
        changed = record_heartbeat_interval(self.paths, 'run-1', idle, clock=lambda: 3000.0)
        self.assertEqual((changed['since'], changed['minutes']), (3000.0, 10))

    def test_beat_persists_60_then_recovers_and_handoff_reads_it_back(self):
        from vibe_guide.supervisor import read_heartbeat_interval, supervisor_handoff
        from vibe_guide.decisions import DecisionMailbox
        self.register()
        request = self.raise_decision()
        args = ['supervisor-beat', '--run-id', 'run-1', '--accept-decision',
                '--session-record', str(self.record), '--json']
        beat = run_cli(args, self.root)
        self.assertEqual(beat.payload['state'], 'decision')
        self.assertIn('60 分钟', beat.text)
        self.assertEqual(read_heartbeat_interval(self.paths, 'run-1')['minutes'], 60)
        with patch('vibe_guide.supervisor._write_handoff_summary',
                   return_value={'path': 'x', 'sha256': 'y'}):
            handoff, text = supervisor_handoff(self.paths, 'run-1')
        self.assertEqual(handoff['heartbeat_interval']['minutes'], 60)
        self.assertIn('心跳间隔：当前 60 分钟', text)
        DecisionMailbox(self.paths).answer(request['decision_id'], 'A',
            request_digest=request['request_digest'])
        after = run_cli(args, self.root)
        self.assertNotEqual(after.payload['state'], 'decision')
        self.assertEqual(after.payload['heartbeat_interval']['minutes'], 10)
        self.assertEqual(read_heartbeat_interval(self.paths, 'run-1')['minutes'], 10)
