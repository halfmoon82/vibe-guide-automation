"""Recover an already-created visible task through the public reconcile API."""
import copy
import hashlib
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support_v45_authorize import publish_complex_probe
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.cli import _load_plan, _public_runner, run_cli
from vibe_guide.models import WorkerProfile
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import load_snapshot, save_snapshot
from vibe_guide.task_registry import TaskBinding, load_task_binding, save_task_binding


class SuccessorReconcileTests(unittest.TestCase):
    def setUp(self):
        self.root = publish_complex_probe(self)
        self.paths = ProjectPaths(self.root)
        run_cli(['authorize', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE'], self.root)
        start = run_cli(['monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE'], self.root)
        self.run_id = start.payload['run_id']
        self.snapshot = load_snapshot(self.paths, self.run_id)
        self.directory, self.plan, self.nodes, card = _load_plan(self.paths, 'probe-plan')
        self.runner = _public_runner(self.paths, card, self.nodes)
        self.node_id = 'probe-node-a'
        node = self.snapshot.nodes[self.node_id]
        self.writer = self.root / node['worktree']
        self.writer.mkdir(parents=True, exist_ok=True)
        self.git('init', '-b', node['branch'])
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                 'commit', '--allow-empty', '-m', 'fixture baseline')
        old = TaskBinding(provider=self.runner.provider, mode='visible',
            issue_id=self.node_id, role='reviewer', task_id='old-reviewer', host='local',
            run_id=self.run_id, worktree=node['worktree'], branch=node['branch'],
            generation=3, status='stopped', cursor='old:9', allowlist=['README.md'])
        save_task_binding(self.paths, old)
        node.update(status='blocked_unknown', active_task=None, start_intent=None,
            reviewer_identity=old.task_id, review_generation=3,
            delivery_evidence={'completion_marker': 'DEVELOPER_DONE'})
        self.snapshot.handles.pop(self.node_id, None)
        save_snapshot(self.paths, self.snapshot)
        self.before = copy.deepcopy(self.snapshot)
        profile = WorkerProfile('fixture', 'fixture-model', 'high', [], {},
            worktree=str(self.writer), branch=old.branch, allowlist=old.allowlist)
        store = ProviderActionStore(self.paths)
        basis = dict(provider=old.provider, run_id=self.run_id, issue_id=self.node_id,
                     role='reviewer', generation=3, sequence=77, native_tool='fixture')
        contract = {'authorization_digest': self.snapshot.authorization_digest,
                    'plan_id': self.plan.plan_id, 'plan_version': self.plan.version}
        with patch('vibe_guide.adapters.task_provider.require_entry'):
            create = store.request(operation='create', request={**contract, 'successor': True,
                'predecessor_task_id': old.task_id, 'worker_profile': profile.to_dict()}, **basis)
            store.complete(create['action_id'], {'binding': {'task_id': 'new-reviewer', 'host': 'local'}})
            for operation, request, result in (
                ('locate', {'threadId': 'new-reviewer'}, {'located': True}),
                ('visibility', {'targets': [{'threadId': 'new-reviewer', 'hostId': 'local'}]},
                 {'visible': True, 'direct_enter': True}),
            ):
                action = store.request(operation=operation, request={**contract, **request}, **basis)
                store.complete(action['action_id'], result)
        preservation = self.root / '.vibe' / 'preservation.json'
        preservation.write_text(json.dumps({'run_id': self.run_id,
            'authorization_digest': self.snapshot.authorization_digest,
            'frozen_head': self.git('rev-parse', 'HEAD'), 'branch': old.branch,
            'worktree': old.worktree, 'allowlist': old.allowlist,
            'reviewer_binding': {'task_id': old.task_id},
            'cumulative_diff': {'stdout': ''}, 'untracked_hashes': {}}))
        self.package = {'kind': 'created_successor', 'node_id': self.node_id,
            'action_id': create['action_id'], 'request_digest': create['request_digest'],
            'predecessor_archive': {'threadId': old.task_id, 'archived': True},
            'preservation_path': str(preservation),
            'preservation_digest': hashlib.sha256(preservation.read_bytes()).hexdigest()}
        self.evidence = self.root / '.vibe' / 'reconcile.json'

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.writer), *args],
            check=True, capture_output=True, text=True).stdout.strip()

    def reconcile(self):
        self.evidence.write_text(json.dumps(self.package))
        return run_cli(['reconcile', '--plan', 'probe-plan', '--run-id', self.run_id,
            '--evidence', '.vibe/reconcile.json', '--json'], self.root)

    def test_public_entry_registers_existing_task_and_fresh_monitor_replays(self):
        result = self.reconcile()
        self.assertIn('nodes', result.payload, result.payload)
        bound = load_task_binding(self.paths, self.node_id, 'reviewer', run_id=self.run_id)
        self.assertEqual(bound.task_id, 'new-reviewer')
        self.assertFalse(bound.business_write_allowed)
        current = load_snapshot(self.paths, self.run_id)
        self.assertEqual(current.nodes[self.node_id]['reviewer_identity'], 'new-reviewer')
        self.assertTrue(current.nodes[self.node_id]['retryable_action']['same_task'])
        fresh = Monitor(self.paths, self.plan, self.nodes)
        fresh._reconcile_unapplied_events(self.before)
        self.assertEqual(self.before.nodes[self.node_id]['reviewer_identity'], 'new-reviewer')
        self.assertNotIn(self.node_id, self.before.handles)
        store = ProviderActionStore(self.paths)
        before = {p.name for p in (store.root / 'requests').glob('*.json')}
        resumed = fresh.resume(self.run_id, self.runner)
        additional = [store.action(p.stem) for p in (store.root / 'requests').glob('*.json')
                      if p.name not in before]
        self.assertFalse(any(a['operation'] == 'create' for a in additional))
        self.assertTrue(all(a['generation'] == 3 for a in additional))
        self.assertEqual(resumed.nodes[self.node_id]['review_generation'], 3)
        self.assertTrue(resumed.nodes[self.node_id]['retryable_action']['same_task'])
        self.assertEqual(load_task_binding(self.paths, self.node_id, 'reviewer',
            run_id=self.run_id).task_id, 'new-reviewer')

    def test_foreign_legacy_request_does_not_block_current_successor(self):
        store = ProviderActionStore(self.paths)
        path = store._directory('requests') / ('action-' + 'f' * 32 + '.json')
        legacy = {'run_id': 'run-old', 'issue_id': 'old-node', 'role': 'developer',
                  'generation': 1, 'action_id': path.stem, 'request_digest': 'old'}
        path.write_text(json.dumps(legacy))
        before = path.read_bytes()
        result = self.reconcile()
        self.assertIn('nodes', result.payload, result.payload)
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_current_scope_request_still_blocks_successor(self):
        store = ProviderActionStore(self.paths)
        path = store._directory('requests') / ('action-' + 'f' * 32 + '.json')
        path.write_text(json.dumps({'run_id': self.run_id, 'issue_id': self.node_id,
            'role': 'reviewer', 'generation': 3, 'action_id': path.stem,
            'request_digest': 'invalid'}))
        result = self.reconcile()
        self.assertIn('identity', result.payload.get('reason', ''))
        self.assertEqual(load_task_binding(self.paths, self.node_id, 'reviewer',
            run_id=self.run_id).task_id, 'old-reviewer')

    def test_unreadable_request_scope_cannot_be_treated_as_foreign(self):
        store = ProviderActionStore(self.paths)
        path = store._directory('requests') / ('action-' + 'f' * 32 + '.json')
        valid_scope = {'run_id': self.run_id, 'issue_id': self.node_id,
                       'role': 'reviewer', 'generation': 3}
        for field, value in [('run_id', None), ('issue_id', ''), ('role', []),
                             ('generation', '3'), ('generation', True), ('generation', -1)]:
            with self.subTest(field=field, value=value):
                path.write_text(json.dumps({**valid_scope, field: value}))
                before = path.read_bytes()
                result = self.reconcile()
                self.assertEqual(path.read_bytes(), before)
                self.assertIn('scope', result.payload.get('reason', ''))
                self.assertEqual(load_task_binding(self.paths, self.node_id, 'reviewer',
                    run_id=self.run_id).task_id, 'old-reviewer')

    def test_unverified_archive_preserves_original_binding(self):
        self.package['predecessor_archive']['archived'] = False
        result = self.reconcile()
        self.assertIn('archive', result.payload.get('reason', ''), result.payload)
        self.assertEqual(load_task_binding(self.paths, self.node_id, 'reviewer',
            run_id=self.run_id).task_id, 'old-reviewer')

    def test_successful_reconciliation_revalidates_unchanged_authorization(self):
        marker = self.directory / 'authorization-invalidated.json'
        raw = json.dumps({'status': 'blocked_design', 'reason': 'old projection drift',
                          'change_reason': 'executable_contract_changed'}).encode()
        marker.write_bytes(raw)
        self.reconcile()
        self.assertFalse(marker.exists())
        preserved = list((self.paths.vibe / 'runs' / self.run_id / 'recovery').glob('*.json'))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_bytes(), raw)
        current = load_snapshot(self.paths, self.run_id)
        self.assertEqual(current.authorization_digest, self.snapshot.authorization_digest)

    def test_failed_reconciliation_does_not_clear_invalidation(self):
        marker = self.directory / 'authorization-invalidated.json'
        raw = json.dumps({'status': 'blocked_design', 'reason': 'old projection drift',
                          'change_reason': 'executable_contract_changed'}).encode()
        marker.write_bytes(raw)
        self.package['predecessor_archive']['archived'] = False
        self.reconcile()
        self.assertEqual(marker.read_bytes(), raw)

    def test_malformed_preservation_returns_structured_rejection(self):
        path = Path(self.package['preservation_path'])
        original = json.loads(path.read_text())
        for malformed in ([], {**original, 'reviewer_binding': []},
                          {**original, 'cumulative_diff': []}):
            with self.subTest(malformed=malformed):
                path.write_text(json.dumps(malformed))
                self.package['preservation_digest'] = hashlib.sha256(path.read_bytes()).hexdigest()
                result = self.reconcile()
                self.assertIn('preservation', result.payload.get('reason', ''), result.payload)
                self.assertEqual(load_task_binding(self.paths, self.node_id, 'reviewer',
                    run_id=self.run_id).task_id, 'old-reviewer')

    def archive_developer_delivery(self, *, authorization=None, target='developer-task'):
        current = self.snapshot.nodes[self.node_id]
        developer = TaskBinding(provider=self.runner.provider, mode='visible', issue_id=self.node_id,
            role='developer', task_id='developer-task', host='local', run_id=self.run_id,
            worktree=current['worktree'], branch=current['branch'], generation=1,
            status='delivered', cursor='dev:5', allowlist=['README.md'])
        save_task_binding(self.paths, developer)
        current.update(developer_identity=developer.task_id, developer_generation=1, delivery_evidence=None)
        save_snapshot(self.paths, self.snapshot)
        self.before = copy.deepcopy(self.snapshot)
        (self.writer/'README.md').write_text('delivered artifact')
        store = ProviderActionStore(self.paths)
        with patch('vibe_guide.adapters.task_provider.require_entry'):
            store.request(operation='wait', provider=developer.provider, run_id=self.run_id,
                issue_id=self.node_id, role='developer', generation=1, native_tool='fixture',
                request={'authorization_digest': authorization or self.snapshot.authorization_digest,
                         'plan_id':self.plan.plan_id,'plan_version':self.plan.version,
                         'targets':[{'threadId':target,'hostId':'local'}]})
        store.record_worker_delivery(self.run_id,self.node_id,'developer',{'event':'delivered',
            'delivery_evidence':{'completion_marker':'DEVELOPER_DONE','delivery_path':'README.md',
                                 'thread_status':'complete'}},1)
        path=Path(self.package['preservation_path']);preserved=json.loads(path.read_text())
        preserved['untracked_hashes']={'README.md':hashlib.sha256((self.writer/'README.md').read_bytes()).hexdigest()}
        path.write_text(json.dumps(preserved));self.package['preservation_digest']=hashlib.sha256(path.read_bytes()).hexdigest()
        return store

    def test_missing_projection_recovers_current_consumed_developer_delivery_and_replays(self):
        self.archive_developer_delivery()
        result=self.reconcile()
        self.assertIn('nodes',result.payload,result.payload)
        current=load_snapshot(self.paths,self.run_id)
        self.assertEqual(current.nodes[self.node_id]['delivery_evidence']['completion_marker'],'DEVELOPER_DONE')
        fresh=Monitor(self.paths,self.plan,self.nodes)
        fresh._reconcile_unapplied_events(self.before)
        self.assertEqual(self.before.nodes[self.node_id]['delivery_evidence'],current.nodes[self.node_id]['delivery_evidence'])
        self.assertEqual(current.nodes[self.node_id]['developer_generation'],1)
        self.assertEqual(current.nodes[self.node_id]['review_generation'],3)

    def test_delivery_recovery_rejects_wrong_authorization(self):
        self.archive_developer_delivery(authorization='b'*64)
        result=self.reconcile()
        self.assertIn('delivery',result.payload.get('reason',''),result.payload)
        self.assertEqual(load_task_binding(self.paths,self.node_id,'reviewer',run_id=self.run_id).task_id,'old-reviewer')

    def test_delivery_recovery_replays_after_normal_pair_archive(self):
        self.archive_developer_delivery()
        self.assertIn('nodes', self.reconcile().payload)
        current = load_snapshot(self.paths, self.run_id)
        current.nodes[self.node_id]['review_clearance'] = {'p0': 0, 'p1': 0, 'p2': 0}
        Monitor(self.paths, self.plan, self.nodes)._archive_pair(current, self.node_id)
        Monitor(self.paths, self.plan, self.nodes)._reconcile_unapplied_events(self.before)
        self.assertTrue(self.before.nodes[self.node_id]['pair_archived'])
        self.assertEqual(self.before.nodes[self.node_id]['delivery_evidence'],
                         current.nodes[self.node_id]['delivery_evidence'])

    def test_delivery_recovery_replay_rejects_archive_without_event(self):
        self.archive_developer_delivery()
        self.assertIn('nodes', self.reconcile().payload)
        binding = load_task_binding(self.paths, self.node_id, 'developer', run_id=self.run_id)
        binding.status = 'archived'
        save_task_binding(self.paths, binding)
        with self.assertRaisesRegex(ValueError, 'delivery binding'):
            Monitor(self.paths, self.plan, self.nodes)._reconcile_unapplied_events(self.before)

    def test_initial_delivery_recovery_rejects_archived_binding(self):
        self.archive_developer_delivery()
        current = load_snapshot(self.paths, self.run_id)
        current.nodes[self.node_id]['delivery_evidence'] = {
            'completion_marker': 'DEVELOPER_DONE',
            'delivery_path': 'README.md',
            'thread_status': 'complete',
        }
        save_snapshot(self.paths, current)
        binding = load_task_binding(self.paths, self.node_id, 'developer', run_id=self.run_id)
        binding.status = 'archived'
        save_task_binding(self.paths, binding)
        self.assertIn('delivery', self.reconcile().payload.get('reason', ''))

    def test_missing_developer_binding_with_nonzero_generation_is_structured(self):
        current = load_snapshot(self.paths, self.run_id)
        current.nodes[self.node_id].update(
            developer_identity='missing-developer',
            developer_generation=1,
            delivery_evidence=None,
        )
        save_snapshot(self.paths, current)
        result = self.reconcile()
        self.assertIn('delivery binding', result.payload.get('reason', ''))

    def test_delivery_recovery_rejects_wrong_target(self):
        self.archive_developer_delivery(target='foreign-task')
        result=self.reconcile()
        self.assertIn('delivery',result.payload.get('reason',''),result.payload)
        self.assertEqual(load_task_binding(self.paths,self.node_id,'reviewer',run_id=self.run_id).task_id,'old-reviewer')

    def test_unconsumed_delivery_cannot_restore_snapshot_projection(self):
        store=self.archive_developer_delivery()
        path=store.root/'deliveries'/f'{self.run_id}-{self.node_id}-developer-g1.json'
        data=json.loads(path.read_text());data['consumed_by']=None;path.write_text(json.dumps(data))
        result=self.reconcile()
        self.assertIn('delivery',result.payload.get('reason',''),result.payload)
        self.assertIsNone(load_snapshot(self.paths,self.run_id).nodes[self.node_id]['delivery_evidence'])
