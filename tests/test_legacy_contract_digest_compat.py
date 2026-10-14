"""Runs authorized under v5.0.7 must still load and progress under v5.0.8+.

PR #182 stopped folding the file scope (``files`` / ``allowlist``) into
``executable_contract_digest``.  Every digest a v5.0.7 run persisted -- the
authorization record's ``node_contract_digest``, the per-node
``contract_digest`` stamped at dispatch, and the reauthorization proof's
``node_contract_digests`` -- was computed with the scope included, so a
straight comparison against the new algorithm rejects an untouched run.

The verification side accepts either algorithm, never a third: a digest that
matches neither is still refused, and new writes use the current algorithm.
The v5.0.7 algorithm is reimplemented here on purpose instead of importing
the production fallback, so removing the fallback turns these tests red.
"""
import hashlib
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from vibe_guide import authorization as authorization_module
from vibe_guide.authorization import (
    authorize,
    build_authorization_card,
    executable_contract_digest,
    is_authorization_valid,
)
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.models import AgentCapabilities, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import (
    _decode_snapshot,
    load_events,
    load_snapshot,
    run_dir,
    save_snapshot,
)

# Module imports, not class imports: a TestCase bound at module level would be
# collected and run a second time from this file.
from tests import test_reauthorization_gates as _gates
from tests import test_visible_sdd_contract as _visible_sdd


def _v507_digest(nodes):
    """``executable_contract_digest`` exactly as shipped in v5.0.7."""
    return authorization_module._canonical_digest(
        {"nodes": authorization_module.canonical_node_contracts(nodes)}
    )


def _foreign_digest(nodes):
    """A well-formed digest that neither the current nor the v5.0.7 algorithm yields."""
    return hashlib.sha256(b"foreign:" + _v507_digest(nodes).encode("ascii")).hexdigest()


@contextmanager
def _writes_with(algorithm):
    """Simulate an older vibe that computed every contract digest with ``algorithm``."""
    with ExitStack() as stack:
        for module in ("authorization", "monitor", "state"):
            stack.enter_context(
                patch("vibe_guide.{}.executable_contract_digest".format(module), algorithm)
            )
        yield


_nodes = _gates.ScopeOnlyReauthorizationTests._nodes


class _RunFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vg-v507-compat-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = ProjectPaths(self.root)
        (self.paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n',
            encoding="utf-8",
        )
        save_contract(self.paths, build_contract(self.root, provider="fake", host_id="local"))
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")
        self.plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")

    def _record(self, nodes):
        return authorize(
            build_authorization_card(self.plan, nodes, self.capabilities), "AUTHORIZE"
        )

    @staticmethod
    def _runner():
        return FakeRunner(
            events={
                (node_id, "developer"): [("complete", {"evidence": node_id})]
                for node_id in ("a", "b")
            }
            | {
                (node_id, "reviewer"): [("accepted", {"evidence": node_id})]
                for node_id in ("a", "b")
            }
        )

    def _accepted_run(self):
        nodes = _nodes()
        runner = self._runner()
        monitor = Monitor(self.paths, self.plan, nodes)
        snapshot = monitor.start(self._record(nodes), runner)
        for _ in range(8):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(
            {node_id: current["status"] for node_id, current in snapshot.nodes.items()},
            {"a": "accepted", "b": "accepted"},
        )
        return snapshot

    def _reauthorize_with_changed_acceptance(self, algorithm):
        """Accepted run + same-run reauthorization, written by ``algorithm``."""
        with _writes_with(algorithm):
            snapshot = self._accepted_run()
            preserved = load_snapshot(self.paths, snapshot.run_id)
            changed_nodes = _nodes(a_acceptance="verified_fact: changed")
            changed_record = self._record(changed_nodes)
            rebound = Monitor(self.paths, self.plan, changed_nodes).reauthorize(
                snapshot.run_id, changed_record, FakeRunner(), "executable_contract_changed"
            )
        return preserved, rebound, changed_nodes, changed_record

    def _state_bytes(self, run_id):
        return (run_dir(self.paths, run_id, create=False) / "state.json").read_bytes()


class ReauthorizationProofCompatTests(_RunFixture):
    """state._validate_snapshot: the authorized DAG proof of a reauthorization."""

    def test_v507_reauthorization_proof_loads_under_current_code(self):
        _preserved, rebound, changed_nodes, changed_record = (
            self._reauthorize_with_changed_acceptance(_v507_digest)
        )
        self.assertNotEqual(
            changed_record.node_contract_digest, executable_contract_digest(changed_nodes)
        )

        decoded = _decode_snapshot(
            self._state_bytes(rebound.run_id), load_events(self.paths, rebound.run_id)
        )
        loaded = load_snapshot(self.paths, rebound.run_id)

        for snapshot in (decoded, loaded):
            self.assertEqual(snapshot.authorization_digest, changed_record.digest)
            self.assertEqual(snapshot.node_contract_digest, _v507_digest(changed_nodes))

    def test_current_reauthorization_proof_still_loads(self):
        _preserved, rebound, changed_nodes, changed_record = (
            self._reauthorize_with_changed_acceptance(executable_contract_digest)
        )

        loaded = load_snapshot(self.paths, rebound.run_id)

        self.assertEqual(loaded.authorization_digest, changed_record.digest)
        self.assertEqual(
            loaded.node_contract_digest, executable_contract_digest(changed_nodes)
        )

    def test_proof_matching_neither_algorithm_is_still_refused(self):
        _preserved, rebound, _changed_nodes, _record = (
            self._reauthorize_with_changed_acceptance(_foreign_digest)
        )

        with self.assertRaisesRegex(ValueError, "authorized DAG proof is invalid"):
            _decode_snapshot(
                self._state_bytes(rebound.run_id), load_events(self.paths, rebound.run_id)
            )

    def test_proof_mixing_the_two_algorithms_is_refused(self):
        """Aggregate digest from one algorithm, per-node digests from the other."""

        def mixed(nodes):
            return _v507_digest(nodes) if len(nodes) > 1 else executable_contract_digest(nodes)

        _preserved, rebound, _changed_nodes, _record = (
            self._reauthorize_with_changed_acceptance(mixed)
        )

        with self.assertRaisesRegex(ValueError, "authorized DAG proof is invalid"):
            _decode_snapshot(
                self._state_bytes(rebound.run_id), load_events(self.paths, rebound.run_id)
            )


class AuthorizationRecordCompatTests(_RunFixture):
    """authorization.is_authorization_valid against the live contract."""

    def test_v507_record_stays_valid_for_the_unchanged_contract(self):
        with _writes_with(_v507_digest):
            record = self._record(_nodes())

        self.assertTrue(is_authorization_valid(record, self.plan, _nodes()))

    def test_v507_record_rejects_a_changed_acceptance_contract(self):
        with _writes_with(_v507_digest):
            record = self._record(_nodes())

        self.assertFalse(
            is_authorization_valid(
                record, self.plan, _nodes(a_acceptance="verified_fact: changed")
            )
        )

    def test_v507_record_rejects_a_scope_change(self):
        """The v5.0.7 digest froze the scope, so it still pins it."""
        with _writes_with(_v507_digest):
            record = self._record(_nodes())

        self.assertFalse(
            is_authorization_valid(record, self.plan, _nodes(a_files=["a.py", "extra.py"]))
        )

    def test_record_matching_neither_algorithm_is_refused(self):
        with _writes_with(_foreign_digest):
            record = self._record(_nodes())

        self.assertFalse(is_authorization_valid(record, self.plan, _nodes()))

    def test_new_records_are_written_with_the_current_algorithm(self):
        nodes = _nodes()
        record = self._record(nodes)

        self.assertEqual(record.node_contract_digest, executable_contract_digest(nodes))
        self.assertNotEqual(record.node_contract_digest, _v507_digest(nodes))


class ReauthorizationTransitionReplayCompatTests(_RunFixture):
    """monitor._apply_reauthorization_transition replaying a v5.0.7 event."""

    def _transition(self, run_id):
        return [
            record["data"]
            for record in load_events(self.paths, run_id)
            if record["event"] == "authorization_reauthorized"
        ][-1]

    def test_v507_transition_replays_under_current_code(self):
        preserved, rebound, changed_nodes, changed_record = (
            self._reauthorize_with_changed_acceptance(_v507_digest)
        )
        data = self._transition(rebound.run_id)
        replayed = deepcopy(preserved)

        Monitor(self.paths, self.plan, changed_nodes)._apply_reauthorization_transition(
            replayed, data
        )

        self.assertEqual(replayed.authorization_digest, changed_record.digest)
        self.assertEqual(replayed.nodes["a"]["status"], rebound.nodes["a"]["status"])

    def test_tampered_live_contract_still_refuses_the_transition(self):
        preserved, rebound, _changed_nodes, _record = (
            self._reauthorize_with_changed_acceptance(_v507_digest)
        )
        data = self._transition(rebound.run_id)
        tampered = _nodes(a_acceptance="verified_fact: tampered")

        with self.assertRaises((PermissionError, ValueError)):
            Monitor(self.paths, self.plan, tampered)._apply_reauthorization_transition(
                deepcopy(preserved), data
            )


class DualVisibleAcceptanceCompatTests(_RunFixture):
    """Dual-visible acceptance: live path and replay of a lost acceptance."""

    def test_node_dispatched_under_v507_is_accepted_live(self):
        nodes = _nodes()
        runner = self._runner()
        with _writes_with(_v507_digest):
            snapshot = Monitor(self.paths, self.plan, nodes).start(self._record(nodes), runner)
        self.assertEqual(snapshot.nodes["a"]["contract_digest"], _v507_digest([nodes[0]]))

        monitor = Monitor(self.paths, self.plan, _nodes())
        for _ in range(8):
            snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["a"]["status"], "accepted", snapshot.nodes["a"].get("reason"))
        self.assertEqual(snapshot.nodes["b"]["status"], "accepted", snapshot.nodes["b"].get("reason"))

    def test_v507_acceptance_lost_with_the_snapshot_replays(self):
        nodes = _nodes()
        runner = self._runner()
        with _writes_with(_v507_digest):
            monitor = Monitor(self.paths, self.plan, nodes)
            snapshot = monitor.start(self._record(nodes), runner)
            preserved = None
            for _ in range(8):
                before = deepcopy(snapshot)
                snapshot = monitor.tick(snapshot.run_id, runner)
                if snapshot.nodes["a"]["status"] == "accepted":
                    preserved = before
                    break
        self.assertIsNotNone(preserved)
        save_snapshot(self.paths, preserved)

        recovered = Monitor(self.paths, self.plan, _nodes()).tick(snapshot.run_id, runner)

        self.assertEqual(recovered.nodes["a"]["status"], "accepted", recovered.nodes["a"].get("reason"))

    def test_digest_matching_neither_algorithm_is_still_refused_live(self):
        nodes = _nodes()
        runner = self._runner()
        with _writes_with(_v507_digest):
            snapshot = Monitor(self.paths, self.plan, nodes).start(self._record(nodes), runner)
        snapshot.nodes["a"]["contract_digest"] = _foreign_digest([nodes[0]])
        save_snapshot(self.paths, snapshot)

        monitor = Monitor(self.paths, self.plan, _nodes())
        for _ in range(4):
            snapshot = monitor.tick(snapshot.run_id, runner)
            if snapshot.nodes["a"]["status"] == "blocked_unknown":
                break

        self.assertEqual(snapshot.nodes["a"]["status"], "blocked_unknown")
        self.assertIn("does not match the live node contract", snapshot.nodes["a"]["reason"])


class VisibleSddAcceptanceCompatTests(unittest.TestCase):
    """Visible-sdd acceptance: live path and replay of a lost acceptance."""

    setUp = _visible_sdd.VisibleSddAcceptanceMonitorTests.setUp
    tearDown = _visible_sdd.VisibleSddAcceptanceMonitorTests.tearDown
    _monitor = _visible_sdd.VisibleSddAcceptanceMonitorTests._monitor
    _runner = staticmethod(_visible_sdd.VisibleSddAcceptanceMonitorTests._runner)

    def _v507_started(self):
        with _writes_with(_v507_digest):
            monitor, record = self._monitor()
            runner = self._runner()
            snapshot = monitor.start(record, runner)
        node = monitor.nodes["sdd-a"]
        self.assertEqual(snapshot.nodes["sdd-a"]["contract_digest"], _v507_digest([node]))
        self.assertNotEqual(_v507_digest([node]), executable_contract_digest([node]))
        return monitor, runner, snapshot

    def test_node_dispatched_under_v507_is_accepted_live(self):
        monitor, runner, snapshot = self._v507_started()

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(
            snapshot.nodes["sdd-a"]["status"], "accepted", snapshot.nodes["sdd-a"].get("reason")
        )

    def test_v507_acceptance_lost_with_the_snapshot_replays(self):
        with _writes_with(_v507_digest):
            monitor, record = self._monitor()
            runner = self._runner()
            snapshot = monitor.start(record, runner)
            preserved = deepcopy(snapshot)
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "accepted")
        save_snapshot(self.paths, preserved)

        recovered = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(
            recovered.nodes["sdd-a"]["status"], "accepted", recovered.nodes["sdd-a"].get("reason")
        )

    def test_digest_matching_neither_algorithm_is_still_refused_live(self):
        monitor, runner, snapshot = self._v507_started()
        snapshot.nodes["sdd-a"]["contract_digest"] = _foreign_digest([monitor.nodes["sdd-a"]])
        save_snapshot(self.paths, snapshot)

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "blocked_unknown")
        self.assertIn(
            "does not match the live node contract", snapshot.nodes["sdd-a"]["reason"]
        )


if __name__ == "__main__":
    unittest.main()
