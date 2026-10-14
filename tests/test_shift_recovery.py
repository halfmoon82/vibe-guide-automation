"""Recovery facts must survive a late native callback and a fresh shift."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vibe_guide.adapters.task_provider import ProviderActionStore, ProviderPending, ProviderUnavailable
from vibe_guide.paths import ProjectPaths
from vibe_guide.supervisor import supervisor_handoff
from vibe_guide.runners.provider_action import ProviderActionRunner
from vibe_guide.contracts import RunHandle
from vibe_guide.monitor import classify_provider_failure
from vibe_guide.monitor import Monitor
from vibe_guide.task_registry import TaskBinding


class ActionResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name))
        self.store = ProviderActionStore(self.paths)
        self.action = self.store.request(operation='wait', provider='codex-app-visible',
            run_id='run-1', issue_id='n1', role='developer', generation=1,
            native_tool='codex_app__wait_threads', request={'targets': [{'threadId': 't1', 'hostId': 'local'}]})
        self.delivery = {'status': 'completed', 'event': 'delivered', 'cursor': 'c:2'}

    def test_late_timeout_cannot_replace_a_worker_delivery(self):
        self.store.complete(self.action['action_id'], self.delivery)
        self.store.complete(self.action['action_id'], {'status': 'timeout', 'cursor': 'c:1'})
        self.assertEqual(self.store.result(self.action['action_id']), self.delivery)
        observations = list((self.store.root / 'observations').glob('*.json'))
        self.assertEqual(len(observations), 1)
        self.assertEqual(json.loads(observations[0].read_text())['payload']['status'], 'timeout')

    def test_result_digest_mismatch_is_rejected_before_any_result_write(self):
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.store.complete(self.action['action_id'], self.delivery, request_digest='0' * 64)
        self.assertIsNone(self.store.result(self.action['action_id']))

    def test_corrupt_request_cannot_receive_a_result(self):
        path = self.store.root / 'requests' / (self.action['action_id'] + '.json')
        corrupt = json.loads(path.read_text())
        corrupt['schema_version'] = 99
        path.write_text(json.dumps(corrupt))
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.store.complete(self.action['action_id'], self.delivery)
        self.assertFalse((self.store.root / 'results' / path.name).exists())

    def test_corrupt_request_cannot_expose_a_previous_result(self):
        self.store.complete(self.action['action_id'], self.delivery)
        path = self.store.root / 'requests' / (self.action['action_id'] + '.json')
        corrupt = json.loads(path.read_text())
        corrupt['request']['targets'][0]['threadId'] = 'foreign'
        path.write_text(json.dumps(corrupt))
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.store.result(self.action['action_id'])

    def test_duplicate_is_idempotent_and_two_consumers_keep_one_result(self):
        action_id = self.action['action_id']
        def finish(i):
            return self.store.complete(action_id, {'status': 'completed', 'cursor': 'c:' + str(i)})
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(finish, [1, 2]))
        original = self.store.result(action_id)
        self.assertIn(original['cursor'], ('c:1', 'c:2'))
        self.store.complete(action_id, original)
        self.assertEqual(self.store.result(action_id), original)
        self.assertEqual(len(list((self.store.root / 'observations').glob('*.json'))), 1)

    def test_delivery_racing_timeout_remains_available_for_the_next_wait(self):
        action_id = self.action['action_id']
        path = self.store._delivery_path('run-1', 'n1', 'developer', 1)
        path.parent.mkdir()
        self.store._atomic(path, {'run_id': 'run-1', 'issue_id': 'n1', 'role': 'developer',
                                 'generation': 1, 'payload': self.delivery})
        pending = self.store.pending('run-1')
        self.store.complete(action_id, {'status': 'timeout'})
        with patch.object(self.store, 'pending', return_value=pending):
            self.assertFalse(self.store._consume_archived_delivery('run-1', 'n1', 'developer', 1))
        self.assertFalse(self.store._read(path).get('consumed_by'))
        (self.paths.vibe / "state.json").write_text("{}")
        with patch("vibe_guide.adapters.task_provider.require_entry"):
            next_action = self.store.request(operation='wait', provider='codex-app-visible',
                run_id='run-1', issue_id='n1', role='developer', generation=1, sequence=1,
                native_tool='codex_app__wait_threads', request={'targets': [{'threadId': 't1', 'hostId': 'local'}]})
        self.assertEqual(self.store.result(next_action['action_id'])['event'], 'delivered')
        self.assertEqual(self.store._read(path)['consumed_by'], next_action['action_id'])


class HandoffExecutionTests(unittest.TestCase):
    def test_many_pending_actions_keep_the_handoff_bounded_and_lossless(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            paths.vibe.mkdir()
            (paths.vibe / 'state.json').write_text('{}')
            store = ProviderActionStore(paths)
            with patch('vibe_guide.adapters.task_provider.require_entry'):
                for i in range(100):
                    store.request(operation='wait', provider='codex-app-visible', run_id='run-1',
                        issue_id='n' + str(i), role='reviewer', generation=1,
                        native_tool='codex_app__wait_threads', request={'targets': [{'threadId': 'r' + str(i)}]})
            directory = paths.vibe / 'runs/run-1'
            directory.mkdir(parents=True)
            (directory / 'state.json').write_text('{}')
            snap = SimpleNamespace(plan_id='plan-1', status='review', nodes={},
                                   authorization_digest='a'*64, node_contract_digest='b'*64)
            with patch('vibe_guide.supervisor.load_snapshot', return_value=snap):
                payload, _ = supervisor_handoff(paths, 'run-1')
            path = Path(payload['handoff_summary']['path'])
            summary = json.loads(path.read_text())
            self.assertLessEqual(path.stat().st_size, 24576)
            self.assertTrue(summary['details_omitted'])
            details = json.loads(Path(summary['details']['path']).read_text())
            self.assertEqual(len(details['pending_actions']), 100)
            self.assertTrue(all(a['evidence']['sha256'] for a in details['pending_actions']))

    def test_fresh_shift_receives_runtime_and_action_identity_without_full_history(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            store = ProviderActionStore(paths)
            action = store.request(operation='wait', provider='codex-app-visible',
                run_id='run-1', issue_id='n1', role='reviewer', generation=1,
                native_tool='codex_app__wait_threads', request={'targets': [{'threadId': 'r1', 'hostId': 'local'}]})
            directory = paths.vibe / 'runs/run-1'
            directory.mkdir(parents=True)
            (directory / 'state.json').write_text('{}')
            (directory / 'tasks.json').write_text(json.dumps({'bindings': [{
                'issue_id': 'n1', 'role': 'reviewer', 'generation': 1, 'task_id': 'r1',
                'model': 'gpt-5.6-sol', 'reasoning': 'deep', 'allowlist': ['src.py']}]}))
            snap = SimpleNamespace(plan_id='plan-1', status='review', nodes={'n1': {'status': 'review'}},
                authorization_digest='a'*64, node_contract_digest='b'*64)
            with patch('vibe_guide.supervisor.load_snapshot', return_value=snap):
                payload, _ = supervisor_handoff(paths, 'run-1', command_prefix='python3 /runtime/bridge.py cli')
            summary = json.loads(Path(payload['handoff_summary']['path']).read_text())
            self.assertEqual(summary['pending_actions'][0]['request_digest'], action['request_digest'])
            self.assertEqual(summary['pending_actions'][0]['native_tool'], 'codex_app__wait_threads')
            self.assertEqual(summary['tasks'][0]['model'], 'gpt-5.6-sol')
            self.assertEqual(summary['tasks'][0]['allowlist'], ['src.py'])
            self.assertTrue(Path(summary['runtime']['source_root']).is_dir())
            self.assertTrue(summary['runtime']['evidence'])
            self.assertFalse(summary['runtime']['loaded_code_attested'])
            self.assertEqual(summary['runtime']['evidence_scope'], 'current_disk_sources_at_loaded_module_paths')
            self.assertIn('request_digest', summary['service_protocol'])
            self.assertIn('resume', summary['service_protocol'])
            self.assertLessEqual(Path(payload['handoff_summary']['path']).stat().st_size, 24576)


class NativeFailureTests(unittest.TestCase):
    def test_failed_native_wait_keeps_the_error_for_engineering_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            runner = ProviderActionRunner.__new__(ProviderActionRunner)
            runner.paths = paths
            runner.store = ProviderActionStore(paths)
            metadata = {'node_id': 'n1', 'role': 'reviewer', 'generation': 1}
            handle = RunHandle('bridge-1')
            error = {'message': 'stream disconnected before completion: invalid_argument: try a different model'}
            events = runner._wait_result(handle, metadata, {'node_id': 'n1'},
                {'status': 'failed', 'error': error, 'reason_class': 'engineering'})
            self.assertEqual(events[0].event, 'failed')
            self.assertEqual(events[0].data['error'], error)
            self.assertEqual(events[0].data['reason'], error['message'])
            self.assertEqual(events[0].data['reason_class'], 'engineering')
            self.assertEqual(classify_provider_failure(events[0].data)['legacy_kind'], 'engineering')

    def test_explicit_failure_event_also_preserves_permission_boundary(self):
        with tempfile.TemporaryDirectory() as root:
            runner = ProviderActionRunner.__new__(ProviderActionRunner)
            runner.paths = ProjectPaths(Path(root))
            runner.store = ProviderActionStore(runner.paths)
            handle = RunHandle('bridge-1')
            events = runner._wait_result(handle, {}, {}, {'status': 'failed', 'event': 'failed',
                'error': {'message': 'permission required'}, 'permission_required': True})
            self.assertEqual(classify_provider_failure(events[0].data)['legacy_kind'], 'external')


class PendingSuccessorTests(unittest.TestCase):
    def test_bound_pending_create_keeps_successor_for_the_same_action(self):
        self._exercise_pending('create')

    def test_completed_callback_before_catch_keeps_the_same_create(self):
        self._exercise_pending('create', complete_before_catch=True)

    def test_pending_locate_replays_the_completed_create(self):
        self._exercise_pending('locate')

    def test_pending_visibility_replays_the_completed_create(self):
        self._exercise_pending('visibility')

    def test_pending_successor_event_recovers_a_lost_snapshot_write(self):
        self._exercise_pending('create', replay=True)

    def test_pending_successor_replay_rejects_invalid_dispatch_phase(self):
        self._exercise_pending('create', replay=True, invalid_phase=True)

    def _exercise_pending(self, operation, complete_before_catch=False, replay=False, invalid_phase=False):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            runner = ProviderActionRunner(paths, 'codex-app-visible', 'codex-app-visible')
            monitor = Monitor.__new__(Monitor)
            monitor.paths = paths
            monitor.plan = SimpleNamespace(version=1)
            monitor.nodes = {'n1': SimpleNamespace(contract={'files': []}, depends_on=[])}
            current = {'status': 'failed', 'worker': 'worker', 'worktree': '.', 'branch': 'branch',
                'developer_generation': 0, 'review_generation': 2, 'reviewer_started': True,
                'reviewer_identity': 'old-reviewer', 'retryable_action': None,
                'active_task': None, 'active_role': None, 'start_intent': None}
            snapshot = SimpleNamespace(run_id='run-1', nodes={'n1': current}, handles={}, tasks={},
                                       capability_contract_digest='', authorization_digest='x',
                                       node_contract_digest='n', event_sequence=0)
            monitor._require_snapshot_authorization = lambda snap: SimpleNamespace(allowed_actions=[], file_scope=[])
            monitor._consistency_binding = lambda *args: {}
            records = []
            from vibe_guide.state import _sanitize_event_data
            def record(snap, event, data, active=None):
                records.append({'sequence': len(records)+1, 'event': event,
                    'data': _sanitize_event_data(data),
                    'provenance': {'role': 'system', 'authorization_digest': 'x', 'node_contract_digest': 'n'}})
            monitor._record = record
            monitor._save_task_binding = lambda *args: None
            monitor._require_binding_gate = lambda *args: None
            monitor._set_binding_status = lambda *args: None
            monitor._validate_execution_topology = lambda *args: None
            monitor._effective_worker_session_limit = lambda *args: 5
            monitor._parallel_group_blocked = lambda *args: set()
            monitor._refuse_path_ownership_conflicts = lambda *args: None
            monitor._recover_quarantined_delivered_developer = lambda *args: False
            monitor._prospective_intent_digest = lambda *args: 'intent'
            contracts = []
            def bind(*args, **kwargs):
                contract = args[7]
                contracts.append(dict(contract))
                if contract.get('successor'):
                    create_request = {'successor': True, 'predecessor_task_id': contract['predecessor_task_id']}
                    binding_result = {'binding': {'task_id': 'new-reviewer', 'host': 'local'}}
                    if operation != 'create':
                        created = runner._action(contract, 'run-1', 'create', create_request)
                        runner.store.complete(created['action_id'], binding_result)
                        (paths.vibe / 'state.json').write_text('{}')
                    target = (create_request if operation == 'create' else
                              {'threadId': 'new-reviewer'} if operation == 'locate' else
                              {'targets': [{'threadId': 'new-reviewer', 'hostId': 'local'}], 'timeoutMs': 0})
                    try:
                        result = runner._require_result(contract, 'run-1', operation, target)
                    except Exception as error:
                        if complete_before_catch and hasattr(error, 'action_id'):
                            runner.store.complete(error.action_id, binding_result)
                        raise
                    if operation != 'create':
                        result = binding_result
                    identity = result['binding']['task_id']
                else:
                    identity = 'old-reviewer'
                return TaskBinding(provider='codex-app-visible', mode='visible', issue_id='n1',
                    role='reviewer', task_id=identity, host='local', worktree='.', branch='branch',
                    run_id='run-1', generation=contract['generation'])
            monitor._binding_for = bind
            fake_runner = SimpleNamespace(provider='codex-app-visible', start=lambda *args: RunHandle('h1'),
                                          is_pending=lambda *args: False)
            with patch('vibe_guide.monitor.validate_runtime_contract', side_effect=lambda c, **kw: c), \
                 patch('vibe_guide.adapters.task_provider.require_entry'), \
                 patch('vibe_guide.monitor.save_snapshot'):
                self.assertFalse(monitor._start_task(snapshot, 'n1', 'reviewer', 'review',
                                                    fake_runner, False, successor=True))
                retry = current['retryable_action']
                self.assertTrue(retry['successor'])
                self.assertFalse(retry['same_task'])
                self.assertEqual(retry['predecessor_task_id'], 'old-reviewer')
                action = runner.store.action(retry['pending_provider_action'])
                runner.store.complete(action['action_id'], {'binding': {'task_id': 'new-reviewer', 'host': 'local'}})
                if replay:
                    current['status'] = 'failed'
                    current['retryable_action'] = None
                    current['quarantine'] = None
                    if invalid_phase:
                        records[-1]['data']['proof']['retry']['phase'] = 'deploy'
                        with patch('vibe_guide.monitor.load_events', return_value=records):
                            with self.assertRaisesRegex(ValueError, 'replay proof'):
                                monitor._reconcile_unapplied_events(snapshot)
                        self.assertIsNone(current['retryable_action'])
                        return
                    with patch('vibe_guide.monitor.load_events', return_value=records):
                        monitor._reconcile_unapplied_events(snapshot)
                    self.assertTrue(current['retryable_action']['successor'])
                (paths.vibe / 'state.json').write_text('{}')
                with patch('vibe_guide.adapters.task_provider.require_entry'):
                    with patch('vibe_guide.monitor.acquire_writer_lease', return_value=True):
                        monitor._schedule_ready(snapshot, fake_runner)
            self.assertEqual(current['active_task']['task_id'], 'new-reviewer')
            self.assertEqual(contracts[0]['generation'], contracts[1]['generation'])
            self.assertEqual(len(list((runner.store.root / 'requests').glob('*.json'))),
                             1 if operation == 'create' else 2)

    def test_locate_and_visibility_cannot_prove_a_successor_without_its_create(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            runner = ProviderActionRunner(paths, 'codex-app-visible', 'codex-app-visible')
            monitor = Monitor.__new__(Monitor)
            monitor.paths = paths
            monitor.plan = SimpleNamespace(plan_id='plan-1', version=1)
            snapshot = SimpleNamespace(run_id='run-1')
            contract = {'node_id': 'n1', 'role': 'reviewer', 'generation': 3,
                        'authorization_digest': 'a'*64, 'plan_id': 'plan-1', 'plan_version': 1}
            def proven(action):
                return monitor._proven_successor_action(snapshot, 'n1', 'reviewer', 3,
                    runner.provider, action['action_id'], action['request_digest'], 'old', 'a'*64)
            for operation, target in [('locate', {'threadId': 'new'}),
                                      ('visibility', {'targets': [{'threadId': 'new', 'hostId': 'local'}]})]:
                if paths.vibe.exists():
                    (paths.vibe / 'state.json').write_text('{}')
                with patch('vibe_guide.adapters.task_provider.require_entry'):
                    action = runner._action(contract, 'run-1', operation, target)
                self.assertFalse(proven(action))
            with patch('vibe_guide.adapters.task_provider.require_entry'):
                create = runner._action(contract, 'run-1', 'create',
                    {'successor': True, 'predecessor_task_id': 'old'})
            runner.store.complete(create['action_id'], {'binding': {'task_id': 'new', 'host': 'local'}})
            self.assertTrue(proven(action))
            for field, value in [('authorization_digest', 'b'*64), ('plan_version', 2),
                                 ('predecessor_task_id', 'other')]:
                path = runner.store.root / 'requests' / (create['action_id'] + '.json')
                original = path.read_bytes()
                changed = json.loads(original)
                changed['request'][field] = value
                unsigned = dict(changed)
                unsigned.pop('request_digest')
                from vibe_guide.adapters.task_provider import _canonical_digest
                changed['request_digest'] = _canonical_digest(unsigned)
                path.write_text(json.dumps(changed))
                self.assertFalse(proven(action))
                path.write_bytes(original)
            self.assertFalse(monitor._proven_successor_action(snapshot, 'n1', 'reviewer', 3,
                runner.provider, '../../other', action['request_digest'], 'old', 'a'*64))


if __name__ == '__main__':
    unittest.main()


class PromptTemplateUpgradeTests(unittest.TestCase):
    """A vibe upgrade may rewrite the dispatch prompt template mid-run.

    The worker already received the original prompt, so a request whose
    result is bound keeps its original bytes; any other field change, or a
    request not yet answered, is still identity drift.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name))
        self.paths.vibe.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / 'state.json').write_text('{}')
        gate = patch('vibe_guide.adapters.task_provider.require_entry')
        gate.start()
        self.addCleanup(gate.stop)
        self.store = ProviderActionStore(self.paths)
        self.request = {'prompt': 'old template', 'model': 'm1', 'issue_id': 'n1'}
        self.old = self.emit(self.request)
        self.path = self.store.root / 'requests' / (self.old['action_id'] + '.json')

    def emit(self, request):
        return self.store.request(operation='create', provider='claude-code-visible',
            run_id='run-1', issue_id='n1', role='developer', generation=1,
            native_tool='ccd_session__spawn_task', request=request)

    def test_bound_request_keeps_original_prompt(self):
        self.store.complete(self.old['action_id'], {'binding': {'task_id': 't1', 'host': 'h'}})
        before = self.path.read_bytes()
        again = self.emit({**self.request, 'prompt': 'new template'})
        self.assertEqual(again, self.old)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNotNone(self.store.result(self.old['action_id']))

    def test_unbound_prompt_change_is_still_drift(self):
        with self.assertRaisesRegex(ValueError, 'identity drift'):
            self.emit({**self.request, 'prompt': 'new template'})

    def test_bound_non_prompt_change_is_still_drift(self):
        self.store.complete(self.old['action_id'], {'binding': {'task_id': 't1', 'host': 'h'}})
        for change in ({'model': 'm2'}, {'prompt': 'new template', 'model': 'm2'}):
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, 'identity drift'):
                    self.emit({**self.request, **change})


class ParameterRecoveryReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name))
        self.runner = ProviderActionRunner(self.paths, 'codex', 'codex-app-visible')
        self.profile = {'worker': 'worker', 'model': 'gpt-5.6-sol', 'reasoning': 'normal',
            'fallbacks': [], 'selection_basis': {'issue_complexity_ref': 'n1'}}
        self.contract = {'run_id': 'run-1', 'node_id': 'n1', 'role': 'developer',
            'generation': 1, 'project_id': 'fixture-project', 'worker_profile': self.profile}
        self.paths.vibe.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / 'state.json').write_text('{}')
        self.gate = patch('vibe_guide.adapters.task_provider.require_entry')
        self.gate.start()
        self.addCleanup(self.gate.stop)
        with self.assertRaises(ProviderPending):
            self.runner.task_binding(self.contract, self.paths.root, 'run-1', 'start_pending')
        self.old = self.runner.store.pending()[0]
        self.recovered = {**self.contract, 'provider_options': {'omit_thinking': True}}

    def test_confirmed_rejected_create_uses_new_sequence_and_keeps_old_receipts(self):
        self.runner.store.complete(self.old['action_id'], {'status': 'failed',
            'task_created': False, 'reason': 'could not validate reasoning effort'})
        old_path = self.runner.store.root / 'requests' / (self.old['action_id'] + '.json')
        old_bytes = old_path.read_bytes()
        for _ in range(2):
            with self.assertRaises(ProviderPending):
                self.runner.task_binding(self.recovered, self.paths.root, 'run-1', 'start_pending')
        actions = self.runner.store.pending()
        self.assertEqual(len(actions), 1)
        adapted = actions[0]
        self.assertEqual(adapted['sequence'], 1)
        self.assertEqual(adapted['generation'], self.old['generation'])
        self.assertNotIn('thinking', adapted['request'])
        self.assertEqual(old_path.read_bytes(), old_bytes)
        self.runner.store.complete(adapted['action_id'], {'binding': {'threadId': 'new-task', 'hostId': 'local'}})
        with self.assertRaises(ProviderPending):
            self.runner.task_binding(self.recovered, self.paths.root, 'run-1', 'start_pending')
        locate = self.runner.store.pending()[0]
        self.assertEqual(locate['operation'], 'locate')
        self.assertEqual(locate['sequence'], 1)
        self.assertEqual(self.runner.store.related_action(locate, 'create')['action_id'], adapted['action_id'])

    def test_pending_or_ambiguous_or_created_result_never_creates_second_task(self):
        for result in (None, {'status': 'timeout', 'task_created': False},
                       {'status': 'failed', 'reason': 'could not validate reasoning effort'},
                       {'status': 'failed', 'task_created': False, 'binding': {'threadId': 'existing'}},
                       {'status': 'failed', 'task_created': False, 'clientThreadId': 'setup-id'},
                       {'status': 'failed', 'task_created': False, 'reason': 'permission denied'},
                       *({'status': status, 'failed': True, 'task_created': False,
                          'reason': 'could not validate reasoning effort'}
                         for status in ('timeout', 'unknown', 'pending', {'invalid': True})),
                       {'status': 'failed', 'created': True, 'task_created': False,
                        'reason': 'could not validate reasoning effort'}):
            with self.subTest(result=result):
                if result is not None:
                    path = self.runner.store.root / 'results' / (self.old['action_id'] + '.json')
                    if path.exists():
                        path.unlink()
                    self.runner.store.complete(self.old['action_id'], result)
                with self.assertRaises((ProviderUnavailable, ValueError)):
                    self.runner.task_binding(self.recovered, self.paths.root, 'run-1', 'start_pending')
                self.assertEqual(len(list((self.runner.store.root / 'requests').glob('*.json'))), 1)

    def test_failed_create_surfaces_native_parameter_reason(self):
        self.runner.store.complete(self.old['action_id'], {'status': 'failed',
            'task_created': False, 'reason': 'could not validate reasoning effort'})
        with self.assertRaisesRegex(ProviderUnavailable, 'could not validate reasoning'):
            self.runner.task_binding(self.contract, self.paths.root, 'run-1', 'start_pending')

    def test_parameter_recovery_cannot_change_authorization_or_model(self):
        self.runner.store.complete(self.old['action_id'], {'status': 'failed',
            'task_created': False, 'reason': 'could not validate reasoning effort'})
        for change in ({'authorization_digest': 'b' * 64},
                       {'worker_profile': {**self.profile, 'model': 'another-model'}}):
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, 'identity drift'):
                    self.runner.task_binding({**self.recovered, **change}, self.paths.root,
                        'run-1', 'start_pending')
                self.assertEqual(len(list((self.runner.store.root / 'requests').glob('*.json'))), 1)
