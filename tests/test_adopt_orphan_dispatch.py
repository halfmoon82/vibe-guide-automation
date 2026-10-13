"""Adopt a dispatched-but-unregistered developer session back into its run.

Reproduces run-9edb07bb (2026-10-13): a supervisor ran trunk sources, the
provider create/locate went out and the visible session started working, but
no start_intent/start_confirmed reached the formal run.  A same-run
reauthorization then changed the authorization digest, so the quarantined
generation-1 lease (old intent digest) blocks every new writer and resume
skips the node.  ``vibe reconcile`` with an ``adopted_dispatch`` package must
re-bind the original task from on-disk evidence only, and fail closed when
that evidence is missing or contradictory.
"""
import json
import subprocess
import unittest
from unittest.mock import patch

from tests.support_v45_authorize import publish_complex_probe
from vibe_guide.adapters.task_provider import ProviderActionStore, _canonical_digest
from vibe_guide.cli import _load_plan, _observed_topology_rulings, run_cli
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import (
    acquire_writer_lease,
    load_events,
    load_snapshot,
    quarantine_writer_lease,
    save_snapshot,
)
from vibe_guide.task_registry import load_task_binding

NODE = 'probe-node-a'
TASK = 'local_original-task'
HOST = 'fixture-host'
ALLOWLIST = ['README.md']


def _git(cwd, *args):
    return subprocess.run(
        ['git', '-C', str(cwd), '-c', 'user.name=Fixture',
         '-c', 'user.email=fixture@example.test', *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


class AdoptOrphanDispatchTests(unittest.TestCase):
    def setUp(self):
        self.root = publish_complex_probe(self)
        self.paths = ProjectPaths(self.root)
        _git(self.root, 'init', '-b', 'main')
        (self.root / '.gitignore').write_text('.vibe/\n.worktrees/\n')
        _git(self.root, 'add', '.gitignore')
        _git(self.root, 'commit', '-m', 'baseline')
        run_cli(['authorize', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE'], self.root)
        start = run_cli(['monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE'], self.root)
        self.run_id = start.payload['run_id']
        snapshot = load_snapshot(self.paths, self.run_id)
        self.old_digest = snapshot.authorization_digest
        current = snapshot.nodes[NODE]
        self.worktree_rel = current['worktree']
        self.branch = current['branch']
        self.writer = self.root / self.worktree_rel
        _git(self.root, 'worktree', 'add', '-b', self.branch, self.worktree_rel)
        self.store = ProviderActionStore(self.paths)
        self.old_lease_digest = Monitor._prospective_intent_digest(
            snapshot, NODE, 'developer', 1, current)

    # -- fixture helpers ---------------------------------------------------

    def _write_action(self, operation, request, result=None, *, generation=1,
                      node=NODE, sequence=0):
        """Persist a provider action exactly as ProviderActionStore would."""
        basis = {'operation': operation, 'provider': 'claude-code-visible',
                 'run_id': self.run_id, 'issue_id': node, 'role': 'developer',
                 'generation': generation, 'sequence': sequence}
        action_id = 'action-' + _canonical_digest(basis)[:32]
        payload = {'schema_version': 1, 'action_id': action_id, **basis,
                   'native_tool': 'ccd_session__spawn_task', 'request': request}
        payload['request_digest'] = _canonical_digest(payload)
        self.store._atomic(self.store._directory('requests') / (action_id + '.json'), payload)
        if result is not None:
            self.store.complete(action_id, result)
        return payload

    def build_orphan(self, *, authorization=None, located=True):
        """Leave the node exactly as the trunk-source supervisor left it."""
        digest = authorization or self.old_digest
        _directory, plan, nodes, _card = _load_plan(self.paths, 'probe-plan')
        topology = Monitor(self.paths, plan, nodes,
                           topology_rulings=_observed_topology_rulings(self.paths)
                           )._node_dispatch_topology(next(n for n in nodes if n.id == NODE))
        snapshot = load_snapshot(self.paths, self.run_id)
        envelope = {'authorization_digest': digest, 'plan_id': plan.plan_id,
                    'plan_version': plan.version, 'dispatcher': 'monitor'}
        child = {'parent_run_id': self.run_id, 'plan_revision': str(plan.version),
                 'authorization_digest': digest, 'node_id': NODE, 'role': 'developer',
                 'writer': 'worker', 'worktree': self.worktree_rel, 'branch': self.branch,
                 'allowlist': list(ALLOWLIST),
                 'capability_contract_digest': snapshot.capability_contract_digest}
        self.create = self._write_action(
            'create', {**envelope, 'origin': 'worker_dispatch', 'child_binding': child,
                       'topology': topology, 'prompt': 'fixture'},
            {'binding': {'host': HOST, 'task_id': TASK}})
        self._write_action('locate', {**envelope, 'threadId': TASK}, {'located': located})
        heads = self.store._directory('dispatch-heads')
        (heads / '{}-{}-developer-g1.json'.format(self.run_id, NODE)).write_text(
            json.dumps({'head': _git(self.root, 'rev-parse', 'HEAD')}))
        self.assertTrue(acquire_writer_lease(
            self.paths, NODE, self.worktree_rel, self.run_id,
            self.old_lease_digest, 'developer', 1))
        self.assertTrue(quarantine_writer_lease(
            self.paths, NODE, self.worktree_rel, self.run_id, 'trunk dispatch unregistered'))
        run_cli(['authorize', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE'], self.root)
        run_cli(['monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE',
                 '--run-id', self.run_id], self.root)
        snapshot = load_snapshot(self.paths, self.run_id)
        self.assertNotEqual(snapshot.authorization_digest, self.old_digest)
        current = snapshot.nodes[NODE]
        # The observed production state after the failed re-dispatch.
        current.update(status='blocked_unknown', active_task=None, active_role=None,
                       start_intent=None, retryable_action=None, developer_generation=0,
                       developer_identity=None,
                       quarantine={'run_id': self.run_id, 'handle_id': None,
                                   'reason': 'writer lease is already owned by another run'})
        snapshot.handles.pop(NODE, None)
        save_snapshot(self.paths, snapshot)

    def leases(self):
        found = []
        for path in sorted((self.root / '.vibe' / 'leases').glob('*.json')):
            data = json.loads(path.read_text())
            if data.get('node_id') == NODE:
                found.append(data)
        return found

    def adopt(self, **overrides):
        package = {'kind': 'adopted_dispatch', 'node_id': NODE,
                   'action_id': self.create['action_id']}
        package.update(overrides)
        evidence = self.root / '.vibe' / 'adopt.json'
        evidence.write_text(json.dumps(package))
        return run_cli(['reconcile', '--plan', 'probe-plan', '--run-id', self.run_id,
                        '--evidence', '.vibe/adopt.json', '--json'], self.root)

    def assert_untouched(self, result, reason):
        self.assertEqual(result.payload.get('status'), 'blocked_unknown', result.payload)
        self.assertIn(reason, result.payload.get('reason', ''))
        current = load_snapshot(self.paths, self.run_id).nodes[NODE]
        self.assertEqual(current['status'], 'blocked_unknown')
        self.assertEqual(current['developer_generation'], 0)
        self.assertIsNone(current['active_task'])
        with self.assertRaises(FileNotFoundError):
            load_task_binding(self.paths, NODE, 'developer', run_id=self.run_id)
        self.assertFalse(any(lease.get('status') == 'active' for lease in self.leases()))
        self.assertNotIn('orphan_dispatch_adopted',
                         [e['event'] for e in load_events(self.paths, self.run_id)])

    # -- reproduction --------------------------------------------------------

    def test_existing_resume_cannot_recover_the_orphan(self):
        self.build_orphan()
        before = {p.name for p in (self.store.root / 'requests').glob('*.json')}
        run_cli(['resume', '--plan', 'probe-plan', '--run-id', self.run_id], self.root)
        current = load_snapshot(self.paths, self.run_id).nodes[NODE]
        self.assertEqual(current['status'], 'blocked_unknown')
        self.assertEqual(current['developer_generation'], 0)
        self.assertIsNone(current['active_task'])
        after = {p.name for p in (self.store.root / 'requests').glob('*.json')}
        self.assertEqual(after, before, 'no second writer may be dispatched')
        self.assertFalse(acquire_writer_lease(
            self.paths, NODE, self.worktree_rel, self.run_id, 'f' * 64, 'developer', 1))
        lease, = self.leases()
        self.assertEqual(lease['status'], 'quarantined')

    # -- adoption ------------------------------------------------------------

    def test_adoption_rebinds_the_original_task(self):
        self.build_orphan()
        before = {p.name for p in (self.store.root / 'requests').glob('*.json')}
        result = self.adopt()
        self.assertIn('nodes', result.payload, result.payload)
        snapshot = load_snapshot(self.paths, self.run_id)
        current = snapshot.nodes[NODE]
        self.assertEqual(current['status'], 'running')
        self.assertEqual(current['developer_generation'], 1)
        self.assertEqual(current['developer_identity'], TASK)
        self.assertEqual(current['active_task']['task_id'], TASK)
        self.assertEqual(current['active_task']['generation'], 1)
        self.assertEqual(snapshot.handles[NODE], current['active_task']['handle_id'])
        self.assertIsNone(current['quarantine'])
        self.assertIsNone(current['retryable_action'])
        binding = load_task_binding(self.paths, NODE, 'developer', run_id=self.run_id)
        self.assertEqual((binding.task_id, binding.host, binding.generation),
                         (TASK, HOST, 1))
        self.assertEqual(binding.allowlist, ALLOWLIST)
        after = {p.name for p in (self.store.root / 'requests').glob('*.json')}
        self.assertEqual(after, before, 'adoption sends nothing to the provider')

        lease, = self.leases()
        self.assertEqual(lease['status'], 'active')
        self.assertEqual((lease['run_id'], lease['role'], lease['generation']),
                         (self.run_id, 'developer', 1))
        self.assertNotEqual(lease['intent_digest'], self.old_lease_digest)

        events = load_events(self.paths, self.run_id)
        names = [e['event'] for e in events]
        adopted = events[names.index('orphan_dispatch_adopted')]
        data = adopted['data']
        self.assertEqual(data['handle_id'], snapshot.handles[NODE])
        self.assertEqual(data['action_id'], self.create['action_id'])
        self.assertEqual(data['request_digest'], self.create['request_digest'])
        self.assertEqual(data['task_id'], TASK)
        self.assertEqual(data['generation'], 1)
        self.assertEqual(data['previous_intent_digest'], self.old_lease_digest)
        self.assertEqual(data['intent_digest'], lease['intent_digest'])
        self.assertEqual(data['dispatch_head'], _git(self.root, 'rev-parse', 'HEAD'))
        self.assertEqual(data['request_authorization_digest'], self.old_digest)
        self.assertEqual(adopted['provenance']['authorization_digest'],
                         snapshot.authorization_digest)
        # One event switches the node; no start_intent half-state exists.
        self.assertEqual(names[names.index('orphan_dispatch_adopted'):],
                         ['orphan_dispatch_adopted', 'execution_topology_observed'])

    def assert_running_after_resume(self):
        result = run_cli(['resume', '--plan', 'probe-plan', '--run-id', self.run_id, '--json'],
                         self.root)
        self.assertNotIn('invalidated', str(result.payload.get('reason')), result.payload)
        snapshot = load_snapshot(self.paths, self.run_id)
        current = snapshot.nodes[NODE]
        # An unanswered wait leaves any started task in same-task retry.
        self.assertIn(current['status'], {'running', 'retry_pending'}, current)
        self.assertEqual((current['developer_identity'], current['developer_generation']), (TASK, 1))
        self.assertEqual(current['active_task']['task_id'], TASK)
        self.assertEqual(snapshot.handles[NODE], current['active_task']['handle_id'])
        retry = current.get('retryable_action')
        self.assertTrue(retry is None or (retry['same_task'] and retry['task_id'] == TASK), retry)
        self.assertEqual(load_task_binding(self.paths, NODE, 'developer',
                                           run_id=self.run_id).task_id, TASK)
        lease, = self.leases()
        self.assertEqual(lease['status'], 'active')
        self.assertEqual([e['event'] for e in load_events(self.paths, self.run_id)]
                         .count('orphan_dispatch_adopted'), 1)

    def test_crash_before_snapshot_save_is_recovered_by_resume(self):
        self.build_orphan()
        with patch('vibe_guide.orphan_recovery.save_snapshot',
                   side_effect=OSError('crash before the snapshot save')):
            self.adopt()
        self.assertEqual(load_snapshot(self.paths, self.run_id).nodes[NODE]['developer_generation'], 0)
        self.assert_running_after_resume()

    def test_crash_before_snapshot_save_is_finished_by_adopting_again(self):
        self.build_orphan()
        with patch('vibe_guide.orphan_recovery.save_snapshot',
                   side_effect=OSError('crash before the snapshot save')):
            self.adopt()
        again = self.adopt()
        self.assertIn('nodes', again.payload, again.payload)
        self.assert_running_after_resume()

    def test_crash_between_adoption_and_topology_event_is_finished_by_adopting_again(self):
        self.build_orphan()
        original = Monitor._record_topology_projection
        calls = []

        def crash_once(monitor, snapshot, entry, *args, **kwargs):
            if entry == 'monitor.reconcile_adopted_dispatch' and not calls:
                calls.append(entry)
                raise OSError('crash after the adoption event')
            return original(monitor, snapshot, entry, *args, **kwargs)

        with patch.object(Monitor, '_record_topology_projection', crash_once):
            self.adopt()
        self.assertEqual(calls, ['monitor.reconcile_adopted_dispatch'])
        again = self.adopt()
        self.assertIn('nodes', again.payload, again.payload)
        self.assert_running_after_resume()

    def test_failed_start_leaves_a_retryable_adoption(self):
        self.build_orphan()
        with patch('vibe_guide.runners.provider_action.ProviderActionRunner.start',
                   side_effect=ValueError('provider handle store unavailable')):
            failed = self.adopt()
        self.assertEqual(failed.payload.get('status'), 'blocked_unknown', failed.payload)
        self.assertNotIn('orphan_dispatch_adopted',
                         [e['event'] for e in load_events(self.paths, self.run_id)])
        current = load_snapshot(self.paths, self.run_id).nodes[NODE]
        self.assertEqual((current['status'], current['developer_generation']), ('blocked_unknown', 0))
        run_cli(['resume', '--plan', 'probe-plan', '--run-id', self.run_id], self.root)
        retried = self.adopt()
        self.assertIn('nodes', retried.payload, retried.payload)
        self.assert_running_after_resume()

    def test_crash_inside_lease_rebind_is_finished_by_adopting_again(self):
        self.build_orphan()
        with patch('pathlib.Path.unlink', side_effect=OSError('crash before old lease removal')):
            self.adopt()
        self.assertEqual(sorted(lease['status'] for lease in self.leases()), ['active', 'quarantined'])
        retried = self.adopt()
        self.assertIn('nodes', retried.payload, retried.payload)
        self.assert_running_after_resume()

    def test_original_session_delivery_is_received_after_adoption(self):
        self.build_orphan()
        self.adopt()
        (self.writer / 'README.md').write_text('probe\n')
        _git(self.writer, 'add', 'README.md')
        _git(self.writer, 'commit', '-m', 'work')
        delivery = self.writer / 'delivery.md'
        delivery.write_text('done\n')
        # The mailbox servicer pages the session and supplies its cursor, as
        # for any normally started task; the delivery gate requires one.
        payload = {'cursor': TASK + ':1',
                   'delivery_evidence': {'completion_marker': 'DELIVERY_COMPLETE',
                                         'delivery_path': str(delivery),
                                         'thread_status': 'completed'}}
        delivered = run_cli(['worker-deliver', '--run-id', self.run_id, '--node', NODE,
                             '--role', 'developer', '--generation', '1',
                             '--payload', json.dumps(payload), '--json'], self.root)
        self.assertTrue(delivered.payload.get('recorded'), delivered.payload)
        run_cli(['resume', '--plan', 'probe-plan', '--run-id', self.run_id], self.root)
        archive = (self.store.root / 'deliveries'
                   / '{}-{}-developer-g1.json'.format(self.run_id, NODE))
        report = json.loads(archive.read_text())
        self.assertTrue(report['consumed_by'], 'the run must take the original delivery')
        wait = self.store.action(report['consumed_by'])
        self.assertEqual(wait['generation'], 1)
        self.assertEqual(wait['request']['targets'][0]['threadId'], TASK)
        delivered = [e for e in load_events(self.paths, self.run_id) if e['event'] == 'delivered']
        self.assertEqual(len(delivered), 1)
        self.assertEqual((delivered[0]['provenance']['task_id'],
                          delivered[0]['provenance']['generation']), (TASK, 1))
        current = load_snapshot(self.paths, self.run_id).nodes[NODE]
        self.assertEqual(current['delivery_evidence']['completion_marker'], 'DELIVERY_COMPLETE')
        self.assertEqual(current['developer_generation'], 1)

    def test_second_adoption_never_creates_a_second_writer(self):
        self.build_orphan()
        self.adopt()
        handles = dict(load_snapshot(self.paths, self.run_id).handles)
        before = {p.name for p in (self.store.root / 'requests').glob('*.json')}
        again = self.adopt()
        self.assertIn('nodes', again.payload, again.payload)
        self.assertEqual(load_snapshot(self.paths, self.run_id).handles, handles)
        self.assertEqual({p.name for p in (self.store.root / 'requests').glob('*.json')}, before)
        self.assertEqual(len(self.leases()), 1)
        self.assertEqual([e['event'] for e in load_events(self.paths, self.run_id)]
                         .count('orphan_dispatch_adopted'), 1)

    # -- fail closed -----------------------------------------------------------

    def test_missing_create_result_fails_closed(self):
        self.build_orphan()
        (self.store.root / 'results' / (self.create['action_id'] + '.json')).unlink()
        self.assert_untouched(self.adopt(), 'create has no bound result')

    def test_missing_dispatch_head_fails_closed(self):
        self.build_orphan()
        next((self.store.root / 'dispatch-heads').glob('*.json')).unlink()
        self.assert_untouched(self.adopt(), 'dispatch baseline is missing')

    def test_missing_quarantined_lease_fails_closed(self):
        self.build_orphan()
        for path in (self.root / '.vibe' / 'leases').glob('*.json'):
            path.unlink()
        self.assert_untouched(self.adopt(), 'exactly one lease')

    def _rewrite_lease(self, **changes):
        for path in (self.root / '.vibe' / 'leases').glob('*.json'):
            data = json.loads(path.read_text())
            if data.get('node_id') == NODE:
                data.update(changes)
                path.write_text(json.dumps(data))

    def test_lease_of_another_generation_fails_closed(self):
        self.build_orphan()
        self._rewrite_lease(generation=2)
        self.assert_untouched(self.adopt(), 'lease belongs to another')

    def test_lease_of_another_run_fails_closed(self):
        self.build_orphan()
        self._rewrite_lease(run_id='run-' + 'b' * 32)
        self.assert_untouched(self.adopt(), 'lease belongs to another')

    def test_second_lease_on_the_writer_root_fails_closed(self):
        self.build_orphan()
        other = self.root / '.vibe' / 'leases' / ('0' * 64 + '.json')
        other.write_text(json.dumps({'schema_version': 1, 'node_id': NODE,
            'worktree': self.worktree_rel, 'run_id': 'run-' + 'c' * 32,
            'role': 'developer', 'generation': 1, 'intent_digest': 'c' * 64,
            'lease_id': 'c' * 64, 'status': 'active'}))
        result = self.adopt()
        self.assertEqual(result.payload.get('status'), 'blocked_unknown', result.payload)
        self.assertIn('lease belongs to another', result.payload.get('reason', ''))
        current = load_snapshot(self.paths, self.run_id).nodes[NODE]
        self.assertEqual(current['developer_generation'], 0)
        self.assertIsNone(current['active_task'])

    def test_worktree_on_another_branch_fails_closed(self):
        self.build_orphan()
        _git(self.writer, 'checkout', '-b', 'someone-else')
        self.assert_untouched(self.adopt(), 'not on the contract branch')

    def test_contradicting_locate_result_fails_closed(self):
        self.build_orphan(located=False)
        self.assert_untouched(self.adopt(), 'locate result contradicts')

    def test_authorization_outside_this_run_fails_closed(self):
        self.build_orphan(authorization='e' * 64)
        self.assert_untouched(self.adopt(), 'never held by this run')

    def test_node_with_a_registered_writer_fails_closed(self):
        self.build_orphan()
        snapshot = load_snapshot(self.paths, self.run_id)
        snapshot.nodes[NODE]['developer_generation'] = 1
        save_snapshot(self.paths, snapshot)
        result = self.adopt()
        self.assertEqual(result.payload.get('status'), 'blocked_unknown', result.payload)
        self.assertIn("not the node's next generation", result.payload.get('reason', ''))
        with self.assertRaises(FileNotFoundError):
            load_task_binding(self.paths, NODE, 'developer', run_id=self.run_id)

    def test_later_generation_create_fails_closed(self):
        self.build_orphan()
        self._write_action('create', {'prompt': 'later'}, generation=2)
        self.assert_untouched(self.adopt(), 'a later generation was already dispatched')

    def test_second_create_of_the_generation_fails_closed(self):
        self.build_orphan()
        self._write_action('create', {'prompt': 'again'}, sequence=1)
        self.assert_untouched(self.adopt(), 'more than one create')

    def test_contradicting_visibility_result_fails_closed(self):
        self.build_orphan()
        self._write_action('visibility', {'targets': [{'threadId': TASK, 'hostId': HOST}]},
                           {'visible': False, 'direct_enter': False})
        self.assert_untouched(self.adopt(), 'visibility result contradicts')

    def test_task_bound_to_another_node_fails_closed(self):
        self.build_orphan()
        snapshot = load_snapshot(self.paths, self.run_id)
        snapshot.nodes['integration-review']['reviewer_identity'] = TASK
        save_snapshot(self.paths, snapshot)
        self.assert_untouched(self.adopt(), 'already bound to integration-review')

    def test_reauthorization_that_changed_the_node_fails_closed(self):
        self.build_orphan()
        real = load_events(self.paths, self.run_id)

        def changed(paths, run_id):
            events = [dict(e) for e in real]
            for event in events:
                if event['event'] == 'authorization_reauthorized':
                    event['data'] = dict(event['data'], affected_nodes=[NODE])
            return events

        with patch('vibe_guide.orphan_recovery.load_events', side_effect=changed):
            self.assert_untouched(self.adopt(), 'node contract changed')

    def test_package_with_unknown_fields_is_rejected(self):
        self.build_orphan()
        self.assert_untouched(self.adopt(task_id='someone-elses-task'), 'package is invalid')


if __name__ == '__main__':
    unittest.main()
