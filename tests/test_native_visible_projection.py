"""Public native shapes, synthetic offline receipts; never real App AC."""
import json
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests import test_native_initial_binding as initial
from vibe_guide.adapters.task_provider import ProviderPending, ProviderUnavailable, ProviderActionStore
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.runners.provider_action import complete_native_mailbox_action
from vibe_guide.task_registry import save_task_binding, load_task_binding


class NativeVisibleProjectionTests(unittest.TestCase):
    def setUp(self):
        self.probe = initial.InitialBindingTests('test_initial_create_enters_wait_then_full_gate_before_locate')
        self.probe.setUp()
        self.addCleanup(self.probe.doCleanups)
        self.capability = build_contract(self.probe.paths.root, provider='codex-app-visible',
            host_id='local', facts={
                'codex.visible_task.' + name: {'status': 'verified_available',
                    'scope': 'project',
                    'route': tool, 'evidence_ref': 'fixture:public-user-owned-sidebar:' + name}
                for name, tool in (('create', 'codex_app__create_thread'),
                    ('enter', 'codex_app__navigate_to_codex_page'),
                    ('resume', 'codex_app__send_message_to_thread'),
                    ('wait', 'codex_app__wait_threads'))})
        save_contract(self.probe.paths, self.capability)
        self.probe.contract['capability_contract_digest'] = self.capability.contract_digest

    def complete(self, operation, body):
        action, = [a for a in self.probe.runner.store.pending('run-initial') if a['operation'] == operation]
        response = {'content': [{'type': 'text', 'text': json.dumps(body)}], 'isError': False}
        complete_native_mailbox_action(self.probe.paths, 'run-initial', action['action_id'],
            response, request_digest=action['request_digest'])
        return action

    def test_codex_active_status_object_projects_create_binding(self):
        p = self.probe
        with self.assertRaises(ProviderPending):
            p.attempt()
        action = [a for a in p.runner.store.pending('run-initial') if a['operation'] == 'create'][0]
        response = {'structuredContent': {
            'threadId': 'active-task', 'hostId': 'local',
            'status': {'type': 'active'},
        }}
        complete_native_mailbox_action(p.paths, 'run-initial', action['action_id'], response,
            request_digest=action['request_digest'])
        result = p.runner.store.result(action['action_id'])
        self.assertEqual(result, {'binding': {'threadId': 'active-task', 'hostId': 'local'}})

    def poll(self):
        return {'timedOut': True, 'polls': [{'thread': {'id': 'new-task', 'hostId': 'local'},
            'cursor': 'native-cursor'}]}

    def ready_navigation(self):
        self.probe.waited()
        with self.assertRaises(ProviderPending):
            self.probe.attempt()
        self.complete('locate', {'navigated': True})
        with self.assertRaises(ProviderPending):
            self.probe.attempt()

    def bound(self):
        self.ready_navigation()
        self.complete('visibility', self.poll())
        return self.probe.attempt()

    def test_ready_create_navigation_and_initial_poll_bind_without_business_send(self):
        p = self.probe
        p.waited()
        with self.assertRaises(ProviderPending):
            p.attempt()
        self.complete('locate', {'navigated': True})
        with self.assertRaises(ProviderPending):
            p.attempt()
        self.complete('visibility', self.poll())
        binding = p.attempt()
        self.assertTrue(binding.visible)
        self.assertTrue(p.runner.binding_gate(p.contract, binding).verified)
        self.assertEqual(binding.task_id, 'new-task')
        self.assertEqual(binding.cursor, 'native-cursor')
        self.assertEqual(p.runner.store.pending('run-initial'), [])
        requests = [p.runner.store._read(path) for path in p.runner.store._directory('requests').glob('*.json')]
        self.assertEqual(sum(a['operation'] == 'create' for a in requests), 1)
        self.assertFalse(any(a['operation'] == 'resume' for a in requests))
        self.assertEqual(p.attempt().task_id, binding.task_id)

    def test_capability_missing_stale_denied_wrong_route_host_or_project_blocks(self):
        for variant in ('missing', 'stale', 'denied', 'route', 'host', 'project', 'digest', 'evidence'):
            with self.subTest(variant=variant):
                probe = NativeVisibleProjectionTests('test_ready_create_navigation_and_initial_poll_bind_without_business_send')
                probe.setUp()
                try:
                    probe.ready_navigation()
                    capability = probe.capability
                    data = capability.to_dict()
                    if variant == 'missing':
                        (probe.probe.paths.vibe / 'session-contract.json').unlink()
                    elif variant in ('project', 'evidence'):
                        # A structurally hashed contract for another root is not project evidence.
                        save_contract(probe.probe.paths, capability)
                        path = probe.probe.paths.vibe / 'session-contract.json'
                        if variant == 'project':
                            data['project_digest'] = 'f' * 64
                        else:
                            data['capabilities']['codex.visible_task.create'].pop('evidence_ref')
                        from vibe_guide.capability_contract import _digest
                        data['contract_digest'] = _digest({k: v for k, v in data.items() if k != 'contract_digest'})
                        path.write_text(json.dumps(data))
                    elif variant == 'stale':
                        pass  # Advance the observer clock without changing the pinned digest.
                    else:
                        facts = {k: {key: value for key, value in v.items() if key != 'name'}
                            for k, v in data['capabilities'].items()}
                        if variant == 'denied':
                            facts['codex.visible_task.enter']['status'] = 'permission_denied'
                        elif variant == 'route':
                            facts['codex.visible_task.enter']['route'] = 'wrong_tool'
                        elif variant == 'digest':
                            facts['codex.visible_task.create']['evidence_ref'] = 'fixture:other-contract'
                        changed = build_contract(probe.probe.paths.root, provider='codex-app-visible',
                            host_id='foreign' if variant == 'host' else 'local', facts=facts,
                            now=None)
                        save_contract(probe.probe.paths, changed)
                    with (patch('vibe_guide.capability_contract._now',
                            return_value=datetime.now(timezone.utc) + timedelta(hours=2))
                            if variant == 'stale' else nullcontext()):
                        action = probe.complete('visibility', probe.poll())
                    self.assertNotEqual(probe.probe.runner.store.result(action['action_id']).get('visible'), True)
                    with self.assertRaises((ValueError, ProviderUnavailable)):
                        probe.probe.attempt()
                finally:
                    probe.doCleanups()

    def test_visibility_poll_ack_and_cross_identity_fail_closed(self):
        import copy
        for variant in ('ack', 'target', 'host', 'missing_cursor', 'error', 'conflict'):
            with self.subTest(variant=variant):
                probe = NativeVisibleProjectionTests('test_ready_create_navigation_and_initial_poll_bind_without_business_send')
                probe.setUp()
                try:
                    probe.ready_navigation()
                    body = probe.poll()
                    if variant == 'ack':
                        body = {'status': 'accepted'}
                    elif variant in ('target', 'host'):
                        body['polls'][0]['thread']['id' if variant == 'target' else 'hostId'] = 'foreign'
                    elif variant == 'missing_cursor':
                        body['polls'][0].pop('cursor')
                    elif variant == 'error':
                        body['errors'] = [{'error': 'denied'}]
                    action, = [a for a in probe.probe.runner.store.pending('run-initial') if a['operation'] == 'visibility']
                    response = {'structuredContent': body}
                    if variant == 'conflict':
                        other = copy.deepcopy(body); other['polls'][0]['cursor'] = 'conflict'
                        response['content'] = [{'type': 'text', 'text': json.dumps(other)}]
                    complete_native_mailbox_action(probe.probe.paths, 'run-initial', action['action_id'],
                        response, request_digest=action['request_digest'])
                    self.assertNotEqual(probe.probe.runner.store.result(action['action_id']).get('visible'), True)
                    with self.assertRaises((ValueError, ProviderUnavailable)):
                        probe.probe.attempt()
                finally:
                    probe.doCleanups()

    def test_consumption_rechecks_capability_and_immutable_receipt_chain(self):
        for variant in ('stale', 'raw', 'request_digest', 'generation', 'owner'):
            with self.subTest(variant=variant):
                probe = NativeVisibleProjectionTests('test_ready_create_navigation_and_initial_poll_bind_without_business_send')
                probe.setUp()
                try:
                    binding = probe.bound()
                    p = probe.probe
                    if variant == 'owner':
                        p.lease.takeover('run-initial', p.epoch,
                            owner={'session_id': 'other-supervisor', 'provider': 'codex'})
                    elif variant == 'stale':
                        (p.paths.vibe / 'session-contract.json').unlink()
                    else:
                        action = p.runner.store.related_action({'provider': p.runner.provider,
                            'run_id': binding.run_id, 'issue_id': binding.issue_id,
                            'role': binding.role, 'generation': binding.generation, 'sequence': 0}, 'locate')
                        path = (p.runner.store._native_receipt_path(action['action_id']) if variant == 'raw'
                            else p.runner.store._directory('requests') / (action['action_id'] + '.json'))
                        data = json.loads(path.read_text())
                        if variant == 'raw':
                            data['native_response'] = {'structuredContent': {'navigated': False}}
                        elif variant == 'request_digest':
                            data['request_digest'] = 'f' * 64
                        else:
                            data['generation'] += 1
                        path.write_text(json.dumps(data))
                    result = p.runner.binding_gate(p.contract, binding)
                    self.assertEqual(result.binding_state, 'blocked_unknown')
                    self.assertFalse(result.business_write_allowed)
                    save_task_binding(p.paths, binding)
                    with self.assertRaises((ValueError, ProviderUnavailable, PermissionError)):
                        p.runner.start(p.contract, p.root)
                    self.assertEqual(list(p.runner.store._directory('handles').glob('*.json')), [])
                finally:
                    probe.doCleanups()

    def test_raw_projection_interruption_replays_without_second_create(self):
        self.ready_navigation()
        p = self.probe
        atomic = ProviderActionStore._atomic
        def interrupted(path, record):
            if path.parent.name == 'results':
                raise OSError('fixture projection interrupted')
            return atomic(path, record)
        with patch.object(ProviderActionStore, '_atomic', new=staticmethod(interrupted)):
            with self.assertRaises(OSError):
                self.complete('visibility', self.poll())
        binding = p.attempt()
        self.assertTrue(binding.visible)
        self.assertEqual(p.attempt().task_id, binding.task_id)
        self.assertEqual(len([path for path in p.runner.store._directory('requests').glob('*.json')
            if p.runner.store._read(path)['operation'] == 'create']), 1)

    def test_pending_create_is_not_ready_visibility(self):
        p = self.probe
        with self.assertRaises(ProviderPending):
            p.attempt()
        self.complete('create', {'clientThreadId': 'setup-pending', 'status': 'pending'})
        with self.assertRaises((ValueError, ProviderUnavailable)):
            p.attempt()
        self.assertFalse(any(a['operation'] in ('wait', 'locate', 'visibility')
            for a in p.runner.store.pending('run-initial')))

    def test_explicit_negative_and_nested_identity_conflicts_never_grant_visibility(self):
        for operation in ('create', 'wait', 'visibility'):
            for variant in ('visible', 'direct_enter', 'background', 'task', 'host', 'project', 'generation'):
                with self.subTest(operation=operation, variant=variant):
                    probe = NativeVisibleProjectionTests('test_ready_create_navigation_and_initial_poll_bind_without_business_send')
                    probe.setUp()
                    try:
                        p = probe.probe
                        with self.assertRaises(ProviderPending):
                            p.attempt()
                        create = {'threadId': 'new-task', 'hostId': 'local'}
                        wait, visible = probe.poll(), probe.poll()
                        body = {'create': create, 'wait': wait, 'visibility': visible}[operation]
                        metadata = (body.setdefault('binding', {}) if operation == 'create'
                            else body['polls'][0]['thread'])
                        if variant in ('visible', 'direct_enter'):
                            body[variant] = False
                        else:
                            key, value = {'background': ('mode', 'background'), 'task': ('task_id', 'foreign'),
                                'host': ('host', 'foreign'), 'project': ('projectId', 'foreign'),
                                'generation': ('generation', 2)}[variant]
                            metadata[key] = value
                        probe.complete('create', create)
                        with self.assertRaises(ProviderPending):
                            p.attempt()
                        probe.complete('wait', wait)
                        with self.assertRaises(ProviderPending):
                            p.attempt()
                        probe.complete('locate', {'navigated': True})
                        with self.assertRaises(ProviderPending):
                            p.attempt()
                        action = probe.complete('visibility', visible)
                        self.assertNotEqual(p.runner.store.result(action['action_id']).get('visible'), True)
                        with self.assertRaises((ValueError, ProviderUnavailable)):
                            p.attempt()
                        self.assertEqual(list(p.runner.store._directory('handles').glob('*.json')), [])
                    finally:
                        probe.doCleanups()

    def test_persisted_binding_rechecks_negative_receipts_clean_and_dirty_before_start(self):
        from vibe_guide.adapters.task_provider import _canonical_digest
        from vibe_guide.monitor import Monitor
        variants = ('visible', 'direct_enter', 'nested_visible', 'nested_direct_enter',
            'background', 'task', 'host', 'project', 'generation', 'run', 'node', 'role')
        for dirty in (False, True):
            for operation in ('create', 'wait', 'visibility'):
                for variant in variants:
                    with self.subTest(dirty=dirty, operation=operation, variant=variant):
                        probe = NativeVisibleProjectionTests('test_ready_create_navigation_and_initial_poll_bind_without_business_send')
                        probe.setUp()
                        try:
                            binding = probe.bound()
                            p = probe.probe
                            save_task_binding(p.paths, binding)
                            basis = {'provider': p.runner.provider, 'run_id': binding.run_id,
                                'issue_id': binding.issue_id, 'role': binding.role,
                                'generation': binding.generation, 'sequence': 0}
                            action = p.runner.store.related_action(basis, operation)
                            path = p.runner.store._native_receipt_path(action['action_id'])
                            raw = p.runner.store._read(path)
                            envelope = raw['native_response']
                            body = envelope.get('structuredContent')
                            if body is None:
                                body = json.loads(envelope['content'][0]['text'])
                            nested = (body.setdefault('binding', {}) if operation == 'create'
                                else body['polls'][0]['thread'])
                            if variant in ('visible', 'direct_enter'):
                                body[variant] = False
                            elif variant.startswith('nested_'):
                                nested[variant[len('nested_'):]] = False
                            else:
                                key, value = {'background': ('mode', 'background'),
                                    'task': ('task_id', 'foreign'), 'host': ('host_id', 'foreign'),
                                    'project': ('project_id', 'foreign'), 'generation': ('generation', 2),
                                    'run': ('run_id', 'other-run'), 'node': ('node_id', 'other-node'),
                                    'role': ('role', 'reviewer')}[variant]
                                nested[key] = value
                            # Preserve a structurally valid raw/result record with its old
                            # positive projection: consumption must recompute its meaning.
                            if 'structuredContent' in envelope:
                                envelope['structuredContent'] = body
                            else:
                                envelope['content'][0]['text'] = json.dumps(body)
                            raw['receipt_digest'] = _canonical_digest({k: v for k, v in raw.items()
                                if k != 'receipt_digest'})
                            p.runner.store._atomic(path, raw)
                            if dirty:
                                (p.root / 'negative-dirty.py').write_text('dirty = True\n')
                            restored = load_task_binding(p.paths, 'n1', 'developer', run_id='run-initial')
                            restored.binding_intent, restored.binding_observation = (
                                binding.binding_intent, binding.binding_observation)
                            contract = dict(p.contract, binding_intent=binding.binding_intent,
                                binding_observation=binding.binding_observation)
                            result = p.runner.binding_gate(contract, restored)
                            self.assertEqual(result.binding_state, 'blocked_unknown')
                            self.assertFalse(result.business_write_allowed)
                            monitor = object.__new__(Monitor); monitor.paths = p.paths
                            with self.assertRaises(ValueError):
                                monitor._require_binding_gate(contract, restored, p.runner)
                            with self.assertRaises((ValueError, ProviderUnavailable)):
                                p.runner.start(contract, p.root)
                            self.assertEqual(list(p.runner.store._directory('handles').glob('*.json')), [])
                        finally:
                            probe.doCleanups()

    def test_isolated_navigation_wrong_mount_and_create_conflicts_cannot_bind(self):
        for variant in ('background', 'project', 'generation', 'isolated'):
            with self.subTest(variant=variant):
                probe = NativeVisibleProjectionTests('test_ready_create_navigation_and_initial_poll_bind_without_business_send')
                probe.setUp()
                try:
                    p = probe.probe
                    with self.assertRaises(ProviderPending):
                        p.attempt()
                    body = {'threadId': 'new-task', 'hostId': 'local'}
                    body.update({'background': {'mode': 'background'},
                        'project': {'projectId': 'wrong-project'}, 'generation': {'generation': 2},
                        'isolated': {}}[variant])
                    create = probe.complete('create', body)
                    with self.assertRaises(ProviderPending):
                        p.attempt()
                    probe.complete('wait', probe.poll())
                    with self.assertRaises(ProviderPending):
                        p.attempt()
                    probe.complete('locate', {'navigated': True})
                    with self.assertRaises(ProviderPending):
                        p.attempt()
                    if variant == 'isolated':
                        (p.runner.store._directory('requests') / (create['action_id'] + '.json')).unlink()
                    action = probe.complete('visibility', probe.poll())
                    self.assertNotEqual(p.runner.store.result(action['action_id']).get('visible'), True)
                    with self.assertRaises((ValueError, ProviderUnavailable)):
                        p.attempt()
                finally:
                    probe.doCleanups()
