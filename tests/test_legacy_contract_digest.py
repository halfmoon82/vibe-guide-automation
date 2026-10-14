"""Runs authorized under the 5.0.7 contract-digest formula stay readable.

5.0.8 (#182) dropped file scope from ``executable_contract_digest``; 5.0.7
digested it.  A run started or reauthorized under 5.0.7 carries 5.0.7
digests in its authorization record, its reauthorization history and every
node's dispatch-time ``contract_digest``.  Reproduces run-9edb07bb
(2026-10-14): 5.0.8 recomputed the historic reauthorization proof with the
new formula, rejected it, and ``status``/``resume`` reported ``unknown``.

The legacy formula covers strictly more than the new one (scope included),
so a legacy match proves the same contract; anything matching neither
formula is still refused.
"""
import json
import unittest
from contextlib import ExitStack
from copy import deepcopy
from unittest.mock import patch

from tests.support_v45_authorize import publish_complex_probe
from tests import test_adopt_orphan_dispatch as adopt_suite
from tests import test_visible_sdd_contract as sdd_suite
from vibe_guide.authorization import (
    AuthorizationRecord,
    executable_contract_digest,
    is_authorization_valid,
    legacy_executable_contract_digest,
)
from vibe_guide.cli import _load_plan, run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import load_events, load_snapshot, run_dir

NODE = adopt_suite.NODE
_PLAN = 'probe-plan'
_AUTHORIZE = ['--plan', _PLAN, '--authorize', 'AUTHORIZE']


def as_507():
    """Write digests the way 5.0.7 did: scope inside the contract digest."""
    stack = ExitStack()
    for module in ('vibe_guide.authorization', 'vibe_guide.monitor'):
        stack.enter_context(patch(module + '.executable_contract_digest',
                                  legacy_executable_contract_digest))
    return stack


class LegacyRunTests(unittest.TestCase):
    def setUp(self):
        with as_507():
            self.root = publish_complex_probe(self)
        self.paths = ProjectPaths(self.root)
        node = self.node()
        # The fixture only reproduces the incident while the formulas differ.
        self.assertNotEqual(executable_contract_digest([node]),
                            legacy_executable_contract_digest([node]))

    def node(self):
        return next(n for n in _load_plan(self.paths, _PLAN)[2] if n.id == NODE)

    def start_507_run(self, *, reauthorize):
        with as_507():
            run_cli(['authorize', *_AUTHORIZE], self.root)
            self.run_id = run_cli(['monitor', *_AUTHORIZE], self.root).payload['run_id']
            if reauthorize:
                run_cli(['authorize', *_AUTHORIZE], self.root)
                run_cli(['monitor', *_AUTHORIZE, '--run-id', self.run_id], self.root)
        if reauthorize:
            self.assertEqual(len(self.reauthorizations()), 1)

    def reauthorizations(self):
        return [e['data'] for e in load_events(self.paths, self.run_id)
                if e['event'] == 'authorization_reauthorized']

    def status(self):
        return run_cli(['status', '--plan', _PLAN, '--run-id', self.run_id, '--json'],
                       self.root).payload

    def record_is_valid(self):
        record = AuthorizationRecord.from_dict(
            load_snapshot(self.paths, self.run_id).authorization)
        _directory, plan, nodes, _card = _load_plan(self.paths, _PLAN)
        return is_authorization_valid(record, plan, nodes)

    def test_507_reauthorized_run_loads(self):
        self.start_507_run(reauthorize=True)
        snapshot = load_snapshot(self.paths, self.run_id)
        # Not a silent fallback to a snapshot older than the reauthorization.
        self.assertEqual(snapshot.authorization_digest,
                         self.reauthorizations()[0]['authorization_digest'])
        events = load_events(self.paths, self.run_id)
        reauth_index = next(i for i, e in enumerate(events)
                            if e['event'] == 'authorization_reauthorized')
        self.assertGreater(snapshot.event_sequence, reauth_index)
        self.assertNotEqual(self.status().get('status'), 'unknown')

    def test_507_authorization_stays_valid_without_reauthorization(self):
        self.start_507_run(reauthorize=True)
        self.assertTrue(self.record_is_valid())
        resumed = run_cli(['resume', '--plan', _PLAN, '--run-id', self.run_id, '--json'],
                          self.root).payload
        self.assertNotEqual(resumed.get('status'), 'unknown', resumed)
        self.assertEqual(len(self.reauthorizations()), 1)

    def test_historic_proof_matching_neither_formula_is_refused(self):
        self.start_507_run(reauthorize=True)
        events_path = run_dir(self.paths, self.run_id) / 'events.jsonl'
        lines = events_path.read_text(encoding='utf-8').splitlines()
        index, record = next((i, json.loads(line)) for i, line in enumerate(lines)
                             if '"authorization_reauthorized"' in line)
        contracts = record['data']['authorized_node_contracts']
        tampered = json.loads(contracts[NODE])
        tampered['contract']['output'] = 'verified_fact: 篡改'
        contracts[NODE] = json.dumps(tampered, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'))
        lines[index] = json.dumps(record, ensure_ascii=False)
        events_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            load_snapshot(self.paths, self.run_id)

    def test_formula_only_reauthorization_changes_no_node(self):
        self.start_507_run(reauthorize=False)
        before = load_snapshot(self.paths, self.run_id).nodes[NODE]['contract_digest']
        self.assertEqual(before, legacy_executable_contract_digest([self.node()]))
        run_cli(['authorize', *_AUTHORIZE], self.root)
        run_cli(['monitor', *_AUTHORIZE, '--run-id', self.run_id], self.root)
        data, = self.reauthorizations()
        self.assertEqual(data['changed_nodes'], [])
        self.assertEqual(data['affected_nodes'], [])
        # The unchanged contract keeps the digest it was dispatched under.
        self.assertEqual(
            load_snapshot(self.paths, self.run_id).nodes[NODE]['contract_digest'], before)
        self.assertNotEqual(self.status().get('status'), 'unknown')

    def test_real_contract_change_after_507_invalidates_the_card(self):
        self.start_507_run(reauthorize=False)
        self.assertTrue(self.record_is_valid())
        path = self.root / '.vibe' / 'plans' / _PLAN / 'nodes.json'
        items = json.loads(path.read_text(encoding='utf-8'))
        next(n for n in items if n['id'] == NODE)['contract']['acceptance_example'] = (
            'verified_fact: README 含两行文本')
        path.write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')
        self.assertFalse(self.record_is_valid())


class AdoptionAfterFormulaOnlyReauthorizationTests(unittest.TestCase):
    """#187 refuses adoption when a later reauthorization changed the node.

    A reauthorization that only moved the digest formula changed nothing, so
    the dispatched session must still be adoptable.
    """

    def setUp(self):
        self.case = adopt_suite.AdoptOrphanDispatchTests('test_adoption_rebinds_the_original_task')
        self.addCleanup(self.case.doCleanups)
        # Dispatch happens under 5.0.7; build_orphan reauthorizes under 5.0.8.
        with as_507():
            self.case.setUp()

    def test_adoption_after_formula_only_reauthorization(self):
        case = self.case
        case.build_orphan()
        data = [e['data'] for e in load_events(case.paths, case.run_id)
                if e['event'] == 'authorization_reauthorized'][-1]
        self.assertEqual(data['changed_nodes'], [])
        result = case.adopt()
        self.assertIn('nodes', result.payload, result.payload)
        current = load_snapshot(case.paths, case.run_id).nodes[NODE]
        self.assertEqual(current['status'], 'running')
        self.assertEqual(current['developer_generation'], 1)
        self.assertEqual(current['developer_identity'], adopt_suite.TASK)
        lease, = case.leases()
        self.assertEqual(lease['status'], 'active')



class AcceptanceOfLegacyDispatchTests(unittest.TestCase):
    """A node dispatched under 5.0.7 carries a legacy ``contract_digest``.

    Accepting its delivery under 5.0.8 compares that digest with the live
    contract; the same contract must be accepted on the live and replay paths.
    """

    def setUp(self):
        self.case = sdd_suite.VisibleSddAcceptanceMonitorTests(
            'test_accepted_event_reuses_dispatch_time_executable_contract_digest')
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        with as_507():
            self.monitor, record = self.case._monitor()
            self.runner = self.case._runner()
            self.snapshot = self.monitor.start(record, self.runner)
        node = self.monitor.nodes['sdd-a']
        self.assertEqual(self.snapshot.nodes['sdd-a']['contract_digest'],
                         legacy_executable_contract_digest([node]))
        self.assertNotEqual(self.snapshot.nodes['sdd-a']['contract_digest'],
                            executable_contract_digest([node]))

    def test_live_delivery_of_legacy_dispatch_is_accepted(self):
        snapshot = self.monitor.tick(self.snapshot.run_id, self.runner)
        current = snapshot.nodes['sdd-a']
        self.assertEqual(current['status'], 'accepted', current.get('reason'))
        self.assertEqual(current['acceptance']['contract_digest'],
                         self.snapshot.nodes['sdd-a']['contract_digest'])

    def test_replay_of_legacy_acceptance_is_accepted(self):
        from vibe_guide.state import save_snapshot

        preserved = deepcopy(self.snapshot)
        with as_507():
            accepted = self.monitor.tick(self.snapshot.run_id, self.runner)
        self.assertEqual(accepted.nodes['sdd-a']['status'], 'accepted')
        save_snapshot(self.case.paths, preserved)
        recovered = self.monitor.tick(self.snapshot.run_id, self.runner)
        self.assertEqual(recovered.nodes['sdd-a']['status'], 'accepted')

    def test_malformed_dispatch_digest_blocks_the_node(self):
        from vibe_guide.state import save_snapshot

        self.snapshot.nodes['sdd-a']['contract_digest'] = '指纹' * 32
        save_snapshot(self.case.paths, self.snapshot)
        snapshot = self.monitor.tick(self.snapshot.run_id, self.runner)
        self.assertEqual(snapshot.nodes['sdd-a']['status'], 'blocked_unknown')

    def test_legacy_dispatch_with_forged_digest_is_still_refused(self):
        import hashlib
        from vibe_guide.state import save_snapshot

        self.snapshot.nodes['sdd-a']['contract_digest'] = hashlib.sha256(
            b'smuggled contract').hexdigest()
        save_snapshot(self.case.paths, self.snapshot)
        snapshot = self.monitor.tick(self.snapshot.run_id, self.runner)
        self.assertEqual(snapshot.nodes['sdd-a']['status'], 'blocked_unknown')
        self.assertIn('does not match the live node contract',
                      snapshot.nodes['sdd-a']['reason'])


if __name__ == '__main__':
    unittest.main()
