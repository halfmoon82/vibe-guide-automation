"""Offline public initial-create binding chain; synthetic receipts are not App proof."""
import subprocess
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from tests import support_vibe_home as hermetic
from vibe_guide.paths import ProjectPaths
from vibe_guide.adapters.task_provider import ProviderPending, ProviderUnavailable
from vibe_guide.runners.provider_action import ProviderActionRunner, complete_native_mailbox_action
from vibe_guide.state import acquire_writer_lease
from vibe_guide.task_registry import runtime_binding_gate, save_task_binding, load_task_binding
from vibe_guide.supervisor import SupervisorLease

setUpModule, tearDownModule = hermetic.module_hooks()


class InitialBindingTests(unittest.TestCase):
    def setUp(self):
        entry = patch('vibe_guide.adapters.task_provider.require_entry')
        entry.start()
        self.addCleanup(entry.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = ProjectPaths(Path(temporary.name))
        self.root = self.paths.root / '.worktrees' / 'n1'
        self.root.mkdir(parents=True)
        self.git('init', '-b', 'node/initial')
        self.git('commit', '--allow-empty', '-m', 'fixture baseline')
        self.contract = dict(run_id='run-initial', node_id='n1', role='developer',
            generation=1, binding_contract_version='3.9', binding_probe=True,
            project_id='fixture-project', worktree=str(self.root), managed_root=str(self.paths.root),
            branch='node/initial', base_sha=self.git('rev-parse', 'HEAD'), files=['a.py'])
        self.contract.update(authorization_digest='b' * 64, consistency_binding={
            'schema_version': 1, 'project_digest': 'c' * 64, 'plan_id': 'fixture-plan',
            'plan_version': 1, 'decision_digest': 'd' * 64,
            'authorization_digest': 'b' * 64, 'issue_contract_digest': 'e' * 64})
        self.assertTrue(acquire_writer_lease(self.paths, 'n1', str(self.root), 'run-initial',
            'a' * 64, role='developer', generation=1))
        (self.paths.vibe / 'state.json').write_text('{}')
        self.runner = ProviderActionRunner(self.paths, 'codex-app-visible', 'codex-app-visible')
        self.lease = SupervisorLease(self.paths)
        self.epoch = self.lease.takeover('run-initial', 0,
            owner={'session_id': 'fixture-supervisor', 'provider': 'codex'})

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), '-c', 'user.name=Fixture',
            '-c', 'user.email=fixture@example.test', *args], stderr=subprocess.STDOUT, text=True).strip()

    def attempt(self):
        return self.runner.task_binding(dict(self.contract), self.root, 'run-initial', 'running')

    def complete(self, operation, body):
        action, = [a for a in self.runner.store.pending('run-initial') if a['operation'] == operation]
        complete_native_mailbox_action(self.paths, 'run-initial', action['action_id'],
            {'structuredContent': body}, request_digest=action['request_digest'])

    def created(self):
        with self.assertRaises(ProviderPending):
            self.attempt()
        self.complete('create', {'threadId': 'new-task', 'hostId': 'local'})

    def waited(self, task='new-task', host='local', cursor='native-cursor'):
        self.created()
        with self.assertRaises(ProviderPending):
            self.attempt()
        self.complete('wait', {'polls': [{'thread': {'id': task, 'hostId': host}, 'cursor': cursor}]})

    def test_initial_create_enters_wait_then_full_gate_before_locate(self):
        self.waited()
        with self.assertRaises(ProviderPending):
            self.attempt()
        self.complete('locate', {'located': True})
        with self.assertRaises(ProviderPending):
            self.attempt()
        self.complete('visibility', {'visible': True, 'direct_enter': True})
        binding = self.attempt()
        self.assertTrue(runtime_binding_gate(self.contract, binding).verified)
        from vibe_guide.monitor import Monitor
        monitor = object.__new__(Monitor)
        monitor.paths = self.paths
        monitor._require_binding_gate(self.contract, binding, self.runner)

    def test_visibility_consumption_identity_drift_blocks(self):
        for kind in ('head', 'branch', 'owner', 'lease'):
            with self.subTest(kind=kind):
                probe = InitialBindingTests('test_initial_create_enters_wait_then_full_gate_before_locate')
                probe.setUp()
                try:
                    probe.ready_visibility()
                    original = probe.runner._require_result
                    def drift(contract, run_id, operation, request):
                        result = original(contract, run_id, operation, request)
                        if operation == 'visibility':
                            if kind == 'head':
                                probe.git('commit', '--allow-empty', '-m', 'drift')
                            elif kind == 'branch':
                                probe.git('switch', '-c', 'node/foreign')
                            elif kind == 'owner':
                                probe.lease.takeover('run-initial', probe.epoch,
                                    owner={'session_id': 'foreign', 'provider': 'codex'})
                            else:
                                from vibe_guide.state import release_writer_lease
                                release_writer_lease(probe.paths, 'n1', str(probe.root), 'run-initial')
                        return result
                    with patch.object(probe.runner, '_require_result', side_effect=drift):
                        with self.assertRaises((ProviderUnavailable, PermissionError)):
                            probe.attempt()
                    self.assertFalse(list(probe.runner.store._directory('handles').glob('*.json')))
                finally:
                    probe.doCleanups()

    def test_start_rechecks_initial_binding_after_return(self):
        self.ready_visibility()
        binding = self.attempt()
        save_task_binding(self.paths, binding)
        live_contract = dict(self.contract, binding_intent=binding.binding_intent,
            binding_observation=binding.binding_observation)
        self.runner.start(live_contract, self.root)
        (self.root / 'late-drift.py').write_text('dirty = True\n')
        with self.assertRaises(ProviderUnavailable):
            self.runner.start(live_contract, self.root)
        self.assertEqual(binding.cursor, 'native-cursor')
        save_task_binding(self.paths, binding)
        saved = load_task_binding(self.paths, 'n1', 'developer', run_id='run-initial')
        self.assertEqual(saved.task_id, 'new-task')
        self.assertFalse(saved.business_write_allowed)

    def test_wrong_wait_identity_never_reaches_locate(self):
        for task, host, cursor in [('foreign', 'local', 'c'), ('new-task', 'foreign', 'c'),
                                   ('new-task', 'local', '')]:
            with self.subTest(task=task, host=host, cursor=cursor):
                # Each subcase needs its own durable journal.
                probe = InitialBindingTests('test_initial_create_enters_wait_then_full_gate_before_locate')
                probe.setUp()
                try:
                    probe.waited(task, host, cursor)
                    with self.assertRaises(ProviderUnavailable):
                        probe.attempt()
                    self.assertFalse(any(a['operation'] in {'locate', 'visibility'}
                        for a in probe.runner.store.pending('run-initial')))
                finally:
                    probe.doCleanups()

    def test_git_drift_and_lease_drift_block_before_locate(self):
        for kind in ('head', 'branch', 'dirty', 'lease'):
            with self.subTest(kind=kind):
                probe = InitialBindingTests('test_initial_create_enters_wait_then_full_gate_before_locate')
                probe.setUp()
                try:
                    probe.created()
                    with probe.assertRaises(ProviderPending):
                        probe.attempt()
                    if kind == 'head':
                        probe.git('commit', '--allow-empty', '-m', 'drift')
                    elif kind == 'branch':
                        probe.git('switch', '-c', 'node/drift')
                    elif kind == 'dirty':
                        (probe.root / 'dirty.py').write_text('x = 1\n')
                    else:
                        from vibe_guide.state import release_writer_lease
                        release_writer_lease(probe.paths, 'n1', str(probe.root), 'run-initial')
                    probe.complete('wait', {'polls': [{'thread': {'id': 'new-task', 'hostId': 'local'}, 'cursor': 'c'}]})
                    with probe.assertRaises(ProviderUnavailable):
                        probe.attempt()
                    self.assertFalse(any(a['operation'] in {'locate', 'visibility'}
                        for a in probe.runner.store.pending('run-initial')))
                finally:
                    probe.doCleanups()

    def test_owner_generation_and_contract_conflicts_block_before_locate(self):
        for kind in ('owner', 'generation', 'base', 'head', 'cursor', 'clean', 'task', 'host', 'lease'):
            with self.subTest(kind=kind):
                probe = InitialBindingTests('test_initial_create_enters_wait_then_full_gate_before_locate')
                probe.setUp()
                try:
                    probe.waited()
                    if kind == 'owner':
                        probe.lease.takeover('run-initial', probe.epoch,
                            owner={'session_id': 'foreign', 'provider': 'codex'})
                    elif kind == 'generation':
                        probe.contract['generation'] = 2
                    elif kind == 'base':
                        probe.contract['base_sha'] = 'b' * 40
                    elif kind == 'head':
                        probe.contract['head_sha'] = 'b' * 40
                    elif kind == 'cursor':
                        probe.contract['cursor'] = 'different'
                    elif kind == 'clean':
                        probe.contract['clean'] = False
                    elif kind == 'task':
                        probe.contract['provider_task_id'] = 'foreign'
                    elif kind == 'host':
                        probe.contract['host_id'] = 'foreign'
                    else:
                        path, = (probe.paths.vibe / 'leases').glob('*.json')
                        row = json.loads(path.read_text())
                        row['generation'] = 2
                        path.write_text(json.dumps(row))
                    with probe.assertRaises((ProviderUnavailable, ProviderPending)):
                        probe.attempt()
                    self.assertFalse(any(a['operation'] in {'locate', 'visibility'}
                        for a in probe.runner.store.pending('run-initial')))
                finally:
                    probe.doCleanups()

    def test_missing_contract_fields_block_before_create(self):
        for name in ('project_id', 'managed_root', 'worktree', 'branch', 'base_sha'):
            with self.subTest(name=name), patch.dict(self.contract):
                self.contract.pop(name)
                with self.assertRaises(ProviderUnavailable):
                    self.attempt()
                self.assertEqual(self.runner.store.pending('run-initial'), [])

    def test_missing_approved_source_blocks_after_create_without_locate(self):
        self.contract.pop('consistency_binding')
        self.created()
        with self.assertRaises(ProviderUnavailable):
            self.attempt()
        self.assertFalse(any(a['operation'] in {'wait', 'locate', 'visibility'}
            for a in self.runner.store.pending('run-initial')))

    def test_wait_projection_without_native_receipt_never_becomes_live_evidence(self):
        self.created()
        with self.assertRaises(ProviderPending):
            self.attempt()
        action, = self.runner.store.pending('run-initial')
        self.runner.store.complete(action['action_id'], {'polls': [
            {'thread': {'id': 'new-task', 'hostId': 'local'}, 'cursor': 'forged'}]},
            request_digest=action['request_digest'])
        with self.assertRaises((ProviderUnavailable, FileNotFoundError)):
            self.attempt()
        self.assertFalse(any(a['operation'] in {'locate', 'visibility'}
            for a in self.runner.store.pending('run-initial')))

    def test_drift_between_locate_and_visibility_is_regated(self):
        self.waited()
        original = self.runner._require_result
        def drift(contract, run_id, operation, request):
            result = original(contract, run_id, operation, request)
            if operation == 'locate':
                (self.root / 'drift.py').write_text('changed = True\n')
            return result
        with self.assertRaises(ProviderPending):
            self.attempt()
        self.complete('locate', {'located': True})
        with patch.object(self.runner, '_require_result', side_effect=drift):
            with self.assertRaises(ProviderUnavailable):
                self.attempt()
        self.assertFalse(any(a['operation'] == 'visibility' for a in self.runner.store.pending('run-initial')))

    def ready_visibility(self):
        self.waited()
        with self.assertRaises(ProviderPending):
            self.attempt()
        self.complete('locate', {'located': True})
        with self.assertRaises(ProviderPending):
            self.attempt()
        self.complete('visibility', {'visible': True, 'direct_enter': True})

    def test_native_create_prompt_is_readonly_without_internal_flags(self):
        from vibe_guide.adapters.task_provider import CodexAppBridge
        for role in ('developer', 'reviewer'):
            with self.subTest(role=role):
                probe = InitialBindingTests()
                probe.setUp()
                try:
                    probe.contract['role'] = role
                    with self.assertRaises(ProviderPending):
                        probe.attempt()
                    action, = probe.runner.store.pending('run-initial')
                    captured = []
                    bridge = CodexAppBridge(create_thread=lambda args: captured.append(args),
                        navigate_to_codex_page=None, send_message_to_thread=None, wait_threads=None)
                    bridge.create(action['request'])
                    public, = captured
                    self.assertNotIn('business_write_allowed', public)
                    self.assertIn('只读绑定探针', public['prompt'])
                    self.assertIn('不得写入任何文件', public['prompt'])
                    self.assertIn('不得调用 worker-deliver', public['prompt'])
                    self.assertNotIn('请执行', public['prompt'])
                    self.assertNotIn('vibe worker-deliver --', public['prompt'])
                    self.assertNotIn('accepted', public['prompt'])
                    self.assertNotIn('业务合同：', public['prompt'])
                finally:
                    probe.doCleanups()

    def test_verified_probe_starts_business_once_on_original_thread(self):
        for role in ('developer', 'reviewer'):
            with self.subTest(role=role):
                probe = InitialBindingTests()
                probe.setUp()
                try:
                    if role == 'reviewer':
                        from vibe_guide.state import release_writer_lease
                        release_writer_lease(probe.paths, 'n1', str(probe.root), 'run-initial')
                        acquire_writer_lease(probe.paths, 'n1', str(probe.root), 'run-initial',
                            'a' * 64, role=role, generation=1)
                    probe.contract['role'] = role
                    probe.ready_visibility()
                    self.assertFalse(any(a['operation'] == 'resume'
                        for a in probe.runner.store.pending('run-initial')))
                    binding = probe.attempt()
                    save_task_binding(probe.paths, binding)
                    contract = dict(probe.contract, binding_intent=binding.binding_intent,
                        binding_observation=binding.binding_observation)
                    handle = probe.runner.start(contract, probe.root)
                    action, = probe.runner.store.pending('run-initial')
                    self.assertEqual(action['operation'], 'resume')
                    self.assertEqual(action['request']['threadId'], 'new-task')
                    self.assertIn('vibe worker-deliver', action['request']['prompt'])
                    self.assertIn('只审查当前 developer 交付' if role == 'reviewer'
                        else '开发：', action['request']['prompt'])
                    before = probe.runner._handle_path(handle.run_id).read_bytes()
                    restarted = ProviderActionRunner(probe.paths, 'codex-app-visible', 'codex-app-visible')
                    self.assertEqual(restarted.start(contract, probe.root), handle)
                    self.assertEqual(probe.runner._handle_path(handle.run_id).read_bytes(), before)
                    probe.complete('resume', {'resumed': True})
                    self.assertEqual(restarted.start(contract, probe.root), handle)
                    self.assertEqual(probe.runner.store.pending('run-initial'), [])
                finally:
                    probe.doCleanups()

    def test_probe_business_request_survives_crash_before_handle(self):
        self.ready_visibility()
        binding = self.attempt()
        save_task_binding(self.paths, binding)
        contract = dict(self.contract, binding_intent=binding.binding_intent,
            binding_observation=binding.binding_observation)
        atomic = self.runner.store._atomic
        def crash(path, payload):
            if Path(path).parent.name == 'handles':
                raise OSError('simulated crash after durable business action')
            return atomic(path, payload)
        with patch.object(self.runner.store, '_atomic', side_effect=crash):
            with self.assertRaisesRegex(OSError, 'simulated crash'):
                self.runner.start(contract, self.root)
        action, = self.runner.store.pending('run-initial')
        self.assertEqual(action['operation'], 'resume')
        original = self.runner.store.action(action['action_id'])
        restarted = ProviderActionRunner(self.paths, 'codex-app-visible', 'codex-app-visible')
        restarted.start(contract, self.root)
        self.assertEqual(restarted.store.pending('run-initial'), [original])
        self.complete('resume', {'resumed': True})
        restarted.start(contract, self.root)
        self.assertEqual(restarted.store.action(original['action_id']), original)
        self.assertEqual(restarted.store.pending('run-initial'), [])

    def test_probe_never_business_starts_with_invalid_binding(self):
        import copy
        for role in ('developer', 'reviewer'):
            for kind in ('source', 'wrong_source', 'dirty', 'owner', 'lease', 'invisible'):
                with self.subTest(role=role, kind=kind):
                    probe = InitialBindingTests()
                    probe.setUp()
                    try:
                        if role == 'reviewer':
                            from vibe_guide.state import release_writer_lease
                            release_writer_lease(probe.paths, 'n1', str(probe.root), 'run-initial')
                            acquire_writer_lease(probe.paths, 'n1', str(probe.root), 'run-initial',
                                'a' * 64, role=role, generation=1)
                        probe.contract['role'] = role
                        probe.ready_visibility()
                        binding = probe.attempt()
                        if kind == 'source':
                            binding.legacy.pop('initial_binding_source')
                        elif kind == 'wrong_source':
                            source = copy.deepcopy(binding.legacy['initial_binding_source'])
                            source['owner']['lease_epoch'] += 1
                            binding.legacy['initial_binding_source'] = source
                        elif kind == 'dirty':
                            (probe.root / 'dirty.py').write_text('drift')
                        elif kind == 'owner':
                            probe.lease.takeover('run-initial', probe.epoch,
                                owner={'session_id': 'foreign', 'provider': 'codex'})
                        elif kind == 'lease':
                            from vibe_guide.state import release_writer_lease
                            release_writer_lease(probe.paths, 'n1', str(probe.root), 'run-initial')
                        else:
                            binding.visible = False
                            with self.assertRaisesRegex(ValueError, 'visibility disagrees'):
                                save_task_binding(probe.paths, binding)
                            continue
                        save_task_binding(probe.paths, binding)
                        contract = dict(probe.contract, binding_intent=binding.binding_intent,
                            binding_observation=binding.binding_observation)
                        with self.assertRaises((ProviderUnavailable, PermissionError)):
                            probe.runner.start(contract, probe.root)
                        self.assertFalse(any(a['operation'] == 'resume'
                            for a in probe.runner.store.pending('run-initial')))
                    finally:
                        probe.doCleanups()

    def test_explicit_native_invisibility_never_starts_business(self):
        self.waited()
        with self.assertRaises(ProviderPending):
            self.attempt()
        self.complete('locate', {'located': True})
        with self.assertRaises(ProviderPending):
            self.attempt()
        self.complete('visibility', {'visible': False, 'direct_enter': False})
        with self.assertRaises((ProviderUnavailable, ValueError)):
            self.attempt()
        self.assertFalse(any(a['operation'] == 'resume'
            for a in self.runner.store.pending('run-initial')))

    def test_existing_legacy_strict_create_keeps_prompt_and_start_permissions(self):
        original = self.runner._action_owned
        def old_request(contract, run_id, operation, request, sequence=0):
            if operation == 'create':
                request = dict(request)
                request.pop('write_barrier_version', None)
                request['prompt'] = 'legacy already-issued business prompt'
            return original(contract, run_id, operation, request, sequence)
        with patch.object(self.runner, '_action_owned', side_effect=old_request):
            with self.assertRaises(ProviderPending):
                self.attempt()
        create, = self.runner.store.pending('run-initial')
        with self.assertRaises(ProviderPending):
            self.attempt()
        self.assertEqual(self.runner.store.action(create['action_id']), create)
        self.complete('create', {'threadId': 'new-task', 'hostId': 'local'})
        with self.assertRaises(ProviderPending):
            self.attempt()
        self.complete('wait', {'polls': [{'thread': {'id': 'new-task', 'hostId': 'local'},
            'cursor': 'native-cursor'}]})
        with self.assertRaises(ProviderPending):
            self.attempt()
        self.complete('locate', {'located': True})
        with self.assertRaises(ProviderPending):
            self.attempt()
        self.complete('visibility', {'visible': True, 'direct_enter': True})
        binding = self.attempt()
        save_task_binding(self.paths, binding)
        self.runner.start(dict(self.contract, binding_intent=binding.binding_intent,
            binding_observation=binding.binding_observation), self.root)
        self.assertEqual(self.runner.store.pending('run-initial'), [])
        self.assertEqual(self.runner.store.action(create['action_id']), create)

    def test_probe_business_start_uses_existing_receipt_recovery(self):
        self.ready_visibility()
        binding = self.attempt()
        save_task_binding(self.paths, binding)
        contract = dict(self.contract, binding_intent=binding.binding_intent,
            binding_observation=binding.binding_observation, intent_digest='f' * 64)
        handle = self.runner.start(contract, self.root)
        intent = dict(role='developer', generation=1, task_id='new-task', intent_digest='f' * 64)
        self.assertIsNone(self.runner.recover_start('run-initial', 'n1', intent, 'b' * 64))
        self.complete('resume', {'resumed': True})
        self.assertEqual(self.runner.recover_start('run-initial', 'n1', intent, 'b' * 64), handle)
        # A replay must not overwrite an already advanced poll cursor/state.
        path = self.runner._handle_path(handle.run_id)
        metadata = self.runner.store._read(path)
        metadata.update(wait_sequence=7, cursor='advanced-cursor', pending_action=None,
            pending_operation=None)
        self.runner.store._atomic(path, metadata)
        before = path.read_bytes()
        self.assertEqual(self.runner.start(contract, self.root), handle)
        self.assertEqual(path.read_bytes(), before)

    def test_relative_writer_lease_completes_initial_binding(self):
        from vibe_guide.state import release_writer_lease
        release_writer_lease(self.paths, 'n1', str(self.root), 'run-initial')
        relative = str(self.root.relative_to(self.paths.root))
        self.assertTrue(acquire_writer_lease(self.paths, 'n1', relative, 'run-initial',
            'a' * 64, role='developer', generation=1))
        self.ready_visibility()
        binding = self.attempt()
        self.assertTrue(runtime_binding_gate(self.contract, binding).verified)
        from vibe_guide.monitor import Monitor
        monitor = object.__new__(Monitor)
        monitor.paths = self.paths
        monitor._require_binding_gate(self.contract, binding, self.runner)

    def test_visibility_consumption_dirty_never_returns_verified_binding(self):
        self.ready_visibility()
        original = self.runner._require_result
        def drift(contract, run_id, operation, request):
            result = original(contract, run_id, operation, request)
            if operation == 'visibility':
                (self.root / 'after-visibility.py').write_text('dirty = True\n')
            return result
        with patch.object(self.runner, '_require_result', side_effect=drift):
            with self.assertRaises(ProviderUnavailable):
                self.attempt()
        self.assertEqual(list(self.runner.store._directory('handles').glob('*.json')), [])

    def test_relative_lease_conflicting_writer_is_rejected(self):
        from vibe_guide.state import release_writer_lease
        release_writer_lease(self.paths, 'n1', str(self.root), 'run-initial')
        relative = str(self.root.relative_to(self.paths.root))
        acquire_writer_lease(self.paths, 'n1', relative, 'run-foreign',
            'a' * 64, role='developer', generation=1)
        self.created()
        with self.assertRaises(ProviderPending):
            self.attempt()
        self.complete('wait', {'polls': [{'thread': {'id': 'new-task', 'hostId': 'local'}, 'cursor': 'c'}]})
        with self.assertRaises((ProviderUnavailable, PermissionError)):
            self.attempt()
        self.assertFalse(any(a['operation'] == 'locate' for a in self.runner.store.pending('run-initial')))

    def test_monitor_scheduled_relative_lease_resolves_without_rewriting_cas(self):
        from tests import test_monitor as fixture
        from vibe_guide.runners.fake import FakeRunner
        from vibe_guide.state import retirement_writer_lease, read_writer_lease_for_root
        probe = fixture.MonitorTests()
        probe.setUp()
        try:
            monitor, authorization = probe.authorized_monitor([fixture.node('n1')])
            snapshot = monitor.start(authorization, FakeRunner())
            relative = snapshot.nodes['n1']['worktree']
            self.assertFalse(Path(relative).is_absolute())
            root = str((probe.paths.root / relative).resolve())
            before = retirement_writer_lease(probe.paths, 'n1', relative, snapshot.run_id)
            self.assertIsNotNone(before)
            resolved = retirement_writer_lease(probe.paths, 'n1', root, snapshot.run_id, resolved_root=True)
            self.assertEqual(before, resolved)
            lease = read_writer_lease_for_root(probe.paths, 'n1', root, snapshot.run_id)
            self.assertEqual(lease.worktree, root)
            self.assertEqual(before, retirement_writer_lease(probe.paths, 'n1', relative, snapshot.run_id))
        finally:
            probe.tearDown()

    def test_deleted_or_empty_source_fails_closed_at_all_consumers(self):
        from vibe_guide.monitor import Monitor
        for variant in ('deleted', 'empty'):
            with self.subTest(variant=variant):
                probe = InitialBindingTests('test_initial_create_enters_wait_then_full_gate_before_locate')
                probe.setUp()
                try:
                    probe.ready_visibility()
                    binding = probe.attempt()
                    if variant == 'deleted':
                        binding.legacy.pop('initial_binding_source')
                    else:
                        binding.legacy['initial_binding_source'] = {}
                    (probe.root / 'dirty-carrier.py').write_text('dirty = True\n')
                    contract = dict(probe.contract, binding_intent=binding.binding_intent,
                        binding_observation=binding.binding_observation)
                    self.assertFalse(probe.runner.binding_gate(contract, binding).verified)
                    monitor = object.__new__(Monitor)
                    monitor.paths = probe.paths
                    with self.assertRaises(ValueError):
                        monitor._require_binding_gate(contract, binding, probe.runner)
                    save_task_binding(probe.paths, binding)
                    with self.assertRaises(ProviderUnavailable):
                        probe.runner.start(contract, probe.root)
                    self.assertFalse(list(probe.runner.store._directory('handles').glob('*.json')))
                finally:
                    probe.doCleanups()

    def test_source_integrity_clean_dirty_matrix_at_all_consumers(self):
        from vibe_guide.monitor import Monitor
        import copy
        for dirty in (False, True):
            for variant in ('deleted', 'empty', 'null', 'list', 'string', 'digest', 'owner', 'lease'):
                with self.subTest(dirty=dirty, variant=variant):
                    probe = InitialBindingTests('test_initial_create_enters_wait_then_full_gate_before_locate')
                    probe.setUp()
                    try:
                        probe.ready_visibility()
                        binding = probe.attempt()
                        source = copy.deepcopy(binding.legacy['initial_binding_source'])
                        if variant == 'deleted':
                            binding.legacy.pop('initial_binding_source')
                        elif variant in ('empty', 'null', 'list', 'string'):
                            binding.legacy['initial_binding_source'] = {
                                'empty': {}, 'null': None, 'list': [], 'string': 'invalid'}[variant]
                        else:
                            if variant == 'digest':
                                source['consistency_binding']['issue_contract_digest'] = 'f' * 64
                            elif variant == 'owner':
                                source['owner']['lease_epoch'] += 1
                            else:
                                source['writer_lease']['record']['generation'] += 1
                            binding.legacy['initial_binding_source'] = source
                        if dirty:
                            (probe.root / 'dirty-carrier.py').write_text('dirty = True\n')
                        # Remove the probe hint too: the journal must determine
                        # provenance when registry metadata cannot do so.
                        contract = dict(probe.contract, binding_intent=binding.binding_intent,
                            binding_observation=binding.binding_observation)
                        contract.pop('binding_probe')
                        result = probe.runner.binding_gate(contract, binding)
                        self.assertEqual(result.binding_state, 'blocked_unknown')
                        self.assertFalse(result.business_write_allowed)
                        monitor = object.__new__(Monitor)
                        monitor.paths = probe.paths
                        with self.assertRaises(ValueError):
                            monitor._require_binding_gate(contract, binding, probe.runner)
                        save_task_binding(probe.paths, binding)
                        before = list(probe.runner.store._directory('handles').glob('*.json'))
                        with self.assertRaises(ProviderUnavailable):
                            probe.runner.start(contract, probe.root)
                        self.assertEqual(list(probe.runner.store._directory('handles').glob('*.json')), before)
                    finally:
                        probe.doCleanups()

    def test_complete_source_survives_reload_and_starts_without_probe_hint(self):
        self.ready_visibility()
        binding = self.attempt()
        save_task_binding(self.paths, binding)
        restored = load_task_binding(self.paths, 'n1', 'developer', run_id='run-initial')
        restored.binding_intent, restored.binding_observation = binding.binding_intent, binding.binding_observation
        contract = dict(self.contract, binding_intent=binding.binding_intent,
            binding_observation=binding.binding_observation)
        contract.pop('binding_probe')
        self.assertTrue(self.runner.binding_gate(contract, restored).verified)
        self.runner.start(contract, self.root)
        self.assertEqual(len(list(self.runner.store._directory('handles').glob('*.json'))), 1)

    def test_missing_create_journal_and_carrier_fail_closed_without_probe_hint(self):
        """No mutable hint may turn absent create evidence into write permission."""
        from vibe_guide.monitor import Monitor
        for dirty in (False, True):
            for variant in ('request', 'result', 'both', 'task_identity', 'host_identity', 'source'):
                with self.subTest(dirty=dirty, variant=variant):
                    probe = InitialBindingTests('test_initial_create_enters_wait_then_full_gate_before_locate')
                    probe.setUp()
                    try:
                        probe.ready_visibility()
                        binding = probe.attempt()
                        create = probe.runner.store.related_action({
                            'provider': probe.runner.provider, 'run_id': binding.run_id,
                            'issue_id': binding.issue_id, 'role': binding.role,
                            'generation': binding.generation, 'sequence': 0,
                        }, 'create')
                        action_id = create['action_id']
                        if variant in ('request', 'both'):
                            (probe.runner.store._directory('requests') / (action_id + '.json')).unlink()
                        if variant in ('result', 'both'):
                            for directory in ('results', 'native-receipts'):
                                for path in probe.runner.store._directory(directory).glob(action_id + '*.json'):
                                    path.unlink()
                        if variant == 'task_identity':
                            binding.task_id = binding.platform_task_id = binding.threadId = 'foreign-task'
                        elif variant == 'host_identity':
                            binding.host = binding.hostId = 'foreign-host'
                        elif variant == 'source':
                            from vibe_guide.adapters.task_provider import _canonical_digest
                            create['request'].pop('initial_binding_source')
                            unsigned = dict(create)
                            unsigned.pop('request_digest')
                            create['request_digest'] = _canonical_digest(unsigned)
                            probe.runner.store._atomic(probe.runner.store._directory('requests') /
                                (action_id + '.json'), create)
                        binding.legacy.pop('initial_binding_source')
                        if dirty:
                            (probe.root / 'dirty-journal.py').write_text('dirty = True\n')
                        contract = dict(probe.contract, binding_intent=binding.binding_intent,
                            binding_observation=binding.binding_observation)
                        contract.pop('binding_probe')
                        result = probe.runner.binding_gate(contract, binding)
                        self.assertEqual(result.binding_state, 'blocked_unknown')
                        self.assertFalse(result.business_write_allowed)
                        monitor = object.__new__(Monitor)
                        monitor.paths = probe.paths
                        with self.assertRaises(ValueError):
                            monitor._require_binding_gate(contract, binding, probe.runner)
                        save_task_binding(probe.paths, binding)
                        restored = load_task_binding(probe.paths, 'n1', 'developer', run_id='run-initial')
                        restored.binding_intent = binding.binding_intent
                        restored.binding_observation = binding.binding_observation
                        self.assertEqual(probe.runner.binding_gate(contract, restored).binding_state,
                            'blocked_unknown')
                        with self.assertRaises(ProviderUnavailable):
                            probe.runner.start(contract, probe.root)
                        self.assertEqual(list(probe.runner.store._directory('handles').glob('*.json')), [])
                    finally:
                        probe.doCleanups()
