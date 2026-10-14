"""A self-reported delivery refused only for its missing cursor.

Reproduces run-9edb07bb (2026-10-14, node heartbeat-contract): the developer
reported a complete visible-sdd delivery with `vibe worker-deliver`; the next
wait took the archive, but its result carries no cursor, so the pre-fix gate
refused it as ``blocked_unknown`` ("current cursor is missing").  Every later
resume only re-issued an unanswered wait, and repair routes the reason to
``reconcile_binding``.  ``vibe reconcile`` with a ``self_reported_delivery``
package re-judges the same bound result under the fixed gate, and refuses
whenever the evidence does not prove it is that task's own delivery.
"""
import json
import unittest
from unittest.mock import patch

from tests import test_adopt_orphan_dispatch as adoption
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.cli import run_cli
from vibe_guide.state import load_events, load_snapshot, save_snapshot
from vibe_guide.task_registry import load_task_binding, save_task_binding

NODE, TASK = adoption.NODE, adoption.TASK
# Borrow the fixture methods; importing the TestCase class itself would make
# discovery run the whole adoption suite a second time from this module.
_Fixture = adoption.AdoptOrphanDispatchTests


class SelfReportRecoveryTests(unittest.TestCase):
    setUp = _Fixture.setUp
    _write_action = _Fixture._write_action
    build_orphan = _Fixture.build_orphan
    adopt = _Fixture.adopt
    leases = _Fixture.leases
    self_report = _Fixture.self_report

    def resume(self):
        return run_cli(['resume', '--plan', 'probe-plan', '--run-id', self.run_id, '--json'],
                       self.root)

    def stuck(self, extra_resumes=1):
        """The node as the pre-fix gate left it: refused, then re-polled."""
        self.build_orphan()
        self.adopt()
        self.self_report()
        # The pre-fix gate had no receipt to accept in place of the cursor.
        with patch.object(ProviderActionStore, 'self_report_receipt', return_value=None):
            self.resume()
            for _ in range(extra_resumes):
                self.resume()
        current = load_snapshot(self.paths, self.run_id).nodes[NODE]
        self.assertIn(current['status'], {'blocked_unknown', 'retry_pending'}, current)
        self.assertNotIn('delivered', self.events())

    def events(self):
        return [e['event'] for e in load_events(self.paths, self.run_id)]

    def reconcile(self, **overrides):
        package = {'kind': 'self_reported_delivery', 'node_id': NODE}
        package.update(overrides)
        (self.root / '.vibe' / 'redeliver.json').write_text(json.dumps(package))
        return run_cli(['reconcile', '--plan', 'probe-plan', '--run-id', self.run_id,
                        '--evidence', '.vibe/redeliver.json', '--json'], self.root)

    def pending_waits(self):
        return [a for a in self.store.pending(self.run_id)
                if a['issue_id'] == NODE and a['operation'] == 'wait']

    def assert_delivery_accepted(self, current):
        """The developer handoff landed; what follows is the reviewer's turn.

        The probe node is dual-visible, so a delivered developer hands over to
        a reviewer dispatch; that start is outside this recovery.
        """
        binding = load_task_binding(self.paths, NODE, 'developer', run_id=self.run_id)
        self.assertEqual(binding.status, 'delivered')
        self.assertTrue(current.get('reviewer_started'), current)
        self.assertNotEqual(current.get('active_role'), 'developer')

    def assert_refused(self, result, reason):
        self.assertEqual(result.payload.get('status'), 'blocked_unknown', result.payload)
        self.assertIn(reason, result.payload.get('reason', ''))
        self.assertNotIn('self_report_delivery_reconciled', self.events())
        self.assertNotIn('delivered', self.events())

    # -- reproduction --------------------------------------------------------

    def test_stuck_node_does_not_recover_on_its_own(self):
        self.stuck()
        for _ in range(2):
            self.resume()
        current = load_snapshot(self.paths, self.run_id).nodes[NODE]
        self.assertNotEqual(current['status'], 'delivered')
        self.assertNotIn('delivered', self.events())
        self.assertEqual(len(self.pending_waits()), 1, 'only an unanswered re-poll remains')

    # -- recovery ------------------------------------------------------------

    def test_reconcile_rejudges_the_same_delivery(self):
        self.stuck(extra_resumes=2)
        later = self.pending_waits()
        self.assertEqual(len(later), 1)
        requests_before = {p.name for p in (self.store.root / 'requests').glob('*.json')}
        result = self.reconcile()
        self.assertIn('nodes', result.payload, result.payload)
        current = load_snapshot(self.paths, self.run_id).nodes[NODE]
        self.assert_delivery_accepted(current)
        self.assertEqual(current['delivery_evidence']['completion_marker'], 'DELIVERY_COMPLETE')
        self.assertEqual((current['developer_identity'], current['developer_generation']), (TASK, 1))
        self.assertIsNone(current['recovery_action'])
        names = self.events()
        self.assertEqual(names.count('self_report_delivery_reconciled'), 1)
        self.assertEqual(names.count('delivered'), 1)
        self.assertLess(names.index('self_report_delivery_reconciled'), names.index('delivered'))
        delivered = next(e for e in load_events(self.paths, self.run_id) if e['event'] == 'delivered')
        self.assertEqual((delivered['provenance']['task_id'], delivered['provenance']['generation']),
                         (TASK, 1))
        # Nothing reaches the session; the unanswered re-poll is retired.
        requests_after = {p.name for p in (self.store.root / 'requests').glob('*.json')}
        self.assertTrue(requests_after >= requests_before)
        self.assertEqual(self.pending_waits(), [])
        stale = json.loads((self.store.root / 'stale' / (later[0]['action_id'] + '.json')).read_text())
        self.assertEqual(stale['reason'], 'answered_by_self_report_receipt')
        self.assertIsNone(self.store.result(later[0]['action_id']), 'retiring is never a result')
        binding = load_task_binding(self.paths, NODE, 'developer', run_id=self.run_id)
        self.assertIsNone(binding.cursor, 'no cursor is invented')
        self.resume()
        self.assertEqual(self.events().count('delivered'), 1, 'resume does not deliver twice')

    def test_second_reconcile_is_refused_without_side_effects(self):
        self.stuck()
        self.reconcile()
        snapshot = load_snapshot(self.paths, self.run_id)
        again = self.reconcile()
        self.assertEqual(again.payload.get('status'), 'blocked_unknown', again.payload)
        self.assertEqual(self.events().count('delivered'), 1)
        self.assertEqual(load_snapshot(self.paths, self.run_id).nodes[NODE]['status'],
                         snapshot.nodes[NODE]['status'])

    def test_replay_after_a_lost_snapshot_save_restores_the_delivery(self):
        self.stuck()
        with patch('vibe_guide.self_report_recovery.save_snapshot',
                   side_effect=OSError('crash before the snapshot save')):
            self.reconcile()
        self.assertIn('delivered', self.events())
        self.resume()
        current = load_snapshot(self.paths, self.run_id).nodes[NODE]
        self.assert_delivery_accepted(current)
        self.assertEqual(self.events().count('delivered'), 1)

    # -- fail closed -----------------------------------------------------------

    def test_binding_with_a_cursor_is_left_to_the_wait_path(self):
        self.stuck()
        binding = load_task_binding(self.paths, NODE, 'developer', run_id=self.run_id)
        binding.cursor = TASK + ':9'
        save_task_binding(self.paths, binding)
        self.assert_refused(self.reconcile(), 'binding has a cursor')

    def test_unconsumed_archive_is_refused(self):
        self.stuck()
        archive = self.store.root / 'deliveries' / '{}-{}-developer-g1.json'.format(self.run_id, NODE)
        report = json.loads(archive.read_text())
        report['consumed_by'] = None
        archive.write_text(json.dumps(report))
        self.assert_refused(self.reconcile(), 'no consumed self-report receipt')

    def test_result_that_differs_from_the_archive_is_refused(self):
        self.stuck()
        archive = self.store.root / 'deliveries' / '{}-{}-developer-g1.json'.format(self.run_id, NODE)
        report = json.loads(archive.read_text())
        report['payload']['delivery_evidence']['completion_marker'] = 'SOMETHING_ELSE'
        archive.write_text(json.dumps(report))
        self.assert_refused(self.reconcile(), 'no consumed self-report receipt')

    def test_wait_aimed_at_another_task_is_refused(self):
        self.stuck()
        archive = self.store.root / 'deliveries' / '{}-{}-developer-g1.json'.format(self.run_id, NODE)
        report = json.loads(archive.read_text())
        original = self.store.action(report['consumed_by'])
        foreign = self._write_action(
            'wait', dict(original['request'], targets=[{'threadId': 'local_someone-else',
                                                        'hostId': 'fixture-host'}]),
            self.store.result(report['consumed_by']), sequence=9)
        report['consumed_by'] = foreign['action_id']
        archive.write_text(json.dumps(report))
        self.assert_refused(self.reconcile(), 'no consumed self-report receipt')

    def test_node_without_an_active_task_is_refused(self):
        self.stuck()
        snapshot = load_snapshot(self.paths, self.run_id)
        snapshot.nodes[NODE]['active_task'] = dict(snapshot.nodes[NODE]['active_task'],
                                                   role='reviewer')
        save_snapshot(self.paths, snapshot)
        self.assert_refused(self.reconcile(), 'no active developer task')

    def test_task_of_another_generation_is_refused(self):
        self.stuck()
        snapshot = load_snapshot(self.paths, self.run_id)
        snapshot.nodes[NODE]['developer_generation'] = 2
        save_snapshot(self.paths, snapshot)
        self.assert_refused(self.reconcile(), "not the node's current developer generation")

    def test_answered_later_wait_is_refused(self):
        self.stuck(extra_resumes=2)
        later, = self.pending_waits()
        self.store.complete(later['action_id'], {'status': 'timeout', 'cursor': TASK + ':2'})
        self.assert_refused(self.reconcile(), 'a later wait was already answered')

    def test_live_supervisor_refuses(self):
        import os
        self.stuck()
        lease = self.root / '.vibe' / 'runs' / self.run_id / 'supervisor.lease'
        lease.write_text(json.dumps({'pid': os.getppid(), 'run_id': self.run_id}))
        self.assert_refused(self.reconcile(), 'a supervisor is running this run')

    def test_package_with_unknown_fields_is_rejected(self):
        self.stuck()
        self.assert_refused(self.reconcile(cursor='made-up'), 'package is invalid')


def load_tests(loader, tests, pattern):
    # Only this module's cases; the borrowed fixture class is not re-run.
    return loader.loadTestsFromTestCase(SelfReportRecoveryTests)


if __name__ == '__main__':
    unittest.main()
