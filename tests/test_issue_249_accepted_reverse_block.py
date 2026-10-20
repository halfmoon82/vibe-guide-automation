"""Issue #249: a clean reviewer acceptance must never brick the node.

The reverse-block was an ordering defect, not a schema defect.  The monitor
recorded the ``accepted`` event and flipped the reviewer binding to
``accepted`` *before* checking the acceptance payload's top-level P0-P2
clearance evidence, so a reviewer that accepted without the evidence object
left the node simultaneously accepted (binding + ``acceptance`` fields) and
``blocked_unknown`` (snapshot) -- a state no recovery gate could reconcile:
the successor recoverer demanded a missing reviewer binding, ``repair``
only re-stamped binding_unknown, and ``reconcile`` required a package for
every unaccepted node.  The contract these tests pin:

1. **Validate first (live path).**  A missing evidence object is a format
   error: no event is recorded, no binding is flipped, the handle stays
   alive and the same reviewer session can re-report a corrected
   acceptance -- exactly the ISSUE-83 contract, now for the reviewer
   acceptance event.
2. **A durable acceptance wins on replay.**  An ``accepted`` record written
   by the ordering bug replays as the acceptance it asserts (identity,
   generation and contract digest were validated before it was recorded),
   not as a fresh block.
3. **The reverse-blocked node is rescued.**  A snapshot already carrying
   the corrupted ``blocked_unknown`` + written acceptance + accepted
   reviewer binding is completed by the recovery pass on the next resume,
   leaving the node accepted with a zero clearance and an archived pair.
"""
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.contracts import RunEvent
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.monitor import Monitor
from vibe_guide.state import append_event, load_events, save_snapshot
from vibe_guide.task_registry import load_task_binding

REVERSE_BLOCK_REASON = (
    "review acceptance has no registered P0-P2 clearance evidence"
)


def node(node_id, depends_on=None):
    return DAGNode(
        node_id,
        node_id,
        depends_on or [],
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


def acceptance_evidence():
    """The top-level evidence object an acceptance payload must carry."""
    return {
        "findings": [],
        "iteration_compatibility": {"evidence": "compatible", "status": "verified"},
        "out_of_scope": [],
        "test_runtime_delivery": {"evidence": "pytest: 10 passed", "status": "verified"},
    }


class Issue249AcceptedReverseBlockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = ProjectPaths(Path(self.temporary.name))
        (self.paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8"
        )
        save_contract(
            self.paths,
            build_contract(self.paths.root, provider="fake", host_id="local"),
        )
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")

    def _monitor(self):
        return self._fresh_monitor()

    def _fresh_monitor(self):
        """Build an isolated monitor environment.

        Loop-driven guard cases must never share a project root: the
        previous run's durable residue (leases, registry entries) changes
        how the next run dispatches.
        """
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        paths = ProjectPaths(Path(temporary.name))
        (paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8"
        )
        save_contract(
            paths,
            build_contract(paths.root, provider="fake", host_id="local"),
        )
        self.paths = paths
        nodes = [node("n1")]
        plan = Plan("plan-1", 1, "docs/prd.md", [item.id for item in nodes], "draft")
        card = build_authorization_card(plan, nodes, self.capabilities)
        return Monitor(paths, plan, nodes), authorize(card, "AUTHORIZE")

    def _events(self, run_id, name=None):
        records = load_events(self.paths, run_id)
        if name is None:
            return records
        return [r for r in records if r["event"] == name]

    def _node_events(self, run_id, name):
        return [r for r in self._events(run_id, name) if r["data"].get("node_id") == "n1"]

    def _drive_to_reviewer(self, monitor, record, reviewer_events=()):
        """Start the run and dispatch the reviewer, leaving its handle live.

        One runner instance spans every tick: the fixture maps handles to
        nodes per instance, so a fresh runner could not poll the reviewer
        handle the first instance created.
        """
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery"})],
                ("n1", "reviewer"): list(reviewer_events),
            }
        )
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        current = snapshot.nodes["n1"]
        self.assertEqual(current["status"], "review")
        self.assertIsInstance(current.get("active_task"), dict)
        self.assertEqual(current["active_task"]["role"], "reviewer")
        self.assertIn("n1", snapshot.handles)
        return snapshot, runner, current["active_task"]

    # ------------------------------------------------------------------
    # 1. Live path: validate before recording.
    # ------------------------------------------------------------------
    def test_accepted_without_evidence_is_a_format_rejection_then_re_report_closes(self):
        monitor, record = self._monitor()
        snapshot, runner, _reviewer_active = self._drive_to_reviewer(
            monitor,
            record,
            reviewer_events=[
                # First report: reviewer accepted but carried no
                # top-level P0-P2 clearance evidence object.
                ("accepted", {"delivery_evidence": {"thread_status": "complete"}}),
                # Corrected re-report on the same live handle.
                ("accepted", {"evidence": acceptance_evidence()}),
            ],
        )
        snapshot = monitor.tick(snapshot.run_id, runner)

        current = snapshot.nodes["n1"]
        # Zero lifecycle pollution: nothing accepted, nothing bricked, and
        # the reviewer session handle is still alive for the re-report.
        self.assertNotEqual(current["status"], "blocked_unknown", current.get("reason"))
        self.assertNotEqual(current["status"], "accepted")
        self.assertIn("n1", snapshot.handles)
        self.assertIsInstance(current.get("active_task"), dict)
        self.assertEqual(self._node_events(snapshot.run_id, "accepted"), [])
        self.assertIsNone(current.get("acceptance"))
        # The reviewer binding was never flipped by the rejected report.
        binding = load_task_binding(self.paths, "n1", "reviewer", run_id=snapshot.run_id)
        self.assertNotEqual(binding.status, "accepted")
        rejections = self._node_events(snapshot.run_id, "acceptance_rejected")
        self.assertEqual(len(rejections), 1, self._events(snapshot.run_id))
        self.assertTrue(rejections[0]["data"].get("recoverable"))
        # A recoverable format error is automatic recovery, not a user
        # decision.
        from vibe_guide.state import map_user_status

        self.assertNotEqual(map_user_status(current), "需要你决定")

        snapshot = monitor.tick(snapshot.run_id, runner)

        current = snapshot.nodes["n1"]
        self.assertEqual(current["status"], "accepted")
        self.assertEqual(current["review_clearance"], {"p0": 0, "p1": 0, "p2": 0})
        self.assertTrue(current.get("pair_archived"))
        self.assertIsNone(current.get("reason"))
        self.assertEqual(len(self._node_events(snapshot.run_id, "accepted")), 1)
        self.assertEqual(len(self._node_events(snapshot.run_id, "acceptance_rejected")), 1)

    # ------------------------------------------------------------------
    # 2. Replay: a durable acceptance record is the acceptance.
    # ------------------------------------------------------------------
    def test_replayed_accepted_without_evidence_completes_instead_of_reblocking(self):
        monitor, record = self._monitor()
        snapshot, _runner, reviewer_active = self._drive_to_reviewer(monitor, record)
        current = snapshot.nodes["n1"]
        # Append the durable record exactly as the ordering bug wrote it:
        # the accepted event with no top-level evidence object.  The
        # snapshot watermark stays behind, so the record is unapplied.
        data = {
            "run_id": snapshot.run_id,
            "node_id": "n1",
            "role": "reviewer",
            "task_id": reviewer_active["task_id"],
            "handle_id": reviewer_active["handle_id"],
            "generation": reviewer_active["generation"],
            "contract_digest": current["contract_digest"],
            "authorization_epoch": snapshot.authorization_digest,
        }
        provenance = {
            "role": "reviewer",
            "task_id": reviewer_active["task_id"],
            "handle_id": reviewer_active["handle_id"],
            "generation": reviewer_active["generation"],
            "authorization_digest": snapshot.authorization_digest,
            "node_contract_digest": snapshot.node_contract_digest,
        }
        append_event(self.paths, RunEvent("accepted", data), provenance)
        save_snapshot(self.paths, snapshot)

        resumed = monitor.resume(snapshot.run_id, FakeRunner())

        current = resumed.nodes["n1"]
        self.assertEqual(current["status"], "accepted")
        self.assertEqual(current["review_clearance"], {"p0": 0, "p1": 0, "p2": 0})
        self.assertTrue(current.get("pair_archived"))
        self.assertIsNone(current.get("reason"))
        self.assertIsNone(current.get("quarantine"))
        binding = load_task_binding(self.paths, "n1", "reviewer", run_id=snapshot.run_id)
        self.assertEqual(binding.status, "archived")

    # ------------------------------------------------------------------
    # 3. Rescue: the already-corrupted snapshot is completed on resume.
    # ------------------------------------------------------------------
    def test_reverse_blocked_acceptance_is_completed_on_resume(self):
        monitor, record = self._monitor()
        snapshot, _runner, reviewer_active = self._drive_to_reviewer(monitor, record)
        current = snapshot.nodes["n1"]
        # Reproduce the ordering bug by hand: record the acceptance, flip
        # the binding, write the acceptance fields, then reverse-block.
        acceptance_data = {
            "run_id": snapshot.run_id,
            "node_id": "n1",
            "role": "reviewer",
            "task_id": reviewer_active["task_id"],
            "handle_id": reviewer_active["handle_id"],
            "generation": reviewer_active["generation"],
            "contract_digest": current["contract_digest"],
            "authorization_epoch": snapshot.authorization_digest,
        }
        monitor._record_runner_event(
            snapshot, "n1", RunEvent("accepted", acceptance_data), reviewer_active
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

        # The corrupted state is exactly the observed deadlock signature.
        self.assertEqual(current["status"], "blocked_unknown")
        self.assertEqual(current["reason"], REVERSE_BLOCK_REASON)
        self.assertIsInstance(current.get("acceptance"), dict)
        # The reverse-block's own residue is stamped by _mark_blocked_unknown
        # and must not veto the rescue: reconcile_binding plus a quarantine
        # whose handle was already retired (handle_id null).
        self.assertEqual(current["recovery_action"], "reconcile_binding")
        self.assertIsNone(current["quarantine"].get("handle_id"))

        resumed = monitor.resume(snapshot.run_id, FakeRunner())

        recovered = resumed.nodes["n1"]
        self.assertEqual(recovered["status"], "accepted")
        self.assertEqual(recovered["review_clearance"], {"p0": 0, "p1": 0, "p2": 0})
        self.assertTrue(recovered.get("pair_archived"))
        self.assertIsNone(recovered.get("reason"))
        self.assertIsNone(recovered.get("quarantine"))
        self.assertIsNone(recovered.get("recovery_action"))
        self.assertIsInstance(recovered.get("acceptance"), dict)
        binding = load_task_binding(self.paths, "n1", "reviewer", run_id=snapshot.run_id)
        self.assertEqual(binding.status, "archived")
        # The rescue is auditable, not silent.
        self.assertEqual(len(self._node_events(snapshot.run_id, "acceptance_recovered")), 1)

    def test_rescue_leaves_other_blocked_unknown_nodes_untouched(self):
        monitor, record = self._monitor()
        snapshot, _runner, _reviewer_active = self._drive_to_reviewer(monitor, record)
        current = snapshot.nodes["n1"]
        current["status"] = "blocked_unknown"
        current["reason"] = "some genuinely unknown provider failure"
        current["acceptance"] = None
        monitor._save_snapshot(snapshot)

        resumed = monitor.resume(snapshot.run_id, FakeRunner())

        # A different blocked_unknown must not be rescued by signature: the
        # node stays blocked and the reason is not the reverse-block text
        # (the raw reason itself is redacted on the snapshot round-trip).
        self.assertEqual(resumed.nodes["n1"]["status"], "blocked_unknown")
        self.assertNotEqual(resumed.nodes["n1"]["reason"], REVERSE_BLOCK_REASON)
        self.assertFalse(resumed.nodes["n1"].get("pair_archived"))
        self.assertIsNone(resumed.nodes["n1"].get("review_clearance"))
        self.assertEqual(self._node_events(snapshot.run_id, "acceptance_recovered"), [])

    # ------------------------------------------------------------------
    # 4. Signature fail-closed: drift and live work veto the rescue.
    # ------------------------------------------------------------------
    def _corrupt_reverse_block(
        self, monitor, snapshot, reviewer_active, epoch=None, digest=None, marker=None
    ):
        """Hand-build the #249 corrupted snapshot, optionally drifted."""
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
        if epoch is not None:
            current["acceptance"]["authorization_epoch"] = epoch
        if digest is not None:
            current["acceptance"]["contract_digest"] = digest
        current["reason"] = None
        current["active_role"] = None
        current["active_task"] = None
        current["quarantine"] = None
        snapshot.handles.pop("n1", None)
        monitor._mark_blocked_unknown(snapshot, "n1", REVERSE_BLOCK_REASON)
        # Live-work markers ride on top of the faithful corruption: the
        # reverse-block's own reconcile_binding/quarantine stamps are part of
        # the rescued signature, so veto markers must be applied after it.
        if marker is not None:
            key, value = marker
            if key == "handle":
                snapshot.handles["n1"] = reviewer_active["handle_id"]
            else:
                current[key] = value
        monitor._save_snapshot(snapshot)
        return snapshot

    def test_rescue_signature_rejects_epoch_and_digest_drift(self):
        drifts = [
            ("epoch", {"epoch": "epoch-from-another-authorization"}),
            ("digest", {"digest": "0" * 64}),
        ]
        for label, kwargs in drifts:
            with self.subTest(drift=label):
                monitor, record = self._fresh_monitor()
                snapshot, _runner, reviewer_active = self._drive_to_reviewer(
                    monitor, record
                )
                self._corrupt_reverse_block(monitor, snapshot, reviewer_active, **kwargs)

                resumed = monitor.resume(snapshot.run_id, FakeRunner())

                self.assertEqual(resumed.nodes["n1"]["status"], "blocked_unknown")
                self.assertIsNone(resumed.nodes["n1"].get("review_clearance"))
                self.assertFalse(resumed.nodes["n1"].get("pair_archived"))
                self.assertEqual(
                    self._node_events(snapshot.run_id, "acceptance_recovered"), []
                )

    def test_rescue_signature_rejects_live_work_markers(self):
        markers = [
            ("active_task", {"role": "reviewer", "task_id": "t", "generation": 1}),
            ("active_role", "reviewer"),
            ("start_intent", {"role": "reviewer", "phase": "review"}),
            ("retryable_action", {"role": "reviewer", "phase": "review"}),
            # A recovery action other than the reverse-block's own
            # reconcile_binding stamp means somebody else is mid-recovery.
            ("recovery_action", "retry_same_task"),
            # A quarantine still bound to a live handle is live work; the
            # reverse-block's own stamp always carries handle_id null.
            ("quarantine", {"handle_id": "live-handle-1", "reason": "provider stall"}),
            ("handle", None),
        ]
        for key, value in markers:
            with self.subTest(marker=key):
                monitor, record = self._fresh_monitor()
                snapshot, _runner, reviewer_active = self._drive_to_reviewer(
                    monitor, record
                )
                self._corrupt_reverse_block(
                    monitor, snapshot, reviewer_active, marker=(key, value)
                )

                resumed = monitor.resume(snapshot.run_id, FakeRunner())

                # Live work vetoes the rescue: the node stays blocked, the
                # closeout never runs, and no audit event claims otherwise.
                self.assertEqual(resumed.nodes["n1"]["status"], "blocked_unknown")
                self.assertIsNone(resumed.nodes["n1"].get("review_clearance"))
                self.assertFalse(resumed.nodes["n1"].get("pair_archived"))
                self.assertEqual(
                    self._node_events(snapshot.run_id, "acceptance_recovered"), []
                )

    def test_rescue_write_failure_restores_every_field_and_retries_next_resume(self):
        monitor, record = self._monitor()
        snapshot, _runner, reviewer_active = self._drive_to_reviewer(monitor, record)
        current = snapshot.nodes["n1"]
        current["quarantine"] = None
        current["recovery_action"] = None
        self._corrupt_reverse_block(monitor, snapshot, reviewer_active)
        original_record = monitor._record

        def flaky_record(snap, event, data):
            if event == "acceptance_recovered":
                raise OSError("simulated append failure")
            return original_record(snap, event, data)

        monitor._record = flaky_record
        try:
            resumed = monitor.resume(snapshot.run_id, FakeRunner())
        finally:
            monitor._record = original_record

        # Fail closed with zero half-done residue: the node is still
        # blocked_unknown with its original reason, the pair is NOT
        # archived, and the reviewer binding was never flipped to archived
        # -- the very residue that would mint a new deadlock.
        blocked = resumed.nodes["n1"]
        self.assertEqual(blocked["status"], "blocked_unknown")
        self.assertFalse(blocked.get("pair_archived"))
        self.assertIsNone(blocked.get("review_clearance"))
        binding = load_task_binding(self.paths, "n1", "reviewer", run_id=resumed.run_id)
        self.assertEqual(binding.status, "accepted")
        self.assertEqual(
            self._node_events(snapshot.run_id, "acceptance_recovered"), []
        )

        # The next pass retries cleanly from the restored state.
        resumed = monitor.resume(resumed.run_id, FakeRunner())
        recovered = resumed.nodes["n1"]
        self.assertEqual(recovered["status"], "accepted")
        self.assertTrue(recovered.get("pair_archived"))
        binding = load_task_binding(self.paths, "n1", "reviewer", run_id=resumed.run_id)
        self.assertEqual(binding.status, "archived")
        self.assertEqual(
            len(self._node_events(resumed.run_id, "acceptance_recovered")), 1
        )

    def test_rescue_lease_failure_before_mutation_is_a_clean_noop(self):
        monitor, record = self._monitor()
        snapshot, _runner, reviewer_active = self._drive_to_reviewer(monitor, record)
        self._corrupt_reverse_block(monitor, snapshot, reviewer_active)
        original_release = monitor._release_node_lease

        def boom(snap, node_id):
            raise OSError("simulated lease unlink failure")

        monitor._release_node_lease = boom
        try:
            resumed = monitor.resume(snapshot.run_id, FakeRunner())
        finally:
            monitor._release_node_lease = original_release

        # The lease release runs before any mutation, so the failed pass
        # leaves the signature intact for the next resume.
        blocked = resumed.nodes["n1"]
        self.assertEqual(blocked["status"], "blocked_unknown")
        self.assertFalse(blocked.get("pair_archived"))
        binding = load_task_binding(self.paths, "n1", "reviewer", run_id=resumed.run_id)
        self.assertEqual(binding.status, "accepted")

        resumed = monitor.resume(resumed.run_id, FakeRunner())
        self.assertEqual(resumed.nodes["n1"]["status"], "accepted")
        self.assertTrue(resumed.nodes["n1"].get("pair_archived"))


if __name__ == "__main__":
    unittest.main()
