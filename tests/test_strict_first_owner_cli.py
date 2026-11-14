"""Strict initial dispatch via the public monitor entry, with offline fixtures."""
import copy
import json
import os
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock
from unittest.mock import patch

from tests import support_vibe_home as hermetic
from tests.support_v45_authorize import _NODE_SPEC, _REQUEST
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.cli import _load_plan, run_cli
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.provider_action import (
    ProviderActionRunner, complete_native_mailbox_action, _supervisor_owner_projection,
)
from vibe_guide.task_registry import load_task_binding
from vibe_guide.state import load_snapshot, save_snapshot
from vibe_guide.monitor import Monitor
from vibe_guide.supervisor import SupervisorLeaseConflict, register_supervisor_address

setUpModule, tearDownModule = hermetic.module_hooks()


class StrictFirstOwnerCLI(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='first-owner-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = hermetic.override_env(CODEX_HOME=str(self.root / 'codex-home'))
        self.environment.__enter__()
        self.addCleanup(self.environment.__exit__, None, None, None)
        self.paths = ProjectPaths(self.root)
        self.store = ProviderActionStore(self.paths)
        self.runner = ProviderActionRunner(self.paths, 'codex-app-visible', 'codex-app-visible')

    def cli(self, *args, runner=None):
        return run_cli([*args, '--json'], self.root, runner=runner, environ=os.environ)

    def prepare(self, strict=True, writer=None):
        result = self.cli('init', '--confirm')
        self.assertEqual(result.payload.get('status'), 'ok', result.payload)
        routes = {
            'create': 'codex_app__create_thread', 'enter': 'codex_app__navigate_to_codex_page',
            'resume': 'codex_app__send_message_to_thread', 'wait': 'codex_app__wait_threads'}
        caps = {'codex.visible_task.' + name: {'status': 'verified_available', 'scope': 'project',
                    'route': tool, 'evidence_ref': 'fixture:offline:' + name}
                for name, tool in routes.items()} if writer else {}
        save_contract(self.paths, build_contract(self.root, provider='codex-app-visible', host_id='local', facts=caps))
        facts = {'codex.' + name: True for name in (
            'agent', 'shell', 'subprocess', 'worktree', 'visible_task.create',
            'visible_task.enter', 'visible_task.resume', 'visible_task.wait')}
        self.store.publish_capabilities('codex', facts, 'offline fixture', project_id='fixture-project')
        spec = copy.deepcopy(_NODE_SPEC)
        spec['project_id'] = 'fixture-project'
        spec['capabilities']['agent_id'] = 'codex'
        spec['nodes'][0]['contract'].update(
            project_id='fixture-project', managed_root=str(self.root),
            worktree=str(writer or self.root / '.worktrees' / 'probe-node-a'),
            branch='fix/v528-r4-preconditions' if writer else 'node/probe-node-a',
            base_sha='78ee5bb17374438d5e87a016353f0f180aeffd11' if writer else 'a' * 40,
            adapter_id='codex', files=['probe.py'])
        if strict:
            spec['nodes'][0]['contract'].update(binding_contract_version='3.9', binding_probe=True)
        (self.root / 'spec.json').write_text(json.dumps(spec), encoding='utf-8')
        planned = self.cli('plan', '--request', _REQUEST, '--s1', '5,5,5,5,5',
                           '--plan-id', 'probe-plan', '--node-spec', 'spec.json', '--adapter', 'codex')
        self.assertEqual(planned.payload.get('status'), 'ok', planned.payload)
        shown = self.cli('authorize', '--plan', 'probe-plan', '--show-card')
        self.assertEqual(shown.payload.get('status'), 'card_shown', shown.payload)
        authorized = self.cli('authorize', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE')
        self.assertEqual(authorized.payload.get('status'), 'ok', authorized.payload)

    def test_missing_caller_does_not_persist_strict_create(self):
        self.prepare()
        started = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE',
                           runner=self.runner)
        self.assertEqual(started.payload.get('status'), 'blocked_unknown', started.payload)
        self.assertIn('supervisor-register', started.payload.get('handoff', ''))
        creates = [a for a in self.store.pending() if a['operation'] == 'create']
        if creates:
            self.assertTrue(all(a['request'].get('binding_probe') is True for a in creates))
            self.assertTrue(all(a['request'].get('business_write_allowed') is False for a in creates))
        self.assertEqual(len(creates), 0,
            'missing caller must stop before immutable strict create; owners=' +
            repr([a['request'].get('initial_binding_source', {}).get('owner') for a in creates]))
        if not started.payload.get('run_id'):
            reason = str(started.payload.get('reason', '')).lower()
            self.assertTrue(any(word in reason for word in ('owner', 'supervisor', 'session', '登记')),
                            started.payload)

    def test_initial_anchor_owner_projection_ignores_heartbeat_only_fields(self):
        base = {
            'run_id': 'run-1', 'lease_epoch': 1,
            'owner': {'provider': 'codex', 'session_id': 'session-1', 'host': 'local'},
            'released': False, 'heartbeat': 10.0, 'pid': 100,
        }
        changed = dict(base, heartbeat=20.0, pid=200)
        self.assertEqual(_supervisor_owner_projection(base), _supervisor_owner_projection(changed))
        self.assertEqual(
            _supervisor_owner_projection(None),
            _supervisor_owner_projection({'run_id': 'run-1', 'lease_epoch': 0,
                                           'owner': None, 'released': False,
                                           'heartbeat': 10.0, 'pid': 100}),
        )
        self.assertNotEqual(
            _supervisor_owner_projection(base),
            _supervisor_owner_projection(dict(base, lease_epoch=2)),
        )

    def test_same_fixture_non_strict_reaches_public_dispatch(self):
        self.prepare(strict=False)
        started = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE',
                           runner=self.runner)
        self.assertIsNotNone(started.payload.get('run_id'), started.payload)
        creates = [a for a in self.store.pending(started.payload['run_id'])
                   if a['operation'] == 'create']
        self.assertEqual(len(creates), 1)
        self.assertNotIn('initial_binding_source', creates[0]['request'])

    def test_public_create_is_claimed_before_exposing_invocation(self):
        self.prepare(strict=False)
        started = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE',
                           runner=self.runner)
        run_id = started.payload['run_id']
        view = self.cli('supervisor-mailbox', '--run-id', run_id)
        self.assertEqual(view.payload.get('status'), 'ok', view.payload)
        create, = [item for item in view.payload['pending'] if item['operation'] == 'create']
        self.assertIn('native_tool', create)
        self.assertIn('prompt', create)
        claim_file = self.store.root / 'create-claims' / (create['action_id'] + '.json')
        self.assertTrue(claim_file.is_file(), 'claim must be durable before the body is returned')
        claim = json.loads(claim_file.read_text())
        self.assertEqual(claim['request_digest'], create['request_digest'])
        self.assertEqual(claim['status'], 'claimed')
        replay = self.cli('supervisor-mailbox', '--run-id', run_id)
        self.assertFalse([item for item in replay.payload['pending']
                          if item['action_id'] == create['action_id'] and 'native_tool' in item])

    def journal(self, session='fixture-supervisor'):
        directory = Path(os.environ['CODEX_HOME']) / 'sessions' / '2026' / '11' / '10'
        directory.mkdir(parents=True, exist_ok=True)
        record = directory / ('rollout-test-' + session + '.jsonl')
        rows = [{'type': 'session_meta', 'payload': {'id': session}}, {'context_tokens': 100}]
        record.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        return record

    def start_and_register(self):
        self.prepare()
        record = self.journal()
        started = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE',
                           runner=self.runner)
        run_id = started.payload['run_id']
        self.assertEqual(self.store.pending(run_id), [])
        registered = self.cli('supervisor-register', '--run-id', run_id,
            '--provider', 'codex', '--session-id', 'fixture-supervisor',
            '--host', 'local', '--session-record', str(record))
        self.assertEqual(registered.payload.get('status'), 'ok', registered.payload)
        return run_id, record

    def test_public_register_and_resume_binds_first_create_to_current_owner(self):
        self.prepare()
        with hermetic.override_env(CODEX_THREAD_ID='fixture-supervisor'):
            record = self.journal()
            started = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE',
                               runner=self.runner)
            run_id = started.payload.get('run_id')
            self.assertIsNotNone(run_id, started.payload)
            self.assertEqual(self.store.pending(run_id), [])
            registered = self.cli('supervisor-register', '--run-id', run_id,
                '--provider', 'codex', '--session-id', 'fixture-supervisor',
                '--host', 'local', '--session-record', str(record))
            self.assertEqual(registered.payload.get('status'), 'ok', registered.payload)
            resumed = self.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=self.runner)
            creates = [a for a in self.store.pending(run_id) if a['operation'] == 'create']
            self.assertEqual(len(creates), 1, (resumed.payload, load_snapshot(self.paths, run_id).nodes))
            lease = json.loads((self.paths.vibe / 'runs' / run_id / 'supervisor.lease').read_text())
            registry = json.loads((self.paths.vibe / 'runs' / run_id / 'supervisor-registry.json').read_text())['current']
            source = creates[0]['request']['initial_binding_source']['owner']
            self.assertEqual(source, {k: lease[k] for k in ('run_id', 'lease_epoch', 'owner', 'released')})
            self.assertEqual(source['owner']['session_id'], registry['session_id'])
            self.assertEqual(source['owner']['host'], registry['host'])
            self.assertEqual(source['lease_epoch'], registry['lease_epoch'])
            self.assertGreater(source['lease_epoch'], 0)
            self.assertFalse(source['released'])
            before = creates[0]
            self.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=self.runner)
            self.assertEqual([a for a in self.store.pending(run_id) if a['operation'] == 'create'], [before])

    def test_invalid_current_owner_or_journal_never_creates(self):
        cases = ('missing_caller', 'foreign_caller', 'missing_journal', 'wrong_journal', 'archived_journal',
                 'wrong_host', 'wrong_provider', 'expired_capability', 'released', 'foreign_lease')
        for case in cases:
            with self.subTest(case=case):
                probe = StrictFirstOwnerCLI('test_missing_caller_does_not_persist_strict_create')
                probe.setUp()
                try:
                    with hermetic.override_env(CODEX_THREAD_ID='fixture-supervisor'):
                        run_id, journal = probe.start_and_register()
                        root = probe.paths.vibe / 'runs' / run_id
                        lease_path = root / 'supervisor.lease'
                        if case == 'missing_journal':
                            journal.unlink()
                        elif case == 'archived_journal':
                            archived = Path(os.environ['CODEX_HOME']) / 'archived_sessions'
                            archived.mkdir(exist_ok=True)
                            journal.rename(archived / journal.name)
                        elif case == 'wrong_journal':
                            journal.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'foreign'}}) + '\n')
                        elif case == 'wrong_host':
                            save_contract(probe.paths, build_contract(probe.root, provider='codex', host_id='foreign'))
                        elif case == 'wrong_provider':
                            save_contract(probe.paths, build_contract(probe.root, provider='workbuddy', host_id='local'))
                        elif case == 'expired_capability':
                            save_contract(probe.paths, build_contract(probe.root, provider='codex', host_id='local',
                                now=datetime.now(timezone.utc) - timedelta(hours=2)))
                        elif case in ('released', 'foreign_lease'):
                            lease = json.loads(lease_path.read_text())
                            if case == 'released': lease['released'] = True
                            else: lease['owner']['session_id'] = 'foreign'
                            lease_path.write_text(json.dumps(lease))
                        caller = None if case == 'missing_caller' else (
                            'foreign' if case == 'foreign_caller' else 'fixture-supervisor')
                        with hermetic.override_env(CODEX_THREAD_ID=caller):
                            probe.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=probe.runner)
                        self.assertFalse([a for a in probe.store.pending(run_id) if a['operation'] == 'create'])
                finally:
                    probe.doCleanups()

    def test_same_owner_registration_and_repeated_monitor_are_idempotent(self):
        with hermetic.override_env(CODEX_THREAD_ID='fixture-supervisor'):
            run_id, record = self.start_and_register()
            again = self.cli('supervisor-register', '--run-id', run_id, '--provider', 'codex',
                '--session-id', 'fixture-supervisor', '--host', 'local', '--session-record', str(record))
            self.assertEqual(again.payload['status'], 'ok')
            self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
            creates = [a for a in self.store.pending(run_id) if a['operation'] == 'create']
            self.assertEqual(len(creates), 1)
            before = creates[0]
            self.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=self.runner)
            self.assertEqual([a for a in self.store.pending(run_id) if a['operation'] == 'create'], [before])

    def test_concurrent_public_first_monitor_has_one_run(self):
        with hermetic.override_env(CODEX_THREAD_ID='fixture-supervisor'):
            self.prepare()
            journal = self.journal()
            entered, second_start = Event(), Event()
            count_lock = Lock()
            count = 0
            original_start = Monitor.start

            def observe_start(monitor, record, runner):
                nonlocal count
                with count_lock:
                    count += 1
                    index = count
                    if index == 1:
                        entered.set()
                    else:
                        second_start.set()
                if index == 1:
                    second_start.wait(timeout=2)
                return original_start(monitor, record, runner)

            def invoke():
                return self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)

            with patch.object(Monitor, 'start', observe_start):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(invoke)
                    self.assertTrue(entered.wait(timeout=10), 'first monitor did not reach start')
                    second = executor.submit(invoke)
                    results = [first.result(timeout=25), second.result(timeout=25)]
            runs = [p.name for p in (self.paths.vibe / 'runs').iterdir()
                    if p.is_dir() and (p / 'state.json').exists()]
            ids = [result.payload.get('run_id') for result in results]
            self.assertEqual(len(runs), 1, (runs, [(r.exit_code, r.payload) for r in results]))
            self.assertEqual(ids, [runs[0], runs[0]], [(r.exit_code, r.payload) for r in results])
            pointer = json.loads((self.paths.vibe / 'plans' / 'probe-plan' / 'current-run.json').read_text())
            self.assertEqual(pointer['run_id'], runs[0])
            self.assertEqual(self.store.pending(runs[0]), [])
            registration = self.cli('supervisor-register', '--run-id', runs[0], '--provider', 'codex',
                '--session-id', 'fixture-supervisor', '--host', 'local', '--session-record', str(journal))
            self.assertEqual(registration.payload.get('status'), 'ok', registration.payload)
            self.cli('resume', '--plan', 'probe-plan', '--run-id', runs[0], runner=self.runner)
            creates = [a for a in self.store.pending(runs[0]) if a['operation'] == 'create']
            self.assertEqual(len(creates), 1)
            self.assertEqual(creates[0]['request']['initial_binding_source']['owner']['run_id'], runs[0])

    def test_waiting_monitor_recovers_run_after_pointer_write_crash(self):
        from vibe_guide import cli as cli_module
        with hermetic.override_env(CODEX_THREAD_ID='fixture-supervisor'):
            self.prepare()
            journal = self.journal()
            before_pointer, release = Event(), Event()
            atomic = cli_module._atomic_json
            interrupted = []

            def crash_once(path, payload):
                if path.name == 'current-run.json' and not interrupted:
                    interrupted.append(True)
                    before_pointer.set()
                    if not release.wait(timeout=10):
                        raise AssertionError('waiting monitor was not released')
                    raise OSError('fixture crash before run pointer')
                return atomic(path, payload)

            def invoke():
                return self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)

            with patch.object(cli_module, '_atomic_json', crash_once):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(invoke)
                    self.assertTrue(before_pointer.wait(timeout=10))
                    second = executor.submit(invoke)
                    release.set()
                    results = [first.result(timeout=25), second.result(timeout=25)]
            self.assertNotEqual(results[0].exit_code, 0, results[0].payload)
            runs = [p.name for p in (self.paths.vibe / 'runs').iterdir()
                    if p.is_dir() and (p / 'state.json').exists()]
            self.assertEqual(len(runs), 1, (runs, [r.payload for r in results]))
            self.assertEqual(results[1].payload.get('run_id'), runs[0], results[1].payload)
            pointer = json.loads((self.paths.vibe / 'plans' / 'probe-plan' / 'current-run.json').read_text())
            self.assertEqual(pointer['run_id'], runs[0])
            self.assertEqual(self.store.pending(runs[0]), [])
            registration = self.cli('supervisor-register', '--run-id', runs[0], '--provider', 'codex',
                '--session-id', 'fixture-supervisor', '--host', 'local', '--session-record', str(journal))
            self.assertEqual(registration.payload.get('status'), 'ok', registration.payload)
            self.cli('resume', '--plan', 'probe-plan', '--run-id', runs[0], runner=self.runner)
            self.assertEqual(len([a for a in self.store.pending(runs[0]) if a['operation'] == 'create']), 1)

    def test_public_monitor_without_injected_runner_does_not_start_daemon(self):
        self.prepare()
        with patch('vibe_guide.cli._auto_start_daemon', side_effect=AssertionError('daemon called')):
            started = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE')
        self.assertIsNotNone(started.payload.get('run_id'), started.payload)
        self.assertEqual(self.store.pending(started.payload['run_id']), [])

    def test_crash_before_cli_run_pointer_recovers_same_run(self):
        self.prepare()
        from vibe_guide import cli as cli_module
        atomic = cli_module._atomic_json
        interrupted = []
        def crash_before_pointer(path, payload):
            if path.name == 'current-run.json' and not interrupted:
                interrupted.append(True)
                raise OSError('fixture crash before run pointer')
            return atomic(path, payload)
        with patch.object(cli_module, '_atomic_json', side_effect=crash_before_pointer):
            self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertTrue(interrupted)
        run_dirs = [p for p in (self.paths.vibe / 'runs').iterdir() if p.is_dir() and (p / 'state.json').exists()]
        self.assertEqual(len(run_dirs), 1)
        self.assertEqual(self.store.pending(), [])
        again = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertEqual(again.payload.get('run_id'), run_dirs[0].name, again.payload)
        self.assertEqual(self.store.pending(), [])

    def exercise_non_strict_public_crash(self, target):
        self.prepare(strict=False)
        from vibe_guide import cli as cli_module
        atomic = cli_module._atomic_json
        interrupted = []
        def crash_before_pointer(path, payload):
            if path.name == target and not interrupted:
                interrupted.append(True)
                raise OSError('fixture crash before run pointer')
            return atomic(path, payload)
        with patch.object(cli_module, '_atomic_json', crash_before_pointer):
            first = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertTrue(interrupted)
        self.assertNotEqual(first.exit_code, 0, first.payload)
        runs = [p.name for p in (self.paths.vibe / 'runs').iterdir()
                if p.is_dir() and (p / 'state.json').exists()]
        self.assertEqual(len(runs), 1)
        before = [a for a in self.store.pending(runs[0]) if a['operation'] == 'create']
        self.assertEqual(len(before), 1)
        again = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertEqual(again.payload.get('run_id'), runs[0], again.payload)
        pointer = json.loads((self.paths.vibe / 'plans' / 'probe-plan' / 'current-run.json').read_text())
        self.assertEqual(pointer['run_id'], runs[0])
        self.assertEqual([a for a in self.store.pending(runs[0]) if a['operation'] == 'create'], before)
        self.assertEqual(self.store.pending(runs[0]), before)
        self.assertEqual(len([p for p in (self.paths.vibe / 'runs').iterdir()
                              if p.is_dir() and (p / 'state.json').exists()]), 1)
        resumed = self.cli('resume', '--plan', 'probe-plan', '--run-id', runs[0], runner=self.runner)
        self.assertEqual(resumed.payload.get('run_id'), runs[0], resumed.payload)
        self.assertEqual([a for a in self.store.pending(runs[0]) if a['operation'] == 'create'], before)
        self.assertEqual(self.store.pending(runs[0]), before)

    def test_non_strict_pointer_crash_public_monitor_recovers_original_run(self):
        self.exercise_non_strict_public_crash('current-run.json')

    def test_non_strict_confirmation_crash_public_monitor_recovers_original_run(self):
        self.exercise_non_strict_public_crash('plan-confirmation.json')

    def test_non_strict_authorization_crash_public_monitor_recovers_original_run(self):
        self.exercise_non_strict_public_crash('authorization.json')

    def test_non_strict_orphan_conflicts_fail_closed(self):
        from vibe_guide import cli as cli_module
        from vibe_guide.adapters.task_provider import _canonical_digest
        cases = ('request_digest', 'request_authorization', 'request_profile', 'lease_intent', 'confirmation', 'workflow_auth', 'workflow_token',
                 'other_artifact', 'trailing_event', 'multiple_runs', 'raw_receipt', 'orphan_lease', 'foreign_request', 'invalid_registry')
        for case in cases:
            with self.subTest(case=case):
                probe = StrictFirstOwnerCLI('test_missing_caller_does_not_persist_strict_create')
                probe.setUp()
                try:
                    probe.prepare(strict=False)
                    atomic = cli_module._atomic_json
                    original_plan = (probe.paths.vibe / 'plans' / 'probe-plan' / 'plan.json').read_bytes()
                    original_confirmation = (probe.paths.vibe / 'plans' / 'probe-plan' / 'plan-confirmation.json').read_bytes()
                    def crash(path, payload):
                        if path.name == 'current-run.json':
                            raise OSError('fixture crash before pointer')
                        return atomic(path, payload)
                    with patch.object(cli_module, '_atomic_json', crash):
                        first = probe.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=probe.runner)
                    self.assertNotEqual(first.exit_code, 0)
                    root = probe.paths.vibe / 'runs'
                    runs = [p for p in root.iterdir() if p.is_dir() and (p / 'state.json').exists()]
                    self.assertEqual(len(runs), 1)
                    run_id = runs[0].name
                    action, = [a for a in probe.store.pending(run_id) if a['operation'] == 'create']
                    request_path = probe.store.root / 'requests' / (action['action_id'] + '.json')
                    if case in ('request_digest', 'request_authorization', 'request_profile'):
                        if case == 'request_profile':
                            protected = probe.store._read_controlled_payload(action)
                            self.assertIsNotNone(protected)
                            protected['request']['child_binding']['worker_profile']['writer'] = 'foreign-writer'
                            protected['request']['child_binding']['writer'] = 'foreign-writer'
                            protected['payload_digest'] = _canonical_digest({k: v for k, v in protected.items() if k != 'payload_digest'})
                            probe.store._atomic(probe.store._create_payload_path(action['action_id']), protected)
                        else:
                            protected = probe.store._read_controlled_payload(action)
                            self.assertIsNotNone(protected)
                            protected['request']['authorization_digest'] = 'f' * 64
                            protected['payload_digest'] = _canonical_digest({k: v for k, v in protected.items() if k != 'payload_digest'})
                            probe.store._atomic(probe.store._create_payload_path(action['action_id']), protected)
                        if case != 'request_digest':
                            action['request_digest'] = _canonical_digest({k: v for k, v in action.items() if k != 'request_digest'})
                        request_path.write_text(json.dumps(action))
                    elif case == 'lease_intent':
                        lease_path, = (probe.paths.vibe / 'leases').glob('*.json')
                        lease = json.loads(lease_path.read_text())
                        lease['intent_digest'] = 'f' * 64
                        lease_path.write_text(json.dumps(lease))
                    elif case == 'confirmation':
                        path = probe.paths.vibe / 'plans' / 'probe-plan' / 'plan-confirmation.json'
                        data = json.loads(path.read_text())
                        data['event_sequence'] += 1
                        path.write_text(json.dumps(data))
                    elif case in ('workflow_auth', 'workflow_token'):
                        path = probe.paths.vibe / 'state.json'
                        data = json.loads(path.read_text())
                        authorization = data['task_workflow']['probe-plan']['node_records']['user_authorization']
                        if case == 'workflow_auth': authorization['input']['authorization_digest'] = 'f' * 64
                        else: authorization['output']['token'] = 'NOT_AUTHORIZED'
                        path.write_text(json.dumps(data))
                    elif case == 'other_artifact':
                        (probe.paths.vibe / 'plans' / 'probe-plan' / 'prd.md').write_text('tampered approved review')
                    elif case == 'trailing_event':
                        (runs[0] / 'events.jsonl').write_text('{invalid}\n')
                    elif case == 'multiple_runs':
                        from vibe_guide.authorization import AuthorizationRecord
                        directory, plan, nodes, card = _load_plan(probe.paths, 'probe-plan')
                        record = AuthorizationRecord.from_dict(json.loads((directory / 'authorization.json').read_text()))
                        (directory / 'plan.json').write_bytes(original_plan)
                        (directory / 'plan-confirmation.json').write_bytes(original_confirmation)
                        extra = Monitor(probe.paths, plan, nodes,
                            topology_rulings=cli_module._observed_topology_rulings(probe.paths, card)).start(record, probe.runner)
                        self.assertNotEqual(extra.run_id, run_id)
                        self.assertEqual(len([p for p in root.iterdir()
                                              if p.is_dir() and (p / 'state.json').exists()]), 2)
                    elif case == 'raw_receipt':
                        folder = probe.store.root / 'native-receipts'
                        folder.mkdir(exist_ok=True)
                        (folder / (action['action_id'] + '-unverified.json')).write_text('{}')
                    elif case == 'orphan_lease':
                        from vibe_guide.state import acquire_writer_lease
                        snapshot = load_snapshot(probe.paths, run_id)
                        other = snapshot.nodes['integration-review']['worktree']
                        self.assertTrue(acquire_writer_lease(probe.paths, 'integration-review', other, run_id,
                            'f' * 64, role='reviewer', generation=1))
                    elif case == 'foreign_request':
                        foreign = copy.deepcopy(action)
                        foreign['run_id'] = 'run-ffffffffffffffffffffffffffffffff'
                        basis = {k: foreign[k] for k in ('operation', 'provider', 'run_id', 'issue_id', 'role', 'generation', 'sequence')}
                        foreign['action_id'] = 'action-' + _canonical_digest(basis)[:32]
                        foreign['request_digest'] = _canonical_digest({k: v for k, v in foreign.items() if k != 'request_digest'})
                        (request_path.parent / (foreign['action_id'] + '.json')).write_text(json.dumps(foreign))
                        protected = probe.store._read_controlled_payload(action)
                        self.assertIsNotNone(protected)
                        protected['action_id'] = foreign['action_id']
                        protected['public_request_digest'] = foreign['request_digest']
                        protected['payload_digest'] = _canonical_digest({k: v for k, v in protected.items() if k != 'payload_digest'})
                        probe.store._atomic(probe.store._create_payload_path(foreign['action_id']), protected)
                    elif case == 'invalid_registry':
                        (runs[0] / 'tasks.json').write_text('{invalid')
                    response = probe.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=probe.runner)
                    if case in ('request_digest', 'request_authorization', 'request_profile', 'lease_intent',
                                'confirmation', 'workflow_auth', 'workflow_token', 'multiple_runs',
                                'raw_receipt', 'orphan_lease', 'foreign_request', 'invalid_registry'):
                        self.assertEqual(response.payload.get('status'), 'blocked_unknown', response.payload)
                    self.assertIsNone(response.payload.get('run_id'), response.payload)
                    self.assertFalse((probe.paths.vibe / 'plans' / 'probe-plan' / 'current-run.json').exists())
                    self.assertEqual(len(list((probe.store.root / 'requests').glob('action-*.json'))),
                                     2 if case == 'foreign_request' else 1)
                finally:
                    probe.doCleanups()

    def test_concurrent_non_strict_public_monitor_has_one_run_and_create(self):
        self.prepare(strict=False)
        entered, other_start, count_lock = Event(), Event(), Lock()
        count = 0
        original = Monitor.start
        def observe_start(monitor, record, runner):
            nonlocal count
            with count_lock:
                count += 1
                first = count == 1
                (entered if first else other_start).set()
            if first:
                other_start.wait(timeout=2)
            return original(monitor, record, runner)
        def invoke():
            return self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        with patch.object(Monitor, 'start', observe_start):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(invoke)
                self.assertTrue(entered.wait(timeout=10))
                second = executor.submit(invoke)
                results = [first.result(timeout=25), second.result(timeout=25)]
        runs = [p.name for p in (self.paths.vibe / 'runs').iterdir()
                if p.is_dir() and (p / 'state.json').exists()]
        self.assertEqual(len(runs), 1, (runs, [r.payload for r in results]))
        self.assertEqual([r.payload.get('run_id') for r in results], [runs[0], runs[0]])
        pointer = json.loads((self.paths.vibe / 'plans' / 'probe-plan' / 'current-run.json').read_text())
        self.assertEqual(pointer['run_id'], runs[0])
        self.assertEqual(len([a for a in self.store.pending(runs[0]) if a['operation'] == 'create']), 1)

    def test_initialization_lock_timeout_retries_same_public_run(self):
        self.prepare()
        entered, release = Event(), Event()
        original = Monitor.start
        def paused_start(monitor, record, runner):
            entered.set()
            if not release.wait(timeout=25):
                raise AssertionError('fixture release missing')
            return original(monitor, record, runner)
        def invoke():
            return self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        with patch.object(Monitor, 'start', paused_start):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(invoke)
                self.assertTrue(entered.wait(timeout=10))
                waiting = executor.submit(invoke)
                try:
                    timed = waiting.result(timeout=18)
                    self.assertEqual(timed.payload.get('status'), 'unknown', timed.payload)
                    self.assertIn('timed out', timed.payload.get('reason', ''))
                    self.assertIsNone(timed.payload.get('run_id'))
                finally:
                    release.set()
                initial = first.result(timeout=10)
        run_id = initial.payload.get('run_id')
        self.assertTrue(run_id, initial.payload)
        retry = invoke()
        self.assertEqual(retry.payload.get('run_id'), run_id, retry.payload)
        runs = [p.name for p in (self.paths.vibe / 'runs').iterdir()
                if p.is_dir() and (p / 'state.json').exists()]
        self.assertEqual(runs, [run_id])
        self.assertEqual(self.store.pending(run_id), [])

    def test_non_strict_early_provider_result_not_silently_claimed(self):
        self.prepare(strict=False)
        from vibe_guide import cli as cli_module
        atomic = cli_module._atomic_json
        def crash(path, payload):
            if path.name == 'current-run.json':
                raise OSError('fixture crash before pointer')
            return atomic(path, payload)
        with patch.object(cli_module, '_atomic_json', crash):
            first = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertNotEqual(first.exit_code, 0)
        run_dir, = [p for p in (self.paths.vibe / 'runs').iterdir() if p.is_dir() and (p / 'state.json').exists()]
        action, = [a for a in self.store.pending(run_dir.name) if a['operation'] == 'create']
        complete_native_mailbox_action(self.paths, run_dir.name, action['action_id'],
            {'structuredContent': {'threadId': 'fixture-existing-task', 'hostId': 'local'}},
            request_digest=action['request_digest'])
        resumed = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertIsNone(resumed.payload.get('run_id'), resumed.payload)
        self.assertFalse((self.paths.vibe / 'plans' / 'probe-plan' / 'current-run.json').exists())
        requests = [self.store.action(p.stem) for p in (self.store.root / 'requests').glob('action-*.json')]
        self.assertEqual(requests, [action])

    def test_initial_recovery_does_not_restore_pointer_after_claim_without_receipt(self):
        self.prepare(strict=False)
        from vibe_guide import cli as cli_module
        atomic = cli_module._atomic_json
        def crash(path, payload):
            if path.name == 'current-run.json':
                raise OSError('fixture crash before pointer')
            return atomic(path, payload)
        with patch.object(cli_module, '_atomic_json', crash):
            self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        run_dir, = [p for p in (self.paths.vibe / 'runs').iterdir() if p.is_dir() and (p / 'state.json').exists()]
        action, = self.store.pending(run_dir.name)
        self.assertEqual(self.store.stage_create(action['action_id'], 'crashed-consumer',
                                                  action['request_digest'])['status'], 'claimed')
        recovered = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertEqual(recovered.payload.get('status'), 'blocked_unknown', recovered.payload)
        self.assertFalse((self.paths.vibe / 'plans' / 'probe-plan' / 'current-run.json').exists())
        self.assertEqual(self.store.pending(run_dir.name), [action])

    def test_strict_pointer_missing_after_create_never_starts_new_run(self):
        with hermetic.override_env(CODEX_THREAD_ID='fixture-supervisor'):
            run_id, _ = self.start_and_register()
            self.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=self.runner)
            before, = [a for a in self.store.pending(run_id) if a['operation'] == 'create']
            pointer = self.paths.vibe / 'plans' / 'probe-plan' / 'current-run.json'
            pointer.unlink()
            result = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
            self.assertIsNone(result.payload.get('run_id'), result.payload)
            self.assertFalse(pointer.exists())
            runs = [p.name for p in (self.paths.vibe / 'runs').iterdir()
                    if p.is_dir() and (p / 'state.json').exists()]
            self.assertEqual(runs, [run_id])
            self.assertEqual([a for a in self.store.pending(run_id) if a['operation'] == 'create'], [before])

    def test_interrupted_recovery_fences_public_resume(self):
        self.prepare(strict=False)
        from vibe_guide import cli as cli_module
        atomic = cli_module._atomic_json
        def crash_pointer(path, payload):
            if path.name == 'current-run.json':
                raise OSError('fixture initial publication crash')
            return atomic(path, payload)
        with patch.object(cli_module, '_atomic_json', crash_pointer):
            self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        run_dir, = [p for p in (self.paths.vibe / 'runs').iterdir() if p.is_dir() and (p / 'state.json').exists()]
        before = self.store.pending(run_dir.name)
        def crash_materialization(*args, **kwargs):
            raise OSError('fixture recovery publication crash')
        with patch.object(cli_module, 'materialize_workflow_evidence', crash_materialization):
            recovering = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertIsNone(recovering.payload.get('run_id'), recovering.payload)
        directory = self.paths.vibe / 'plans' / 'probe-plan'
        self.assertTrue((directory / '.initial-recovery-pending.json').exists())
        blocked = self.cli('resume', '--plan', 'probe-plan', '--run-id', run_dir.name, runner=self.runner)
        self.assertEqual(blocked.payload.get('status'), 'blocked_unknown', blocked.payload)
        _, plan, nodes, _ = _load_plan(self.paths, 'probe-plan')
        monitor = Monitor(self.paths, plan, nodes)
        with self.assertRaises(PermissionError):
            monitor.resume(run_dir.name, self.runner, poll_handles=False)
        with self.assertRaises(PermissionError):
            monitor.tick(run_dir.name, self.runner)
        self.assertEqual(self.store.pending(run_dir.name), before)

    def test_interrupted_recovery_fence_status_is_blocked_unknown(self):
        self.prepare(strict=False)
        from vibe_guide import cli as cli_module
        atomic = cli_module._atomic_json
        def crash(path, payload):
            if path.name == 'current-run.json':
                raise OSError('fixture initial publication crash')
            return atomic(path, payload)
        with patch.object(cli_module, '_atomic_json', crash):
            self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        run_dir, = [p for p in (self.paths.vibe / 'runs').iterdir() if p.is_dir() and (p / 'state.json').exists()]
        def crash_recovery(*args, **kwargs):
            raise OSError('fixture recovery publication crash')
        with patch.object(cli_module, 'materialize_workflow_evidence', crash_recovery):
            self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        status = self.cli('status', '--plan', 'probe-plan', '--run-id', run_dir.name, runner=self.runner)
        self.assertEqual(status.payload.get('status'), 'blocked_unknown', status.payload)

    def test_public_monitor_completes_interrupted_recovery(self):
        self.prepare(strict=False)
        from vibe_guide import cli as cli_module
        atomic = cli_module._atomic_json
        def crash_pointer(path, payload):
            if path.name == 'current-run.json':
                raise OSError('fixture initial publication crash')
            return atomic(path, payload)
        with patch.object(cli_module, '_atomic_json', crash_pointer):
            self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        run_dir, = [p for p in (self.paths.vibe / 'runs').iterdir() if p.is_dir() and (p / 'state.json').exists()]
        action, = self.store.pending(run_dir.name)
        request_path = self.store.root / 'requests' / (action['action_id'] + '.json')
        raw = request_path.read_bytes()
        def crash_recovery(*args, **kwargs):
            raise OSError('fixture recovery publication crash')
        with patch.object(cli_module, 'materialize_workflow_evidence', crash_recovery):
            self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        fence = self.paths.vibe / 'plans' / 'probe-plan' / '.initial-recovery-pending.json'
        self.assertTrue(fence.is_file())
        recovered = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertEqual(recovered.payload.get('run_id'), run_dir.name, recovered.payload)
        self.assertFalse(fence.exists())
        self.assertEqual(request_path.read_bytes(), raw)
        self.assertEqual(self.store.pending(run_dir.name), [action])
        resumed = self.cli('resume', '--plan', 'probe-plan', '--run-id', run_dir.name, runner=self.runner)
        self.assertEqual(resumed.payload.get('run_id'), run_dir.name, resumed.payload)
        self.assertEqual(request_path.read_bytes(), raw)
        self.assertEqual(self.store.pending(run_dir.name), [action])

    def test_create_is_anchored_before_provider_request_is_published(self):
        self.prepare(strict=False)
        from vibe_guide.state import load_events
        original = ProviderActionStore._atomic
        observed = []
        def observe(path, payload):
            if path.parent.name == 'requests' and payload.get('operation') == 'create':
                rows = load_events(self.paths, payload['run_id'])
                anchors = [row for row in rows
                           if row.get('event') == 'initial_create_anchored']
                observed.append((path.exists(), payload, anchors))
            return original(path, payload)
        with patch.object(ProviderActionStore, '_atomic', side_effect=observe):
            started = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertTrue(started.payload.get('run_id'), started.payload)
        self.assertEqual(len(observed), 1)
        visible_before, action, anchors = observed[0]
        self.assertFalse(visible_before)
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]['data'].get('action_id'), action['action_id'])
        self.assertEqual(anchors[0]['data'].get('request_digest'), action['request_digest'])

    def interrupted_initial_recovery(self, crash_stage):
        from vibe_guide import cli as cli_module
        self.prepare(strict=False)
        atomic = cli_module._atomic_json
        def first_crash(path, payload):
            if path.name == 'current-run.json':
                raise OSError('initial pointer crash')
            return atomic(path, payload)
        with patch.object(cli_module, '_atomic_json', first_crash):
            self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        run_dir, = [p for p in (self.paths.vibe / 'runs').iterdir() if p.is_dir() and (p / 'state.json').exists()]
        action, = self.store.pending(run_dir.name)
        directory = self.paths.vibe / 'plans' / 'probe-plan'
        def recovery_crash(path, payload):
            if (crash_stage == 'before_pointer' and path.name == 'current-run.json'):
                raise OSError('recovery pointer crash')
            return atomic(path, payload)
        original_materialize = cli_module.materialize_workflow_evidence
        def crash_workflow(*args, **kwargs):
            if crash_stage == 'before_workflow':
                raise OSError('recovery workflow crash')
            result = original_materialize(*args, **kwargs)
            if crash_stage == 'after_workflow':
                raise OSError('recovery cleanup crash')
            return result
        with patch.object(cli_module, '_atomic_json', recovery_crash), \
                patch.object(cli_module, 'materialize_workflow_evidence', crash_workflow):
            result = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue((directory / '.initial-recovery-pending.json').is_file())
        return run_dir, action, directory

    def test_recovery_fence_crash_boundaries_are_publicly_idempotent(self):
        for stage in ('before_pointer', 'before_workflow', 'after_workflow'):
            with self.subTest(stage=stage):
                probe = StrictFirstOwnerCLI('test_missing_caller_does_not_persist_strict_create')
                probe.setUp()
                try:
                    run, action, directory = probe.interrupted_initial_recovery(stage)
                    request = probe.store.root / 'requests' / (action['action_id'] + '.json')
                    raw = request.read_bytes()
                    status = probe.cli('status', '--plan', 'probe-plan', '--run-id', run.name, runner=probe.runner)
                    self.assertEqual(status.payload.get('status'), 'blocked_unknown', status.payload)
                    result = probe.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=probe.runner)
                    self.assertEqual(result.payload.get('run_id'), run.name, result.payload)
                    self.assertFalse((directory / '.initial-recovery-pending.json').exists())
                    self.assertEqual(request.read_bytes(), raw)
                    self.assertEqual(probe.store.pending(run.name), [action])
                    again = probe.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=probe.runner)
                    self.assertEqual(again.payload.get('run_id'), run.name, again.payload)
                    self.assertEqual(request.read_bytes(), raw)
                    self.assertEqual(probe.store.pending(run.name), [action])
                finally:
                    probe.doCleanups()

    def test_recovery_rejects_tamper_and_legacy_fence_without_replacing_request(self):
        from vibe_guide.adapters.task_provider import _canonical_digest
        cases = ('prompt', 'missing_request', 'missing_anchor', 'forged_anchor', 'fence_run', 'fence_auth',
                 'fence_actions', 'legacy_fence', 'wrong_pointer', 'raw_receipt', 'result', 'observation',
                 'task_registry', 'owner', 'writer_lease')
        for case in cases:
            with self.subTest(case=case):
                probe = StrictFirstOwnerCLI('test_missing_caller_does_not_persist_strict_create')
                probe.setUp()
                try:
                    run, action, directory = probe.interrupted_initial_recovery('before_workflow')
                    request = probe.store.root / 'requests' / (action['action_id'] + '.json')
                    fence = directory / '.initial-recovery-pending.json'
                    fence_data = json.loads(fence.read_text())
                    if case == 'prompt':
                        protected = probe.store._read_controlled_payload(action)
                        self.assertIsNotNone(protected)
                        protected['request']['prompt'] += '\nchanged after publication'
                        protected['payload_digest'] = _canonical_digest({k: v for k, v in protected.items() if k != 'payload_digest'})
                        probe.store._atomic(probe.store._create_payload_path(action['action_id']), protected)
                    elif case == 'missing_request':
                        request.unlink()
                    elif case in ('missing_anchor', 'forged_anchor'):
                        events_path = run / 'events.jsonl'
                        rows = [json.loads(line) for line in events_path.read_text().splitlines()]
                        anchor = next(row for row in rows if row['event'] == 'initial_create_anchored')
                        if case == 'missing_anchor':
                            anchor['event'] = 'model_tier_unresolved'
                        else:
                            anchor['data']['request_digest'] = 'f' * 64
                        previous = None
                        for row in rows:
                            row['previous_event_digest'] = previous
                            row['event_digest'] = _canonical_digest({k: v for k, v in row.items() if k != 'event_digest'})
                            previous = row['event_digest']
                        events_path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
                    elif case in ('fence_run', 'fence_auth', 'fence_actions', 'legacy_fence'):
                        if case == 'fence_run': fence_data['run_id'] = 'run-' + 'f' * 32
                        elif case == 'fence_auth': fence_data['authorization_digest'] = 'f' * 64
                        elif case == 'fence_actions': fence_data['action_ids'] = ['action-' + 'f' * 32]
                        else: fence_data.pop('action_ids')
                        fence.write_text(json.dumps(fence_data))
                    elif case == 'wrong_pointer':
                        (directory / 'current-run.json').write_text(json.dumps({'run_id': 'run-' + 'f' * 32}))
                    elif case in ('raw_receipt', 'result', 'observation'):
                        folder = probe.store.root / {'raw_receipt': 'native-receipts', 'result': 'results', 'observation': 'observations'}[case]
                        folder.mkdir(exist_ok=True)
                        (folder / (action['action_id'] + '-fixture.json')).write_text('{}')
                    elif case == 'task_registry':
                        (run / 'tasks.json').write_text('{invalid')
                    elif case == 'owner':
                        (run / 'supervisor.lease').write_text(json.dumps({'lease_epoch': 1, 'owner': {'session_id': 'foreign'}}))
                    elif case == 'writer_lease':
                        lease, = (probe.paths.vibe / 'leases').glob('*.json')
                        data = json.loads(lease.read_text()); data['intent_digest'] = 'f' * 64
                        lease.write_text(json.dumps(data))
                    before = request.read_bytes() if request.exists() else None
                    saved_fence = fence.read_bytes()
                    result = probe.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=probe.runner)
                    self.assertEqual(result.payload.get('status'), 'blocked_unknown', (case, result.payload))
                    self.assertEqual(fence.read_bytes(), saved_fence)
                    self.assertEqual(request.read_bytes() if request.exists() else None, before)
                    self.assertEqual(len(list((probe.store.root / 'requests').glob('action-*.json'))), 0 if before is None else 1)
                finally:
                    probe.doCleanups()

    def test_create_changed_after_anchor_before_visibility_is_never_published(self):
        self.prepare(strict=False)
        from vibe_guide.adapters.task_provider import _canonical_digest
        original = ProviderActionStore.request
        captured = []
        def tamper(store, **kwargs):
            if kwargs['operation'] == 'create':
                body = dict(kwargs['request']); body['prompt'] += '\nchanged after anchor'
                kwargs['request'] = body
                basis = {k: kwargs[k] for k in ('operation', 'provider', 'run_id', 'issue_id', 'role', 'generation', 'sequence')}
                action_id = 'action-' + _canonical_digest(basis)[:32]
                captured.append((kwargs['run_id'], action_id))
            return original(store, **kwargs)
        with patch.object(ProviderActionStore, 'request', tamper):
            self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        run_id, action_id = captured[0]
        self.assertFalse((self.store.root / 'requests' / (action_id + '.json')).exists())
        self.assertEqual(self.store.pending(run_id), [])

    def test_missing_anchor_never_publishes_initial_create(self):
        self.prepare(strict=False)
        from vibe_guide import state as state_module
        append = state_module.append_event
        def drop_anchor(paths, event, *args, **kwargs):
            if event.event == 'initial_create_anchored':
                return len(state_module.load_events(paths, event.data['run_id']))
            return append(paths, event, *args, **kwargs)
        with patch.object(state_module, 'append_event', drop_anchor):
            started = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertFalse(self.store.pending(started.payload['run_id']))

    def test_recovery_rejects_raw_request_format_change_even_when_json_identity_matches(self):
        run, action, directory = self.interrupted_initial_recovery('before_workflow')
        path = self.store.root / 'requests' / (action['action_id'] + '.json')
        path.write_text(json.dumps(action, ensure_ascii=False, indent=2))
        altered = path.read_bytes()
        result = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertEqual(result.payload.get('status'), 'blocked_unknown', result.payload)
        self.assertEqual(path.read_bytes(), altered)
        self.assertTrue((directory / '.initial-recovery-pending.json').exists())

    def test_public_resume_before_initial_snapshot_save_never_rewrites_original_create(self):
        self.prepare(strict=False)
        original = Monitor._save_snapshot
        failed = []
        def crash_after_request(monitor, snapshot):
            pending = self.store.root / 'requests'
            if not failed and pending.exists() and list(pending.glob('action-*.json')):
                failed.append(True)
                raise OSError('fixture crash after request before snapshot save')
            return original(monitor, snapshot)
        with patch.object(Monitor, '_save_snapshot', crash_after_request):
            first = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertTrue(failed, first.payload)
        run, = [p for p in (self.paths.vibe / 'runs').iterdir() if p.is_dir() and (p / 'state.json').exists()]
        action, = self.store.pending(run.name)
        path = self.store.root / 'requests' / (action['action_id'] + '.json')
        before = path.read_bytes()
        self.cli('resume', '--plan', 'probe-plan', '--run-id', run.name, runner=self.runner)
        from vibe_guide.state import load_events
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.store.pending(run.name), [action])
        self.assertFalse(any(row['event'] == 'create_request_superseded' for row in load_events(self.paths, run.name)))

    def test_mailbox_rejects_foreign_anchor_context_even_with_matching_request_digest(self):
        from vibe_guide.adapters.task_provider import _canonical_digest
        run, action, _ = self.interrupted_initial_recovery('before_workflow')
        fence = self.paths.vibe / 'plans' / 'probe-plan' / '.initial-recovery-pending.json'
        fence.unlink()  # isolated fixture: exercise the final publication boundary
        path = self.store.root / 'requests' / (action['action_id'] + '.json')
        before = path.read_bytes()
        events_path = run / 'events.jsonl'
        rows = [json.loads(line) for line in events_path.read_text().splitlines()]
        anchor = next(row for row in rows if row['event'] == 'initial_create_anchored')
        anchor['data']['authorization_digest'] = 'f' * 64
        previous = None
        for row in rows:
            row['previous_event_digest'] = previous
            row['event_digest'] = _canonical_digest({k: v for k, v in row.items() if k != 'event_digest'})
            previous = row['event_digest']
        events_path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
        kwargs = {k: action[k] for k in ('operation', 'provider', 'run_id', 'issue_id', 'role', 'generation', 'sequence', 'native_tool', 'request')}
        with self.assertRaises((PermissionError, ValueError)):
            self.store.request(**kwargs)
        self.assertEqual(path.read_bytes(), before)

    def test_public_mailbox_does_not_expose_unverified_fenced_create(self):
        run, action, directory = self.interrupted_initial_recovery('before_workflow')
        path = self.store.root / 'requests' / (action['action_id'] + '.json')
        raw = path.read_bytes()
        result = self.cli('supervisor-mailbox', '--run-id', run.name)
        self.assertEqual(result.payload.get('status'), 'blocked_unknown', result.payload)
        self.assertFalse(result.payload.get('pending'), result.payload)
        self.assertTrue((directory / '.initial-recovery-pending.json').exists())
        self.assertEqual(path.read_bytes(), raw)

    def test_missing_published_create_is_not_reissued_by_mailbox(self):
        self.prepare(strict=False)
        started = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        run_id = started.payload['run_id']
        action, = self.store.pending(run_id)
        path = self.store.root / 'requests' / (action['action_id'] + '.json')
        path.unlink()  # isolated fixture: disappearance after potential provider read
        kwargs = {k: action[k] for k in ('operation', 'provider', 'run_id', 'issue_id', 'role', 'generation', 'sequence', 'native_tool', 'request')}
        with self.assertRaises((PermissionError, ValueError)):
            self.store.request(**kwargs)
        self.assertFalse(path.exists())

    def test_recovery_lock_file_cannot_masquerade_as_provider_claim(self):
        run, action, directory = self.interrupted_initial_recovery('before_workflow')
        lock = self.store.root / 'locks' / (action['action_id'] + '.lock')
        lock.unlink()
        from vibe_guide.state import interprocess_lock
        with interprocess_lock(lock):
            pass  # another actor left the same durable mark used for a claim
        request = self.store.root / 'requests' / (action['action_id'] + '.json')
        raw = request.read_bytes()
        result = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertEqual(result.payload.get('status'), 'blocked_unknown', result.payload)
        self.assertTrue((directory / '.initial-recovery-pending.json').exists())
        self.assertEqual(request.read_bytes(), raw)

    def test_idempotent_mailbox_read_preserves_later_recovery_provenance(self):
        run, action, directory = self.interrupted_initial_recovery('before_workflow')
        recovered = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertEqual(recovered.payload.get('run_id'), run.name, recovered.payload)
        kwargs = {k: action[k] for k in ('operation', 'provider', 'run_id', 'issue_id', 'role', 'generation', 'sequence', 'native_tool', 'request')}
        self.assertEqual(self.store.request(**kwargs), action)
        (directory / 'current-run.json').unlink()
        retried = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertEqual(retried.payload.get('run_id'), run.name, retried.payload)
        self.assertEqual(self.store.pending(run.name), [action])

    def test_recovery_does_not_recreate_potentially_visible_missing_request(self):
        run, action, directory = self.interrupted_initial_recovery('before_workflow')
        path = self.store.root / 'requests' / (action['action_id'] + '.json')
        original, captured = path.read_bytes(), []
        path.unlink()
        atomic = ProviderActionStore._atomic
        def observe(target, payload):
            if target == path:
                captured.append(payload)
                raise AssertionError('missing create was republished')
            return atomic(target, payload)
        with patch.object(ProviderActionStore, '_atomic', side_effect=observe):
            result = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertEqual(result.payload.get('status'), 'blocked_unknown', result.payload)
        self.assertFalse(captured)
        self.assertFalse(path.exists())
        self.assertTrue((directory / '.initial-recovery-pending.json').exists())
        self.assertTrue(original)

    def test_legacy_anchored_recovery_is_never_claimed_without_publication_proof(self):
        run, action, directory = self.interrupted_initial_recovery('before_workflow')
        lock = self.store.root / 'locks' / (action['action_id'] + '.lock')
        lock.write_text(json.dumps({'pid': 123, 'owner': 'legacy-unproven'}))
        status = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertEqual(status.payload.get('status'), 'blocked_unknown', status.payload)
        self.assertTrue((directory / '.initial-recovery-pending.json').exists())

    def test_fenced_request_cannot_be_replaced_through_mailbox(self):
        from vibe_guide.state import load_events
        run, action, directory = self.interrupted_initial_recovery('before_workflow')
        path = self.store.root / 'requests' / (action['action_id'] + '.json')
        before = path.read_bytes()
        protected = self.store._read_controlled_payload(action)
        self.assertIsNotNone(protected)
        request = copy.deepcopy(protected['request'])
        request['prompt'] += '\nnew prompt after recovery interruption'
        kwargs = {k: action[k] for k in ('operation', 'provider', 'run_id', 'issue_id', 'role', 'generation', 'sequence', 'native_tool')}
        try:
            self.store.request(**kwargs, request=request)
        except (PermissionError, ValueError):
            pass
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(any(row['event'] == 'create_request_superseded' for row in load_events(self.paths, run.name)))
        self.assertTrue((directory / '.initial-recovery-pending.json').is_file())

    def test_mailbox_replacement_racing_recovery_keeps_original_request(self):
        from vibe_guide import cli as cli_module
        run, action, directory = self.interrupted_initial_recovery('before_pointer')
        path = self.store.root / 'requests' / (action['action_id'] + '.json')
        before = path.read_bytes()
        protected = self.store._read_controlled_payload(action)
        self.assertIsNotNone(protected)
        request = copy.deepcopy(protected['request'])
        request['prompt'] += '\nracing replacement'
        kwargs = {k: action[k] for k in ('operation', 'provider', 'run_id', 'issue_id', 'role', 'generation', 'sequence', 'native_tool')}
        entered, release, attempted = Event(), Event(), Event()
        atomic = cli_module._atomic_json
        def pause_pointer(target, payload):
            if target.name == 'current-run.json':
                entered.set()
                if not release.wait(timeout=10):
                    raise AssertionError('fixture release missing')
            return atomic(target, payload)
        def replace():
            attempted.set()
            try:
                self.store.request(**kwargs, request=request)
            except (PermissionError, ValueError):
                return 'rejected'
            return 'replaced'
        with patch.object(cli_module, '_atomic_json', pause_pointer):
            with ThreadPoolExecutor(max_workers=2) as executor:
                recovering = executor.submit(self.cli, 'monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
                self.assertTrue(entered.wait(timeout=10))
                other = executor.submit(replace)
                self.assertTrue(attempted.wait(timeout=10))
                release.set()
                outcome = other.result(timeout=20)
                recovered = recovering.result(timeout=20)
        self.assertEqual(outcome, 'rejected')
        self.assertEqual(recovered.payload.get('run_id'), run.name, recovered.payload)
        self.assertFalse((directory / '.initial-recovery-pending.json').exists())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.store.pending(run.name), [action])

    def test_provider_receipt_racing_recovery_is_not_lost_or_redispatched(self):
        self.prepare(strict=False)
        from vibe_guide import cli as cli_module
        atomic = cli_module._atomic_json
        def crash(path, payload):
            if path.name == 'current-run.json':
                raise OSError('fixture initial publication crash')
            return atomic(path, payload)
        with patch.object(cli_module, '_atomic_json', crash):
            self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        run_dir, = [p for p in (self.paths.vibe / 'runs').iterdir() if p.is_dir() and (p / 'state.json').exists()]
        action, = self.store.pending(run_dir.name)
        reached, proceed, attempted = Event(), Event(), Event()
        def observe_pointer(path, payload):
            if path.name == 'current-run.json':
                reached.set()
                if not proceed.wait(timeout=10):
                    raise AssertionError('fixture recovery not released')
            return atomic(path, payload)
        def recover():
            return self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        def complete():
            attempted.set()
            return complete_native_mailbox_action(self.paths, run_dir.name, action['action_id'],
                {'structuredContent': {'threadId': 'fixture-racing-task', 'hostId': 'local'}},
                request_digest=action['request_digest'])
        with patch.object(cli_module, '_atomic_json', observe_pointer):
            with ThreadPoolExecutor(max_workers=2) as executor:
                recovery = executor.submit(recover)
                self.assertTrue(reached.wait(timeout=10))
                callback = executor.submit(complete)
                self.assertTrue(attempted.wait(timeout=10))
                proceed.set()
                result = recovery.result(timeout=20)
                self.assertTrue(callback.result(timeout=20))
        self.assertEqual(result.payload.get('run_id'), run_dir.name, result.payload)
        self.assertEqual(self.store.action(action['action_id']), action)
        self.assertIsNotNone(self.store.result(action['action_id']))
        requests = [self.store.action(p.stem) for p in (self.store.root / 'requests').glob('action-*.json')]
        self.assertEqual(requests, [action])

    def test_unknown_orphan_run_blocks_another_initial_run(self):
        self.prepare()
        from vibe_guide import cli as cli_module
        atomic = cli_module._atomic_json
        def crash_before_pointer(path, payload):
            if path.name == 'current-run.json':
                raise OSError('fixture crash before run pointer')
            return atomic(path, payload)
        with patch.object(cli_module, '_atomic_json', side_effect=crash_before_pointer):
            self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        run_dirs = [p for p in (self.paths.vibe / 'runs').iterdir() if p.is_dir() and (p / 'state.json').exists()]
        self.assertEqual(len(run_dirs), 1)
        # The runner cannot prove this run is pristine once its event ledger is unreadable.
        (run_dirs[0] / 'events.jsonl').write_text('{bad\n')
        again = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
        self.assertIsNone(again.payload.get('run_id'), again.payload)
        self.assertEqual(len([p for p in (self.paths.vibe / 'runs').iterdir()
                              if p.is_dir() and (p / 'state.json').exists()]), 1)

    def test_bad_registration_never_claims_first_lease(self):
        for case in ('missing_caller', 'wrong_caller', 'wrong_host', 'wrong_provider', 'wrong_journal'):
            with self.subTest(case=case):
                probe = StrictFirstOwnerCLI('test_missing_caller_does_not_persist_strict_create')
                probe.setUp()
                try:
                    with hermetic.override_env(CODEX_THREAD_ID='fixture-supervisor'):
                        probe.prepare()
                        journal = probe.journal()
                        started = probe.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=probe.runner)
                        run_id = started.payload['run_id']
                        supplied = probe.root / 'untrusted.jsonl' if case == 'wrong_journal' else journal
                        if case == 'wrong_journal': supplied.write_bytes(journal.read_bytes())
                        caller = None if case == 'missing_caller' else ('foreign' if case == 'wrong_caller' else 'fixture-supervisor')
                        provider = 'workbuddy' if case == 'wrong_provider' else 'codex'
                        host = 'foreign' if case == 'wrong_host' else 'local'
                        with hermetic.override_env(CODEX_THREAD_ID=caller):
                            result = probe.cli('supervisor-register', '--run-id', run_id, '--provider', provider,
                                '--session-id', 'fixture-supervisor', '--host', host, '--session-record', str(supplied))
                        self.assertNotEqual(result.payload.get('status'), 'ok', result.payload)
                        self.assertFalse((probe.paths.vibe / 'runs' / run_id / 'supervisor.lease').exists())
                        self.assertEqual(probe.store.pending(run_id), [])
                finally:
                    probe.doCleanups()

    def test_first_registration_still_validates_after_snapshot_reload(self):
        with hermetic.override_env(CODEX_THREAD_ID='fixture-supervisor'):
            self.prepare()
            self.journal()
            started = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
            run_id = started.payload['run_id']
            snapshot = load_snapshot(self.paths, run_id)
            self.assertEqual(snapshot.nodes['probe-node-a']['reason_code'], 'initial_supervisor_registration_required')
            result = self.cli('supervisor-register', '--run-id', run_id, '--provider', 'codex',
                '--session-id', 'fixture-supervisor', '--host', 'local', '--session-record', str(self.root / 'wrong'))
            self.assertNotEqual(result.payload.get('status'), 'ok', result.payload)
            self.assertFalse((self.paths.vibe / 'runs' / run_id / 'supervisor.lease').exists())

    def test_wrong_caller_can_retry_after_correct_caller_returns(self):
        with hermetic.override_env(CODEX_THREAD_ID='fixture-supervisor'):
            run_id, _ = self.start_and_register()
            with hermetic.override_env(CODEX_THREAD_ID='foreign'):
                self.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=self.runner)
            self.assertFalse([a for a in self.store.pending(run_id) if a['operation'] == 'create'])
            for _ in range(3):
                self.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=self.runner)
            self.assertEqual(len([a for a in self.store.pending(run_id) if a['operation'] == 'create']), 1)

    def test_stale_cas_cannot_replace_current_owner(self):
        with hermetic.override_env(CODEX_THREAD_ID='fixture-supervisor'):
            run_id, _ = self.start_and_register()
            other = self.journal('other-supervisor')
            with self.assertRaises(SupervisorLeaseConflict):
                register_supervisor_address(self.paths, run_id,
                    {'provider': 'codex', 'session_id': 'other-supervisor', 'host': 'local'},
                    session_record=str(other), expected_epoch=0)
            self.assertFalse([a for a in self.store.pending(run_id) if a['operation'] == 'create'])
            self.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=self.runner)
            creates = [a for a in self.store.pending(run_id) if a['operation'] == 'create']
            self.assertEqual(len(creates), 1)
            self.assertEqual(creates[0]['request']['initial_binding_source']['owner']['owner']['session_id'],
                             'fixture-supervisor')

    def test_owner_change_never_rewrites_existing_initial_source(self):
        with hermetic.override_env(CODEX_THREAD_ID='fixture-supervisor'):
            run_id, _ = self.start_and_register()
            self.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=self.runner)
            first, = [a for a in self.store.pending(run_id) if a['operation'] == 'create']
            other = self.journal('other-supervisor')
            with hermetic.override_env(CODEX_THREAD_ID='other-supervisor'):
                result = self.cli('supervisor-register', '--run-id', run_id, '--provider', 'codex',
                    '--session-id', 'other-supervisor', '--host', 'local', '--session-record', str(other))
                self.assertEqual(result.payload['status'], 'ok', result.payload)
                self.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=self.runner)
            self.assertEqual([a for a in self.store.pending(run_id) if a['operation'] == 'create'], [first])

    def test_stale_heartbeat_alone_does_not_expire_registered_owner(self):
        with hermetic.override_env(CODEX_THREAD_ID='fixture-supervisor'):
            run_id, _ = self.start_and_register()
            path = self.paths.vibe / 'runs' / run_id / 'supervisor.lease'
            lease = json.loads(path.read_text())
            lease['heartbeat'] = time.time() - 3600
            path.write_text(json.dumps(lease))
            self.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=self.runner)
            creates = [a for a in self.store.pending(run_id) if a['operation'] == 'create']
            self.assertEqual(len(creates), 1)
            self.assertEqual(creates[0]['request']['initial_binding_source']['owner']['lease_epoch'], lease['lease_epoch'])
            self.assertEqual(creates[0]['request']['initial_binding_source']['owner']['owner'], lease['owner'])

    def test_crash_after_create_write_keeps_immutable_request(self):
        with hermetic.override_env(CODEX_THREAD_ID='fixture-supervisor'):
            run_id, _ = self.start_and_register()
            atomic = ProviderActionStore._atomic
            interrupted = []
            def crash_after_create(path, payload):
                atomic(path, payload)
                if path.parent.name == 'requests' and payload.get('operation') == 'create' and not interrupted:
                    interrupted.append(True)
                    raise OSError('fixture crash after durable create')
            with patch.object(ProviderActionStore, '_atomic', side_effect=crash_after_create):
                self.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=self.runner)
            self.assertTrue(interrupted)
            first, = [a for a in self.store.pending(run_id) if a['operation'] == 'create']
            for _ in range(3):
                self.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=self.runner)
            self.assertEqual([a for a in self.store.pending(run_id) if a['operation'] == 'create'], [first])


    def test_reviewer_first_create_uses_same_owner_gate(self):
        # A delivered-node fixture enters the real public scheduler's reviewer
        # branch without pretending to prove developer delivery or real App I/O.
        with hermetic.override_env(CODEX_THREAD_ID='fixture-supervisor'):
            run_id, _ = self.start_and_register()
            snapshot = load_snapshot(self.paths, run_id)
            snapshot.nodes['probe-node-a']['status'] = 'delivered'
            _, plan, nodes, _ = _load_plan(self.paths, 'probe-plan')
            monitor = Monitor(self.paths, plan, nodes)
            monitor._record_topology_projection(snapshot, 'monitor.fixture')
            save_snapshot(self.paths, snapshot)
            resumed = self.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=self.runner)
            creates = [a for a in self.store.pending(run_id) if a['operation'] == 'create']
            self.assertEqual(len(creates), 1, (resumed.payload, load_snapshot(self.paths, run_id).nodes))
            self.assertEqual(creates[0]['role'], 'reviewer')
            self.assertIs(creates[0]['request']['business_write_allowed'], False)
            self.assertIn('不得写入任何文件', creates[0]['request']['prompt'])
            self.assertEqual(creates[0]['request']['initial_binding_source']['owner']['owner']['session_id'],
                             'fixture-supervisor')

    def clone_writer(self):
        writer = self.root / '.worktrees' / 'fixture'
        writer.parent.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1]
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_OPTIONAL_LOCKS='0', GIT_ALLOW_PROTOCOL='file', GIT_TERMINAL_PROMPT='0')
        def git(*args, cwd=None):
            return subprocess.run(['git', *args], cwd=cwd, env=env, text=True,
                                  capture_output=True, check=True).stdout.strip()
        git('clone', '--no-hardlinks', '--local', '--single-branch', '--branch',
            'fix/v528-r4-preconditions', str(source), str(writer))
        git('remote', 'remove', 'origin', cwd=writer)
        self.assertEqual(git('rev-parse', 'HEAD', cwd=writer), '78ee5bb17374438d5e87a016353f0f180aeffd11')
        self.assertEqual(git('symbolic-ref', '--short', 'HEAD', cwd=writer), 'fix/v528-r4-preconditions')
        self.assertEqual(git('status', '--porcelain', '--untracked-files=all', cwd=writer), '')
        self.assertEqual(git('remote', cwd=writer), '')
        common = Path(git('rev-parse', '--git-common-dir', cwd=writer))
        self.assertEqual((common if common.is_absolute() else writer / common).resolve(), (writer / '.git').resolve())
        self.assertFalse((writer / '.git' / 'objects' / 'info' / 'alternates').exists())
        objects = [p for p in (writer / '.git' / 'objects').rglob('*') if p.is_file()]
        self.assertTrue(objects)
        self.assertTrue(all(p.stat().st_nlink == 1 for p in objects))
        return writer, git

    def exercise_public_binding(self, role, relative_lease=False):
        with hermetic.override_env(CODEX_THREAD_ID='fixture-supervisor'):
            writer, git = self.clone_writer()
            self.prepare(writer=writer)
            record = self.journal()
            started = self.cli('monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE', runner=self.runner)
            run_id = started.payload['run_id']
            self.assertEqual(self.store.pending(run_id), [])
            if relative_lease:
                snapshot = load_snapshot(self.paths, run_id)
                snapshot.nodes['probe-node-a']['worktree'] = str(writer.relative_to(self.root))
                save_snapshot(self.paths, snapshot)
            registered = self.cli('supervisor-register', '--run-id', run_id, '--provider', 'codex',
                '--session-id', 'fixture-supervisor', '--host', 'local', '--session-record', str(record))
            self.assertEqual(registered.payload['status'], 'ok', registered.payload)
            if role == 'reviewer':
                # Isolated delivered fixture exercises the reviewer scheduler; it
                # does not claim developer delivery or reviewer business acceptance.
                snapshot = load_snapshot(self.paths, run_id)
                snapshot.nodes['probe-node-a']['status'] = 'delivered'
                _, plan, nodes, _ = _load_plan(self.paths, 'probe-plan')
                Monitor(self.paths, plan, nodes)._record_topology_projection(snapshot, 'monitor.fixture')
                save_snapshot(self.paths, snapshot)
            self.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=self.runner)
            poll = {'timedOut': True, 'polls': [{'thread': {'id': 'fixture-task', 'hostId': 'local'},
                                                  'cursor': 'fixture-cursor'}]}
            responses = {'create': {'threadId': 'fixture-task', 'hostId': 'local'},
                         'wait': poll, 'locate': {'navigated': True}, 'visibility': poll}
            for operation in ('create', 'wait', 'locate', 'visibility'):
                actions = [a for a in self.store.pending(run_id) if a['operation'] == operation]
                self.assertEqual(len(actions), 1, (operation, self.store.pending(run_id),
                    load_snapshot(self.paths, run_id).nodes['probe-node-a']))
                action = actions[0]
                self.assertEqual(action['role'], role)
                if operation == 'create':
                    source = action['request']['initial_binding_source']
                    self.assertIs(action['request']['business_write_allowed'], False)
                    self.assertIn('不得写入任何文件', action['request']['prompt'])
                    from vibe_guide.supervisor import SupervisorLease, _read_lease_record
                    owner_record = _read_lease_record(SupervisorLease(self.paths).path(run_id))
                    self.assertEqual(source['owner'], {k: owner_record[k] for k in ('run_id', 'lease_epoch', 'owner', 'released')})
                    self.assertEqual(source['writer_lease']['record']['run_id'], run_id)
                    self.assertEqual(source['writer_lease']['record']['status'], 'active')
                complete_native_mailbox_action(self.paths, run_id, action['action_id'],
                    {'structuredContent': responses[operation]}, request_digest=action['request_digest'])
                self.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=self.runner)
            actions = [a for a in self.store.pending(run_id) if a['operation'] == 'resume']
            self.assertEqual(len(actions), 1, load_snapshot(self.paths, run_id).nodes['probe-node-a'])
            self.assertEqual(actions[0]['request']['threadId'], 'fixture-task')
            self.assertEqual(actions[0]['request']['hostId'], 'local')
            self.assertEqual(load_task_binding(self.paths, 'probe-node-a', role, run_id=run_id).task_id, 'fixture-task')
            before = actions[0]
            self.cli('resume', '--plan', 'probe-plan', '--run-id', run_id, runner=self.runner)
            self.assertEqual([a for a in self.store.pending(run_id) if a['operation'] == 'resume'], [before])
            all_requests = [self.store._read(p) for p in self.store._directory('requests').glob('*.json')]
            self.assertEqual(len([a for a in all_requests if a['role'] == role and a['operation'] == 'create']), 1)
            self.assertEqual(len([a for a in all_requests if a['role'] == role and a['operation'] == 'resume']), 1)
            self.assertEqual(git('status', '--porcelain', '--untracked-files=all', cwd=writer), '')
            self.assertEqual(git('rev-parse', 'HEAD', cwd=writer), '78ee5bb17374438d5e87a016353f0f180aeffd11')
            self.assertEqual(git('symbolic-ref', '--short', 'HEAD', cwd=writer), 'fix/v528-r4-preconditions')

    def test_public_developer_binding_resumes_original_task(self):
        self.exercise_public_binding('developer')

    def test_public_reviewer_binding_resumes_original_task(self):
        self.exercise_public_binding('reviewer')

    def test_public_binding_with_relative_writer_lease(self):
        self.exercise_public_binding('developer', relative_lease=True)
