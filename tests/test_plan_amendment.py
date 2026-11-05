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
import hashlib
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

    def _lease_file(self):
        from vibe_guide.state import acquire_writer_lease
        self.assertTrue(acquire_writer_lease(
            self.paths, "probe-node-a", ".worktrees/probe-node-a",
            self.snapshot.run_id))
        [lease_file] = list(
            (self.paths.vibe / "leases").glob("*.json"))
        return lease_file

    def test_active_writer_lease_blocks_confirm(self):
        self._lease_file()
        with self.assertRaises(PermissionError):
            plan_amendment.confirm(
                self.paths, "probe-plan", self.digest, self.token)

    def test_quarantined_writer_lease_blocks_confirm(self):
        lease_file = self._lease_file()
        payload = json.loads(lease_file.read_text(encoding="utf-8"))
        payload["status"] = "quarantined"
        lease_file.write_text(
            json.dumps(payload), encoding="utf-8")
        with self.assertRaises(PermissionError):
            plan_amendment.confirm(
                self.paths, "probe-plan", self.digest, self.token)


class TransferLockOwnershipTests(_PreparedTransfer):
    """B06/P1.4: a non-owner entry must never roll back or consume an
    in-flight transfer -- the marker belongs to whoever holds the
    amendment lock."""

    def test_contended_lock_refuses_recovery_and_keeps_marker(self):
        from unittest import mock
        marker = plan_amendment.barrier_path(self.paths, "probe-plan")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "amendment_digest": self.digest,
            "run_id": self.snapshot.run_id}), encoding="utf-8")
        # Another process is mid-transfer: the lock it holds never opens
        # for this entry within the wait budget.
        with mock.patch.object(
                plan_amendment, "interprocess_lock",
                side_effect=TimeoutError("locked")):
            with self.assertRaises(PermissionError):
                plan_amendment.require_no_pending_transfer(
                    self.paths, "probe-plan")
        # Nothing was rolled back: marker still there, candidate intact.
        self.assertTrue(marker.is_file())
        self.assertTrue(plan_amendment.transfer_pending(
            self.paths, "probe-plan"))

    def test_free_lock_recovers_crash_residue_marker(self):
        # A marker whose owner is gone (no lock contention) is crash
        # residue: an uncommitted transfer rolls back cleanly.
        marker = plan_amendment.barrier_path(self.paths, "probe-plan")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "amendment_digest": self.digest,
            "run_id": self.snapshot.run_id}), encoding="utf-8")
        result = plan_amendment.require_no_pending_transfer(
            self.paths, "probe-plan")
        self.assertIsNone(result)
        self.assertFalse(marker.is_file())


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


def _multi_authorized_probe(case):
    """Two business nodes with disjoint files, so the derived
    ``integration-review.contract.files`` union is observable.  Publishes
    through the real ``vibe plan`` CLI exactly like ``_authorized_probe``."""
    import copy as _copy
    import shutil
    from tests import support_v45_authorize as support

    spec = _copy.deepcopy(support._NODE_SPEC)
    first = spec["nodes"][0]
    first["files"] = ["alpha/one.py"]
    second = _copy.deepcopy(first)
    second["id"] = "probe-node-b"
    second["title"] = "探针节点 B"
    second["files"] = ["beta/two.py"]
    spec["nodes"] = [first, second]

    root = Path(tempfile.mkdtemp(prefix="v45-amend-multi-"))
    case.addCleanup(shutil.rmtree, str(root), True)
    init = run_cli(["init", "--confirm", "--json"], root)
    assert init.payload.get("status") == "ok", init.payload
    store = root / ".vibe" / "provider-actions"
    store.mkdir(parents=True, exist_ok=True)
    (store / "capabilities.json").write_text(
        json.dumps(support._CAPABILITIES, ensure_ascii=False),
        encoding="utf-8")
    (root / "node-spec.json").write_text(
        json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    published = run_cli(
        ["plan", "--json", "--request", support._REQUEST, "--s1",
         "5,5,5,5,5", "--plan-id", "probe-plan",
         "--node-spec", "node-spec.json"], root)
    assert published.payload.get("status") == "ok", published.payload
    support.show_card(root, "probe-plan")

    paths = plan_amendment.project_paths(root)
    plan_root = paths.vibe / "plans" / "probe-plan"
    from vibe_guide.authorize_entry import materialize_workflow_evidence
    materialize_workflow_evidence(paths, "probe-plan", "AUTHORIZE")
    card = _authorization_card(_read(plan_root, "authorization-card.json"))
    record = authorize(card, "AUTHORIZE")
    (plan_root / "authorization.json").write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8")
    return root, paths, plan_root, record


class IntegrationReviewScopeTests(unittest.TestCase):
    """P1-6: ``integration-review.contract.files`` is the derived union of
    the business write scopes (``dag.integration_review_scope``).  A files
    amendment must re-derive it or the stored projection disagrees with the
    candidate card's file_scope and the reviewer loses sight of moved/new
    paths; declaring it in the spec is an engineering claim and refused."""

    def setUp(self):
        (self.root, self.paths, self.plan_root,
         self.record) = _multi_authorized_probe(self)
        self.snapshot, _runner = _start_run(
            self.paths, self.plan_root, self.record, quiesce=True)
        self.spec = _amendment_spec(self.plan_root)

    def _merged(self, candidate):
        directory = plan_amendment.candidate_dir(
            self.paths, "probe-plan", candidate["amendment_digest"])
        return {n["id"]: n for n in _read(directory, "nodes.json")}

    def _set_files(self, node_id, files):
        for item in self.spec["nodes"]:
            if item["id"] == node_id:
                item["contract"]["files"] = list(files)

    def test_ir_files_starts_as_business_union(self):
        nodes = _read(self.plan_root, "nodes.json")
        ir = next(n for n in nodes if n["id"] == "integration-review")
        self.assertEqual(sorted(ir["contract"]["files"]),
                         ["alpha/one.py", "beta/two.py"])

    def test_ir_files_rederived_on_business_files_amendment(self):
        self._set_files("probe-node-a", ["gamma/new_a.py"])
        candidate = plan_amendment.propose(
            self.paths, "probe-plan", self.spec, "迁移节点 A 文件")
        merged = self._merged(candidate)
        self.assertEqual(
            sorted(merged["integration-review"]["contract"]["files"]),
            ["beta/two.py", "gamma/new_a.py"])
        self.assertIn("gamma/new_a.py",
                      candidate["candidate_card"]["file_scope"])

    def test_files_migration_between_nodes_refreshes_all_projections(self):
        # The real-plan case: moving paths between business nodes refreshes
        # each node's profile allowlist and the IR union stays the same set.
        self._set_files("probe-node-a", ["beta/two.py"])
        self._set_files("probe-node-b", ["alpha/one.py"])
        candidate = plan_amendment.propose(
            self.paths, "probe-plan", self.spec, "节点间迁移文件")
        merged = self._merged(candidate)
        node_a, node_b = merged["probe-node-a"], merged["probe-node-b"]
        self.assertEqual(node_a["contract"]["files"], ["beta/two.py"])
        self.assertEqual(node_b["contract"]["files"], ["alpha/one.py"])
        self.assertEqual(
            node_a["contract"]["worker_profile"]["allowlist"],
            ["beta/two.py"])
        self.assertEqual(
            node_b["contract"]["worker_profile"]["allowlist"],
            ["alpha/one.py"])
        self.assertEqual(
            sorted(merged["integration-review"]["contract"]["files"]),
            ["alpha/one.py", "beta/two.py"])

    def test_ir_files_declaration_is_engineering_and_refused(self):
        ir = next(n for n in self.spec["nodes"]
                  if n["id"] == "integration-review")
        ir["contract"]["files"] = ["forged/scope.py"]
        with self.assertRaises(ValueError) as context:
            plan_amendment.propose(
                self.paths, "probe-plan", self.spec, "伪造 IR 视野")
        self.assertIn("files", str(context.exception))

    def _serialize_nodes(self):
        """Legalise identical write scopes: B depends on A and leaves the
        parallel group, so the ["."] scopes are serialized, not racing."""
        for item in self.spec["nodes"]:
            if item["id"] == "probe-node-b":
                item["depends_on"] = ["probe-node-a"]
                item["parallel_group"] = None
                item["contract"]["dependency_reasons"] = {
                    "probe-node-a":
                        "verified_fact: B 需要 A 的交付物作为输入"}

    def test_business_files_emptied_narrows_ir_union(self):
        # An empty business ``files`` derives the ["."] profile allowlist
        # exactly like at publish, and contributes nothing to the IR union.
        # ["."] covers beta/ too, so the two nodes must serialize first.
        self._serialize_nodes()
        self._set_files("probe-node-a", [])
        self._set_files("probe-node-b", ["beta/two.py"])
        candidate = plan_amendment.propose(
            self.paths, "probe-plan", self.spec, "清空节点 A 文件范围")
        merged = self._merged(candidate)
        node_a = merged["probe-node-a"]["contract"]
        self.assertEqual(node_a["files"], [])
        self.assertEqual(node_a["worker_profile"]["allowlist"], ["."])
        self.assertEqual(
            merged["integration-review"]["contract"]["files"],
            ["beta/two.py"])

    def test_ir_empty_union_gets_publish_fallback_profile(self):
        # Publish parity: when every business files list empties the IR
        # node needs files=[] plus the whole-project fallback profile or
        # dispatch can never bind a reviewer session to it.  Identical
        # ["."] scopes must be serialized first.
        self._serialize_nodes()
        self._set_files("probe-node-a", [])
        self._set_files("probe-node-b", [])
        candidate = plan_amendment.propose(
            self.paths, "probe-plan", self.spec, "清空全部文件范围")
        merged = self._merged(candidate)
        contract = merged["integration-review"]["contract"]
        self.assertEqual(contract["files"], [])
        profile = contract.get("worker_profile") or {}
        self.assertEqual(profile.get("allowlist"), ["."])

    def _tamper_ir_profile(self, profile):
        """Rewrite the stored IR worker_profile as a real earlier revision
        (or corruption) would have left it; nodes.json is the amendment
        input, not a live artifact this test pretends to mutate."""
        path = self.plan_root / "nodes.json"
        nodes = _read(self.plan_root, "nodes.json")
        for node in nodes:
            if node["id"] == "integration-review":
                node["contract"]["worker_profile"] = profile
        path.write_text(json.dumps(nodes, ensure_ascii=False),
                        encoding="utf-8")

    def test_ir_narrowed_dict_profile_returns_to_project_wide_on_empty_union(self):
        # P1-7: a dict profile left by an earlier narrowing amendment (or a
        # plan hand-edited to that shape) must fall back to the publish-time
        # whole-project allowlist when the business union empties; keeping
        # the stale entries would both reject this legal amendment and bind
        # files no business node still owns to the reviewer.
        previous_profile = {
            "worker": "reviewer", "writer": "reviewer",
            "model": "m", "worktree": "wt-ir", "branch": "node/ir",
            "allowlist": ["alpha/one.py"],
        }
        self._tamper_ir_profile(dict(previous_profile))
        self._serialize_nodes()
        self._set_files("probe-node-a", [])
        self._set_files("probe-node-b", [])
        candidate = plan_amendment.propose(
            self.paths, "probe-plan", self.spec, "清空全部文件范围")
        merged = self._merged(candidate)
        contract = merged["integration-review"]["contract"]
        self.assertEqual(contract["files"], [])
        profile = contract.get("worker_profile") or {}
        self.assertEqual(profile.get("allowlist"), ["."])
        for key, value in previous_profile.items():
            if key != "allowlist":
                self.assertEqual(profile.get(key), value)

    def test_ir_malformed_profile_rejected(self):
        # A non-dict worker_profile on the integration reviewer cannot carry
        # any derived projection; proposing must fail closed instead of
        # passing the corrupt value to the candidate card and dispatch.
        self._tamper_ir_profile("corrupted-profile")
        self._set_files("probe-node-a", ["gamma/new_a.py"])
        with self.assertRaises(ValueError) as context:
            plan_amendment.propose(
                self.paths, "probe-plan", self.spec, "迁移节点 A 文件")
        self.assertIn("worker_profile", str(context.exception))


class FilesScopeProjectionTests(unittest.TestCase):
    """contract.files is the business field; worker_profile.allowlist is its
    derived engineering projection and must be refreshed together or the
    write-scope audit collides with the stale projection."""

    def setUp(self):
        self.root, self.paths, self.plan_root, self.record = _authorized_probe(self)
        self.snapshot, _runner = _start_run(
            self.paths, self.plan_root, self.record, quiesce=True)
        self.spec = _amendment_spec(self.plan_root)

    def _files(self, node_dict):
        return list((node_dict.get("contract") or {}).get("files") or [])

    def test_files_change_refreshes_allowlist_and_propose_passes(self):
        old_nodes = _read(self.plan_root, "nodes.json")
        target = next(n for n in old_nodes if n["id"] != "integration-review")
        old_files = self._files(target)
        new_files = ["telemetry/{}.py".format(target["id"].replace("-", "_"))]
        self.assertNotEqual(old_files, new_files)
        for item in self.spec["nodes"]:
            if item["id"] == target["id"]:
                item["contract"]["files"] = list(new_files)
        candidate = plan_amendment.propose(
            self.paths, "probe-plan", self.spec, "迁移节点文件范围")
        directory = plan_amendment.candidate_dir(
            self.paths, "probe-plan", candidate["amendment_digest"])
        merged = {n["id"]: n for n in _read(directory, "nodes.json")}
        node = merged[target["id"]]
        self.assertEqual(self._files(node), new_files)
        profile = (node.get("contract") or {}).get("worker_profile") or {}
        previous_profile = (target.get("contract") or {}).get(
            "worker_profile") or {}
        if previous_profile:
            self.assertEqual(profile.get("allowlist"), new_files)
            for key, value in previous_profile.items():
                if key != "allowlist":
                    self.assertEqual(profile.get(key), value)
        # The candidate card's file scope agrees with the new projection.
        self.assertIn(new_files[0],
                      candidate["candidate_card"]["file_scope"])

    def test_other_worker_profile_fields_still_rejected(self):
        # A profile change that is not exactly allowlist==new files still
        # fails closed -- exercised end-to-end by corrupting the merge
        # output, plus unit-checked on the predicate itself.
        from unittest import mock

        original = plan_amendment._merge_nodes

        def forged_merge(spec, previous_nodes):
            nodes = original(spec, previous_nodes)
            for node in nodes:
                profile = node.contract.get("worker_profile")
                if isinstance(profile, dict):
                    profile["model"] = "forged-model"
                    break
            return nodes

        with mock.patch.object(
                plan_amendment, "_merge_nodes", forged_merge):
            with self.assertRaises(ValueError):
                plan_amendment.propose(
                    self.paths, "probe-plan", self.spec, "篡改工程字段")

    def test_profile_projection_diff_unit(self):
        diff = plan_amendment._profile_projection_diff
        old_profile = {"allowlist": ["old.py"], "model": "m",
                       "worker": "w", "branch": "node/x"}
        # The one allowed difference: allowlist becomes the new files.
        self.assertIsNone(diff(old_profile, {
            "allowlist": ["new.py"], "model": "m",
            "worker": "w", "branch": "node/x",
            "files": ["new.py"]}))
        # allowlist set to something other than files: rejected.
        self.assertEqual(diff(old_profile, {
            "allowlist": ["other.py"], "model": "m",
            "worker": "w", "branch": "node/x",
            "files": ["new.py"]}), "worker_profile")
        # Any other field drift: rejected.
        self.assertEqual(diff(old_profile, {
            "allowlist": ["new.py"], "model": "m2",
            "worker": "w", "branch": "node/x",
            "files": ["new.py"]}), "worker_profile.model")
        # Same profile: clean.
        self.assertIsNone(diff(old_profile, dict(old_profile)))
        # Missing/new types: rejected.
        self.assertEqual(diff(old_profile, {"model": "m"}),
                         "worker_profile.allowlist")


class _LegacyUninvokedFixture(unittest.TestCase):
    """The real-run shape the r4 amendment confirm hit: a dispatcg-generation
    node left ``running`` with a provider-pending retryable action, one
    unclaimed create request, an active writer lease -- while the native
    create was provably never invoked and the consuming supervisor shift is
    dead and fenced by a successor registration.
    """

    NODE = "probe-node-a"
    OLD_OWNER = "old-owner-session"
    NEW_OWNER = "01a1aa72-4b33-70c1-9138-a6776b3fc293"

    def setUp(self):
        self.root, self.paths, self.plan_root, self.record = _authorized_probe(self)
        from unittest.mock import patch
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        env = patch.dict("os.environ", {"CODEX_HOME": str(self.codex_home)})
        env.start()
        self.addCleanup(env.stop)
        self.observer_record = (self.codex_home / "sessions" / "2026" / "11" / "05" /
                                ("rollout-2026-11-05T10-00-00-" + self.NEW_OWNER + ".jsonl"))
        self.observer_record.parent.mkdir(parents=True)
        self.observer_record.write_text("\n".join(json.dumps(row) for row in [
            {"type": "session_meta", "payload": {"id": self.NEW_OWNER}, "ordinal": 0},
            {"type": "response_item", "ordinal": 1, "payload": {"type": "message",
             "role": "user", "content": [{"type": "input_text",
                                              "text": "Resume this registered supervisor shift"}]}},
        ]) + "\n")
        self.snapshot, _runner = _start_run(
            self.paths, self.plan_root, self.record, quiesce=True)
        self.spec = _amendment_spec(self.plan_root)
        self.candidate = plan_amendment.propose(
            self.paths, "probe-plan", self.spec, "修订验收")
        plan_amendment.show(
            self.paths, "probe-plan", self.candidate["amendment_digest"])
        self.digest = self.candidate["amendment_digest"]
        self.token = plan_amendment.confirmation_token(self.digest)
        self.run_id = self.snapshot.run_id
        self.auth_digest = self.record.digest
        self._legacy_state()

    # -- fixture builders ------------------------------------------------

    def _git_init_branch(self, worktree, branch):
        import subprocess
        worktree.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(worktree)], check=True,
                       capture_output=True)
        subprocess.run(
            ["git", "-C", str(worktree), "symbolic-ref", "HEAD",
             "refs/heads/" + branch],
            check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(worktree), "commit", "--allow-empty",
             "-m", "base", "-q"], check=True, capture_output=True)
        return subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()

    def _write_lease(self):
        from vibe_guide.state import acquire_writer_lease
        from vibe_guide.monitor import Monitor
        snapshot = load_snapshot(self.paths, self.run_id)
        intent = Monitor._prospective_intent_digest(
            snapshot, self.NODE, "developer", 1, snapshot.nodes[self.NODE])
        self.assertTrue(acquire_writer_lease(
            self.paths, self.NODE, self.node["worktree"], self.run_id,
            intent_digest=intent, role="developer", generation=1))
        [lease_file] = list((self.paths.vibe / "leases").glob("*.json"))
        return lease_file

    def _write_registry(self, history=None, owner_session=None,
                        current_session=None, epoch=2):
        from vibe_guide.supervisor import _record_digest
        run_dir = self.paths.vibe / "runs" / self.run_id
        registry = {
            "current": {
                "provider": "codex",
                "session_id": current_session or self.NEW_OWNER,
                "host": "local",
                "lease_epoch": epoch,
                "session_record_digest": _record_digest(self.observer_record),
            },
            "history": list(history if history is not None else [
                {"provider": "codex", "session_id": self.OLD_OWNER,
                 "host": "local", "lease_epoch": 1},
            ]),
            "rejected": [],
        }
        (run_dir / "supervisor-registry.json").write_text(
            json.dumps(registry, sort_keys=True), encoding="utf-8")
        (run_dir / "supervisor.lease").write_text(json.dumps({
            "lease_epoch": epoch,
            "owner": {"provider": "codex",
                      "session_id": owner_session or self.NEW_OWNER},
        }, sort_keys=True), encoding="utf-8")

    def _rollout(self, entries, name="rollout-owner.jsonl"):
        path = Path(tempfile.mkdtemp(prefix="r4-rollout-")) / name
        self.addCleanup(lambda: path.parent.exists() and None)
        with path.open("w", encoding="utf-8") as stream:
            for entry in entries:
                stream.write(json.dumps(entry) + "\n")
        return path

    def _receipt(self, session_id, archive="archived", read="archived"):
        path = Path(tempfile.mkdtemp(prefix="r4-receipt-")) / (
            "receipt-" + session_id + ".json")
        read_result = {"id": session_id, "hostId": "local",
                       "status": {"type": read},
                       "latest_turn": [{"id": "terminal-turn", "status": "completed", "error": None}]}
        archive_result = {"threadId": session_id, "archived": archive == "archived"}
        suffix = "{}-{}".format(session_id, len(self.observer_record.read_text().splitlines()))
        entries = []
        for call_id, tool, args, result in [
            ("read-terminal-" + suffix, "read_thread", {"threadId": session_id}, read_result),
            ("archive-terminal-" + suffix, "set_thread_archived", {"threadId": session_id, "archived": True}, archive_result),
        ]:
            entries.extend([
                {"type": "response_item", "payload": {"type": "function_call", "name": tool,
                    "call_id": call_id, "arguments": json.dumps(args)}},
                {"type": "response_item", "payload": {"type": "function_call_output",
                    "call_id": call_id, "output": json.dumps(result)}}])
        existing = self.observer_record.read_text().splitlines()
        for entry in entries:
            entry["ordinal"] = len(existing)
            existing.append(json.dumps(entry))
        self.observer_record.write_text("\n".join(existing) + "\n")
        source = self._rollout([json.loads(line) for line in existing], name="native-observer.jsonl")
        source.write_bytes(self.observer_record.read_bytes())
        payload = {
            "schema": 1, "kind": "native_consumer_termination_evidence",
            "plan_id": "probe-plan", "run_id": self.run_id,
            "source": "Codex App public read_thread and set_thread_archived tools",
            "observer": {"session_id": self.NEW_OWNER, "lease_epoch": 2,
                         "session_record": str(source),
                         "original_session_record": str(self.observer_record),
                         "source_prefix_bytes": source.stat().st_size,
                         "source_prefix_sha256": self._sha(source)},
            "consumers": [{"thread_id": session_id,
                           "native_observation": read_result,
                           "native_archive_result": archive_result,
                           "native_call_refs": {"read_thread": "read-terminal-" + suffix,
                                                "set_thread_archived": "archive-terminal-" + suffix}}],
        }
        path.write_text(json.dumps(payload, sort_keys=True),
                        encoding="utf-8")
        return path

    def _consumer(self, session_id, path, calls=None, archive="archived",
                  read="archived", kind="owner"):
        lines = path.read_text(encoding="utf-8").splitlines()
        if not any(json.loads(line).get("type") == "session_meta" for line in lines):
            path.write_text(json.dumps({"type": "session_meta", "payload": {"id": session_id}})
                            + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows.append({"type": "event_msg", "payload": {"type": "task_complete",
                     "turn_id": "terminal-turn"}})
        for index, row in enumerate(rows):
            row["ordinal"] = index
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        archive_path = self.codex_home / "archived_sessions" / (
            "rollout-2026-11-05T10-00-00-" + session_id + ".jsonl")
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(path.read_bytes())
        receipt = self._receipt(session_id, archive, read)
        return {
            "session_id": session_id,
            "kind": kind,
            "host": "local",
            "rollout_path": str(path),
            "rollout_sha256": self._sha(path),
            "native_terminal": {
                "archive_status": archive,
                "read_thread_status": read,
                "receipt_path": str(receipt),
                "receipt_sha256": self._sha(receipt),
                "source_rollout_path": json.loads(receipt.read_text())["observer"]["session_record"],
                "source_rollout_sha256": self._sha(Path(json.loads(receipt.read_text())["observer"]["session_record"])),
            },
            "native_calls": list(calls if calls is not None else []),
        }

    def _sha(self, path):
        return plan_amendment._artifact_hash(path)

    def _legacy_state(self):
        """Freeze the node into the exact pending-create shape."""
        self.node = dict(self.snapshot.nodes[self.NODE])
        self.head = self._git_init_branch(
            self.root / self.node["worktree"], self.node["branch"])
        self.node["status"] = "running"
        self.node["worktree_base"] = self.head
        self.node["user_status"] = "自动修复中"
        self.node["reason"] = "provider action pending"
        self.node["retryable_action"] = {
            "role": "developer", "phase": "develop",
            "last_observation_ref": "provider-pending",
            "same_task": True, "same_task_required": True,
            "successor": False,
        }
        snapshot = load_snapshot(self.paths, self.run_id)
        snapshot.nodes[self.NODE] = self.node
        save_snapshot(self.paths, snapshot)
        self.lease_file = self._write_lease()
        self._write_registry()
        # Real Codex rollout encoding (archived supervisor logs): native
        # work appears as custom_tool_call(name="exec", input=<JS>) with a
        # matching custom_tool_call_output; event_msg lines interleave and
        # are not calls.
        owner_log = self._rollout([
            {"type": "session_meta", "payload": {"id": self.OLD_OWNER,
                                                 "cwd": str(self.root)}},
            {"type": "response_item",
             "payload": {"type": "custom_tool_call",
                         "name": "exec", "call_id": "call-1",
                         "input": "text(await tools.exec_command({cmd: \"ls\"}))"}},
            {"type": "response_item",
             "payload": {"type": "custom_tool_call_output",
                         "call_id": "call-1", "output": "ok"}},
            {"type": "event_msg", "payload": {"type": "turn_context"}},
            {"type": "item_completed",
             "payload": {"type": "function_call",
                         "name": "exec_command",
                         "call_id": "call-2"}},
            {"type": "item_completed",
             "payload": {"type": "function_call_output",
                         "call_id": "call-2", "output": "done"}},
        ])
        aux_log = self._rollout([
            {"type": "session_meta", "payload": {"id": "aux-1"}},
            {"type": "event_msg", "payload": {"type": "token_count"}},
        ], name="rollout-aux.jsonl")
        self.consumers = [
            self._consumer(self.OLD_OWNER, owner_log, calls=[
                {"name": "exec", "call_id": "call-1", "returned": True},
                {"name": "exec_command", "call_id": "call-2",
                 "returned": True},
            ]),
            self._consumer("aux-1", aux_log, kind="auxiliary"),
        ]
        # The request must satisfy ProviderActionStore.action()'s identity
        # proof: action_id and request_digest are canonical digests of the
        # immutable record, not hand-picked strings.
        basis = {
            "operation": "create",
            "provider": "codex-app-visible",
            "run_id": self.run_id,
            "issue_id": self.NODE,
            "role": "developer",
            "generation": 1,
            "sequence": 0,
        }
        self.action_id = (
            "action-" + plan_amendment._canonical_digest(basis)[:32])
        self.request = {
            "schema_version": 1,
            "action_id": self.action_id,
            "native_tool": "codex_app__create_thread",
            "request": {
                "authorization_digest": self.auth_digest,
                "target": {"type": "project", "projectId": "same-project",
                           "environment": {"type": "local"}},
                "child_binding": {
                    "node_id": self.NODE, "role": "developer",
                    "worktree": self.node["worktree"],
                    "branch": self.node["branch"],
                },
            },
            **basis,
        }
        self.request["request_digest"] = plan_amendment._canonical_digest(
            dict(self.request))
        request_dir = self.paths.vibe / "provider-actions" / "requests"
        request_dir.mkdir(parents=True, exist_ok=True)
        request_path = request_dir / (self.action_id + ".json")
        request_path.write_text(
            json.dumps(self.request, sort_keys=True), encoding="utf-8")
        self.request_path = request_path
        self.evidence = {
            "schema_version": 1,
            "kind": "retire_uninvoked_legacy",
            "plan_id": "probe-plan",
            "run_id": self.run_id,
            "amendment_digest": self.digest,
            "authorization_digest": self.auth_digest,
            "owner_takeover": {
                "old_session_id": self.OLD_OWNER,
                "old_lease_epoch": 1,
                "new_session_id": self.NEW_OWNER,
                "new_lease_epoch": 2,
            },
            "retired_consumers": self.consumers,
            "requests": [{
                "action_id": self.action_id,
                "request_digest": self.request["request_digest"],
                "request_sha256": self._sha(request_path),
                "issue_id": self.NODE,
                "role": "developer",
                "generation": 1,
            }],
            "worktrees": [{
                "node_id": self.NODE,
                "worktree": self.node["worktree"],
                "branch": self.node["branch"],
                "head": self.head,
            }],
        }

    def _mutate(self, mutate):
        evidence = copy.deepcopy(self.evidence)
        mutate(evidence)
        return evidence

    def _retire(self, evidence=None):
        return plan_amendment.retire_uninvoked_legacy(
            self.paths, "probe-plan", self.digest,
            evidence if evidence is not None else self.evidence)

    def _confirm_blocked(self):
        with self.assertRaises(PermissionError):
            plan_amendment.confirm(
                self.paths, "probe-plan", self.digest, self.token)


class RetireUninvokedLegacyTests(_LegacyUninvokedFixture):
    """The legacy pending-create shape blocks confirm until every link of the
    uninvoked proof chain verifies; then retirement converges and confirm
    passes on the same run."""

    def test_pending_create_shape_blocks_confirm_before_retire(self):
        self._confirm_blocked()

    def test_full_proof_retires_and_confirm_succeeds(self):
        result = self._retire()
        self.assertEqual(result["status"], "retired")
        self.assertEqual(result["action_ids"], [self.action_id])
        store = plan_amendment.ProviderActionStore(self.paths)
        self.assertFalse(store.pending(self.run_id))
        self.assertIsNone(store.result(self.action_id))
        # The immutable request stays; only the stale audit marker sits beside it.
        self.assertTrue(self.request_path.is_file())
        stale = json.loads((self.paths.vibe / "provider-actions" / "stale" /
                            (self.action_id + ".json")).read_text())
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(
            stale["reason"], plan_amendment.RETIRE_UNINVOKED_REASON)
        self.assertEqual(stale["amendment_digest"], self.digest)
        snapshot = load_snapshot(self.paths, self.run_id)
        node = snapshot.nodes[self.NODE]
        self.assertEqual(node["status"], "planned")
        self.assertIsNone(node["retryable_action"])
        self.assertFalse(list((self.paths.vibe / "leases").glob("*.json")))
        events = [event for event in load_events(self.paths, self.run_id)
                  if event["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT]
        self.assertEqual(len(events), 1)
        self.assertIn(self.action_id, json.dumps(events[0]))
        # The same candidate-bound token now completes the transfer.
        result = plan_amendment.confirm(
            self.paths, "probe-plan", self.digest, self.token)
        self.assertEqual(result["status"], "amended")
        self.assertEqual(result["run_id"], self.run_id)

    def test_rerun_is_idempotent_without_second_event(self):
        self._retire()
        events_before = len(load_events(self.paths, self.run_id))
        again = self._retire()
        self.assertEqual(again["status"], "retired")
        self.assertTrue(again.get("converged"))
        events_after = load_events(self.paths, self.run_id)
        self.assertEqual(
            len([e for e in events_after
                 if e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT]), 1)
        self.assertEqual(len(events_after), events_before)

    def test_missing_evidence_field_refuses(self):
        evidence = self._mutate(lambda e: e.pop("run_id"))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_wrong_run_or_digest_refuses(self):
        for mutator in (
            lambda e: e.update(run_id="run-other"),
            lambda e: e.update(amendment_digest="0" * 64),
            lambda e: e.update(authorization_digest="0" * 64),
        ):
            with self.subTest(mutator=mutator):
                with self.assertRaises((PermissionError, ValueError)):
                    self._retire(self._mutate(mutator))

    def test_request_sha_mismatch_refuses(self):
        evidence = self._mutate(
            lambda e: e["requests"][0].update(request_sha256="0" * 64))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_request_tampered_after_evidence_refuses(self):
        request = dict(self.request)
        request["request"]["child_binding"]["branch"] = "node/forged"
        self.request_path.write_text(json.dumps(request, sort_keys=True))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()

    def test_owner_takeover_missing_refuses(self):
        self._write_registry(current_session="still-old",
                             owner_session=self.OLD_OWNER, epoch=1)
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()

    def test_owner_epoch_not_advanced_refuses(self):
        evidence = self._mutate(
            lambda e: e["owner_takeover"].update(new_lease_epoch=3))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_registry_history_consumer_uncovered_refuses(self):
        self._write_registry(history=[
            {"provider": "codex", "session_id": self.OLD_OWNER,
             "host": "local", "lease_epoch": 1},
            {"provider": "codex", "session_id": "ghost-shift",
             "host": "local", "lease_epoch": 1},
        ])
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()

    def test_consumer_without_archive_refuses(self):
        evidence = self._mutate(
            lambda e: e["retired_consumers"][0]["native_terminal"].update(
                archive_status="active"))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_rollout_with_create_thread_call_refuses(self):
        bad_log = self._rollout([
            {"type": "response_item",
             "payload": {"type": "function_call",
                         "name": "create_thread",
                         "call_id": "call-9"}},
            {"type": "response_item",
             "payload": {"type": "function_call_output",
                         "call_id": "call-9", "output": "{}"}},
        ], name="rollout-bad.jsonl")
        evidence = self._mutate(lambda e: e["retired_consumers"][0].update({
            "rollout_path": str(bad_log),
            "rollout_sha256": self._sha(bad_log),
            "native_calls": [{"name": "create_thread",
                              "call_id": "call-9", "returned": True}],
        }))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_exec_payload_embedding_create_refuses(self):
        # A create hidden inside an exec-style input is a real invocation;
        # only the payload is scanned, so prompt/documentation text quoting
        # the signature does not trip this rule.
        bad_log = self._rollout([
            {"type": "response_item",
             "payload": {"type": "custom_tool_call",
                         "name": "exec", "call_id": "call-emb",
                         "input": "text(await "
                                  "tools.mcp__codex_app__create_thread("
                                  "{prompt: \"x\"}))"}},
            {"type": "response_item",
             "payload": {"type": "custom_tool_call_output",
                         "call_id": "call-emb", "output": "{}"}},
        ], name="rollout-emb.jsonl")
        evidence = self._mutate(lambda e: e["retired_consumers"][0].update({
            "rollout_path": str(bad_log),
            "rollout_sha256": self._sha(bad_log),
            "native_calls": [{"name": "exec",
                              "call_id": "call-emb", "returned": True}],
        }))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_documentation_quoting_create_passes_audit(self):
        # A bare mention of the tool signature in message or docs payloads
        # is not an invocation: the audit only scans real call payloads.
        good_log = self._rollout([
            {"type": "response_item",
             "payload": {"type": "message",
                         "content": "see tools.mcp__codex_app__create_thread "
                                    "docs"}},
        ], name="rollout-docs.jsonl")
        consumer = self._consumer("docs-1", good_log, kind="auxiliary")
        evidence = self._mutate(
            lambda e: e["retired_consumers"].append(consumer))
        result = self._retire(evidence)
        self.assertEqual(result["status"], "retired")

    def test_unpaired_native_call_refuses(self):
        bad_log = self._rollout([
            {"type": "response_item",
             "payload": {"type": "function_call",
                         "name": "exec_command",
                         "call_id": "call-open"}},
        ], name="rollout-unpaired.jsonl")
        evidence = self._mutate(lambda e: e["retired_consumers"][0].update({
            "rollout_path": str(bad_log),
            "rollout_sha256": self._sha(bad_log),
            "native_calls": [{"name": "exec_command",
                              "call_id": "call-open", "returned": True}],
        }))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_undeclared_native_call_refuses(self):
        evidence = self._mutate(
            lambda e: e["retired_consumers"][0].update(native_calls=[]))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_unreadable_rollout_refuses(self):
        evidence = self._mutate(
            lambda e: e["retired_consumers"][0].update(
                rollout_path=str(Path(tempfile.gettempdir()) / "absent.jsonl")))
        with self.assertRaises((PermissionError, ValueError, OSError)):
            self._retire(evidence)

    def test_existing_result_refuses(self):
        results_dir = self.paths.vibe / "provider-actions" / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        (results_dir / (self.action_id + ".json")).write_text(json.dumps({
            "schema_version": 1, "action_id": self.action_id,
            "request_digest": self.request["request_digest"],
            "payload": {"thread_id": "t-1"},
        }), encoding="utf-8")
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()

    def test_claim_evidence_refuses(self):
        observations = self.paths.vibe / "provider-actions" / "observations"
        observations.mkdir(parents=True, exist_ok=True)
        (observations / (self.action_id + "-x.json")).write_text(
            json.dumps({"action_id": self.action_id}), encoding="utf-8")
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()

    def test_active_task_or_handle_refuses(self):
        snapshot = load_snapshot(self.paths, self.run_id)
        node = snapshot.nodes[self.NODE]
        node["active_task"] = {"role": "developer", "task_id": "t",
                               "handle_id": "h", "generation": 1}
        snapshot.nodes[self.NODE] = node
        save_snapshot(self.paths, snapshot)
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()

    def test_dirty_worktree_refuses(self):
        worktree = self.root / self.node["worktree"]
        (worktree / "README.md").write_text("changed", encoding="utf-8")
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()

    def test_worktree_head_mismatch_refuses(self):
        evidence = self._mutate(
            lambda e: e["worktrees"][0].update(head="0" * 40))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_node_without_pending_retry_refuses(self):
        snapshot = load_snapshot(self.paths, self.run_id)
        node = snapshot.nodes[self.NODE]
        node["retryable_action"] = {"role": "developer", "phase": "develop",
                                    "last_observation_ref": "provider-error"}
        snapshot.nodes[self.NODE] = node
        save_snapshot(self.paths, snapshot)
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()


class RetireLateArrivalTests(_LegacyUninvokedFixture):
    """A native call that was never invoked cannot produce a durable result;
    if one arrives anyway it is surfaced as blocked_unknown -- never hidden
    and never consumed."""

    def test_late_result_after_retire_blocks_confirm(self):
        self._retire()
        results_dir = self.paths.vibe / "provider-actions" / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        (results_dir / (self.action_id + ".json")).write_text(json.dumps({
            "schema_version": 1, "action_id": self.action_id,
            "request_digest": self.request["request_digest"],
            "payload": {"thread_id": "ghost"},
        }), encoding="utf-8")
        self._confirm_blocked()
        # The late arrival is evidence, not consumed truth.
        snapshot = load_snapshot(self.paths, self.run_id)
        self.assertIsNone(snapshot.nodes[self.NODE].get("active_task"))

    def test_late_observation_after_retire_blocks_confirm(self):
        self._retire()
        observations = self.paths.vibe / "provider-actions" / "observations"
        observations.mkdir(parents=True, exist_ok=True)
        (observations / (self.action_id + "-ghost.json")).write_text(
            json.dumps({"action_id": self.action_id}), encoding="utf-8")
        self._confirm_blocked()

    def test_late_worktree_write_after_retire_blocks_confirm(self):
        self._retire()
        worktree = self.root / self.node["worktree"]
        (worktree / "ghost.py").write_text("# ghost", encoding="utf-8")
        self._confirm_blocked()



class RetireCrashRecoveryTests(_LegacyUninvokedFixture):
    """Pre/post-event crash residue must converge, never deadlock or
    duplicate the retire boundary."""

    def _retire_with_fault(self, fault):
        return plan_amendment.retire_uninvoked_legacy(
            self.paths, "probe-plan", self.digest, self.evidence,
            _fault_at=fault)

    def test_pre_event_crash_leaves_convergent_residue(self):
        with self.assertRaises(RuntimeError):
            self._retire_with_fault("before_event")
        # Intent, held locks and stale markers all remain; no event was
        # appended and the node is untouched.
        locks = self.paths.vibe / "provider-actions" / "locks"
        intents = list(locks.glob("retire-*.intent"))
        self.assertEqual(len(intents), 1)
        intent = json.loads(intents[0].read_text())
        self.assertEqual(intent["phase"], "locked")
        self.assertEqual(intent["action_ids"], [self.action_id])
        self.assertTrue((self.paths.vibe / "provider-actions" / "stale" /
                         (self.action_id + ".json")).is_file())
        self.assertFalse(any(
            event["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT
            for event in load_events(self.paths, self.run_id)))
        self.assertEqual(
            load_snapshot(self.paths, self.run_id)
            .nodes[self.NODE]["status"], "running")
        # Rerun converges the same boundary -- no deadlock on its own
        # residue, no second event, then confirm passes.
        result = self._retire()
        self.assertEqual(result["status"], "retired")
        self.assertEqual(json.loads(intents[0].read_text())["phase"], "complete")
        self.assertTrue((locks / (self.action_id + ".lock")).is_file())
        self.assertEqual(
            len([e for e in load_events(self.paths, self.run_id)
                 if e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT]), 1)
        result = plan_amendment.confirm(
            self.paths, "probe-plan", self.digest, self.token)
        self.assertEqual(result["status"], "amended")

    def test_post_event_crash_resumes_after_boundary(self):
        with self.assertRaises(RuntimeError):
            self._retire_with_fault("after_event")
        # The durable boundary event exists but snapshot/lease/cleanup did
        # not complete.
        self.assertEqual(
            len([e for e in load_events(self.paths, self.run_id)
                 if e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT]), 1)
        result = self._retire()
        self.assertEqual(result["status"], "retired")
        self.assertTrue(result.get("converged"))
        self.assertEqual(
            len([e for e in load_events(self.paths, self.run_id)
                 if e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT]), 1)
        snapshot = load_snapshot(self.paths, self.run_id)
        self.assertEqual(snapshot.nodes[self.NODE]["status"], "planned")
        self.assertFalse(list((self.paths.vibe / "leases").glob("*.json")))
        result = plan_amendment.confirm(
            self.paths, "probe-plan", self.digest, self.token)
        self.assertEqual(result["status"], "amended")

    def test_foreign_lock_residue_refuses(self):
        # A lock file with no intent record is foreign claim evidence,
        # never our residue.
        locks = self.paths.vibe / "provider-actions" / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        (locks / (self.action_id + ".lock")).write_text(
            json.dumps({"pid": 1, "owner": "foreign"}), encoding="utf-8")
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()
        self.assertFalse(any(
            event["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT
            for event in load_events(self.paths, self.run_id)))

    def test_foreign_intent_record_refuses(self):
        locks = self.paths.vibe / "provider-actions" / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        (locks / "retire-{}.intent".format(self.digest[:16])).write_text(
            json.dumps({
                "kind": "retire_uninvoked_legacy_intent",
                "run_id": "run-other", "plan_id": "probe-plan",
                "amendment_digest": self.digest,
                "action_ids": [self.action_id],
                "phase": "locked", "lock_shas": {}}),
            encoding="utf-8")
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()


class RetireDeclaredCreatesTests(_LegacyUninvokedFixture):
    """A retired consumer may carry declared bootstrap/successor creates;
    undeclared, binding, unreturned or phantom creates all refuse."""

    def _create_log(self, entries, name):
        return self._rollout(entries, name=name)

    def _consumer_with_create(self, payload_extra=None, returned_threads=None,
                              output_text=None, name="create_thread",
                              call_id="call-boot"):
        successor = self.NEW_OWNER
        prompt = "Resume this registered supervisor shift"
        target = {"type": "project", "projectId": "same-project",
                  "environment": {"type": "local"}}
        successor_log = self._rollout([
            {"type": "session_meta", "payload": {"id": successor}},
            {"type": "response_item", "payload": {"type": "message",
                "role": "user", "content": [{"type": "input_text", "text": prompt}]}},
        ], name="successor.jsonl")
        from vibe_guide.supervisor import _record_digest
        registry_path = self.paths.vibe / "runs" / self.run_id / "supervisor-registry.json"
        registry = json.loads(registry_path.read_text())
        registry["current"]["session_record_digest"] = _record_digest(self.observer_record)
        registry_path.write_text(json.dumps(registry))
        output_text = output_text or json.dumps(
            {"threadId": successor, "hostId": "local", "target": target})
        call_payload = {
            "type": "function_call", "name": name,
            "call_id": call_id, "arguments": json.dumps({"prompt": prompt, "target": target})}
        if payload_extra:
            call_payload.update(payload_extra)
        entries = [
            {"type": "response_item", "payload": call_payload},
            {"type": "response_item",
             "payload": {"type": "function_call_output",
                         "call_id": call_id, "output": output_text}},
        ]
        log = self._create_log(entries, "rollout-boot.jsonl")
        consumer = self._consumer(
            "boot-supervisor", log,
            calls=[{"name": name, "call_id": call_id, "returned": True}])
        # _consumer adds the terminal receipt to the same observer record.
        if returned_threads is not None:
            consumer["declared_creates"] = [{
                "call_id": call_id,
                "purpose": "supervisor rotation bootstrap",
                "successor_record_path": str(self.observer_record),
                "successor_record_sha256": self._sha(self.observer_record),
                "returned_thread_ids": returned_threads}]
        return consumer

    def test_declared_bootstrap_create_passes(self):
        consumer = self._consumer_with_create(
            returned_threads=[self.NEW_OWNER])
        evidence = self._mutate(
            lambda e: e["retired_consumers"].append(consumer))
        result = self._retire(evidence)
        self.assertEqual(result["status"], "retired")
        event = [e for e in load_events(self.paths, self.run_id)
                 if e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT][0]
        creates = event["data"]["retirement_proof"]["creates"]
        self.assertEqual(creates[0]["session_id"], "boot-supervisor")
        self.assertEqual(creates[0]["successor_session_id"], self.NEW_OWNER)
        self.assertEqual(len(creates[0]["parameters_digest"]), 64)
        self.assertEqual(len(creates[0]["return_digest"]), 64)

    def test_undeclared_create_refuses(self):
        consumer = self._consumer_with_create(returned_threads=None)
        evidence = self._mutate(
            lambda e: e["retired_consumers"].append(consumer))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)
        self.assertFalse(any(
            event["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT
            for event in load_events(self.paths, self.run_id)))

    def test_create_binding_retired_action_refuses(self):
        consumer = self._consumer_with_create(
            payload_extra={"arguments": json.dumps(
                {"action_id": self.action_id})},
            returned_threads=[self.NEW_OWNER])
        evidence = self._mutate(
            lambda e: e["retired_consumers"].append(consumer))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_create_binding_retired_digest_refuses(self):
        consumer = self._consumer_with_create(
            payload_extra={"arguments": json.dumps(
                {"request_digest": self.request["request_digest"]})},
            returned_threads=[self.NEW_OWNER])
        evidence = self._mutate(
            lambda e: e["retired_consumers"].append(consumer))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_declared_wrong_thread_refuses(self):
        consumer = self._consumer_with_create(
            returned_threads=["00000000-0000-0000-0000-000000000000"])
        evidence = self._mutate(
            lambda e: e["retired_consumers"].append(consumer))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_declared_phantom_create_refuses(self):
        consumer = self._consumer_with_create(
            returned_threads=[self.NEW_OWNER])
        consumer["declared_creates"].append({
            "call_id": "call-ghost", "purpose": "phantom",
            "returned_thread_ids": ["x"]})
        evidence = self._mutate(
            lambda e: e["retired_consumers"].append(consumer))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_host_delegation_records_are_not_tool_returns(self):
        # epoch2-shape: delegation injections look like outputs but carry
        # only an fco_ id and no call_id.  They are skipped, never paired.
        log = self._rollout([
            {"type": "response_item",
             "payload": {"type": "function_call", "name": "exec_command",
                         "call_id": "call-ok"}},
            {"type": "response_item",
             "payload": {"type": "function_call_output",
                         "call_id": "call-ok", "output": "done"}},
            {"type": "response_item",
             "payload": {"type": "function_call_output",
                         "id": "fco_delegation",
                         "output": "<delegation>"}},
        ], name="rollout-delegation.jsonl")
        audit = plan_amendment._rollout_native_calls(log)
        self.assertEqual(audit["skipped_returns"], 1)
        self.assertEqual(audit["unpaired"], [])
        consumer = self._consumer(
            "delegation-session", log,
            calls=[{"name": "exec_command", "call_id": "call-ok",
                    "returned": True}])
        evidence = self._mutate(
            lambda e: e["retired_consumers"].append(consumer))
        result = self._retire(evidence)
        self.assertEqual(result["status"], "retired")


class RetireBaseCorrectionTests(_LegacyUninvokedFixture):
    """The dispatch-base correction must be proven by the audited creation
    call, its real return output and an unmoved create-only reflog."""

    def setUp(self):
        super().setUp()
        import shutil
        import subprocess
        # Replace the standalone test repo with a real linked worktree whose
        # branch was created at a different base than the recorded snapshot
        # base -- the exact projection drift the correction exists for.
        stale = self.root / self.node["worktree"]
        if stale.exists():
            shutil.rmtree(stale)
        source = Path(tempfile.mkdtemp(prefix="r4-source-"))
        subprocess.run(["git", "init", str(source)], check=True,
                       capture_output=True)
        subprocess.run(
            ["git", "-C", str(source), "commit", "--allow-empty",
             "-m", "dispatch-base", "-q"], check=True, capture_output=True)
        self.corrected_base = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        add = subprocess.run(
            ["git", "-C", str(source), "worktree", "add",
             str(stale), "-b", self.node["branch"], self.corrected_base],
            check=True, capture_output=True, text=True)
        self.add_output = add.stdout
        self.fake_base = "6ab87860e954704526820b58beedf6c3ce015c8b"
        snapshot = load_snapshot(self.paths, self.run_id)
        node = snapshot.nodes[self.NODE]
        node["worktree_base"] = self.fake_base
        snapshot.nodes[self.NODE] = node
        save_snapshot(self.paths, snapshot)
        command = (
            "git worktree add {} -b {} {}".format(
                self.node["worktree"], self.node["branch"],
                self.corrected_base))
        output = (
            "Preparing worktree (new branch '{}')" + chr(10)
            + "HEAD is now at {} dispatch-base" + chr(10)).format(
                self.node["branch"], self.corrected_base[:7])
        entries = [
            {"type": "response_item",
             "payload": {"type": "custom_tool_call", "name": "exec",
                         "call_id": "call-add",
                         "input": "text(await tools.exec_command({cmd:" + json.dumps(command) + "}))"}},
            {"type": "response_item",
             "payload": {"type": "custom_tool_call_output",
                         "call_id": "call-add",
                         "output": output}},
            {"type": "response_item",
             "payload": {"type": "custom_tool_call", "name": "exec",
                         "call_id": "call-1",
                         "input": "text(await tools.exec_command())"}},
            {"type": "response_item",
             "payload": {"type": "custom_tool_call_output",
                         "call_id": "call-1", "output": "ok"}},
        ]
        owner_log = self._rollout(entries, name="rollout-owner-add.jsonl")
        self.owner_log = owner_log
        consumer = self._consumer(
            self.OLD_OWNER, owner_log,
            calls=[{"name": "exec", "call_id": "call-add",
                    "returned": True},
                   {"name": "exec", "call_id": "call-1",
                    "returned": True}])
        self.evidence["retired_consumers"] = [
            consumer, self.consumers[1]]
        reflog = subprocess.run(
            ["git", "-C", str(stale), "reflog", "show",
             self.node["branch"], "--format=%H %gs"],
            check=True, capture_output=True, text=True).stdout.strip()
        payload = entries[0]["payload"]
        self.evidence["worktrees"][0].update({
            "head": self.corrected_base,
            "base_correction": {
                "old_snapshot_base": self.fake_base,
                "corrected_base": self.corrected_base,
                "creation_rollout_sha256": self._sha(owner_log),
                "creation_call_id": "call-add",
                "creation_command_digest": hashlib.sha256(
                    json.dumps(payload, ensure_ascii=False,
                               sort_keys=True).encode("utf-8")).hexdigest(),
                "output_digest": hashlib.sha256(
                    output.encode("utf-8")).hexdigest(),
                "reflog_digest": hashlib.sha256(
                    reflog.encode("utf-8")).hexdigest(),
                "actual_output": output,
            },
        })

    def test_correction_retires_and_updates_projection(self):
        result = self._retire()
        self.assertEqual(result["status"], "retired")
        snapshot = load_snapshot(self.paths, self.run_id)
        self.assertEqual(
            snapshot.nodes[self.NODE]["worktree_base"], self.corrected_base)
        events = [e for e in load_events(self.paths, self.run_id)
                  if e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT]
        self.assertEqual(len(events), 1)
        corrections = events[0]["data"]["base_corrections"]
        self.assertEqual(
            corrections[self.NODE]["corrected_base_digest"],
            self.corrected_base)
        result = plan_amendment.confirm(
            self.paths, "probe-plan", self.digest, self.token)
        self.assertEqual(result["status"], "amended")

    def test_forged_actual_output_refuses(self):
        evidence = self._mutate(
            lambda e: e["worktrees"][0]["base_correction"].update(
                actual_output="forged output with HEAD is now at {}".format(
                    self.corrected_base)))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_output_digest_tamper_refuses(self):
        evidence = self._mutate(
            lambda e: e["worktrees"][0]["base_correction"].update(
                output_digest="0" * 64))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_creation_call_outside_audited_rollout_refuses(self):
        evidence = self._mutate(
            lambda e: e["worktrees"][0]["base_correction"].update(
                creation_call_id="call-not-in-log"))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_reflog_drift_refuses(self):
        evidence = self._mutate(
            lambda e: e["worktrees"][0]["base_correction"].update(
                reflog_digest="0" * 64))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_moved_branch_rejects_correction(self):
        import subprocess
        stale = self.root / self.node["worktree"]
        subprocess.run(
            ["git", "-C", str(stale), "commit", "--allow-empty",
             "-m", "mutation", "-q"], check=True, capture_output=True)
        new_head = subprocess.run(
            ["git", "-C", str(stale), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        evidence = self._mutate(
            lambda e: e["worktrees"][0].update(head=new_head))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(evidence)

    def test_correction_premise_mismatch_refuses(self):
        snapshot = load_snapshot(self.paths, self.run_id)
        node = snapshot.nodes[self.NODE]
        node["worktree_base"] = self.corrected_base
        snapshot.nodes[self.NODE] = node
        save_snapshot(self.paths, snapshot)
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()


class R9ReviewTests(_LegacyUninvokedFixture):
    def test_truncated_consumer_with_recomputed_digest_is_not_complete(self):
        consumer = self.evidence["retired_consumers"][0]
        original = Path(consumer["rollout_path"])
        truncated = self._rollout([json.loads(original.read_text().splitlines()[0])],
                                  name="truncated.jsonl")
        consumer.update(rollout_path=str(truncated),
                        rollout_sha256=self._sha(truncated), native_calls=[])
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()
        self.assertTrue(self.lease_file.exists())

    def test_archive_middle_removal_and_terminal_mismatch_refuse(self):
        consumer = self.evidence["retired_consumers"][0]
        original = Path(consumer["rollout_path"])
        archive = next((self.codex_home / "archived_sessions").glob("*" + self.OLD_OWNER + ".jsonl"))
        original_bytes = original.read_bytes()
        archive_bytes = archive.read_bytes()
        rows = [json.loads(line) for line in archive_bytes.splitlines()]
        for change in ("missing_middle", "wrong_turn", "duplicate_archive"):
            with self.subTest(change=change):
                original.write_bytes(original_bytes)
                archive.write_bytes(archive_bytes)
                altered = copy.deepcopy(rows)
                if change == "missing_middle":
                    del altered[1]
                elif change == "wrong_turn":
                    altered[-1]["payload"]["turn_id"] = "other-turn"
                if change != "duplicate_archive":
                    data = ("\n".join(json.dumps(row) for row in altered) + "\n").encode()
                    original.write_bytes(data)
                    archive.write_bytes(data)
                    consumer["rollout_sha256"] = self._sha(original)
                else:
                    duplicate = archive.with_name("rollout-2026-11-05T11-00-00-" + self.OLD_OWNER + ".jsonl")
                    duplicate.write_bytes(archive_bytes)
                with self.assertRaises((PermissionError, ValueError)):
                    self._retire()
                if change == "duplicate_archive":
                    duplicate.unlink()
        original.write_bytes(original_bytes)
        archive.write_bytes(archive_bytes)
        consumer["rollout_sha256"] = self._sha(original)

    def test_observer_prefix_drift_and_wrong_registered_identity_refuse(self):
        consumer = self.evidence["retired_consumers"][0]
        terminal = consumer["native_terminal"]
        receipt_path = Path(terminal["receipt_path"])
        original_receipt = json.loads(receipt_path.read_text())
        source_path = Path(terminal["source_rollout_path"])
        source_bytes = source_path.read_bytes()
        for change in ("prefix_drift", "observer_identity", "observer_epoch"):
            with self.subTest(change=change):
                receipt = copy.deepcopy(original_receipt)
                source_path.write_bytes(source_bytes)
                if change == "prefix_drift":
                    source_path.write_bytes(source_bytes.replace(b'"ordinal": 1', b'"ordinal": 9', 1))
                    terminal["source_rollout_sha256"] = self._sha(source_path)
                    receipt["observer"]["source_prefix_sha256"] = self._sha(source_path)
                elif change == "observer_identity":
                    receipt["observer"]["session_id"] = "someone-else"
                else:
                    receipt["observer"]["lease_epoch"] = 99
                receipt_path.write_text(json.dumps(receipt))
                terminal["receipt_sha256"] = self._sha(receipt_path)
                with self.assertRaises((PermissionError, ValueError)):
                    self._retire()
        source_path.write_bytes(source_bytes)

    def test_self_fabricated_observer_is_not_native_source(self):
        consumer = self.evidence["retired_consumers"][0]
        terminal = consumer["native_terminal"]
        receipt_path = Path(terminal["receipt_path"])
        receipt = json.loads(receipt_path.read_text())
        forged = self._rollout([json.loads(line) for line in
            Path(terminal["source_rollout_path"]).read_text().splitlines()[1:]],
            name="forged-observer.jsonl")
        receipt["observer"]["session_record"] = str(forged)
        receipt["observer"]["original_session_record"] = str(forged)
        receipt["observer"]["source_prefix_bytes"] = forged.stat().st_size
        receipt["observer"]["source_prefix_sha256"] = self._sha(forged)
        receipt_path.write_text(json.dumps(receipt))
        terminal.update(receipt_sha256=self._sha(receipt_path),
                        source_rollout_path=str(forged), source_rollout_sha256=self._sha(forged))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()
        self.assertTrue(self.lease_file.exists())

    def test_observation_arriving_after_verification_blocks_retirement(self):
        from unittest.mock import patch
        from vibe_guide.adapters.task_provider import (
            ProviderActionStore, UnresolvedNativeTool)
        original = plan_amendment._verify_retire_evidence

        def deliver_after_validation(*args):
            context = original(*args)
            with self.assertRaises(UnresolvedNativeTool):
                ProviderActionStore(self.paths).complete(
                    self.action_id,
                    {"error": "No such tool available: codex_app__create_thread"})
            return context

        with patch.object(plan_amendment, "_verify_retire_evidence",
                          side_effect=deliver_after_validation):
            with self.assertRaises((PermissionError, ValueError)):
                self._retire()
        self.assertTrue(self.lease_file.exists())
        self.assertFalse(any(e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT
                             for e in load_events(self.paths, self.run_id)))

    def test_callback_before_retire_lock_blocks_event_and_lease_release(self):
        import subprocess
        import sys
        from unittest.mock import patch
        original = plan_amendment._verify_retire_evidence
        for kind in ("unresolved", "fork"):
            with self.subTest(kind=kind):
                fixture = R9ReviewTests(methodName="runTest")
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                child = (
                    "from pathlib import Path; import sys; "
                    "from vibe_guide.paths import ProjectPaths; "
                    "from vibe_guide.adapters.task_provider import ProviderActionStore; "
                    "s=ProviderActionStore(ProjectPaths(Path(sys.argv[1]))); "
                    "p={'error':'No such tool available: codex_app__create_thread'} "
                    "if sys.argv[3]=='unresolved' else {'threadId':'forked-test'}; "
                    "s.complete(sys.argv[2],p)")
                if kind == "fork":
                    record = fixture.codex_home / "archived_sessions" / "rollout-2026-11-05T10-00-00-forked-test.jsonl"
                    record.write_text(json.dumps({"type": "session_meta", "payload": {
                        "id": "forked-test", "forked_from_id": "parent"}}) + "\n")

                def inject(*args):
                    context = original(*args)
                    process = subprocess.run([sys.executable, "-c", child, str(fixture.root),
                                              fixture.action_id, kind], capture_output=True, text=True)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertIn("UnresolvedNativeTool" if kind == "unresolved"
                                  else "ForkedProviderSession", process.stderr)
                    return context
                with patch.object(plan_amendment, "_verify_retire_evidence", side_effect=inject):
                    with self.assertRaises((PermissionError, ValueError)):
                        fixture._retire()
                self.assertTrue(fixture.lease_file.exists())
                self.assertFalse(any(e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT
                                     for e in load_events(fixture.paths, fixture.run_id)))

    def test_callback_queued_behind_retire_is_preserved_and_blocks_confirm(self):
        import subprocess
        import sys
        import time
        from unittest.mock import patch
        original = plan_amendment.save_snapshot
        for kind in ("unresolved", "fork"):
            with self.subTest(kind=kind):
                fixture = R9ReviewTests(methodName="runTest")
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                if kind == "fork":
                    record = fixture.codex_home / "archived_sessions" / "rollout-2026-11-05T10-00-00-forked-test.jsonl"
                    record.write_text(json.dumps({"type": "session_meta", "payload": {
                        "id": "forked-test", "forked_from_id": "parent"}}) + "\n")
                child = ("from pathlib import Path; import sys; "
                    "from vibe_guide.paths import ProjectPaths; "
                    "from vibe_guide.adapters.task_provider import ProviderActionStore; "
                    "print('ready',flush=True); "
                    "p={'error':'No such tool available: codex_app__create_thread'} "
                    "if sys.argv[3]=='unresolved' else {'threadId':'forked-test'}; "
                    "ProviderActionStore(ProjectPaths(Path(sys.argv[1]))).complete(sys.argv[2],p)")
                spawned = []
                def queue(*args):
                    p = subprocess.Popen([sys.executable, "-c", child, str(fixture.root),
                                          fixture.action_id, kind], stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
                    spawned.append(p)
                    self.assertEqual(p.stdout.readline().strip(), "ready")
                    time.sleep(0.2)
                    self.assertIsNone(p.poll(), "callback must wait for action lock")
                    return original(*args)
                with patch.object(plan_amendment, "save_snapshot", side_effect=queue):
                    self.assertEqual(fixture._retire()["status"], "retired")
                _stdout, stderr = spawned[0].communicate(timeout=10)
                self.assertIn("UnresolvedNativeTool" if kind == "unresolved"
                              else "ForkedProviderSession", stderr)
                self.assertTrue(list((fixture.paths.vibe / "provider-actions" / "observations")
                                     .glob(fixture.action_id + "-*.json")))
                fixture._confirm_blocked()

    def test_public_cli_status_and_resume_after_amendment(self):
        from vibe_guide.runners.fake import FakeRunner
        self._retire()
        confirmed = run_cli(["plan-amend", "confirm", "--plan", "probe-plan",
                             "--amendment", self.digest, "--confirm-token", self.token,
                             "--json"], self.root, runner=FakeRunner())
        self.assertEqual(confirmed.payload.get("status"), "amended", confirmed.payload)
        for command in ("status", "resume"):
            with self.subTest(command=command):
                result = run_cli([command, "--plan", "probe-plan", "--run-id",
                                  self.run_id, "--json"], self.root, runner=FakeRunner())
                if command == "status":
                    self.assertEqual(result.exit_code, 0, result.payload)
                else:
                    # This isolated fake runner may legitimately block at a
                    # separate dispatch gate; it must reach the same run.
                    self.assertIn(result.exit_code, (0, 4), result.payload)
                    self.assertNotIn("amendment_revision", str(result.payload.get("reason", "")))
                self.assertEqual(result.payload.get("run_id"), self.run_id)
                self.assertEqual(load_snapshot(self.paths, self.run_id).authorization_digest,
                                 confirmed.payload["authorization_digest"])
        self.assertEqual(_read(self.plan_root, "plan.json")["amendment_revision"],
                         self.candidate["revision"])

    def test_bad_amendment_revision_is_rejected_by_public_status(self):
        self._retire()
        confirmed = run_cli(["plan-amend", "confirm", "--plan", "probe-plan",
                             "--amendment", self.digest, "--confirm-token", self.token,
                             "--json"], self.root)
        self.assertEqual(confirmed.payload.get("status"), "amended", confirmed.payload)
        plan_path = self.plan_root / "plan.json"
        original = json.loads(plan_path.read_text())
        for value in (True, 0, "1", None):
            with self.subTest(value=value):
                plan_path.write_text(json.dumps(dict(original, amendment_revision=value)))
                status = run_cli(["status", "--plan", "probe-plan", "--run-id",
                                  self.run_id, "--json"], self.root)
                self.assertEqual(status.exit_code, 4, status.payload)
                self.assertIn("amendment_revision", status.payload.get("reason", ""))

    def test_static_exec_legal_escape_and_regex_are_not_create(self):
        for source in (r'text(await tools.exec_command({cmd:"printf \;"}));',
                       'text(/create_thread|read_thread/.test("read_thread"));'):
            with self.subTest(source=source):
                self.assertEqual(plan_amendment._embedded_create_invocations(source), 0)

    def test_real_history_shapes_and_failed_compile_are_audited(self):
        from unittest.mock import patch
        # These are distinct, redacted forms observed in the archived calls.
        cases = [
            'text((await tools.exec_command({cmd:"printf \\;"})).output);',
            'text(await tools.exec_command({cmd:"echo \\("}));',
            'text(await tools.exec_command({cmd:"echo \\+"}));',
            'text(await tools.exec_command({cmd:"echo \\."}));',
            'text(await tools.exec_command({cmd:"echo \\|"}));',
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(plan_amendment._embedded_create_invocations(source), 0)
        with self.assertRaises(ValueError):
            plan_amendment._embedded_create_invocations(
                'const found = ALL_TOOLS.filter(t => /create_thread|read_thread/.test(t.name));')
        source = 'text(await tools.exec_command({cmd:"bad\nstring"}));'
        bad = self._rollout([
            {"type": "response_item", "payload": {"type": "custom_tool_call",
              "name": "exec", "call_id": "syntax", "input": source}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output",
              "call_id": "syntax", "output": [{"type": "input_text", "text":
              "Script failed\nWall time 0.0 seconds\nOutput:\n"},
              {"type": "input_text", "text": "Script error:\nSyntaxError: Invalid or unexpected token"}]}}
        ], name="syntax-error.jsonl")
        audit = plan_amendment._rollout_native_calls(bad)
        self.assertEqual(audit["calls"][0]["call_id"], "syntax")
        self.assertFalse(audit["calls"][0]["embedded_create"])
        entries = [json.loads(line) for line in bad.read_text().splitlines()]
        entries[-1]["payload"]["output"] = "Script completed\nWall time 0.0 seconds\nOutput:\n"
        bad.write_text("\n".join(json.dumps(row) for row in entries) + "\n")
        with self.assertRaises(ValueError):
            plan_amendment._rollout_native_calls(bad)


class R10ReviewTests(_LegacyUninvokedFixture):
    def _exec_consumer(self, source, output, name):
        call_id = "r10-call"
        log = self._rollout([
            {"type": "response_item", "payload": {"type": "custom_tool_call",
             "name": "exec", "call_id": call_id, "input": source}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output",
             "call_id": call_id, "output": output}},
        ], name=name + ".jsonl")
        return self._consumer(name, log, kind="auxiliary",
                              calls=[{"name": "exec", "call_id": call_id, "returned": True}])

    def test_escaped_host_or_computed_property_cannot_retire_create(self):
        cases = [
            r'await t\u006fols.svc__codex_app__create\u005fthread({prompt:"unrelated"})',
            r'await t\u006fols["mcp__codex_app__create"+"_thread"]({prompt:"other"})',
            r'await t\u006fols[`mcp__codex_app__create_thread`]({prompt:"other"})',
            r'await tools["mcp__codex_app__create"+"_thread"]({prompt:"other"})',
        ]
        for i, source in enumerate(cases):
            with self.subTest(source=source):
                fixture = R10ReviewTests(methodName="runTest")
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                consumer = fixture._exec_consumer(source,
                    [{"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
                     {"type": "input_text", "text": json.dumps({"threadId": "spawned", "hostId": "local"})}],
                    "escaped-{}".format(i))
                fixture.evidence["retired_consumers"].append(consumer)
                with self.assertRaises((ValueError, PermissionError)):
                    fixture._retire()
                self.assertTrue(fixture.lease_file.exists())
                self.assertFalse(any(e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT
                                     for e in load_events(fixture.paths, fixture.run_id)))

    def test_inert_escaped_text_and_regex_can_retire(self):
        source = r'text("t\u006fols.mcp__codex_app__create_thread()"); text(/create_thread/.test("x"));'
        consumer = self._exec_consumer(source, "ok", "inert")
        self.evidence["retired_consumers"].append(consumer)
        self.assertEqual(self._retire()["status"], "retired")

    def test_static_compile_error_requires_no_external_runtime(self):
        from unittest.mock import patch
        source = 'text(await tools.exec_command({cmd:"bad\nstring"}));'
        output = [{"type": "input_text", "text": "Script failed\nWall time 0.0 seconds\nOutput:\n"},
                  {"type": "input_text", "text": "Script error:\nSyntaxError: Invalid or unexpected token"}]
        consumer = self._exec_consumer(source, output, "compile-error")
        self.evidence["retired_consumers"].append(consumer)
        with patch("vibe_guide.plan_amendment.subprocess.run", side_effect=AssertionError("external runtime invoked")):
            audit = plan_amendment._rollout_native_calls(Path(consumer["rollout_path"]))
        self.assertTrue(audit["calls"][0]["syntax_rejected"])

    def test_runtime_syntaxerror_or_mismatched_output_cannot_prove_no_call(self):
        source = r'await t\u006fols.mcp__codex_app__create_thread({}); throw new SyntaxError("Invalid or unexpected token");'
        outputs = [
            [{"type": "input_text", "text": "Script failed\nWall time 0.0 seconds\nOutput:\n"},
             {"type": "input_text", "text": "Script error:\nSyntaxError: Invalid or unexpected token"}],
            [{"type": "input_text", "text": "Script failed\nWall time 0.0 seconds\nOutput:\n"},
             {"type": "input_text", "text": "Script error:\nSyntaxError: different engine message"}],
            "Script completed\nWall time 0.0 seconds\nOutput:\n",
        ]
        for i, output in enumerate(outputs):
            with self.subTest(output=output):
                consumer = self._exec_consumer(source, output, "runtime-{}".format(i))
                with self.assertRaises(ValueError):
                    plan_amendment._rollout_native_calls(Path(consumer["rollout_path"]))


class R11ReviewTests(_LegacyUninvokedFixture):
    _exec_consumer = R10ReviewTests._exec_consumer
    def test_dynamic_indirection_cannot_retire_create(self):
        sources = [
            'await ({}).constructor["constructor"]("return tools")()["mcp__codex_app__create"+"_thread"]({prompt:"unrelated"})',
            'const key="constructor"; await ([])[key][key]("return tools")()["mcp__codex_app__create_thread"]({})',
            'const get = async () => tools; await (await get())["mcp__codex_app__create_thread"]({})',
            'const proxy = new Proxy({}, {}); proxy["call"]()',
        ]
        for i, source in enumerate(sources):
            with self.subTest(source=source):
                fixture = R10ReviewTests(methodName="runTest")
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                consumer = fixture._exec_consumer(source,
                    [{"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
                     {"type": "input_text", "text": json.dumps({"threadId": "spawned", "hostId": "local"})}],
                    "indirect-{}".format(i))
                fixture.evidence["retired_consumers"].append(consumer)
                with self.assertRaises((ValueError, PermissionError)):
                    fixture._retire()
                self.assertTrue(fixture.lease_file.exists())
                self.assertFalse(any(e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT
                                     for e in load_events(fixture.paths, fixture.run_id)))

    def test_inert_strings_comments_and_regex_stay_classifiable(self):
        source = '/* constructor and tools["x"]() */ text("constructor"); text(/constructor/.test("x")); // tools\n'
        consumer = self._exec_consumer(source, "ok", "inert-r11")
        self.evidence["retired_consumers"].append(consumer)
        self.assertEqual(self._retire()["status"], "retired")


class R13StaticSubsetTests(_LegacyUninvokedFixture):
    _exec_consumer = R10ReviewTests._exec_consumer

    def _classified_count(self, source):
        try:
            return plan_amendment._embedded_create_invocations(source)
        except ValueError as error:
            self.fail("static form was not classified: {}".format(error))

    def test_const_known_exec_call_is_safe_in_complete_retire(self):
        source = 'const r = await tools.exec_command({cmd:"printf ok"}); text(r.output);'
        consumer = self._exec_consumer(source, "ok", "r13-const-safe")
        self.evidence["retired_consumers"].append(consumer)
        self.assertEqual(self._classified_count(source), 0)
        self.assertEqual(self._retire()["status"], "retired")

    def test_const_create_call_is_counted_and_blocks_complete_retire(self):
        source = 'const r = await tools.mcp__codex_app__create_thread({prompt:"x"}); text("done");'
        self.assertEqual(self._classified_count(source), 1)
        consumer = self._exec_consumer(source,
            [{"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
             {"type": "input_text", "text": json.dumps({"threadId": "spawned", "hostId": "local"})}],
            "r13-const-create")
        self.evidence["retired_consumers"].append(consumer)
        with self.assertRaises((ValueError, PermissionError)):
            self._retire()
        self.assertTrue(self.lease_file.exists())
        self.assertFalse(any(e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT
                             for e in load_events(self.paths, self.run_id)))

    def test_all_settled_known_calls_are_safe_in_complete_retire(self):
        source = ('const results = await Promise.allSettled(['
                  'tools.exec_command({cmd:"a"}), '
                  'tools.exec_command({cmd:"b"})]);')
        consumer = self._exec_consumer(source, "ok", "r13-all-safe")
        self.evidence["retired_consumers"].append(consumer)
        self.assertEqual(self._classified_count(source), 0)
        self.assertEqual(self._retire()["status"], "retired")

    def test_all_settled_create_is_counted_and_blocks_complete_retire(self):
        source = ('const results = await Promise.allSettled(['
                  'tools.exec_command({cmd:"a"}), '
                  'tools.mcp__codex_app__create_thread({prompt:"x"})]);')
        self.assertEqual(self._classified_count(source), 1)
        consumer = self._exec_consumer(source,
            [{"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
             {"type": "input_text", "text": json.dumps({"threadId": "spawned", "hostId": "local"})}],
            "r13-all-create")
        self.evidence["retired_consumers"].append(consumer)
        with self.assertRaises((ValueError, PermissionError)):
            self._retire()
        self.assertTrue(self.lease_file.exists())
        self.assertFalse(any(e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT
                             for e in load_events(self.paths, self.run_id)))

    def test_all_settled_dynamic_member_is_unknown_and_blocks_complete_retire(self):
        source = ('const key = "exec_command"; '
                  'const results = await Promise.allSettled([tools[key]({cmd:"x"})]);')
        with self.assertRaises(ValueError):
            plan_amendment._embedded_create_invocations(source)
        consumer = self._exec_consumer(source, "ok", "r13-all-dynamic")
        self.evidence["retired_consumers"].append(consumer)
        with self.assertRaises((ValueError, PermissionError)):
            self._retire()
        self.assertTrue(self.lease_file.exists())
        self.assertFalse(any(e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT
                             for e in load_events(self.paths, self.run_id)))

    def test_reserved_names_and_dynamic_result_access_refuse(self):
        sources = [
            'const tools = await tools.exec_command({cmd:"x"});',
            'const text = await tools.exec_command({cmd:"x"}); text("x");',
            'const Promise = await tools.exec_command({cmd:"x"}); '
            'const x = await Promise.allSettled([tools.exec_command({cmd:"x"})]);',
            'const r = await tools.exec_command({cmd:"x"}); text(r["output"]);',
            'const r = await tools.exec_command({cmd:"x"}); r.output();',
            'const r = await tools.mcp__codex_app__create_thread({prompt:"x"}); text(r.output);',
            'const r = await tools.exec_command({cmd:"x"}); '
            'text(({}).constructor["constructor"]("return tools")());',
        ]
        for index, source in enumerate(sources):
            with self.subTest(source=source):
                fixture = R13StaticSubsetTests(methodName="runTest")
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                consumer = fixture._exec_consumer(source, "ok", "r13-unknown-{}".format(index))
                fixture.evidence["retired_consumers"].append(consumer)
                with self.assertRaises((ValueError, PermissionError)):
                    fixture._retire()
                self.assertTrue(fixture.lease_file.exists())
                self.assertFalse(any(e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT
                                     for e in load_events(fixture.paths, fixture.run_id)))

    def test_all_settled_create_matching_action_is_not_excluded(self):
        source = ('const results = await Promise.allSettled(['
                  'tools.mcp__codex_app__create_thread({action_id:'
                  + json.dumps(self.action_id) + '})]);')
        self.assertEqual(self._classified_count(source), 1)
        consumer = self._exec_consumer(source,
            [{"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
             {"type": "input_text", "text": json.dumps({"threadId": "spawned", "hostId": "local"})}],
            "r13-action-match")
        self.evidence["retired_consumers"].append(consumer)
        with self.assertRaises(PermissionError):
            self._retire()
        self.assertTrue(self.lease_file.exists())

    def test_result_shadowing_text_later_cannot_hide_create(self):
        source = ('text(await tools.mcp__codex_app__create_thread({prompt:"x"})); '
                  'const text = await tools.exec_command({cmd:"x"});')
        consumer = self._exec_consumer(source,
            [{"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
             {"type": "input_text", "text": json.dumps({"threadId": "spawned", "hostId": "local"})}],
            "r13-shadow-later")
        self.evidence["retired_consumers"].append(consumer)
        with self.assertRaises((ValueError, PermissionError)):
            self._retire()
        self.assertTrue(self.lease_file.exists())

    def test_nested_create_in_arguments_is_not_static_data(self):
        source = ('const r = await tools.exec_command({cmd:'
                  'tools.mcp__codex_app__create_thread({prompt:"x"})}); text(r.output);')
        consumer = self._exec_consumer(source, "ok", "r13-nested-create")
        self.evidence["retired_consumers"].append(consumer)
        with self.assertRaises((ValueError, PermissionError)):
            self._retire()
        self.assertTrue(self.lease_file.exists())

    def test_multiple_known_creates_cannot_be_hidden_in_all_settled(self):
        source = ('const results = await Promise.allSettled(['
                  'tools.mcp__codex_app__create_thread({prompt:"a"}),'
                  'tools.mcp__codex_app__create_thread({prompt:"b"})]);')
        self.assertEqual(self._classified_count(source), 2)
        consumer = self._exec_consumer(source, "ok", "r13-two-creates")
        self.evidence["retired_consumers"].append(consumer)
        with self.assertRaises((ValueError, PermissionError)):
            self._retire()
        self.assertTrue(self.lease_file.exists())

    def test_const_declaration_cannot_treat_following_line_as_new_statement(self):
        source = 'const r = await tools.exec_command({cmd:"x"})\ntext(r.output);'
        with self.assertRaises(ValueError):
            plan_amendment._embedded_create_invocations(source)

    def test_reserved_keyword_is_not_a_result_binding(self):
        for name in ("class", "return", "throw", "import", "break"):
            with self.subTest(name=name):
                source = 'const {} = await tools.exec_command({{cmd:"x"}});'.format(name)
                with self.assertRaises(ValueError):
                    plan_amendment._embedded_create_invocations(source)


class R14StaticSubsetTests(_LegacyUninvokedFixture):
    _exec_consumer = R10ReviewTests._exec_consumer

    def test_tool_names_map_allows_complete_retire(self):
        source = 'text(ALL_TOOLS.map(t => t.name));'
        consumer = self._exec_consumer(source, "ok", "r14-names-map")
        self.evidence["retired_consumers"].append(consumer)
        try:
            count = plan_amendment._embedded_create_invocations(source)
        except ValueError as error:
            self.fail("metadata map is not classified: {}".format(error))
        self.assertEqual(count, 0)
        self.assertEqual(self._retire()["status"], "retired")

    def test_map_callback_cannot_hide_create_in_complete_retire(self):
        sources = [
            'text(ALL_TOOLS.map(t => tools.mcp__codex_app__create_thread({prompt:"x"})));',
            'text(ALL_TOOLS.map(t => (t.name, tools.mcp__codex_app__create_thread({prompt:"x"}))));',
            'text(ALL_TOOLS.map(t => t["name"]));',
            'text(ALL_TOOLS.map(t => t.constructor.constructor("return tools")()));',
            'text(ALL_TOOLS.map(tools => tools.name));',
        ]
        for index, source in enumerate(sources):
            with self.subTest(source=source):
                fixture = R14StaticSubsetTests(methodName="runTest")
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                consumer = fixture._exec_consumer(source,
                    [{"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
                     {"type": "input_text", "text": json.dumps({"threadId": "spawned", "hostId": "local"})}],
                    "r14-map-{}".format(index))
                fixture.evidence["retired_consumers"].append(consumer)
                with self.assertRaises((ValueError, PermissionError)):
                    fixture._retire()
                self.assertTrue(fixture.lease_file.exists())
                self.assertFalse(any(e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT
                                     for e in load_events(fixture.paths, fixture.run_id)))

    def test_create_after_metadata_map_is_counted_and_blocks_retire(self):
        source = ('text(ALL_TOOLS.map(t => t.name)); '
                  'await tools.mcp__codex_app__create_thread({prompt:"x"});')
        consumer = self._exec_consumer(source,
            [{"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
             {"type": "input_text", "text": json.dumps({"threadId": "spawned", "hostId": "local"})}],
            "r14-map-create")
        self.evidence["retired_consumers"].append(consumer)
        try:
            count = plan_amendment._embedded_create_invocations(source)
        except ValueError as error:
            self.fail("sequential create is not classified: {}".format(error))
        self.assertEqual(count, 1)
        with self.assertRaises(PermissionError):
            self._retire()
        self.assertTrue(self.lease_file.exists())
        self.assertFalse(any(e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT
                             for e in load_events(self.paths, self.run_id)))


class R8ReceiptTests(_LegacyUninvokedFixture):
    def test_consumer_rollout_must_be_the_named_session(self):
        consumer = self.evidence["retired_consumers"][0]
        path = Path(consumer["rollout_path"])
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]["payload"]["id"] = "another-session"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        consumer["rollout_sha256"] = self._sha(path)
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()

    def test_arbitrary_digest_file_is_not_terminal_proof(self):
        consumer = self.evidence["retired_consumers"][0]
        receipt = Path(consumer["native_terminal"]["receipt_path"])
        receipt.write_text("# README, not a native receipt")
        consumer["native_terminal"]["receipt_sha256"] = self._sha(receipt)
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()

    def test_receipt_content_must_bind_session_run_host_and_source(self):
        consumer = self.evidence["retired_consumers"][0]
        receipt = Path(consumer["native_terminal"]["receipt_path"])
        original = json.loads(receipt.read_text())
        mutations = [
            lambda d: d["consumers"][0].update(thread_id="other"),
            lambda d: d.update(run_id="run-other"),
            lambda d: d["consumers"][0]["native_observation"].update(hostId="foreign"),
            lambda d: d.update(source=""),
            lambda d: d["consumers"][0]["native_observation"]["status"].update(type="active"),
            lambda d: d["consumers"][0]["native_archive_result"].update(archived=False),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                data = copy.deepcopy(original)
                mutate(data)
                receipt.write_text(json.dumps(data))
                consumer["native_terminal"]["receipt_sha256"] = self._sha(receipt)
                with self.assertRaises((PermissionError, ValueError)):
                    self._retire()



class R8CreateTests(RetireDeclaredCreatesTests):
    def test_rewritten_worker_cannot_self_declare_bootstrap(self):
        consumer = self._consumer_with_create(
            payload_extra={"arguments": json.dumps({
                "prompt": "rewritten worker instructions",
                "target": {"type": "project", "projectId": "same-project",
                           "environment": {"type": "local"}}})},
            returned_threads=[self.NEW_OWNER])
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(self._mutate(
                lambda e: e["retired_consumers"].append(consumer)))

    def test_unknown_javascript_cannot_be_zero_create(self):
        cases = [
            'const f=tools.mcp__codex_app__create_thread; await f({});',
            '`${await tools.mcp__codex_app__create_thread({})}`',
            'await tools.mcp__codex_app__create_thread ({})',
            'const f=tools["mcp__codex_app__create_thread"]; await f({});',
            'const t=globalThis["tools"]; await t["svc__codex_app__create_thread"]({});',
        ]
        for source in cases:
            with self.subTest(source=source):
                try:
                    count = plan_amendment._embedded_create_invocations(source)
                except (ValueError, PermissionError):
                    continue
                self.assertGreater(count, 0)

    def test_mixed_return_text_does_not_bind_created_thread(self):
        consumer = self._consumer_with_create(
            returned_threads=[self.NEW_OWNER],
            output_text='error: no task created; note ' + self.NEW_OWNER)
        with self.assertRaises((PermissionError, ValueError)):
            self._retire(self._mutate(
                lambda e: e["retired_consumers"].append(consumer)))


class R8LockLeaseTests(_LegacyUninvokedFixture):
    def test_legacy_unbound_lease_cannot_retire_action(self):
        from vibe_guide.state import release_writer_lease, acquire_writer_lease
        self.assertTrue(release_writer_lease(self.paths, self.NODE, self.node["worktree"], self.run_id))
        self.assertTrue(acquire_writer_lease(self.paths, self.NODE, self.node["worktree"], self.run_id))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()

    def test_action_lock_identity_remains_after_retire(self):
        self._retire()
        lock = self.paths.vibe / "provider-actions" / "locks" / (self.action_id + ".lock")
        self.assertTrue(lock.is_file(), "shared flock inode must never be unlinked")
        inode = lock.stat().st_ino
        self._retire()
        self.assertEqual(lock.stat().st_ino, inode)

    def test_queued_complete_and_late_complete_share_stable_inode(self):
        import subprocess
        import sys
        import time
        from unittest.mock import patch
        from vibe_guide.adapters.task_provider import ProviderActionStore
        lock = self.paths.vibe / "provider-actions" / "locks" / (self.action_id + ".lock")
        child = ("from pathlib import Path; from vibe_guide.paths import ProjectPaths; "
                 "from vibe_guide.adapters.task_provider import ProviderActionStore; "
                 "import sys; print('ready', flush=True); "
                 "ProviderActionStore(ProjectPaths(Path(sys.argv[1]))).complete("
                 "sys.argv[2], {'thread_id':'late'}, native=False)")
        spawned = []
        original = plan_amendment.save_snapshot

        def queue_before_unlock(*args):
            process = subprocess.Popen([sys.executable, "-c", child, str(self.root), self.action_id],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            spawned.append(process)
            self.assertEqual(process.stdout.readline().strip(), "ready")
            time.sleep(0.2)
            self.assertIsNone(process.poll())
            return original(*args)

        with patch.object(plan_amendment, "save_snapshot", side_effect=queue_before_unlock):
            self._retire()
        inode = lock.stat().st_ino
        output, errors = spawned[0].communicate(timeout=10)
        self.assertEqual(spawned[0].returncode, 0, errors)
        self.assertEqual(lock.stat().st_ino, inode)
        self._confirm_blocked()
        self.assertTrue(ProviderActionStore(self.paths).complete(
            self.action_id, {"thread_id": "late"}, native=False))
        self.assertEqual(lock.stat().st_ino, inode)
        self._confirm_blocked()

    def test_foreign_run_lease_blocks_retire(self):
        from vibe_guide.state import _lease_id
        data = json.loads(self.lease_file.read_text())
        data.update(run_id="run-other", lease_id=_lease_id(
            self.NODE, self.node["worktree"], "run-other", data["role"],
            data["generation"], data["intent_digest"]))
        self.lease_file.write_text(json.dumps(data))
        with self.assertRaises((PermissionError, ValueError)):
            self._retire()
        self.assertTrue(self.lease_file.exists())

    def test_foreign_run_same_root_blocks_confirm(self):
        self._retire()
        from vibe_guide.state import acquire_writer_lease
        self.assertTrue(acquire_writer_lease(
            self.paths, self.NODE, self.node["worktree"], "run-other"))
        self._confirm_blocked()

    def test_release_failure_cannot_report_retired(self):
        from unittest.mock import patch
        with patch.object(plan_amendment, "release_writer_lease", return_value=False):
            with self.assertRaises((PermissionError, ValueError)):
                self._retire()
        self.assertTrue(self.lease_file.exists())

    def test_event_readback_retains_proof_but_redacts_text(self):
        self._retire()
        [event] = [e for e in load_events(self.paths, self.run_id)
                   if e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT]
        proof = event["data"].get("retirement_proof", {})
        self.assertEqual(proof.get("evidence_sha256"), hashlib.sha256(
            json.dumps(self.evidence, ensure_ascii=False,
                       sort_keys=True).encode()).hexdigest())
        self.assertEqual(proof["owner_takeover"]["old_session_id"], self.OLD_OWNER)
        self.assertEqual(proof["retired_consumers"][0]["rollout_sha256"],
                         self.evidence["retired_consumers"][0]["rollout_sha256"])

    def test_event_proof_rejects_sensitive_free_text(self):
        from vibe_guide.contracts import RunEvent
        from vibe_guide.state import append_event
        secret = "sk-sensitive-example-123456"
        append_event(self.paths, RunEvent("retire_uninvoked_legacy", {
            "run_id": self.run_id, "plan_id": "probe-plan",
            "retirement_proof": {"evidence_sha256": "a" * 64,
                "prompt": secret, "requests": [{"action_id": secret,
                    "request_digest": "b" * 64, "provider_output": secret}],
                "owner_takeover": {"old_session_id": secret, "raw": secret}},
        }), {"role": "system", "generation": 0})
        data = load_events(self.paths, self.run_id)[-1]["data"]
        self.assertEqual(data["retirement_proof"]["evidence_sha256"], "a" * 64)
        self.assertEqual(data["retirement_proof"]["requests"][0]["request_digest"], "b" * 64)
        self.assertNotIn(secret, json.dumps(data))


class R8CorrectionRecoveryTests(RetireBaseCorrectionTests):
    def _add_second_pending_action(self):
        from vibe_guide.monitor import Monitor
        from vibe_guide.state import acquire_writer_lease

        node_id = "probe-node-b"
        snapshot = load_snapshot(self.paths, self.run_id)
        node = dict(snapshot.nodes[node_id])
        head = self._git_init_branch(self.root / node["worktree"], node["branch"])
        node.update(status="running", worktree_base=head, active_task=None,
                    active_role=None, retryable_action={
                        "role": "developer", "phase": "develop",
                        "last_observation_ref": "provider-pending",
                        "same_task": True, "same_task_required": True,
                        "successor": False})
        snapshot.nodes[node_id] = node
        save_snapshot(self.paths, snapshot)
        intent = Monitor._prospective_intent_digest(
            snapshot, node_id, "developer", 1, node)
        self.assertTrue(acquire_writer_lease(
            self.paths, node_id, node["worktree"], self.run_id,
            intent_digest=intent, role="developer", generation=1))

        basis = {"operation": "create", "provider": "codex-app-visible",
                 "run_id": self.run_id, "issue_id": node_id,
                 "role": "developer", "generation": 1, "sequence": 0}
        action_id = "action-" + plan_amendment._canonical_digest(basis)[:32]
        request = {"schema_version": 1, "action_id": action_id,
                   "native_tool": "codex_app__create_thread",
                   "request": {"authorization_digest": self.auth_digest,
                               "target": {"type": "project", "projectId": "same-project",
                                          "environment": {"type": "local"}},
                               "child_binding": {"node_id": node_id, "role": "developer",
                                                 "worktree": node["worktree"],
                                                 "branch": node["branch"]}}, **basis}
        request["request_digest"] = plan_amendment._canonical_digest(request)
        path = self.paths.vibe / "provider-actions" / "requests" / (action_id + ".json")
        path.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
        self.evidence["requests"].append({
            "action_id": action_id, "request_digest": request["request_digest"],
            "request_sha256": self._sha(path), "issue_id": node_id,
            "role": "developer", "generation": 1})
        self.evidence["worktrees"].append({"node_id": node_id,
            "worktree": node["worktree"], "branch": node["branch"], "head": head})
        return node_id, action_id, head

    def test_two_actions_recover_each_boundary_without_losing_holds(self):
        from unittest.mock import patch
        from tests import support_v45_authorize

        for boundary in ("stale-first", "stale-second", "before_event", "after_event",
                         "after_snapshot", "lease-first", "lease-second", "after_cleanup"):
            with self.subTest(boundary=boundary):
                fixture = R8CorrectionRecoveryTests(methodName="runTest")
                spec = copy.deepcopy(support_v45_authorize._NODE_SPEC)
                second = copy.deepcopy(spec["nodes"][0])
                second.update(id="probe-node-b", title="探针节点 B")
                spec["nodes"][0]["contract"]["files"] = ["probe-a.py"]
                second["contract"]["files"] = ["probe-b.py"]
                spec["nodes"].append(second)
                with patch.object(support_v45_authorize, "_NODE_SPEC", spec):
                    fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                node_b, action_b, head_b = fixture._add_second_pending_action()
                holds = fixture.paths.vibe / "runs" / fixture.run_id / "supervisor-holds.json"
                hold_bytes = b'{"probe-node-a":{"reason":"hold-a"},"probe-node-b":{"reason":"hold-b"}}'
                holds.write_bytes(hold_bytes)
                fault = {"stale-first": "after_stale:" + fixture.action_id,
                         "stale-second": "after_stale:" + action_b,
                         "lease-first": "after_lease:" + fixture.NODE,
                         "lease-second": "after_lease:" + node_b}.get(boundary, boundary)
                with self.assertRaises(RuntimeError):
                    plan_amendment.retire_uninvoked_legacy(
                        fixture.paths, "probe-plan", fixture.digest, fixture.evidence,
                        _fault_at=fault)
                self.assertEqual(holds.read_bytes(), hold_bytes)
                self.assertEqual(fixture._retire()["status"], "retired")
                self.assertEqual(fixture._retire()["status"], "retired")
                self.assertEqual(holds.read_bytes(), hold_bytes)
                events = [e for e in load_events(fixture.paths, fixture.run_id)
                          if e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT]
                self.assertEqual(len(events), 1)
                self.assertEqual(set(events[0]["data"]["superseded_creates"]),
                                 {fixture.action_id, action_b})
                snapshot = load_snapshot(fixture.paths, fixture.run_id)
                self.assertEqual(snapshot.nodes[fixture.NODE]["worktree_base"], fixture.corrected_base)
                self.assertEqual(snapshot.nodes[node_b]["worktree_base"], head_b)
                for node_id in (fixture.NODE, node_b):
                    self.assertEqual(snapshot.nodes[node_id]["status"], "planned")
                    self.assertIsNone(snapshot.nodes[node_id]["retryable_action"])
                self.assertFalse(list((fixture.paths.vibe / "leases").glob("*.json")))

    def test_correction_replay_all_crash_boundaries(self):
        for fault in ("after_stale:" + self.action_id, "before_event", "after_event",
                      "after_snapshot", "after_lease:" + self.NODE, "after_cleanup"):
            with self.subTest(fault=fault):
                # Each subtest gets its own independent fixture.
                fixture = R8CorrectionRecoveryTests(methodName="runTest")
                fixture.setUp()
                actual_fault = fault.replace(self.action_id, fixture.action_id)
                with self.assertRaises(RuntimeError):
                    plan_amendment.retire_uninvoked_legacy(
                        fixture.paths, "probe-plan", fixture.digest, fixture.evidence, _fault_at=actual_fault)
                self.assertEqual(fixture._retire()["status"], "retired")
                self.assertEqual(fixture._retire()["status"], "retired")
                events = [e for e in load_events(fixture.paths, fixture.run_id)
                          if e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT]
                self.assertEqual(len(events), 1)
                self.assertEqual(load_snapshot(fixture.paths, fixture.run_id).nodes[fixture.NODE]["worktree_base"], fixture.corrected_base)
                fixture.doCleanups()

    def test_snapshot_to_lease_crash_replays_same_correction(self):
        from unittest.mock import patch
        with patch.object(plan_amendment, "release_writer_lease",
                          side_effect=RuntimeError("crash after snapshot")):
            with self.assertRaises(RuntimeError):
                self._retire()
        self.assertEqual(load_snapshot(self.paths, self.run_id)
                         .nodes[self.NODE]["worktree_base"], self.corrected_base)
        self.assertEqual(self._retire()["status"], "retired")
        self.assertEqual(self._retire()["status"], "retired")
        self.assertEqual(len([e for e in load_events(self.paths, self.run_id)
                             if e["event"] == plan_amendment.RETIRE_UNINVOKED_EVENT]), 1)
