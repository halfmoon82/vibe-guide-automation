"""Issue #256: a recovery decision may only read facts that survive persistence.

#249/#250 hit the same distortion three ways: the fact a recovery or rollback
gate decided on was not the fact that was true.  This file pins the contract
that closes the class, not just the one path:

1. **Readable facts (contract ①).**  Gates capture ``RecoveryFacts`` /
   ``BindingFacts`` before the first mutation and decide -- and roll back --
   from those copies.  ``_load_task_binding`` hands out a shared/cached
   ``TaskBinding`` that ``_archive_pair`` mutates in place, so re-reading
   ``.status`` in a rollback can only see ``archived`` and turns the rollback
   into a no-op (the #250 ``019a619`` trace).
2. **Redaction whitelist (contract ②).**  The structural keys a signature
   needs -- the ``acceptance`` object, ``contract_digest``, the epoch,
   binding status/identity/generation -- must read back verbatim.  The
   free-text ``reason`` next to them stays redacted, and a gate that finds a
   placeholder in a signature fact fails closed instead of comparing it.
3. **Defect-adjacent markers (contract ③).**  ``_mark_blocked_unknown``
   always stamps ``recovery_action`` plus a ``quarantine`` carrying the
   retired handle.  Only ``{None, reconcile_binding}`` with a null
   ``handle_id`` may pass a rescue; anything else still vetoes.
4. **Named audit (contract ④).**  The call sites of ``_archive_pair`` /
   ``_release_node_lease`` / ``_mark_blocked_unknown`` each carry a recorded
   verdict: same type and fixed, or different type and argued.
"""
import ast
import tempfile
import unittest
from pathlib import Path

import vibe_guide.monitor as monitor_module
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.contracts import RunEvent
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import (
    DEFECT_ADJACENT_RECOVERY_ACTIONS,
    RECOVERY_FIDELITY_AUDIT,
    BindingFacts,
    Monitor,
    RecoveryFacts,
    defect_adjacent_marker_allows_rescue,
)
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import (
    _PROVIDER_TEXT_KEYS,
    RECOVERY_SIGNATURE_KEYS,
    _sanitize_durable_value,
    is_redacted_value,
    load_events,
    load_snapshot,
)
from vibe_guide.task_registry import TaskBinding, load_task_binding, save_task_binding

REVERSE_BLOCK_REASON = "review acceptance has no registered P0-P2 clearance evidence"
PLACEHOLDER = "[REDACTED_PROVIDER_TEXT]"

#: One structural value per whitelisted signature key.  Deliberately free of
#: provider text so the projection must be an identity.
SIGNATURE_NODE = {
    "acceptance": {"contract_digest": "a" * 64, "authorization_epoch": "e" * 64},
    "active_role": None,
    "active_task": None,
    "contract_digest": "a" * 64,
    "developer_generation": 2,
    "developer_identity": "dev-task-1",
    "handle_id": None,
    "pair_archived": False,
    "quarantine": {"handle_id": None, "run_id": "run-1"},
    "recovery_action": "reconcile_binding",
    "review_clearance": {"p0": 0, "p1": 0, "p2": 0},
    "review_generation": 2,
    "reviewer_identity": "rev-task-1",
    "retryable_action": None,
    "start_intent": None,
    "status": "blocked_unknown",
}


def node(node_id):
    return DAGNode(
        node_id,
        node_id,
        [],
        [],
        "g1",
        {
            "files": [node_id + ".py"],
            "worker": "worker-" + node_id,
            "worktree": ".worktrees/" + node_id,
            "worker_profile": {
                "worker": "codex",
                "model": "test",
                "reasoning": "normal",
                "fallbacks": [],
                "selection_basis": {
                    "issue_complexity_ref": node_id,
                    "complexity_band": "standard",
                    "risk_tags": [],
                    "availability_evidence": "test",
                },
                "writer": "writer",
                "worktree": ".worktrees/" + node_id,
                "branch": "branch-" + node_id,
                "allowlist": [node_id + ".py"],
            },
        },
        "ready",
    )


class _Fixture(unittest.TestCase):
    """Isolated monitor environment for the end-to-end cases."""

    def setUp(self):
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")

    def _fresh_monitor(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        paths = ProjectPaths(Path(temporary.name))
        (paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8"
        )
        save_contract(
            paths, build_contract(paths.root, provider="fake", host_id="local")
        )
        self.paths = paths
        nodes = [node("n1")]
        plan = Plan("plan-1", 1, "docs/prd.md", [item.id for item in nodes], "draft")
        card = build_authorization_card(plan, nodes, self.capabilities)
        return Monitor(paths, plan, nodes), authorize(card, "AUTHORIZE")

    def _node_events(self, run_id, name):
        return [
            record
            for record in load_events(self.paths, run_id)
            if record["event"] == name and record["data"].get("node_id") == "n1"
        ]

    def _drive_to_reviewer(self, monitor, record):
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery"})],
                ("n1", "reviewer"): [],
            }
        )
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["n1"]["status"], "review")
        return snapshot, snapshot.nodes["n1"]["active_task"]

    def _corrupt_reverse_block(self, monitor, snapshot, reviewer_active):
        """Reproduce the #249 ordering defect on the durable state."""
        current = snapshot.nodes["n1"]
        monitor._record_runner_event(
            snapshot,
            "n1",
            RunEvent(
                "accepted",
                {
                    "run_id": snapshot.run_id,
                    "node_id": "n1",
                    "role": "reviewer",
                    "task_id": reviewer_active["task_id"],
                    "handle_id": reviewer_active["handle_id"],
                    "generation": reviewer_active["generation"],
                    "contract_digest": current["contract_digest"],
                    "authorization_epoch": snapshot.authorization_digest,
                },
            ),
            reviewer_active,
        )
        monitor._set_binding_status(snapshot, "n1", "reviewer", "accepted")
        current["status"] = "accepted"
        current["acceptance"] = {
            "contract_digest": current["contract_digest"],
            "authorization_epoch": snapshot.authorization_digest,
        }
        current["reason"] = None
        current["active_role"] = None
        current["active_task"] = None
        current["quarantine"] = None
        snapshot.handles.pop("n1", None)
        monitor._mark_blocked_unknown(snapshot, "n1", REVERSE_BLOCK_REASON)
        monitor._save_snapshot(snapshot)
        return snapshot


class RecoverySignatureWhitelistTests(unittest.TestCase):
    """Contract ②: the signature reads structural keys, never provider text."""

    def test_whitelist_covers_every_signature_key_and_no_provider_text_key(self):
        # The fixture below must cover the whole set, or the projection test
        # would silently stop guarding a newly added key.
        self.assertEqual(set(SIGNATURE_NODE), set(RECOVERY_SIGNATURE_KEYS))
        # A key in both sets would make every recovery comparison read a
        # placeholder -- the exact #256 failure.
        self.assertEqual(RECOVERY_SIGNATURE_KEYS & _PROVIDER_TEXT_KEYS, set())
        # ``reason`` is the provider text the signature must not depend on.
        self.assertNotIn("reason", RECOVERY_SIGNATURE_KEYS)
        self.assertIn("reason", _PROVIDER_TEXT_KEYS)

    def test_signature_keys_survive_the_durable_projection_verbatim(self):
        projected = _sanitize_durable_value(SIGNATURE_NODE)

        self.assertEqual(projected, SIGNATURE_NODE)
        for key in sorted(RECOVERY_SIGNATURE_KEYS):
            with self.subTest(key=key):
                self.assertFalse(
                    any(
                        is_redacted_value(value)
                        for value in _string_leaves(projected[key])
                    ),
                    (key, projected[key]),
                )

    def test_reason_next_to_the_signature_is_still_redacted(self):
        projected = _sanitize_durable_value(
            {"status": "blocked_unknown", "reason": REVERSE_BLOCK_REASON}
        )

        self.assertEqual(projected["status"], "blocked_unknown")
        self.assertEqual(projected["reason"], PLACEHOLDER)
        self.assertTrue(is_redacted_value(projected["reason"]))

    def test_binding_identity_survives_the_registry_round_trip(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        paths = ProjectPaths(Path(temporary.name))
        (paths.vibe).mkdir(parents=True, exist_ok=True)
        (paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8"
        )
        binding = _binding("run-1")
        save_task_binding(paths, binding)

        loaded = load_task_binding(paths, "n1", "reviewer", run_id="run-1")

        # The signature reads status/identity/generation from here, so all
        # three must come back exactly as written.
        self.assertEqual(loaded.status, "accepted")
        self.assertEqual(loaded.task_id, "rev-task-1")
        self.assertEqual(loaded.generation, 2)


class RecoveryFactsCaptureTests(unittest.TestCase):
    """Contract ①: decisions and rollbacks read captured copies only."""

    def test_capture_is_a_copy_not_a_view_of_the_live_node(self):
        live = dict(SIGNATURE_NODE)
        live["acceptance"] = dict(SIGNATURE_NODE["acceptance"])
        facts = RecoveryFacts.capture("n1", live)

        live["status"] = "accepted"
        live["acceptance"]["contract_digest"] = "b" * 64

        self.assertEqual(facts.status, "blocked_unknown")
        self.assertEqual(facts.acceptance_contract_digest, "a" * 64)
        self.assertEqual(facts.unreadable, ())

    def test_unreadable_names_the_redacted_signature_facts(self):
        redacted = dict(SIGNATURE_NODE)
        redacted["status"] = PLACEHOLDER
        redacted["contract_digest"] = PLACEHOLDER
        redacted["acceptance"] = {
            "contract_digest": PLACEHOLDER,
            "authorization_epoch": "e" * 64,
        }

        facts = RecoveryFacts.capture("n1", redacted)

        self.assertEqual(
            facts.unreadable,
            ("status", "contract_digest", "acceptance.contract_digest"),
        )

    def test_binding_facts_survive_in_place_mutation(self):
        binding = _binding("run-1")
        facts = BindingFacts.capture("reviewer", binding)

        # This is exactly what _set_binding_status / _archive_pair do.
        binding.status = "archived"

        self.assertEqual(facts.status, "accepted")
        self.assertEqual(facts.task_id, "rev-task-1")
        self.assertEqual(facts.generation, 2)

    def test_binding_facts_reject_non_scalar_fields(self):
        # frozen=True alone would not stop a mutable field from being shared
        # with the live binding, so the copy refuses one outright.
        for field in ("status", "task_id", "generation"):
            with self.subTest(field=field):
                values = {"role": "reviewer", "status": "accepted",
                          "task_id": "rev-task-1", "generation": 2}
                values[field] = ["archived"]
                with self.assertRaises(TypeError):
                    BindingFacts(**values)


class DefectAdjacentMarkerTests(unittest.TestCase):
    """Contract ③: only the block's own fingerprint may pass a rescue."""

    def test_marker_criterion(self):
        cases = [
            ((None, None), True),
            (("reconcile_binding", None), True),
            (("reconcile_binding", {"handle_id": None}), True),
            (("reconcile_binding", {"handle_id": None, "reason": PLACEHOLDER}), True),
            # A quarantine still bound to a live handle is live work.
            (("reconcile_binding", {"handle_id": "live-handle"}), False),
            # Any other recovery action names a live recovery route.
            (("requeue_same_task", None), False),
            (("repair_node", {"handle_id": None}), False),
            (("reshape_delivery_and_reconcile", {"handle_id": None}), False),
            (("change_reviewer_or_split_node", None), False),
            # Malformed quarantine fails closed.
            ((None, "not-a-mapping"), False),
            ((None, ["handle_id"]), False),
        ]
        for (action, quarantine), expected in cases:
            with self.subTest(action=action, quarantine=quarantine):
                self.assertEqual(
                    defect_adjacent_marker_allows_rescue(action, quarantine), expected
                )

    def test_only_none_and_reconcile_binding_are_accepted_actions(self):
        self.assertEqual(
            DEFECT_ADJACENT_RECOVERY_ACTIONS, frozenset({None, "reconcile_binding"})
        )


class RescueFidelityTests(_Fixture):
    """The end-to-end contract: the persisted form must still be decidable."""

    def test_persisted_redaction_keeps_the_signature_and_the_rescue_hits(self):
        monitor, record = self._fresh_monitor()
        snapshot, reviewer_active = self._drive_to_reviewer(monitor, record)
        self._corrupt_reverse_block(monitor, snapshot, reviewer_active)

        # What the next process actually reads: provider text is a placeholder,
        # every signature fact is intact.
        persisted = load_snapshot(self.paths, snapshot.run_id).nodes["n1"]
        self.assertEqual(persisted["reason"], PLACEHOLDER)
        self.assertEqual(persisted["quarantine"]["reason"], PLACEHOLDER)
        self.assertIsNone(persisted["quarantine"]["handle_id"])
        self.assertEqual(persisted["recovery_action"], "reconcile_binding")
        self.assertEqual(
            persisted["acceptance"]["contract_digest"], snapshot.nodes["n1"]["contract_digest"]
        )

        resumed = monitor.resume(snapshot.run_id, FakeRunner())

        recovered = resumed.nodes["n1"]
        self.assertEqual(recovered["status"], "accepted")
        self.assertTrue(recovered.get("pair_archived"))
        self.assertEqual(
            len(self._node_events(snapshot.run_id, "acceptance_recovered")), 1
        )

    def test_rollback_restores_the_shared_binding_object_from_captured_facts(self):
        monitor, record = self._fresh_monitor()
        snapshot, reviewer_active = self._drive_to_reviewer(monitor, record)
        self._corrupt_reverse_block(monitor, snapshot, reviewer_active)
        # The monitor caches binding objects, so the rescue mutates the very
        # object a rollback would be tempted to re-read.
        cached = monitor._load_task_binding(snapshot, "n1", "reviewer")
        self.assertIs(
            monitor._binding_cache[(snapshot.run_id, "n1", "reviewer")], cached
        )
        self.assertEqual(cached.status, "accepted")
        original_record = monitor._record

        def flaky_record(snap, event, data):
            # Fails after _archive_pair already flipped both bindings.
            if event == "pair_archived":
                raise OSError("simulated append failure")
            return original_record(snap, event, data)

        monitor._record = flaky_record
        try:
            resumed = monitor.resume(snapshot.run_id, FakeRunner())
        finally:
            monitor._record = original_record

        # A rollback that read .status here would have written "archived"
        # again and left the node unrescuable forever.
        self.assertEqual(resumed.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(cached.status, "accepted")
        self.assertEqual(
            load_task_binding(self.paths, "n1", "developer", run_id=resumed.run_id).status,
            "delivered",
        )
        self.assertEqual(self._node_events(snapshot.run_id, "acceptance_recovered"), [])

        # The signature still matches, so the next pass closes the node.
        resumed = monitor.resume(resumed.run_id, FakeRunner())
        self.assertEqual(resumed.nodes["n1"]["status"], "accepted")
        self.assertTrue(resumed.nodes["n1"].get("pair_archived"))

    def test_rescue_refuses_when_a_signature_fact_reads_back_redacted(self):
        monitor, record = self._fresh_monitor()
        snapshot, reviewer_active = self._drive_to_reviewer(monitor, record)
        self._corrupt_reverse_block(monitor, snapshot, reviewer_active)
        current = snapshot.nodes["n1"]
        # Simulate the failure mode #256 is about: a signature fact that
        # persistence replaced with a placeholder.
        current["acceptance"]["contract_digest"] = PLACEHOLDER
        monitor._save_snapshot(snapshot)

        resumed = monitor.resume(snapshot.run_id, FakeRunner())

        # Fail closed: no comparison against a placeholder, no silent pass.
        self.assertEqual(resumed.nodes["n1"]["status"], "blocked_unknown")
        self.assertFalse(resumed.nodes["n1"].get("pair_archived"))
        self.assertIsNone(resumed.nodes["n1"].get("review_clearance"))
        self.assertEqual(self._node_events(snapshot.run_id, "acceptance_recovered"), [])


class RescueExceptionBoundaryTests(_Fixture):
    """The rescue declines on documented failures and never swallows the rest."""

    def _corrupted(self):
        monitor, record = self._fresh_monitor()
        snapshot, reviewer_active = self._drive_to_reviewer(monitor, record)
        self._corrupt_reverse_block(monitor, snapshot, reviewer_active)
        # Decide on the durable state, exactly like the next process does.
        return monitor, load_snapshot(self.paths, snapshot.run_id)

    def _rescue_with_release_failure(self, error):
        monitor, snapshot = self._corrupted()
        original = monitor._release_node_lease

        def boom(snap, node_id):
            raise error

        monitor._release_node_lease = boom
        try:
            outcome = monitor._recover_reverse_blocked_acceptance(snapshot, "n1")
        finally:
            monitor._release_node_lease = original
        return outcome, snapshot

    def test_documented_failures_decline_the_rescue_without_touching_state(self):
        # OSError is the unlink failure the ordering exists for; TypeError and
        # ValueError are the registry validators' failures.
        for error in (OSError("unlink"), FileNotFoundError("gone"),
                      TypeError("bad type"), ValueError("bad value")):
            with self.subTest(error=type(error).__name__):
                outcome, snapshot = self._rescue_with_release_failure(error)

                self.assertFalse(outcome)
                node = snapshot.nodes["n1"]
                self.assertEqual(node["status"], "blocked_unknown")
                self.assertFalse(node.get("pair_archived"))
                self.assertIsNone(node.get("review_clearance"))

    def test_unexpected_failures_propagate_instead_of_declining(self):
        # A programming error must fail the pass loudly, and Ctrl-C must stay
        # able to stop it: neither is a "rescue declined" outcome.
        for error in (RuntimeError("bug"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    self._rescue_with_release_failure(error)


class RecoveryFidelityAuditTests(unittest.TestCase):
    """Contract ④: the three named call surfaces carry a recorded verdict."""

    def test_audit_covers_exactly_the_three_named_sites(self):
        self.assertEqual(
            [entry["site"] for entry in RECOVERY_FIDELITY_AUDIT],
            ["_archive_pair", "_release_node_lease", "_mark_blocked_unknown"],
        )
        for entry in RECOVERY_FIDELITY_AUDIT:
            with self.subTest(site=entry["site"]):
                self.assertTrue(entry["callers"])

    def test_call_site_counts_match_the_source(self):
        # The audit's numbers are facts, not prose: count ``self.<site>(``
        # call expressions in monitor.py and require the declared count.
        tree = ast.parse(Path(monitor_module.__file__).read_text(encoding="utf-8"))
        counted = {entry["site"]: 0 for entry in RECOVERY_FIDELITY_AUDIT}
        for item in ast.walk(tree):
            if (
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and item.func.attr in counted
            ):
                counted[item.func.attr] += 1

        for entry in RECOVERY_FIDELITY_AUDIT:
            with self.subTest(site=entry["site"]):
                self.assertGreater(entry["call_sites"], 0)
                self.assertEqual(entry["call_sites"], counted[entry["site"]])

    def test_every_caller_entry_is_argued(self):
        for entry in RECOVERY_FIDELITY_AUDIT:
            for caller in entry["callers"]:
                with self.subTest(site=entry["site"], caller=caller["caller"]):
                    self.assertTrue(caller["caller"].strip())
                    self.assertIn(
                        caller["verdict"], {"same_type_fixed", "different_type"}
                    )
                    # A verdict without a reason is not an audit conclusion.
                    self.assertTrue(caller["rationale"].strip())

    def test_named_sites_still_exist(self):
        for entry in RECOVERY_FIDELITY_AUDIT:
            with self.subTest(site=entry["site"]):
                self.assertTrue(callable(getattr(Monitor, entry["site"], None)))


def _binding(run_id):
    return TaskBinding(
        provider="fake",
        mode="visible",
        issue_id="n1",
        role="reviewer",
        task_id="rev-task-1",
        host="local",
        generation=2,
        status="accepted",
        run_id=run_id,
        worktree=".worktrees/n1",
        branch="branch-n1",
    )


def _string_leaves(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _string_leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _string_leaves(item)
    elif isinstance(value, str):
        yield value


if __name__ == "__main__":
    unittest.main()
