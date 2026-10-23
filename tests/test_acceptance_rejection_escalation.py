"""Issues #254/#255: structured acceptance-format rejections and the escape
hatch when no live session can re-report.

The two defects are two faces of one function, ``_reject_acceptance_format``:

* #254 -- the free-text rejection reason is redacted to a single
  ``[REDACTED_PROVIDER_TEXT]`` placeholder on persistence, so an auditor could
  not tell a wrong key set from a wrong type from an empty value.  The
  rejection now also records a stable, machine-readable code built only from
  program constants (error class plus the involved key names / expected
  types), under an event key that is not provider text.
* #255 -- the observed incident (run-1ebcb792f2564205a5c4c585a2976250) had the
  reviewer session exit right after delivering, so nothing alive could
  re-report: every ``vibe resume`` tick re-read the same delivery and
  re-rejected it (events seq 101/105), and the node sat in ``review`` forever.
  Consecutive rejections of the same session are now counted durably, and past
  ``ACCEPTANCE_FORMAT_REJECTION_LIMIT`` the rejection escalates to a visible
  ``blocked_unknown`` with a recovery action instead of replaying.

The contract these tests pin:

1. **A first rejection stays recoverable (#249/#83 do not regress).**  The
   handle and active task survive, the node is not blocked, and the same
   session's corrected re-report closes the node.
2. **Escalation is terminal-but-walkable.**  Past the limit the node parks
   ``blocked_unknown`` with the human recovery action named, the replay loop
   stops, and the reshaped delivery still reaches ``accepted`` through
   ``Monitor.reconcile_evidence`` (the ``vibe reconcile`` entry) -- the
   escalation must not become a new deadlock.
3. **The code survives redaction, the free text does not.**  The persisted
   ``acceptance_rejected`` event carries the exact code while its ``reason``
   is the redaction placeholder.
"""
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.evidence import build_integration_review_evidence
from vibe_guide.models import AgentCapabilities, DAGNode, Plan, node_branch
from vibe_guide.monitor import (
    ACCEPTANCE_FORMAT_ESCALATION_RECOVERY,
    ACCEPTANCE_FORMAT_REJECTION_LIMIT,
    Monitor,
)
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import load_events, load_snapshot, save_snapshot
from vibe_guide.task_registry import TaskBinding, load_task_binding

# The lineage-complete snapshot stand-in and cleared claim from the closeout
# entry tests; the pure derivation reads the same lineage fields.
from tests.test_integration_review_closeout_entry import CLEARED_CLAIM, _snapshot


def _business_node(node_id):
    """A minimal dual-visible node: developer delivers, reviewer accepts."""
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


def _reviewer_evidence():
    """The top-level evidence object a reviewer acceptance must carry."""
    return {
        "findings": [],
        "iteration_compatibility": {"evidence": "compatible", "status": "verified"},
        "out_of_scope": [],
        "test_runtime_delivery": {"evidence": "pytest: 10 passed", "status": "verified"},
    }


class IntegrationClaimCodeTests(unittest.TestCase):
    """#254: every claim-shape deviation raises with a stable code."""

    def build(self, claim):
        return build_integration_review_evidence(_snapshot(), claim, ["AGENTS.md"], [])

    def test_codes_are_stable_and_assertable(self):
        cases = [
            ("P0-P2 cleared", "claim_shape:not_an_object"),
            (
                {key: value for key, value in CLEARED_CLAIM.items() if key != "out_of_scope"},
                "claim_shape:unexpected_keys:missing_out_of_scope",
            ),
            (
                dict(CLEARED_CLAIM, extra="reviewer-supplied"),
                "claim_shape:unexpected_keys:extra_only",
            ),
            (dict(CLEARED_CLAIM, findings="not-a-list"), "claim_shape:type:findings:list"),
            (
                dict(CLEARED_CLAIM, out_of_scope="not-a-list"),
                "claim_shape:type:out_of_scope:list",
            ),
            (
                dict(CLEARED_CLAIM, iteration_compatibility="not-an-object"),
                "claim_shape:type:iteration_compatibility:object",
            ),
            (
                dict(CLEARED_CLAIM, test_runtime_delivery="not-an-object"),
                "claim_shape:type:test_runtime_delivery:object",
            ),
            (
                dict(CLEARED_CLAIM, iteration_compatibility={"status": "verified", "evidence": ""}),
                "claim_shape:iteration_compatibility:empty_evidence",
            ),
            (
                dict(
                    CLEARED_CLAIM,
                    test_runtime_delivery={"status": "verified", "evidence": "   "},
                ),
                "claim_shape:test_runtime_delivery:empty_evidence",
            ),
            (
                dict(CLEARED_CLAIM, findings=["not-an-object"]),
                "claim_findings:invalid_entry",
            ),
            (
                dict(CLEARED_CLAIM, findings=[{"severity": "critical", "status": "open"}]),
                "claim_findings:invalid_severity",
            ),
            (
                dict(CLEARED_CLAIM, findings=[{"severity": "p0", "status": "maybe"}]),
                "claim_findings:invalid_status",
            ),
        ]
        for claim, expected in cases:
            with self.subTest(code=expected):
                with self.assertRaises(ValueError) as raised:
                    self.build(claim)
                # A ValueError subclass keeps every existing caller working.
                self.assertIsInstance(raised.exception, ValueError)
                self.assertEqual(raised.exception.code, expected)

    def test_the_free_text_message_still_names_the_schema(self):
        """The redacted reason must stay self-describing for the session."""
        with self.assertRaises(ValueError) as raised:
            self.build("P0-P2 cleared")
        self.assertIn("out_of_scope", str(raised.exception))


class AcceptanceRejectionMonitorTests(unittest.TestCase):
    """Live monitor path: reject, escalate, recover."""

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

    def _monitor(self, evidence_files=False):
        """Build an isolated monitor environment.

        Loop-driven guard cases must never share a project root: the previous
        run's durable residue (leases, registry entries) changes how the next
        run dispatches.  ``evidence_files`` declares the status/handoff files
        the reconcile recovery path verifies on disk.
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
        node = _business_node("n1")
        if evidence_files:
            node.contract.update({"status_file": "status.txt", "handoff_file": "handoff.md"})
        nodes = [node]
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

    def _drive_to_reviewer(self, monitor, record, reviewer_events):
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
        self.assertIn("n1", snapshot.handles)
        return snapshot, runner

    # ------------------------------------------------------------------
    # 1. First rejection stays recoverable (#249/#83 do not regress).
    # ------------------------------------------------------------------
    def test_first_format_rejection_keeps_the_re_report_channel(self):
        monitor, record = self._monitor()
        snapshot, runner = self._drive_to_reviewer(
            monitor,
            record,
            reviewer_events=[
                # First report: accepted but no top-level P0-P2 clearance
                # evidence object.
                ("accepted", {"delivery_evidence": {"thread_status": "complete"}}),
                # Corrected re-report on the same live handle.
                ("accepted", {"evidence": _reviewer_evidence()}),
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
        rejections = self._node_events(snapshot.run_id, "acceptance_rejected")
        self.assertEqual(len(rejections), 1)
        self.assertTrue(rejections[0]["data"].get("recoverable"))
        self.assertEqual(
            rejections[0]["data"].get("rejection_code"), "acceptance_evidence:missing"
        )
        self.assertEqual(rejections[0]["data"].get("rejection_count"), 1)
        # The counter is durable (snapshot), not in-memory only.
        self.assertEqual(current["acceptance_format_rejections"]["count"], 1)

        snapshot = monitor.tick(snapshot.run_id, runner)

        current = snapshot.nodes["n1"]
        self.assertEqual(current["status"], "accepted")
        self.assertIsNone(current.get("reason"))
        # The corrected re-report clears the rejection state, counter included.
        self.assertNotIn("acceptance_format_rejections", current)
        self.assertEqual(len(self._node_events(snapshot.run_id, "accepted")), 1)
        self.assertEqual(len(self._node_events(snapshot.run_id, "acceptance_rejected")), 1)

    # ------------------------------------------------------------------
    # 2. Escalation past the limit, and the walkable recovery after it.
    # ------------------------------------------------------------------
    def test_consecutive_rejections_escalate_and_the_run_recovers(self):
        monitor, record = self._monitor(evidence_files=True)
        snapshot, runner = self._drive_to_reviewer(
            monitor,
            record,
            # The same malformed report, re-read on every tick: no live
            # entity can correct it (the run-1ebcb792 incident shape).
            reviewer_events=[
                ("accepted", {"delivery_evidence": {"thread_status": "complete"}}),
            ]
            * (ACCEPTANCE_FORMAT_REJECTION_LIMIT + 2),
        )

        for expected_count in (1, 2):
            snapshot = monitor.tick(snapshot.run_id, runner)
            current = snapshot.nodes["n1"]
            self.assertEqual(current["status"], "review", expected_count)
            self.assertIn("n1", snapshot.handles, expected_count)
            self.assertEqual(
                current["acceptance_format_rejections"]["count"], expected_count
            )
            self.assertEqual(
                len(self._node_events(snapshot.run_id, "acceptance_rejected")),
                expected_count,
            )

        # The third consecutive rejection escalates: visible blocked_unknown,
        # handle retired, durable counter and recovery action stamped.
        snapshot = monitor.tick(snapshot.run_id, runner)

        current = snapshot.nodes["n1"]
        self.assertEqual(current["status"], "blocked_unknown")
        self.assertNotIn("n1", snapshot.handles)
        self.assertIsNone(current.get("active_task"))
        self.assertIsNone(current.get("active_role"))
        self.assertIsNone(current.get("retryable_action"))
        self.assertEqual(
            current.get("recovery_action"), ACCEPTANCE_FORMAT_ESCALATION_RECOVERY
        )
        self.assertIn("no live session", current["reason"])
        self.assertEqual(snapshot.status, "blocked_unknown")
        rejections = self._node_events(snapshot.run_id, "acceptance_rejected")
        self.assertEqual(len(rejections), ACCEPTANCE_FORMAT_REJECTION_LIMIT)
        self.assertTrue(all(r["data"].get("recoverable") for r in rejections[:-1]))
        self.assertFalse(rejections[-1]["data"].get("recoverable"))
        self.assertEqual(
            rejections[-1]["data"].get("rejection_count"),
            ACCEPTANCE_FORMAT_REJECTION_LIMIT,
        )
        record = current["acceptance_format_rejections"]
        self.assertEqual(record["count"], ACCEPTANCE_FORMAT_REJECTION_LIMIT)
        self.assertTrue(record.get("escalated"))

        # The replay loop is over: further ticks re-read nothing and append
        # nothing; the node stays parked exactly where it escalated.
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(
            len(self._node_events(snapshot.run_id, "acceptance_rejected")),
            ACCEPTANCE_FORMAT_REJECTION_LIMIT,
        )
        self.assertNotIn("n1", snapshot.handles)

        # Recovery: the human reshapes the delivery per the pinned schema and
        # re-delivers through the reconcile entry, which replays ordinary
        # delivered/accepted events on the original bindings.
        package = self._reconcile_package(monitor, snapshot.run_id)
        recovered = monitor.reconcile_evidence(snapshot.run_id, package)

        current = recovered.nodes["n1"]
        self.assertEqual(current["status"], "accepted", current)
        self.assertIsNone(current.get("reason"))
        self.assertIsNone(current.get("recovery_action"))
        self.assertNotIn("acceptance_format_rejections", current)
        self.assertEqual(recovered.status, "complete")
        self.assertEqual(
            len(self._node_events(recovered.run_id, "accepted")), 1
        )

    def _reconcile_package(self, monitor, run_id):
        """The provider-free same-run recovery package for the parked node.

        Modelled on the ``vibe reconcile`` contract: the original bindings, a
        real worktree HEAD, and the declared status/handoff evidence files.
        """
        snapshot = load_snapshot(self.paths, run_id)
        developer = load_task_binding(self.paths, "n1", "developer", run_id=run_id)
        reviewer = load_task_binding(self.paths, "n1", "reviewer", run_id=run_id)
        worktree = self.paths.root / ".worktrees" / "n1"
        worktree.mkdir(parents=True, exist_ok=True)
        (worktree / "status.txt").write_text("done\n", encoding="utf-8")
        (worktree / "handoff.md").write_text("handoff\n", encoding="utf-8")
        for command in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "test@example.com"],
            ["git", "config", "user.name", "Test"],
            ["git", "add", "."],
            ["git", "commit", "-qm", "evidence"],
            ["git", "branch", "-M", node_branch("n1")],
        ):
            subprocess.run(command, cwd=worktree, check=True, capture_output=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=worktree, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

        def digest(path):
            return hashlib.sha256(path.read_bytes()).hexdigest()

        def claim(binding, clearance=None):
            item = {
                "task_id": binding.task_id,
                "generation": binding.generation,
                "worktree": binding.worktree,
                "branch": binding.branch,
                "status": binding.status,
                "head": head,
                "status_file": {
                    "path": "status.txt",
                    "sha256": digest(worktree / "status.txt"),
                },
                "handoff_file": {
                    "path": "handoff.md",
                    "sha256": digest(worktree / "handoff.md"),
                },
            }
            if clearance is not None:
                item["clearance"] = clearance
            return item

        return {
            "schema_version": 1,
            "run_id": run_id,
            "plan_id": snapshot.plan_id,
            "plan_revision": snapshot.plan_version,
            "authorization_digest": snapshot.authorization_digest,
            "node_contract_digest": snapshot.node_contract_digest,
            "nodes": [{
                "node_id": "n1",
                "developer": claim(developer),
                "reviewer": claim(reviewer, {"p0": 0, "p1": 0, "p2": 0}),
            }],
        }

    # ------------------------------------------------------------------
    # 3. The code survives redaction; the free text does not.
    # ------------------------------------------------------------------
    def test_rejection_code_survives_redaction_while_the_reason_does_not(self):
        monitor, record = self._monitor()
        snapshot, runner = self._drive_to_reviewer(
            monitor,
            record,
            reviewer_events=[
                ("accepted", {"delivery_evidence": {"thread_status": "complete"}}),
            ],
        )

        snapshot = monitor.tick(snapshot.run_id, runner)

        rejection = self._node_events(snapshot.run_id, "acceptance_rejected")[-1]
        data = rejection["data"]
        self.assertEqual(data.get("rejection_code"), "acceptance_evidence:missing")
        # The free-text detail is redacted on persistence; the structured
        # code is not.
        self.assertEqual(data.get("reason"), "[REDACTED_PROVIDER_TEXT]")
        self.assertNotIn("P0-P2", data.get("reason"))


class IntegrationReviewerEscalationEndToEndTests(unittest.TestCase):
    """The incident path end to end: integration reviewer, mailbox, CLI.

    The reviewer session reports the same malformed claim on every wait; past
    the escalation limit the node parks visible with the recovery action, and
    ``vibe repair`` (``Monitor.repair_node``) re-dispatches the review under a
    fresh reviewer identity -- the run then walks to complete on a claim that
    follows the pinned schema.
    """

    maxDiff = None

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="v254-escalation-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = ProjectPaths(self.root)

    def cli(self, *argv):
        from vibe_guide.cli import run_cli

        return run_cli(list(argv) + ["--json"], self.root)

    def test_escalated_integration_reviewer_closes_through_the_human_entry(self):
        from vibe_guide.adapters.task_provider import ProviderActionStore
        from vibe_guide.cli import _load_plan, _observed_topology_rulings
        from vibe_guide.dag import append_integration_review_node  # noqa: F401
        from tests.test_integration_review_closeout_entry import (
            FACTS, FIXTURE, REQUEST,
        )

        self.assertEqual(self.cli("init", "--confirm").payload["status"], "ok")
        (self.root / "facts.json").write_text(json.dumps(FACTS), encoding="utf-8")
        self.cli("attest", "--adapter", "claude-code", "--facts", "facts.json",
                 "--provenance", "escalation e2e: native visible-task tools observed",
                 "--project-id", "escalationprobe")
        shutil.copy(FIXTURE, self.root / "product-spec.json")
        self.cli("plan", "--request", REQUEST, "--plan-id", "esc", "--s1", "5,5,5,5,5",
                 "--from-prd", "product-spec.json")
        self.cli("authorize", "--plan", "esc", "--authorize", "AUTHORIZE")
        result = self.cli("monitor", "--plan", "esc", "--authorize", "AUTHORIZE")
        run_id = result.payload["run_id"]

        store = ProviderActionStore(self.paths)
        waits = {}
        escalated = False
        for _ in range(80):
            for action in store.pending():
                store.complete(action["action_id"], self._reply(action, waits))
            try:
                result = self.cli("resume", "--plan", "esc")
            except OSError:
                continue
            nodes = result.payload.get("nodes") or {}
            escalated = any(
                isinstance(node.get("acceptance_format_rejections"), dict)
                and node["acceptance_format_rejections"].get("escalated")
                for node in nodes.values()
            )
            if escalated:
                break
        self.assertTrue(escalated, "the rejection never escalated")

        snapshot = load_snapshot(self.paths, run_id)
        node = snapshot.nodes["integration-review"]
        self.assertEqual(node["status"], "blocked_unknown")
        self.assertEqual(
            node.get("recovery_action"), ACCEPTANCE_FORMAT_ESCALATION_RECOVERY
        )
        self.assertEqual(snapshot.status, "blocked_unknown")
        self.assertNotIn("integration-review", snapshot.handles)
        rejections = [
            record for record in load_events(self.paths, run_id)
            if record["event"] == "acceptance_rejected"
            and record["data"].get("node_id") == "integration-review"
        ]
        self.assertEqual(len(rejections), ACCEPTANCE_FORMAT_REJECTION_LIMIT)
        self.assertTrue(all(r["data"].get("recoverable") for r in rejections[:-1]))
        self.assertFalse(rejections[-1]["data"].get("recoverable"))
        self.assertEqual(
            rejections[-1]["data"].get("rejection_code"), "claim_shape:not_an_object"
        )
        self.assertEqual(rejections[-1]["data"].get("reason"), "[REDACTED_PROVIDER_TEXT]")
        self.assertEqual(
            node["acceptance_format_rejections"]["count"],
            ACCEPTANCE_FORMAT_REJECTION_LIMIT,
        )

        # The human entry named by the recovery action: `vibe repair` retires
        # the dead reviewer identity and queues a successor review for the
        # scheduler.  The run must be able to walk to complete afterwards --
        # the escalation may not become a new deadlock.
        _directory, plan, nodes, card = _load_plan(self.paths, "esc")
        monitor = Monitor(
            self.paths, plan, nodes,
            topology_rulings=_observed_topology_rulings(self.paths, card),
        )
        repaired = monitor.repair_node("integration-review", run_id)
        self.assertEqual(repaired["kind"], "reviewer_replacement")
        waits["reviewer_replaced"] = True

        for _ in range(80):
            for action in store.pending():
                store.complete(action["action_id"], self._reply(action, waits))
            try:
                result = self.cli("resume", "--plan", "esc")
            except OSError:
                continue
            nodes_state = result.payload.get("nodes") or {}
            if (
                nodes_state.get("integration-review", {}).get("status") == "accepted"
                and result.payload.get("status") == "complete"
            ):
                break

        closed = load_snapshot(self.paths, run_id)
        self.assertEqual(closed.status, "complete")
        self.assertEqual(
            closed.nodes["integration-review"]["status"], "accepted"
        )
        self.assertEqual(
            closed.nodes["integration-review"]["review_clearance"],
            {"p0": 0, "p1": 0, "p2": 0},
        )

    @staticmethod
    def _reply(action, waits):
        """Mailbox replies; the reviewer's claim is malformed on every wait.

        Before the repair every reviewer wait replays the same free-form
        string -- the shape error the incident replayed forever, because no
        live session could correct it.  Once the escalation has been repaired
        (``reviewer_replaced``), the freshly dispatched reviewer reports the
        pinned claim schema.
        """
        operation = action["operation"]
        if operation == "create":
            return {"binding": {"task_id": "sess_" + action["action_id"], "host": "probe"}}
        if operation == "locate":
            return {"located": True}
        if operation == "visibility":
            return {"visible": True, "direct_enter": True}
        if operation == "resume":
            return {"resumed": True}
        key = (action.get("issue_id"), action.get("role"), action.get("generation"))
        waits[key] = waits.get(key, 0) + 1
        cursor = "c_%s_%d" % (action["action_id"][:6], waits[key])
        if waits[key] == 1:
            return {"status": "timeout", "cursor": cursor}
        if action.get("role") == "reviewer":
            if waits.get("reviewer_replaced"):
                # The replacement reviewer reports the pinned schema.
                return {"status": "completed", "cursor": cursor, "event": "accepted",
                        "evidence": dict(CLEARED_CLAIM)}
            return {"status": "completed", "cursor": cursor, "event": "accepted",
                    "evidence": "P0-P2 cleared"}
        return {"status": "completed", "cursor": cursor, "event": "complete",
                "delivery_evidence": {"completion_marker": "done",
                                      "delivery_path": "src/export.ts",
                                      "thread_status": "complete"},
                "in_session_review": {
                    "protocol": "vibe_guide/protocols/visible-sdd-worker.md",
                    "evidence_ref": "escalation-e2e#review-round-1",
                    "clearance": {"p0": 0, "p1": 0, "p2": 0},
                    "environment_facts_ref": "none",
                    "coverage": {"mode": "ocr", "listed": 3, "reviewed": 3, "skipped": []}}}


if __name__ == "__main__":
    unittest.main()
