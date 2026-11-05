"""P1: a protected same-plan/run contract amendment path (B01-B05, B12 path A).

A confirmed run's business contract can be amended only through a public,
protected three-step path: ``propose`` builds an immutable candidate (spec,
diff, DAG audit, pending authorization card), ``show`` records a display
receipt bound to that candidate's digest, and ``confirm`` -- with a
candidate-bound token -- transfers the same plan/run inside one protected
transaction after re-verifying lineage, owner, tasks, leases and pending
side effects.  Nothing about the live card, record or run changes before
that token.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import vibe_guide
from vibe_guide import plan_amendment
from vibe_guide.authorization import (
    AuthorizationRecord,
    authorize,
)
from vibe_guide.cli import _authorization_card, run_cli
from vibe_guide.state import load_events, load_snapshot, save_snapshot

from tests.support_v45_authorize import publish_complex_probe


FROZEN_HEAD = "885236783e1a680d7165ee2ee2c4e0002430a30a"


def setUpModule():
    import subprocess
    root = Path(vibe_guide.__file__).resolve().parents[1]
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        head = "unreadable"
    print("[loaded vibe_guide: {} version={} head={} frozen={}]".format(
        vibe_guide.__file__, getattr(vibe_guide, "__version__", "?"),
        head, FROZEN_HEAD))


def _read(root, name):
    return json.loads((root / name).read_text(encoding="utf-8"))


def _amendment_spec(plan_root):
    """A business-only spec that mirrors the published node set and edits the
    acceptance example of the first business node."""
    nodes = _read(plan_root, "nodes.json")
    business = [item for item in nodes if item["id"] != "integration-review"]
    integration = [item for item in nodes if item["id"] == "integration-review"]
    spec_nodes = []
    for index, item in enumerate(nodes):
        entry = {
            "id": item["id"],
            "title": item["title"],
            "depends_on": list(item.get("depends_on") or []),
            "integration_after": list(item.get("integration_after") or []),
            "parallel_group": item.get("parallel_group"),
            "contract": {
                "input": item["contract"].get("input", "i"),
                "output": item["contract"].get("output", "o"),
                "error_behavior": item["contract"].get("error_behavior", "e"),
                "acceptance_example": (
                    "verified_fact: amended acceptance"
                    if index == 0
                    else item["contract"].get("acceptance_example", "a")
                ),
            },
        }
        spec_nodes.append(entry)
    return {
        "title": "修订后的探针计划",
        "objective": "verified_fact: 修订既有节点验收示例",
        "decisions": [],
        "nodes": spec_nodes,
    }


def _authorized_probe(case):
    """A published complex plan with workflow evidence and an authorization."""
    root = publish_complex_probe(case)
    paths = plan_amendment.project_paths(root)
    plan_root = paths.vibe / "plans" / "probe-plan"
    # Authorize exactly the way the public command does.
    from vibe_guide.authorize_entry import materialize_workflow_evidence
    materialize_workflow_evidence(paths, "probe-plan", "AUTHORIZE")
    card = _authorization_card(_read(plan_root, "authorization-card.json"))
    record = authorize(card, "AUTHORIZE")
    (plan_root / "authorization.json").write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8")
    return root, paths, plan_root, record


def _start_run(paths, plan_root, record, quiesce=False):
    """Create one run through the real Monitor so tasks/leases are genuine.

    ``quiesce=True`` rewinds the freshly dispatched handles and marks every
    node delivered so the amendment safety checks see a still run -- the
    same state a completed run reaches through real deliveries.
    """
    from vibe_guide.models import DAGNode, Plan
    from vibe_guide.monitor import Monitor
    from vibe_guide.runners.fake import FakeRunner
    plan = plan_amendment._plan_object(
        plan_root, plan_amendment._plan_data(plan_root))
    monitor = Monitor(paths, plan, list(plan.nodes))
    runner = FakeRunner()
    snapshot = monitor.start(record, runner)
    if quiesce:
        from vibe_guide.state import release_writer_lease, save_snapshot
        snapshot.handles.clear()
        for node_id, node in snapshot.nodes.items():
            node["status"] = "delivered"
            node["active_task"] = None
            node["active_role"] = None
            node["retryable_action"] = None
            release_writer_lease(
                paths, node_id, str(node.get("worktree", "")),
                snapshot.run_id)
        save_snapshot(paths, snapshot)
    # The CLI monitor path pins the live run on the plan; mirror that so
    # the amendment CAS can bind the same run id.
    import json as _json
    (plan_root / "current-run.json").write_text(
        _json.dumps({"run_id": snapshot.run_id}), encoding="utf-8")
    return snapshot, runner


class ProposeTests(unittest.TestCase):
    """B01: propose produces an immutable candidate and touches nothing live."""

    def setUp(self):
        self.root, self.paths, self.plan_root, self.record = _authorized_probe(self)

    def _live_digests(self):
        names = ["authorization-card.json", "authorization.json", "plan.json",
                 "nodes.json", "plan-confirmation.json", "prd.md"]
        return {name: (self.plan_root / name).read_bytes() for name in names
                if (self.plan_root / name).is_file()}

    def test_propose_writes_candidate_only(self):
        before = self._live_digests()
        spec = _amendment_spec(self.plan_root)
        candidate = plan_amendment.propose(
            self.paths, "probe-plan", spec, "修订业务验收示例")
        self.assertEqual(candidate["status"], "pending_authorization")
        for key in ("amendment_digest", "candidate_card", "diff",
                    "dag_audit", "revision", "node_ids",
                    "previous_authorization_digest"):
            self.assertIn(key, candidate)
        self.assertEqual(
            candidate["previous_authorization_digest"], self.record.digest)
        self.assertEqual(candidate["node_ids"],
                         sorted(item["id"] for item in
                                _read(self.plan_root, "nodes.json")))
        # Nothing live moved, byte for byte.
        self.assertEqual(before, self._live_digests())
        # The candidate card is pending: no run, no record, not authorized.
        self.assertFalse(
            (self.plan_root / "current-run.json").exists())
        # The candidate directory exists and carries the immutable payload.
        cand_dir = plan_amendment.candidate_dir(
            self.paths, "probe-plan", candidate["amendment_digest"])
        self.assertTrue((cand_dir / "candidate.json").is_file())
        self.assertTrue((cand_dir / "authorization-card.json").is_file())
        self.assertTrue((cand_dir / "spec.json").is_file())

    def test_rejections_leave_live_bytes_unchanged(self):
        before = self._live_digests()
        spec = _amendment_spec(self.plan_root)
        # 1. wrong plan id
        with self.assertRaises(ValueError):
            plan_amendment.propose(self.paths, "no-such-plan", spec, "reason")
        # 2. wrong previous digest
        bad = dict(spec)
        bad["previous_authorization_digest"] = "0" * 64
        with self.assertRaises(ValueError):
            plan_amendment.propose(self.paths, "probe-plan", bad, "reason")
        # 3. node added
        added = copy.deepcopy(spec)
        added["nodes"].append(dict(added["nodes"][0], id="extra-node"))
        with self.assertRaises(ValueError):
            plan_amendment.propose(self.paths, "probe-plan", added, "reason")
        # 4. node removed
        removed = copy.deepcopy(spec)
        removed["nodes"] = removed["nodes"][:-1]
        with self.assertRaises(ValueError):
            plan_amendment.propose(self.paths, "probe-plan", removed, "reason")
        # 5. engineering field injection
        injected = copy.deepcopy(spec)
        injected["capabilities"] = {"agent_id": "x"}
        with self.assertRaises(ValueError):
            plan_amendment.propose(self.paths, "probe-plan", injected, "reason")
        injected_node = copy.deepcopy(spec)
        injected_node["nodes"][0]["contract"]["adapter_id"] = "other"
        with self.assertRaises(ValueError):
            plan_amendment.propose(
                self.paths, "probe-plan", injected_node, "reason")
        # 6. unresolved product decision
        undecided = copy.deepcopy(spec)
        undecided["decisions"] = [{
            "question": "q", "options": ["a", "b"], "impact": "i",
            "recommendation": "a", "status": "needs_confirmation",
            "selected": None, "field": "f"}]
        with self.assertRaises(PermissionError):
            plan_amendment.propose(self.paths, "probe-plan", undecided, "reason")
        # 7. missing reason
        with self.assertRaises(ValueError):
            plan_amendment.propose(self.paths, "probe-plan", spec, "")
        self.assertEqual(before, self._live_digests())

    def test_candidate_card_keeps_node_identities_and_topology(self):
        spec = _amendment_spec(self.plan_root)
        candidate = plan_amendment.propose(
            self.paths, "probe-plan", spec, "修订验收")
        card = candidate["candidate_card"]
        old_card = _read(self.plan_root, "authorization-card.json")
        self.assertEqual(tuple(card["node_ids"]), tuple(old_card["node_ids"]))
        self.assertEqual(card["plan_id"], old_card["plan_id"])
        self.assertEqual(card["plan_version"], old_card["plan_version"])
        self.assertNotEqual(card["digest"], old_card["digest"])
        self.assertTrue(card["digest"], candidate["amendment_digest"])


class ShowAndConfirmBindingTests(unittest.TestCase):
    """B03: the display receipt and the token bind one candidate only."""

    def setUp(self):
        self.root, self.paths, self.plan_root, self.record = _authorized_probe(self)
        self.spec = _amendment_spec(self.plan_root)
        self.candidate = plan_amendment.propose(
            self.paths, "probe-plan", self.spec, "修订验收")

    def test_show_binds_receipt_to_candidate(self):
        shown = plan_amendment.show(
            self.paths, "probe-plan", self.candidate["amendment_digest"])
        receipt = shown["display_receipt"]
        self.assertEqual(
            receipt["amendment_digest"], self.candidate["amendment_digest"])
        self.assertIn("card", shown)
        self.assertIn("dependency_graph", shown)

    def test_confirm_requires_candidate_bound_receipt_and_token(self):
        digest = self.candidate["amendment_digest"]
        # No show yet: refuse.
        with self.assertRaises(PermissionError):
            plan_amendment.confirm(
                self.paths, "probe-plan", digest,
                plan_amendment.confirmation_token(digest))
        shown = plan_amendment.show(self.paths, "probe-plan", digest)
        # Wrong token shapes refused.
        for token in ("AUTHORIZE", "", "AMEND", "AMEND " + "0" * 64):
            with self.assertRaises(PermissionError, msg=token):
                plan_amendment.confirm(self.paths, "probe-plan", digest, token)
        # Another candidate's token refused.
        other = plan_amendment.propose(
            self.paths, "probe-plan",
            dict(self.spec, objective="verified_fact: second candidate"),
            "第二个候选")
        with self.assertRaises(PermissionError):
            plan_amendment.confirm(
                self.paths, "probe-plan", digest,
                plan_amendment.confirmation_token(other["amendment_digest"]))
        with self.assertRaises(PermissionError):
            plan_amendment.confirm(
                self.paths, "probe-plan", other["amendment_digest"],
                plan_amendment.confirmation_token(digest))

    def test_tampered_candidate_cannot_be_confirmed(self):
        digest = self.candidate["amendment_digest"]
        plan_amendment.show(self.paths, "probe-plan", digest)
        cand_dir = plan_amendment.candidate_dir(self.paths, "probe-plan", digest)
        stored = json.loads(
            (cand_dir / "candidate.json").read_text(encoding="utf-8"))
        stored["reason"] = "tampered after display"
        (cand_dir / "candidate.json").write_text(
            json.dumps(stored, ensure_ascii=False, sort_keys=True),
            encoding="utf-8")
        with self.assertRaises(PermissionError):
            plan_amendment.confirm(
                self.paths, "probe-plan", digest,
                plan_amendment.confirmation_token(digest))


class SafetyRecheckTests(unittest.TestCase):
    """B05: active or unknown execution refuses the switch."""

    def setUp(self):
        self.root, self.paths, self.plan_root, self.record = _authorized_probe(self)
        self.spec = _amendment_spec(self.plan_root)
        self.candidate = plan_amendment.propose(
            self.paths, "probe-plan", self.spec, "修订验收")
        plan_amendment.show(
            self.paths, "probe-plan", self.candidate["amendment_digest"])
        self.token = plan_amendment.confirmation_token(
            self.candidate["amendment_digest"])
        self.digest = self.candidate["amendment_digest"]

    def _snapshot_bytes(self):
        runs = self.paths.vibe / "runs"
        return {item.name: item.read_bytes() for item in runs.rglob("state.json")} \
            if runs.exists() else {}

    def test_active_task_refuses_switch(self):
        snapshot, _runner = _start_run(self.paths, self.plan_root, self.record)
        # FakeRunner dispatch left a live handle; amend must fail closed.
        with self.assertRaises(PermissionError):
            plan_amendment.confirm(
                self.paths, "probe-plan", self.digest, self.token)
        # Old record still binds the run.
        self.assertEqual(
            load_snapshot(self.paths, snapshot.run_id).authorization_digest,
            self.record.digest)

    def test_unknown_task_state_refuses_switch(self):
        snapshot, _runner = _start_run(self.paths, self.plan_root, self.record)
        # Simulated unknown: drop the handle but keep the node 'running'.
        tampered = load_snapshot(self.paths, snapshot.run_id)
        tampered.handles.clear()
        for node in tampered.nodes.values():
            if node.get("status") == "running":
                node["status"] = "running"  # already; ensure the branch hits
        save_snapshot(self.paths, tampered)
        with self.assertRaises(PermissionError):
            plan_amendment.confirm(
                self.paths, "probe-plan", self.digest, self.token)

    def test_pending_side_effect_refuses_switch(self):
        snapshot, _runner = _start_run(self.paths, self.plan_root, self.record)
        store_dir = self.paths.vibe / "provider-actions" / "requests"
        store_dir.mkdir(parents=True, exist_ok=True)
        pending = {
            "action_id": "action-pending-1",
            "status": "pending",
            "run_id": snapshot.run_id,
            "operation": "create",
        }
        (store_dir / "action-pending-1.json").write_text(
            json.dumps(pending), encoding="utf-8")
        with self.assertRaises(PermissionError):
            plan_amendment.confirm(
                self.paths, "probe-plan", self.digest, self.token)


if __name__ == "__main__":
    unittest.main()


class _PreparedTransfer(unittest.TestCase):
    """Shared fixture: published plan, authorized record, a real (quiet) run
    and a displayed candidate ready for confirm."""

    def setUp(self):
        self.root, self.paths, self.plan_root, self.record = _authorized_probe(self)
        self.snapshot, _runner = _start_run(
            self.paths, self.plan_root, self.record, quiesce=True)
        self.spec = _amendment_spec(self.plan_root)
        self.candidate = plan_amendment.propose(
            self.paths, "probe-plan", self.spec, "修订验收")
        plan_amendment.show(
            self.paths, "probe-plan", self.candidate["amendment_digest"])
        self.digest = self.candidate["amendment_digest"]
        self.token = plan_amendment.confirmation_token(self.digest)


class CandidateArtifactIntegrityTests(_PreparedTransfer):
    """B03/P1.4: every artifact the transfer installs must be bound by the
    candidate digest -- tampering any of them fails closed."""

    def _tamper(self, name):
        """Schema-valid tampering: only a byte-level integrity check can
        catch it -- a malformed-payload failure would prove nothing."""
        directory = plan_amendment.candidate_dir(
            self.paths, "probe-plan", self.digest)
        path = directory / name
        data = json.loads(path.read_text(encoding="utf-8"))
        if name == "authorization-card.json":
            data["plan_id"] = "forged-plan"
        elif name == "nodes.json":
            data[0]["title"] = "forged title"
        elif name == "spec.json":
            data["nodes"][0]["title"] = "forged title"
        else:
            raise AssertionError(name)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _refuses(self, name):
        with self.subTest(artifact=name):
            self._tamper(name)
            with self.assertRaises(PermissionError):
                plan_amendment.confirm(
                    self.paths, "probe-plan", self.digest, self.token)

    def test_tampered_card_refused(self):
        self._refuses("authorization-card.json")

    def test_tampered_nodes_refused(self):
        self._refuses("nodes.json")

    def test_tampered_spec_refused(self):
        self._refuses("spec.json")

    def test_tampered_artifacts_fail_show_too(self):
        # show loads the same directory through the candidate contract.
        self._tamper("nodes.json")
        with self.assertRaises(PermissionError):
            plan_amendment.show(self.paths, "probe-plan", self.digest)


class NoDispatchDuringTransferTests(_PreparedTransfer):
    """B06/P1.4: confirm must never dispatch, stop or consume executors."""

    def _ready_one_node(self):
        snapshot = load_snapshot(self.paths, self.snapshot.run_id)
        node = snapshot.nodes["probe-node-a"]
        node["status"] = "planned"
        node["retryable_action"] = None
        snapshot.nodes["probe-node-a"] = node
        save_snapshot(self.paths, snapshot)
        return snapshot.run_id

    def test_confirm_does_not_dispatch_ready_nodes(self):
        run_id = self._ready_one_node()
        result = plan_amendment.confirm(
            self.paths, "probe-plan", self.digest, self.token)
        self.assertEqual(result["status"], "amended")
        snapshot = load_snapshot(self.paths, run_id)
        self.assertFalse(snapshot.handles)
        for node_id, node in snapshot.nodes.items():
            self.assertIsNone(node.get("active_task"))
            self.assertNotIn(node.get("status"),
                             ("running", "blocked_unknown"),
                             (node_id, node.get("status"), node.get("reason")))

    def test_active_task_appearing_in_window_refuses_transfer(self):
        """An executor proven active *inside* the transfer window (between
        quiescence check and the reauthorization) aborts the amendment;
        it must not be runner-stopped or silently consumed."""
        from vibe_guide.models import DAGNode, Plan
        from vibe_guide.monitor import Monitor
        from vibe_guide.runners.fake import FakeRunner
        plan = plan_amendment._plan_object(
            self.plan_root, plan_amendment._plan_data(self.plan_root))
        monitor = Monitor(self.paths, plan, list(plan.nodes))
        run_id = self.snapshot.run_id
        # Fabricate a live handle the way a racing monitor would leave it.
        snapshot = load_snapshot(self.paths, run_id)
        node = snapshot.nodes["probe-node-a"]
        node["status"] = "running"
        node["active_task"] = {
            "role": "developer", "task_id": "race-task",
            "handle_id": "race-handle", "generation": 1,
        }
        node["active_role"] = "developer"
        snapshot.handles["probe-node-a"] = "race-handle"
        save_snapshot(self.paths, snapshot)
        new_card = _authorization_card(
            _read(plan_amendment.candidate_dir(
                self.paths, "probe-plan", self.digest),
                "authorization-card.json"))
        new_record = authorize(new_card, "AUTHORIZE")
        with self.assertRaises((PermissionError, RuntimeError)):
            monitor.reauthorize(
                run_id, new_record, FakeRunner(),
                "plan_amendment", amendment_digest=self.digest)
        # The racing executor's state was not consumed or stopped.
        after = load_snapshot(self.paths, run_id)
        self.assertEqual(
            after.nodes["probe-node-a"].get("active_task", {}).get("handle_id"),
            "race-handle")


class LeaseQuiescenceTests(_PreparedTransfer):
    """P1.4/§2.6: writer leases are part of the quiescence proof."""

    def test_active_writer_lease_blocks_confirm(self):
        from vibe_guide.state import acquire_writer_lease
        self.assertTrue(acquire_writer_lease(
            self.paths, "probe-node-a", ".worktrees/probe-node-a",
            self.snapshot.run_id))
        with self.assertRaises(PermissionError):
            plan_amendment.confirm(
                self.paths, "probe-plan", self.digest, self.token)


class AmendmentTokenEvidenceTests(_PreparedTransfer):
    """P2: the recorded authorization evidence must name the token that was
    actually required -- `AMEND <digest>`, never a fabricated `AUTHORIZE`."""

    def test_amended_evidence_records_amend_token(self):
        plan_amendment.confirm(
            self.paths, "probe-plan", self.digest, self.token)
        state = json.loads(
            (self.paths.vibe / "state.json").read_text(encoding="utf-8"))
        record = state["task_workflow"]["probe-plan"]["node_records"][
            "user_authorization"]
        self.assertIn(self.token, json.dumps(record, ensure_ascii=False))
        self.assertNotIn(
            "AUTHORIZE token", json.dumps(record, ensure_ascii=False))
        self.assertNotIn(
            '"granted": true, "token": "AUTHORIZE"',
            json.dumps(record, ensure_ascii=False).replace(" ", "").replace(
                '"', '"') or "")
