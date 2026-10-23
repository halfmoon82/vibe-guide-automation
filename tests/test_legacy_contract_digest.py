"""Runs authorized by 5.0.7 must stay readable after the #182 digest change.

#182 dropped ``files``/``allowlist`` from ``executable_contract_digest``.
Every digest 5.0.7 persisted (authorization record, reauthorization event,
per-node ``contract_digest``) was computed with file scope included, so a
newer reader recomputing with the scope-free algorithm rejected those runs.
The fixture reproduces 5.0.7 by patching the digest back to the
scope-inclusive algorithm while the run is written, then reads it unpatched.
"""

import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from vibe_guide.authorization import (
    _canonical_digest,
    authorize,
    build_authorization_card,
    canonical_node_contracts,
    executable_contract_digest,
    executable_contract_digest_matches,
    is_authorization_valid,
    legacy_executable_contract_digest,
)
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.models import AgentCapabilities, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import (
    scope_only_expansion_binds,
    _decode_snapshot,
    load_events,
    load_snapshot,
    run_dir,
    save_snapshot,
)

# Module imports, not class imports: a TestCase bound at module level here
# would be collected and run a second time by discovery.
from tests import test_reauthorization_gates as _reauthorization_gates
from tests import test_visible_sdd_contract as _visible_sdd_contract


def _digest_as_507(nodes):
    # 5.0.7's executable_contract_digest, verbatim: file scope included.
    return _canonical_digest({"nodes": canonical_node_contracts(nodes)})


def _as_written_by_507():
    stack = ExitStack()
    for module in ("authorization", "monitor", "state"):
        stack.enter_context(
            patch(
                "vibe_guide.{}.executable_contract_digest".format(module),
                _digest_as_507,
            )
        )
    return stack


class LegacyContractDigestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vg-legacy-digest-")
        self.root = Path(self.temporary.name)
        self.paths = ProjectPaths(self.root)
        (self.paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n',
            encoding="utf-8",
        )
        save_contract(
            self.paths, build_contract(self.root, provider="fake", host_id="local")
        )
        self.capabilities = AgentCapabilities(
            "fake", True, True, True, True, True, "full"
        )
        self.plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _nodes(a_files=None):
        return _reauthorization_gates.ScopeOnlyReauthorizationTests._nodes(a_files=a_files)

    def _record(self, nodes):
        return authorize(
            build_authorization_card(self.plan, nodes, self.capabilities),
            "AUTHORIZE",
        )

    def _reauthorized_507_run(self):
        """Start, then reauthorize a scope expansion — all written as 5.0.7."""
        with _as_written_by_507():
            nodes = self._nodes()
            snapshot = Monitor(self.paths, self.plan, nodes).start(
                self._record(nodes), FakeRunner()
            )
            changed = self._nodes(a_files=["a.py", "tests/test_a.py"])
            snapshot = Monitor(self.paths, self.plan, changed).reauthorize(
                snapshot.run_id,
                self._record(changed),
                FakeRunner(),
                "executable_contract_changed",
            )
        return snapshot, changed

    def test_dual_visible_replay_accepts_a_507_acceptance(self):
        """A 5.0.7 acceptance event left unapplied replays under new code."""
        nodes = self._nodes()
        runner = self._accepting_runner()
        with _as_written_by_507():
            snapshot = Monitor(self.paths, self.plan, nodes).start(
                self._record(nodes), runner
            )
            monitor = Monitor(self.paths, self.plan, nodes)
            preserved = None
            for _ in range(10):
                if snapshot.nodes["b"]["status"] == "review":
                    preserved = copy.deepcopy(snapshot)
                snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertIsNotNone(preserved)
        self.assertEqual(snapshot.nodes["b"]["status"], "accepted")
        save_snapshot(self.paths, preserved)

        recovered = Monitor(self.paths, self.plan, nodes).tick(
            snapshot.run_id, runner
        )

        self.assertEqual(recovered.nodes["b"]["status"], "accepted")
        self.assertEqual(
            recovered.nodes["b"]["acceptance"]["contract_digest"],
            _digest_as_507([nodes[1]]),
        )

    def _accepted_507_run(self):
        """Both nodes accepted under 5.0.7, so every stored digest is the old form."""
        nodes = self._nodes()
        runner = self._accepting_runner()
        with _as_written_by_507():
            snapshot = Monitor(self.paths, self.plan, nodes).start(
                self._record(nodes), runner
            )
            monitor = Monitor(self.paths, self.plan, nodes)
            for _ in range(10):
                snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(
            {node_id: current["status"] for node_id, current in snapshot.nodes.items()},
            {"a": "accepted", "b": "accepted"},
        )
        return snapshot

    def _reauthorize_under_new_code(self, snapshot, nodes):
        rebound = Monitor(self.paths, self.plan, nodes).reauthorize(
            snapshot.run_id,
            self._record(nodes),
            FakeRunner(),
            "executable_contract_changed",
        )
        load_snapshot(self.paths, rebound.run_id)
        Monitor(self.paths, self.plan, nodes).tick(rebound.run_id, FakeRunner())
        return rebound

    def test_new_code_reauthorization_keeps_scope_only_expanded_507_nodes(self):
        """#224: a 5.0.7 run whose node only gained files keeps its acceptance."""
        snapshot = self._accepted_507_run()
        changed = self._nodes()
        changed[1].contract["files"].append("tests/test_b.py")
        changed[1].contract["worker_profile"]["allowlist"].append("tests/test_b.py")
        rebound = self._reauthorize_under_new_code(snapshot, changed)
        for index, node_id in enumerate(("a", "b")):
            self.assertEqual(rebound.nodes[node_id]["status"], "accepted")
            self.assertEqual(
                rebound.nodes[node_id]["acceptance"]["contract_digest"],
                executable_contract_digest([changed[index]]),
            )
        # Replay of the recorded disposition accepts it too.
        load_snapshot(self.paths, rebound.run_id)

    def test_scope_expansion_with_a_semantic_change_is_still_invalidated(self):
        snapshot = self._accepted_507_run()
        changed = self._nodes()
        changed[1].contract["files"].append("tests/test_b.py")
        changed[1].contract["worker_profile"]["allowlist"].append("tests/test_b.py")
        changed[1].contract["acceptance_example"] = "verified_fact: changed"
        rebound = self._reauthorize_under_new_code(snapshot, changed)
        self.assertEqual(rebound.nodes["a"]["status"], "accepted")
        self.assertNotEqual(rebound.nodes["b"]["status"], "accepted")
        self.assertIsNone(rebound.nodes["b"]["acceptance"])

    def test_union_increment_cancelling_between_nodes_redispatches(self):
        """Known ceiling: A adds and B drops the same file, so nothing is provable."""
        nodes = self._nodes()
        nodes[1].contract["files"].append("shared.py")
        nodes[1].contract["worker_profile"]["allowlist"].append("shared.py")
        runner = self._accepting_runner()
        with _as_written_by_507():
            snapshot = Monitor(self.paths, self.plan, nodes).start(
                self._record(nodes), runner
            )
            monitor = Monitor(self.paths, self.plan, nodes)
            for _ in range(10):
                snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["a"]["status"], "accepted")
        changed = self._nodes()
        changed[0].contract["files"].append("shared.py")
        changed[0].contract["worker_profile"]["allowlist"].append("shared.py")
        rebound = self._reauthorize_under_new_code(snapshot, changed)
        self.assertNotEqual(rebound.nodes["a"]["status"], "accepted")
        self.assertNotEqual(rebound.nodes["b"]["status"], "accepted")

    def test_new_code_reauthorization_of_unchanged_507_dag_keeps_everything(self):
        snapshot = self._accepted_507_run()
        rebound = self._reauthorize_under_new_code(snapshot, self._nodes())
        self.assertEqual(
            {node_id: current["status"] for node_id, current in rebound.nodes.items()},
            {"a": "accepted", "b": "accepted"},
        )

    def test_new_code_reauthorization_still_invalidates_a_507_semantic_change(self):
        snapshot = self._accepted_507_run()
        changed = _reauthorization_gates.ScopeOnlyReauthorizationTests._nodes(
            a_acceptance="verified_fact: changed"
        )
        rebound = self._reauthorize_under_new_code(snapshot, changed)
        self.assertEqual(rebound.nodes["a"]["status"], "rework")
        self.assertEqual(rebound.nodes["b"]["status"], "blocked_unknown")

    def _decode_with(self, run_id, mutate_event=None, mutate_state=None):
        records = copy.deepcopy(load_events(self.paths, run_id))
        state = json.loads((run_dir(self.paths, run_id) / "state.json").read_text("utf-8"))
        for record in records:
            if record.get("event") == "authorization_reauthorized" and mutate_event:
                mutate_event(record["data"])
        if mutate_state:
            mutate_state(state)
        return _decode_snapshot(json.dumps(state).encode("utf-8"), records)

    def test_fixture_really_writes_the_old_algorithm(self):
        snapshot, changed = self._reauthorized_507_run()
        self.assertNotEqual(executable_contract_digest(changed), _digest_as_507(changed))
        self.assertEqual(snapshot.node_contract_digest, _digest_as_507(changed))

    def test_reauthorized_507_run_loads(self):
        snapshot, _changed = self._reauthorized_507_run()
        loaded = load_snapshot(self.paths, snapshot.run_id)
        self.assertEqual(loaded.authorization_digest, snapshot.authorization_digest)

    def test_507_authorization_record_stays_valid_against_live_nodes(self):
        nodes = self._nodes()
        with _as_written_by_507():
            record = self._record(nodes)
        self.assertTrue(is_authorization_valid(record, self.plan, nodes))

    def test_507_record_is_rejected_when_the_contract_drifted(self):
        nodes = self._nodes()
        with _as_written_by_507():
            record = self._record(nodes)
        drifted = self._nodes()
        drifted[0].contract["acceptance_example"] = "verified_fact: changed"
        self.assertFalse(is_authorization_valid(record, self.plan, drifted))

    @staticmethod
    def _accepting_runner():
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

    def test_507_dispatched_nodes_reach_acceptance_under_new_code(self):
        nodes = self._nodes()
        runner = self._accepting_runner()
        with _as_written_by_507():
            snapshot = Monitor(self.paths, self.plan, nodes).start(
                self._record(nodes), runner
            )
        self.assertEqual(
            snapshot.nodes["a"]["contract_digest"], _digest_as_507([nodes[0]])
        )
        monitor = Monitor(self.paths, self.plan, nodes)
        for _ in range(10):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(
            {node_id: current["status"] for node_id, current in snapshot.nodes.items()},
            {"a": "accepted", "b": "accepted"},
        )
        self.assertEqual(
            load_snapshot(self.paths, snapshot.run_id).nodes["b"]["status"], "accepted"
        )

    def test_reauthorized_507_run_ticks_like_507(self):
        snapshot, changed = self._reauthorized_507_run()
        monitor = Monitor(self.paths, self.plan, changed)
        for _ in range(3):
            snapshot = monitor.tick(snapshot.run_id, self._accepting_runner())
        # 5.0.7 itself leaves a scope-expanded node waiting for its continuation.
        self.assertEqual(
            {node_id: current["status"] for node_id, current in snapshot.nodes.items()},
            {"a": "rework", "b": "planned"},
        )
        load_snapshot(self.paths, snapshot.run_id)

    def test_untouched_proof_decodes(self):
        snapshot, _changed = self._reauthorized_507_run()
        self._decode_with(snapshot.run_id)

    def test_tampered_507_proof_is_rejected(self):
        snapshot, _changed = self._reauthorized_507_run()

        def tamper(data):
            node = json.loads(data["authorized_node_contracts"]["a"])
            node["contract"]["files"] = ["evil.py"]
            data["authorized_node_contracts"]["a"] = json.dumps(
                node, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )

        with self.assertRaisesRegex(ValueError, "authorized DAG proof is invalid"):
            self._decode_with(snapshot.run_id, mutate_event=tamper)

    def test_mixed_algorithms_in_one_proof_are_rejected(self):
        """An old aggregate beside a new per-node digest is a proof neither wrote."""
        snapshot, changed = self._reauthorized_507_run()
        new_b = executable_contract_digest([n for n in changed if n.id == "b"])

        def mix_event(data):
            data["node_contract_digests"]["b"] = new_b
            data["previous_node_contract_digests"]["b"] = new_b

        def mix_state(state):
            state["nodes"]["b"]["contract_digest"] = new_b

        with self.assertRaisesRegex(ValueError, "authorized DAG proof is invalid"):
            self._decode_with(
                snapshot.run_id, mutate_event=mix_event, mutate_state=mix_state
            )



class LegacyVisibleSddReplayTests(unittest.TestCase):
    setUp = _visible_sdd_contract.VisibleSddAcceptanceMonitorTests.setUp
    tearDown = _visible_sdd_contract.VisibleSddAcceptanceMonitorTests.tearDown
    _monitor = _visible_sdd_contract.VisibleSddAcceptanceMonitorTests._monitor
    _runner = staticmethod(_visible_sdd_contract.VisibleSddAcceptanceMonitorTests._runner)

    def test_visible_sdd_replay_accepts_a_507_acceptance(self):
        with _as_written_by_507():
            monitor, record = self._monitor()
            runner = self._runner()
            snapshot = monitor.start(record, runner)
            preserved = copy.deepcopy(snapshot)
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "accepted")
        legacy = _digest_as_507([monitor.nodes["sdd-a"]])
        self.assertEqual(snapshot.nodes["sdd-a"]["contract_digest"], legacy)
        save_snapshot(self.paths, preserved)

        recovered = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(recovered.nodes["sdd-a"]["status"], "accepted")
        self.assertEqual(
            recovered.nodes["sdd-a"]["acceptance"]["contract_digest"], legacy
        )


class ScopeOnlyExpansionPredicateTests(unittest.TestCase):
    def test_unhashable_scope_entries_are_unprovable_not_errors(self):
        node = LegacyContractDigestTests._nodes()[1]
        node.contract["worker_profile"]["allowlist"].append({"bad": 1})
        self.assertFalse(
            scope_only_expansion_binds(
                node, legacy_executable_contract_digest([node]), ["x"], ["x", "y"]
            )
        )


class MalformedContractDigestTests(unittest.TestCase):
    """A corrupt persisted digest is a mismatch, never a crash."""

    def test_both_algorithms_still_match(self):
        self.assertTrue(
            executable_contract_digest_matches([], executable_contract_digest([]))
        )
        self.assertTrue(
            executable_contract_digest_matches(
                [], legacy_executable_contract_digest([])
            )
        )

    def test_malformed_digests_are_mismatches_not_errors(self):
        current = executable_contract_digest([])
        for digest in ("\u00e9", "\u00e9" * 64, current.upper(), current[:-1], "", None, 7):
            with self.subTest(digest=digest):
                self.assertFalse(executable_contract_digest_matches([], digest))


if __name__ == "__main__":
    unittest.main()
