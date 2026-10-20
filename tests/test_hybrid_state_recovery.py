"""Hybrid state: a wait was retired, yet the handle still points at it and the
binding carries a cursor.

``retire_wait`` used to hide the request without clearing the handle's
``pending_action``; a later wait result had also stored a cursor on the
binding.  The self-report recovery used to send every cursor-bearing binding
to the normal wait path, which cannot leave this state, so the run stayed
``blocked_unknown``.  The recovery now recognises the state only when the
retired marker, the consumed self-report receipt and the handle all agree, and
otherwise refuses before the first write.
"""
import json
import unittest

from tests.test_self_report_recovery import NODE, TASK, SelfReportRecoveryTests as _Base
from vibe_guide.state import load_events, load_snapshot
from vibe_guide.task_registry import load_task_binding, save_task_binding


class HybridStateRecoveryTests(unittest.TestCase):
    # Borrow the fixture methods; subclassing would re-run the base cases.
    setUp = _Base.setUp
    _write_action = _Base._write_action
    build_orphan = _Base.build_orphan
    adopt = _Base.adopt
    leases = _Base.leases
    self_report = _Base.self_report
    resume = _Base.resume
    stuck = _Base.stuck
    events = _Base.events
    reconcile = _Base.reconcile
    pending_waits = _Base.pending_waits
    assert_delivery_accepted = _Base.assert_delivery_accepted
    assert_refused = _Base.assert_refused

    def hybrid(self, reason='answered_by_self_report_receipt'):
        self.stuck(extra_resumes=2)
        later, = self.pending_waits()
        self.store.retire_wait(later['action_id'], reason)
        binding = load_task_binding(self.paths, NODE, 'developer', run_id=self.run_id)
        binding.cursor = TASK + ':9'
        save_task_binding(self.paths, binding)
        self.assertEqual(self.pending_waits(), [])
        self.assertTrue(self.handle()['pending_action'], 'the handle still dangles')
        return later['action_id']

    def handle(self):
        # The node-to-handle mapping is dropped once the delivery lands, so the
        # handle id is remembered from before the recovery.
        if not hasattr(self, 'handle_id'):
            self.handle_id = load_snapshot(self.paths, self.run_id).handles[NODE]
        return json.loads((self.store.root / 'handles' / (self.handle_id + '.json')).read_text())

    def state_digest(self):
        snapshot = load_snapshot(self.paths, self.run_id)
        return (json.dumps(snapshot.nodes[NODE], sort_keys=True, default=str),
                json.dumps(self.handle(), sort_keys=True), len(load_events(self.paths, self.run_id)))

    def test_hybrid_state_recovers_and_leaves_a_replayable_record(self):
        action_id = self.hybrid()
        result = self.reconcile()
        self.assertIn('nodes', result.payload, result.payload)
        self.assertIsNone(self.handle()['pending_action'])
        self.assertIsNone(self.handle()['pending_operation'])
        current = load_snapshot(self.paths, self.run_id).nodes[NODE]
        self.assert_delivery_accepted(current)
        names = self.events()
        self.assertEqual(names.count('self_report_delivery_reconciled'), 1)
        self.assertEqual(names.count('delivered'), 1)
        record = next(e for e in load_events(self.paths, self.run_id)
                      if e['event'] == 'self_report_delivery_reconciled')
        self.assertEqual(record['data']['source'], 'retired_wait_with_cursor')
        self.assertIsNone(self.store.result(action_id), 'recovery never fabricates a result')
        binding = load_task_binding(self.paths, NODE, 'developer', run_id=self.run_id)
        self.assertEqual(binding.cursor, TASK + ':9', 'the existing cursor is kept')
        self.resume()
        self.assertEqual(self.events().count('delivered'), 1)

    def test_missing_self_report_receipt_is_refused_untouched(self):
        self.hybrid()
        archive = self.store.root / 'deliveries' / '{}-{}-developer-g1.json'.format(self.run_id, NODE)
        report = json.loads(archive.read_text())
        report['consumed_by'] = None
        archive.write_text(json.dumps(report))
        before = self.state_digest()
        self.assert_refused(self.reconcile(), 'no consumed self-report receipt')
        self.assertEqual(self.state_digest(), before)

    def test_retire_for_another_reason_is_refused_untouched(self):
        self.hybrid(reason='operator_cleanup')
        before = self.state_digest()
        self.assert_refused(self.reconcile(), 'retired for another reason')
        self.assertEqual(self.state_digest(), before)

    def test_cursor_with_a_live_wait_is_still_left_to_the_wait_path(self):
        self.stuck(extra_resumes=2)
        binding = load_task_binding(self.paths, NODE, 'developer', run_id=self.run_id)
        binding.cursor = TASK + ':9'
        save_task_binding(self.paths, binding)
        before = self.state_digest()
        self.assert_refused(self.reconcile(), 'binding has a cursor')
        self.assertEqual(self.state_digest(), before)

    def test_handle_pointing_at_another_task_is_refused_untouched(self):
        self.hybrid()
        self.handle()
        path = self.store.root / 'handles' / (self.handle_id + '.json')
        metadata = json.loads(path.read_text())
        metadata['task_id'] = 'local_someone-else'
        path.write_text(json.dumps(metadata))
        before = self.state_digest()
        self.assert_refused(self.reconcile(), 'handle metadata names another task')
        self.assertEqual(self.state_digest(), before)

    def test_live_supervisor_is_refused_untouched(self):
        import os
        self.hybrid()
        lease = self.root / '.vibe' / 'runs' / self.run_id / 'supervisor.lease'
        lease.write_text(json.dumps({'pid': os.getppid(), 'run_id': self.run_id}))
        before = self.state_digest()
        self.assert_refused(self.reconcile(), 'a supervisor is running this run')
        self.assertEqual(self.state_digest(), before)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(HybridStateRecoveryTests)


if __name__ == '__main__':
    unittest.main()
