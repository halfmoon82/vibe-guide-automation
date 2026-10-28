"""Regression tests for the gates that blocked a same-run reauthorization.

Every test here reproduces a failure that was observed on a real run rather
than a hypothetical one:

``(C)`` the authorization card advertised ``dual-visible`` for all twelve
nodes while the dispatcher ruled them ``visible-sdd``, because
``refresh_authorization_card`` carried ``previous.workers`` through verbatim.

``(a)`` ``Monitor.reauthorize`` reset every never-started node back to
``planned`` and then dispatched without refreshing the execution projection,
so ``_schedule_ready``'s opening ``_validate_execution_topology`` compared the
new ready set against the stale one and failed closed with ``ready-set
drift``.

``(d)`` ``snapshot.prd_digest``/``spec_digest`` were written once by
``Monitor.start`` and never again, so any post-authorization PRD/Spec edit
made ``resume`` report ``PRD/Spec source drift`` forever.

``(b)`` lived in ``cli.py`` (publish the card only after reauthorize
succeeds) and is covered by ``tests/test_cli.py``.
"""
import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from vibe_guide import cli as cli_module
from vibe_guide import monitor as monitor_module
from vibe_guide.authorize_entry import load_live_workflow
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.authorization import (
    BACKGROUND_MODE_DISCLOSURES,
    authorize,
    build_authorization_card,
    dispatch_topology_for_node,
    acceptance_contract_projection,
    verified_authorized_nodes,
    canonical_node_contracts,
    legacy_executable_contract_digest,
    executable_contract_digest,
    is_authorization_valid,
    refresh_authorization_card,
    validate_runtime_contract,
)
from vibe_guide.cli import (
    _VOLATILE_CARD_FIELDS,
    _card_field_diff,
    _engine_evidence_required,
    _load_plan,
    _normalize_card_value,
    _observed_adapter,
    _staged_engine_attestation,
    run_cli,
)
from vibe_guide.engine_attestation import (
    create_engine_attestation,
    validate_engine_attestation,
)
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import load_events, load_snapshot
from vibe_guide.task_registry import TaskBinding, load_task_binding, save_task_binding

from tests.support_v45_authorize import publish_complex_probe


def _business_node(node_id, adapter_id="workbuddy", **contract_overrides):
    contract = {
        "files": [node_id + ".py"],
        "worker": "worker-" + node_id,
        "worktree": ".worktrees/" + node_id,
        "adapter_id": adapter_id,
    }
    contract.update(contract_overrides)
    return DAGNode(node_id, node_id, [], [], "g1", contract, "ready")


def _worker_profile(allowlist, node_id="alpha"):
    return {
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
        "allowlist": allowlist,
    }


class ReauthorizationContractDigestTests(unittest.TestCase):
    """File-scope changes must not invalidate acceptance evidence."""

    def test_file_scope_expansion_keeps_the_node_contract_digest(self):
        original = _business_node("alpha")
        expanded = _business_node("alpha")
        expanded.contract["files"].append("tests/test_alpha.py")

        self.assertEqual(
            executable_contract_digest([original]),
            executable_contract_digest([expanded]),
        )

    def test_acceptance_semantics_still_change_the_node_contract_digest(self):
        original = _business_node("alpha")
        changed = _business_node("alpha", acceptance_example="verified_fact: changed")

        self.assertNotEqual(
            executable_contract_digest([original]),
            executable_contract_digest([changed]),
        )

    def test_scope_expansion_still_changes_the_authorization_file_scope(self):
        plan = Plan("plan-1", 1, "docs/prd.md", ["alpha"], "draft")
        original = _business_node("alpha")
        card = build_authorization_card(plan, [original], AgentCapabilities("fake", True, True, True, True, True, "full"))
        expanded = _business_node("alpha", files=["alpha.py", "tests/test_alpha.py"])

        refreshed = refresh_authorization_card(plan, [expanded], card)

        self.assertEqual(refreshed.node_contract_digest, card.node_contract_digest)
        self.assertIn("tests/test_alpha.py", refreshed.file_scope)
        self.assertNotEqual(refreshed.digest, card.digest)

    def test_worker_profile_allowlist_drift_invalidates_authorization(self):
        plan = Plan("plan-1", 1, "docs/prd.md", ["alpha"], "draft")
        original = _business_node(
            "alpha", worker_profile=_worker_profile(["alpha.py"])
        )
        changed = _business_node(
            "alpha", worker_profile=_worker_profile(["outside.py"])
        )
        capabilities = AgentCapabilities(
            "fake", True, True, True, True, True, "full"
        )
        record = authorize(
            build_authorization_card(plan, [original], capabilities), "AUTHORIZE"
        )

        self.assertFalse(is_authorization_valid(record, plan, [changed]))

    def test_worker_profile_allowlist_drift_blocks_provider_start(self):
        temporary = tempfile.TemporaryDirectory(prefix="vg-issue-147-allowlist-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        paths = ProjectPaths(root)
        (paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n',
            encoding="utf-8",
        )
        save_contract(paths, build_contract(root, provider="fake", host_id="local"))
        capabilities = AgentCapabilities(
            "fake", True, True, True, True, True, "full"
        )
        original = _business_node(
            "alpha", worker_profile=_worker_profile(["alpha.py"])
        )
        changed = _business_node(
            "alpha", worker_profile=_worker_profile(["outside.py"])
        )
        plan = Plan("plan-1", 1, "docs/prd.md", ["alpha"], "draft")
        record = authorize(
            build_authorization_card(plan, [original], capabilities), "AUTHORIZE"
        )
        runner = FakeRunner()

        with self.assertRaises(PermissionError):
            Monitor(paths, plan, [changed]).start(record, runner)
        self.assertEqual(runner.start_calls, [])

    def test_runtime_contract_rejects_worker_profile_allowlist_outside_scope(self):
        with self.assertRaises(ValueError):
            validate_runtime_contract(
                {
                    "files": ["alpha.py"],
                    "worker_profile": {"allowlist": ["outside.py"]},
                },
                authorized_files=("alpha.py",),
            )


class ScopeOnlyReauthorizationTests(unittest.TestCase):
    """A scope-only edit must not invalidate a downstream acceptance."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vg-issue-147-")
        self.root = Path(self.temporary.name)
        self.paths = ProjectPaths(self.root)
        (self.paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n',
            encoding="utf-8",
        )
        save_contract(
            self.paths,
            build_contract(self.root, provider="fake", host_id="local"),
        )
        self.capabilities = AgentCapabilities(
            "fake", True, True, True, True, True, "full"
        )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _nodes(a_files=None, a_acceptance="verified_fact: accepted"):
        a = _business_node("a", files=a_files or ["a.py"], acceptance_example=a_acceptance)
        b = _business_node("b")
        b.depends_on = ["a"]
        for current in (a, b):
            current.contract["worker_profile"] = {
                "worker": "codex",
                "model": "test",
                "reasoning": "normal",
                "fallbacks": [],
                "selection_basis": {
                    "issue_complexity_ref": current.id,
                    "complexity_band": "standard",
                    "risk_tags": [],
                    "availability_evidence": "test",
                },
                "writer": "writer",
                "worktree": ".worktrees/" + current.id,
                "branch": "branch-" + current.id,
                "allowlist": list(current.contract["files"]),
            }
        return [a, b]

    def _accepted_run(self):
        nodes = self._nodes()
        plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")
        record = authorize(
            build_authorization_card(plan, nodes, self.capabilities), "AUTHORIZE"
        )
        runner = FakeRunner(
            events={
                (node_id, "developer"): [("complete", {"evidence": node_id})]
                for node_id in ("a", "b")
            }
            | {
                (node_id, "reviewer"): [("accepted", {"evidence": node_id})]
                for node_id in ("a", "b")
            }
        )
        monitor = Monitor(self.paths, plan, nodes)
        snapshot = monitor.start(record, runner)
        for _ in range(8):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(
            {node_id: current["status"] for node_id, current in snapshot.nodes.items()},
            {"a": "accepted", "b": "accepted"},
        )
        cursors = {}
        for role in ("developer", "reviewer"):
            binding = load_task_binding(self.paths, "b", role, run_id=snapshot.run_id)
            binding.cursor = "cursor-b-" + role
            save_task_binding(self.paths, binding)
            cursors[role] = binding.cursor
        return snapshot, nodes, cursors

    def test_scope_only_expansion_keeps_a_and_b_accepted(self):
        snapshot, _old_nodes, cursors = self._accepted_run()
        changed_nodes = self._nodes(a_files=["a.py", "tests/test_a.py"])
        changed_plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")
        changed_record = authorize(
            build_authorization_card(changed_plan, changed_nodes, self.capabilities),
            "AUTHORIZE",
        )

        rebound = Monitor(self.paths, changed_plan, changed_nodes).reauthorize(
            snapshot.run_id,
            changed_record,
            FakeRunner(),
            "executable_contract_changed",
        )

        self.assertEqual(rebound.nodes["a"]["status"], "accepted")
        self.assertEqual(rebound.nodes["b"]["status"], "accepted")
        for role, cursor in cursors.items():
            self.assertEqual(
                load_task_binding(self.paths, "b", role, run_id=snapshot.run_id).cursor,
                cursor,
            )

    def test_acceptance_projection_preserves_protected_and_unknown_fields(self):
        import copy
        original = self._nodes()[0]
        protected = (
            ("worktree", "outside"), ("files", ["outside.py"]),
            ("actions", ["test"]), ("required_capabilities", ["new"]),
            ("unknown_business_rule", "changed"),
        )
        for key, value in protected:
            with self.subTest(key=key):
                changed = copy.deepcopy(original)
                changed.contract[key] = value
                self.assertNotEqual(acceptance_contract_projection(original),
                                    acceptance_contract_projection(changed))
        for key, value in (("writer", "new-writer"), ("branch", "new-branch"),
                           ("allowlist", ["outside.py"]), ("fallbacks", ["new"]),
                           ("selection_basis", {"risk_tags": ["changed"]})):
            with self.subTest(profile_key=key):
                changed = copy.deepcopy(original)
                changed.contract["worker_profile"][key] = value
                self.assertNotEqual(acceptance_contract_projection(original),
                                    acceptance_contract_projection(changed))

    def test_old_dag_proof_requires_per_node_scope_and_matching_commitments(self):
        nodes = self._nodes()
        plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")
        record = authorize(build_authorization_card(plan, nodes, self.capabilities), "AUTHORIZE")
        encoded = {item["id"]: json.dumps(item) for item in canonical_node_contracts(nodes)}
        digests = {node.id: executable_contract_digest([node]) for node in nodes}
        scopes = {node.id: legacy_executable_contract_digest([node]) for node in nodes}
        self.assertEqual(set(verified_authorized_nodes(encoded, digests, record, scopes)), {"a", "b"})
        self.assertEqual(verified_authorized_nodes(encoded, digests, record), {})
        altered = json.loads(encoded["a"])
        altered["contract"]["files"] = ["b.py"]
        altered_encoded = dict(encoded, a=json.dumps(altered))
        self.assertEqual(verified_authorized_nodes(altered_encoded, digests, record, scopes), {})
        forged_scopes = dict(scopes, a=legacy_executable_contract_digest([DAGNode.from_dict(altered)]))
        self.assertEqual(verified_authorized_nodes(altered_encoded, digests, record, forged_scopes), {})
        self.assertEqual(verified_authorized_nodes(encoded, dict(digests, a="0" * 64), record, scopes), {})

    def test_route_only_change_retains_acceptances_under_the_new_epoch(self):
        snapshot, old_nodes, _cursors = self._accepted_run()
        old_epoch = snapshot.authorization_digest
        changed_nodes = self._nodes()
        for node in changed_nodes:
            node.contract["adapter_id"] = "cursor"
            node.contract["worker_profile"]["model"] = "new-model"
        plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")
        record = authorize(build_authorization_card(plan, changed_nodes, self.capabilities), "AUTHORIZE")
        self.assertNotEqual(record.node_contract_digest, executable_contract_digest(old_nodes))
        rebound = Monitor(self.paths, plan, changed_nodes).reauthorize(
            snapshot.run_id, record, FakeRunner(), "executable_contract_changed"
        )
        self.assertNotEqual(old_epoch, record.digest)
        for node in changed_nodes:
            self.assertEqual(rebound.nodes[node.id]["status"], "accepted")
            self.assertEqual(rebound.nodes[node.id]["acceptance"], {
                "contract_digest": executable_contract_digest([node]),
                "authorization_epoch": record.digest,
            })
        loaded = load_snapshot(self.paths, snapshot.run_id)
        self.assertEqual(loaded.authorization_digest, record.digest)

    def test_route_change_with_scope_redistribution_does_not_retain_acceptance(self):
        snapshot, _old_nodes, _cursors = self._accepted_run()
        changed_nodes = self._nodes()
        for node, scope in zip(changed_nodes, (["b.py"], ["a.py"])):
            node.contract["adapter_id"] = "cursor"
            node.contract["files"] = list(scope)
            node.contract["worker_profile"]["allowlist"] = list(scope)
        plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")
        record = authorize(build_authorization_card(plan, changed_nodes, self.capabilities), "AUTHORIZE")
        rebound = Monitor(self.paths, plan, changed_nodes).reauthorize(
            snapshot.run_id, record, FakeRunner(), "executable_contract_changed"
        )
        self.assertIsNone(rebound.nodes["a"]["acceptance"])
        self.assertIsNone(rebound.nodes["b"]["acceptance"])
        transition = next(event for event in reversed(load_events(self.paths, snapshot.run_id))
                          if event["event"] == "authorization_reauthorized")
        self.assertEqual(transition["data"]["retained_acceptances"], {})

    # -- #289 draft section 2: platform_handoff is a first-class reason ------

    def _handoff_nodes(self, **kwargs):
        nodes = self._nodes(**kwargs)
        for node in nodes:
            node.contract["adapter_id"] = "cursor"
            node.contract["worker_profile"]["worker"] = "cursor"
            node.contract["worker_profile"]["model"] = "cursor-model"
        return nodes

    def _switch_platform(self):
        save_contract(
            self.paths, build_contract(self.root, provider="cursor", host_id="local")
        )

    def _handoff(self, snapshot, nodes, runner=None):
        plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")
        record = authorize(
            build_authorization_card(plan, nodes, self.capabilities), "AUTHORIZE"
        )
        monitor = Monitor(self.paths, plan, nodes)
        return monitor, record, monitor.reauthorize(
            snapshot.run_id, record, runner or FakeRunner(), "platform_handoff"
        )

    def test_platform_handoff_retains_acceptances_across_capability_change(self):
        snapshot, _old_nodes, _cursors = self._accepted_run()
        previous_capability = snapshot.capability_contract_digest
        self._switch_platform()
        nodes = self._handoff_nodes()
        _monitor, record, rebound = self._handoff(snapshot, nodes)

        self.assertNotEqual(rebound.capability_contract_digest, previous_capability)
        for node in nodes:
            self.assertEqual(rebound.nodes[node.id]["status"], "accepted")
            self.assertEqual(rebound.nodes[node.id]["acceptance"], {
                "contract_digest": executable_contract_digest([node]),
                "authorization_epoch": record.digest,
            })
        transition = [event for event in load_events(self.paths, snapshot.run_id)
                      if event["event"] == "authorization_reauthorized"][-1]["data"]
        self.assertEqual(transition["change_reason"], "platform_handoff")
        self.assertEqual(transition["changed_nodes"], [])
        self.assertEqual(sorted(transition["retained_acceptances"]), ["a", "b"])
        # Replay validation accepts the persisted handoff lineage.
        self.assertEqual(
            load_snapshot(self.paths, snapshot.run_id).authorization_digest, record.digest
        )

    def test_platform_handoff_with_acceptance_change_is_contract_drift(self):
        snapshot, _old_nodes, _cursors = self._accepted_run()
        self._switch_platform()
        nodes = self._handoff_nodes(a_acceptance="verified_fact: changed")
        before = len(load_events(self.paths, snapshot.run_id))
        with self.assertRaisesRegex(PermissionError, "contract_drift"):
            self._handoff(snapshot, nodes)
        self.assertEqual(len(load_events(self.paths, snapshot.run_id)), before)
        self.assertEqual(
            load_snapshot(self.paths, snapshot.run_id).authorization_digest,
            snapshot.authorization_digest,
        )

    def test_platform_handoff_with_scope_change_is_contract_drift(self):
        # A file-scope change is an acceptance-contract change even when the
        # ordinary reauthorization would keep it as a scope-only expansion.
        snapshot, _old_nodes, _cursors = self._accepted_run()
        self._switch_platform()
        nodes = self._handoff_nodes(a_files=["a.py", "tests/test_a.py"])
        with self.assertRaisesRegex(PermissionError, "contract_drift"):
            self._handoff(snapshot, nodes)

    def _scope_expansion_transition(self):
        """A genuine scope-only expansion reauthorization, plus the pre-state.

        The ordinary reason accepts it (scope-only expansion keeps acceptance).
        Relabelled as ``platform_handoff`` it must be refused on replay: a
        handoff never moves the write scope, and the write-side gate that
        enforces that does not run on replay.
        """
        import copy

        snapshot, _old_nodes, _cursors = self._accepted_run()
        before = copy.deepcopy(load_snapshot(self.paths, snapshot.run_id))
        nodes = self._nodes(a_files=["a.py", "tests/test_a.py"])
        plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")
        record = authorize(
            build_authorization_card(plan, nodes, self.capabilities), "AUTHORIZE"
        )
        monitor = Monitor(self.paths, plan, nodes)
        monitor.reauthorize(
            snapshot.run_id, record, FakeRunner(), "executable_contract_changed"
        )
        events = load_events(self.paths, snapshot.run_id)
        transition = [e for e in events if e["event"] == "authorization_reauthorized"][-1]
        self.assertEqual(transition["data"]["changed_nodes"], [])
        return monitor, before, snapshot.run_id, events

    def test_state_replay_rejects_a_handoff_that_widens_the_file_scope(self):
        import copy
        from vibe_guide.state import _decode_snapshot, run_dir

        _monitor, _before, run_id, events = self._scope_expansion_transition()
        records = copy.deepcopy(events)
        for record in records:
            if record["event"] == "authorization_reauthorized":
                record["data"]["change_reason"] = "platform_handoff"
        state = (run_dir(self.paths, run_id) / "state.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "platform handoff transition changed"):
            _decode_snapshot(state, records)
        # The same log with the genuine reason still decodes.
        _decode_snapshot(state, copy.deepcopy(events))

    def test_monitor_replay_rejects_a_handoff_that_widens_the_file_scope(self):
        import copy

        monitor, before, _run_id, events = self._scope_expansion_transition()
        data = copy.deepcopy(
            [e for e in events if e["event"] == "authorization_reauthorized"][-1]["data"]
        )
        monitor._apply_reauthorization_transition(copy.deepcopy(before), copy.deepcopy(data))
        data["change_reason"] = "platform_handoff"
        with self.assertRaisesRegex(ValueError, "platform handoff transition changed"):
            monitor._apply_reauthorization_transition(copy.deepcopy(before), data)

    def test_unknown_reason_still_cannot_absorb_a_capability_change(self):
        snapshot, _old_nodes, _cursors = self._accepted_run()
        self._switch_platform()
        nodes = self._handoff_nodes()
        plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")
        record = authorize(
            build_authorization_card(plan, nodes, self.capabilities), "AUTHORIZE"
        )
        with self.assertRaisesRegex(PermissionError, "capability_contract_unknown"):
            Monitor(self.paths, plan, nodes).reauthorize(
                snapshot.run_id, record, FakeRunner(), "platform_switch"
            )

    def test_platform_handoff_clears_the_supervisor_contract_drift_gate(self):
        from vibe_guide.supervisor import contract_drift

        snapshot, _old_nodes, _cursors = self._accepted_run()
        self._switch_platform()
        drift = contract_drift(self.paths, snapshot.run_id)
        self.assertEqual(drift["state"], "contract_drift")
        # The gate names the legal exit for a platform switch.
        self.assertIn("--platform-handoff", drift["action"])
        self._handoff(snapshot, self._handoff_nodes())
        self.assertIsNone(contract_drift(self.paths, snapshot.run_id))

    def test_acceptance_semantic_change_invalidates_downstream_b(self):
        snapshot, _old_nodes, _cursors = self._accepted_run()
        changed_nodes = self._nodes(a_acceptance="verified_fact: changed")
        changed_plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")
        changed_record = authorize(
            build_authorization_card(changed_plan, changed_nodes, self.capabilities),
            "AUTHORIZE",
        )

        rebound = Monitor(self.paths, changed_plan, changed_nodes).reauthorize(
            snapshot.run_id,
            changed_record,
            FakeRunner(),
            "executable_contract_changed",
        )

        self.assertEqual(rebound.nodes["a"]["status"], "rework")
        self.assertEqual(rebound.nodes["b"]["status"], "blocked_unknown")
        self.assertIsNone(rebound.nodes["b"]["acceptance"])


class PlatformHandoffReworkTests(unittest.TestCase):
    """#289 section 3 (PM ruling 2026-10-26): successor only after a handoff.

    Rework of a node whose task was bound before a recorded platform_handoff
    goes to a successor developer on the new platform (the old task cannot be
    resumed there).  Without a handoff -- or for tasks created after it -- the
    ordinary rule stays: Review findings return to the same worker.
    """

    # Reuse the fixture without re-running the parent class's tests.
    setUp = ScopeOnlyReauthorizationTests.setUp
    tearDown = ScopeOnlyReauthorizationTests.tearDown
    _nodes = staticmethod(ScopeOnlyReauthorizationTests._nodes)
    _handoff_nodes = ScopeOnlyReauthorizationTests._handoff_nodes
    _switch_platform = ScopeOnlyReauthorizationTests._switch_platform
    _handoff = ScopeOnlyReauthorizationTests._handoff
    _accepted_run = ScopeOnlyReauthorizationTests._accepted_run

    def _in_review_run(self, b_reviewer_events=()):
        nodes = self._nodes()
        plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")
        record = authorize(
            build_authorization_card(plan, nodes, self.capabilities), "AUTHORIZE"
        )
        runner = FakeRunner(events={
            ("a", "developer"): [("complete", {"evidence": "a"})],
            ("a", "reviewer"): [("accepted", {"evidence": "a"})],
            ("b", "developer"): [("complete", {"evidence": "b"})],
            ("b", "reviewer"): list(b_reviewer_events),
        })
        monitor = Monitor(self.paths, plan, nodes)
        snapshot = monitor.start(record, runner)
        for _ in range(8):
            snapshot = monitor.tick(snapshot.run_id, runner)
            if snapshot.nodes["b"]["status"] == "review" and not b_reviewer_events:
                break
        return monitor, snapshot, runner

    def _starts(self, runner, node_id, role):
        return [call for call in runner.start_calls
                if call["node_id"] == node_id and call["role"] == role]

    def test_same_platform_finding_returns_to_the_same_developer(self):
        _monitor, snapshot, runner = self._in_review_run(
            [("review_finding", {"finding": "fix it", "in_contract": True})]
        )
        developer = self._starts(runner, "b", "developer")
        self.assertGreaterEqual(len(developer), 2)
        self.assertTrue(developer[-1]["continuation"])
        self.assertFalse(developer[-1]["successor"])
        self.assertEqual(developer[-1]["task_id"], developer[0]["task_id"])

    def test_in_flight_node_is_redispatched_as_a_successor_after_handoff(self):
        _monitor, snapshot, runner = self._in_review_run()
        self.assertEqual(snapshot.nodes["b"]["status"], "review")
        old_developer = snapshot.nodes["b"]["developer_identity"]
        old_reviewer = snapshot.nodes["b"]["reviewer_identity"]
        self._switch_platform()
        handoff_runner = FakeRunner(events={
            ("b", "developer"): [("complete", {"evidence": "b2"})],
            ("b", "reviewer"): [("accepted", {"evidence": "b2"})],
        })
        monitor, _record, rebound = self._handoff(
            snapshot, self._handoff_nodes(), handoff_runner
        )
        # The accepted upstream node keeps its acceptance.
        self.assertEqual(rebound.nodes["a"]["status"], "accepted")
        developer = self._starts(handoff_runner, "b", "developer")
        self.assertEqual(len(developer), 1)
        self.assertTrue(developer[0]["successor"])
        self.assertFalse(developer[0]["continuation"])
        self.assertEqual(developer[0]["predecessor_task_id"], old_developer)
        self.assertNotEqual(developer[0]["task_id"], old_developer)
        old = [item for item in json.loads(
            (self.paths.vibe / "runs" / snapshot.run_id / "tasks.json").read_text()
        )["bindings"] if item["task_id"] == old_developer]
        self.assertEqual([item["status"] for item in old], ["archived"])
        new = load_task_binding(self.paths, "b", "developer", run_id=snapshot.run_id)
        self.assertEqual(new.successor_of, old_developer)
        self.assertEqual(new.handoff_from_provider, "runner")
        events = load_events(self.paths, snapshot.run_id)
        reconciled = [event["data"] for event in events
                      if event["event"] == "handoff_predecessor_reconciled"]
        self.assertEqual(reconciled[-1]["predecessor_task_id"], old_developer)
        self.assertEqual(reconciled[-1]["proof"], "platform_handoff")

        # The re-review after the successor delivers also needs a new-platform
        # reviewer: the old reviewer task cannot be resumed either.
        for _ in range(6):
            rebound = monitor.tick(snapshot.run_id, handoff_runner)
        reviewer = self._starts(handoff_runner, "b", "reviewer")
        self.assertEqual(len(reviewer), 1)
        self.assertTrue(reviewer[0]["successor"])
        self.assertEqual(reviewer[0]["predecessor_task_id"], old_reviewer)
        self.assertEqual(rebound.nodes["b"]["status"], "accepted")

    def test_post_handoff_task_finding_returns_to_the_successor_itself(self):
        _monitor, snapshot, _runner = self._in_review_run()
        self._switch_platform()
        handoff_runner = FakeRunner(events={
            ("b", "developer"): [("complete", {"evidence": "b2"}),
                                 ("complete", {"evidence": "b3"})],
            ("b", "reviewer"): [
                ("review_finding", {"finding": "again", "in_contract": True}),
                ("accepted", {"evidence": "b3"}),
            ],
        })
        monitor, _record, rebound = self._handoff(
            snapshot, self._handoff_nodes(), handoff_runner
        )
        for _ in range(10):
            rebound = monitor.tick(snapshot.run_id, handoff_runner)
        developer = self._starts(handoff_runner, "b", "developer")
        self.assertEqual(len(developer), 2)
        self.assertTrue(developer[0]["successor"])
        # Same platform from here on: back to the successor, not another one.
        self.assertTrue(developer[1]["continuation"])
        self.assertFalse(developer[1]["successor"])
        self.assertEqual(developer[1]["task_id"], developer[0]["task_id"])
        self.assertEqual(rebound.nodes["b"]["status"], "accepted")

    def _foreign_successor_create(self, snapshot, lineage, generation=1):
        from vibe_guide.adapters.task_provider import ProviderActionStore

        request = {"successor": True, "predecessor_task_id": "reviewer:b",
                   "authorization_digest": "0" * 64}
        if lineage:
            request["handoff_from_provider"] = lineage
        with patch("vibe_guide.adapters.task_provider.require_entry"):
            return ProviderActionStore(self.paths).request(
                operation="create", provider="workbuddy", run_id=snapshot.run_id,
                issue_id="b", role="reviewer", generation=generation, native_tool="native",
                request=request,
            )

    def _reconcile_foreign(self, monitor, snapshot, action):
        from types import SimpleNamespace

        # The recovery only serves visible reviewers; FakeRunner registers a
        # generic background-mode row, so make the archived predecessor the
        # visible row the real bridge would have written.
        path = next(self.paths.vibe.glob("runs*/" + snapshot.run_id + "/tasks.json"))
        registry = json.loads(path.read_text())
        for row in registry["bindings"]:
            if row["task_id"] == "reviewer:b":
                row["mode"] = "visible"
                row["visible"] = True
                row["host"] = "old-host"
        path.write_text(json.dumps(registry))
        monitor._reset_binding_cache()

        return monitor.reconcile_created_successor(
            snapshot.run_id, "b", SimpleNamespace(provider="workbuddy"),
            action_id=action["action_id"], request_digest=action["request_digest"],
            predecessor_archive={"threadId": "reviewer:b", "archived": True},
            preservation_path="unused", preservation_digest="unused",
        )

    def test_created_successor_recovery_keeps_the_provider_gate_without_handoff(self):
        snapshot, nodes, _cursors = self._accepted_run()
        plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")
        action = self._foreign_successor_create(snapshot, "runner")
        with self.assertRaisesRegex(ValueError, "predecessor archive is unverified"):
            self._reconcile_foreign(Monitor(self.paths, plan, nodes), snapshot, action)

    def test_created_successor_recovery_opens_the_provider_gate_after_handoff(self):
        snapshot, _nodes, _cursors = self._accepted_run()
        self._switch_platform()
        monitor, _record, rebound = self._handoff(snapshot, self._handoff_nodes())
        missing = self._foreign_successor_create(rebound, None)
        with self.assertRaisesRegex(ValueError, "predecessor archive is unverified"):
            self._reconcile_foreign(monitor, rebound, missing)
        # With the recorded lineage the provider gate passes; the next proof
        # (create lineage under the current authorization) still applies.
        recorded = self._foreign_successor_create(rebound, "runner", generation=2)
        with self.assertRaisesRegex(ValueError, "create lineage is invalid"):
            self._reconcile_foreign(monitor, rebound, recorded)

    def test_live_predecessor_blocks_the_handoff_successor(self):
        _monitor, snapshot, _runner = self._in_review_run()
        old_developer = snapshot.nodes["b"]["developer_identity"]
        self._switch_platform()
        nodes = self._handoff_nodes()
        plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")
        record = authorize(
            build_authorization_card(plan, nodes, self.capabilities), "AUTHORIZE"
        )
        monitor = Monitor(self.paths, plan, nodes)
        handoff_runner = FakeRunner()
        # Simulate an old-platform developer that is still running (no
        # terminal proof): the successor must not be created.
        original = monitor._schedule_ready

        def schedule_after_reviving(snapshot_, runner_, *args, **kwargs):
            binding = load_task_binding(self.paths, "b", "developer", run_id=snapshot.run_id)
            if binding.task_id == old_developer and binding.status != "running":
                binding.status = "running"
                save_task_binding(self.paths, binding)
                monitor._reset_binding_cache()
            return original(snapshot_, runner_, *args, **kwargs)

        with patch.object(monitor, "_schedule_ready", schedule_after_reviving):
            rebound = monitor.reauthorize(
                snapshot.run_id, record, handoff_runner, "platform_handoff"
            )
        self.assertEqual(self._starts(handoff_runner, "b", "developer"), [])
        self.assertEqual(rebound.nodes["b"]["status"], "blocked_unknown")
        self.assertIn("handoff", rebound.nodes["b"]["reason"])


    def test_handoff_predecessor_is_terminal_only_when_delivered_else_unproven(self):
        # Termination basis after a handoff: the old platform cannot be
        # queried, so the only proof is "already delivered" in the local
        # registry.  Any other state, or a missing/mismatched binding, is
        # unproven (the caller turns that into blocked_unknown).
        monitor, snapshot, _runner = self._in_review_run()
        developer = load_task_binding(self.paths, "b", "developer", run_id=snapshot.run_id)
        self.assertEqual(developer.status, "delivered")
        for status in ("running", "created", "blocked_unknown"):
            with self.subTest(status=status):
                developer.status = status
                save_task_binding(self.paths, developer)
                monitor._reset_binding_cache()
                self.assertIsNone(
                    monitor._prove_handoff_predecessor_terminal(snapshot, "b", "developer")
                )
        developer.status = "delivered"
        save_task_binding(self.paths, developer)
        monitor._reset_binding_cache()
        # reauthorize drops every live handle before redispatching.
        snapshot.handles.clear()
        snapshot.nodes["b"]["active_role"] = None
        snapshot.nodes["b"]["active_task"] = None
        with self.subTest(case="identity mismatch"):
            ghost = json.loads(json.dumps(snapshot.nodes["b"]))
            snapshot.nodes["b"]["developer_identity"] = "ghost-task"
            self.assertIsNone(
                monitor._prove_handoff_predecessor_terminal(snapshot, "b", "developer")
            )
            snapshot.nodes["b"].update(ghost)
        with self.subTest(case="delivered"):
            proven = monitor._prove_handoff_predecessor_terminal(snapshot, "b", "developer")
            self.assertEqual(proven[0], developer.task_id)

class TemporaryReadFailureTests(unittest.TestCase):
    def test_resume_read_failure_stays_unknown_without_invalidation_marker(self):
        root = Path(tempfile.mkdtemp(prefix="vg-temporary-read-"))
        self.addCleanup(shutil.rmtree, root, True)
        paths = ProjectPaths(root)
        directory = root / ".vibe" / "plans" / "probe-plan"
        directory.mkdir(parents=True)
        (root / ".vibe" / "state.json").write_text(
            json.dumps({
                "workflow_version": 4,
                "execution_mode": "sdd_first",
                "session_gate": "s0_required",
                "capability_contract_required": True,
            }),
            encoding="utf-8",
        )
        plan = Plan("probe-plan", 1, "docs/prd.md", ["probe-node"], "authorized")
        node = _business_node("probe-node")
        card = build_authorization_card(
            plan, [node], AgentCapabilities("fake", True, True, True, True, True, "full")
        )
        record = authorize(card, "AUTHORIZE")
        (directory / "authorization.json").write_text(
            json.dumps(record.to_dict()), encoding="utf-8"
        )

        with patch.object(
            cli_module.Monitor,
            "resume",
            side_effect=PermissionError("temporary read failure"),
        ), patch.object(
            cli_module, "_load_plan", return_value=(directory, plan, [node], card)
        ), patch.object(
            cli_module, "require_capability_contract", return_value=None
        ):
            result = run_cli(
                [
                    "resume",
                    "--plan", "probe-plan",
                    "--run-id", "run-probe",
                    "--json",
                ],
                root,
                runner=FakeRunner(),
            )

        self.assertEqual(result.payload.get("status"), "unknown", result.payload)
        self.assertFalse((directory / "authorization-invalidated.json").exists())

    def test_engine_attestation_read_failure_stays_unknown_without_invalidation_marker(self):
        root = publish_complex_probe(self)
        paths = ProjectPaths(root)
        authorized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            root,
        )
        self.assertEqual(authorized.payload.get("status"), "ok", authorized.payload)
        directory, plan, nodes, card = _load_plan(paths, "probe-plan")
        snapshot = Monitor(paths, plan, nodes).start(
            authorize(card, "AUTHORIZE"), FakeRunner()
        )
        attestation_path = directory / "engine-attestation.json"
        (directory / "authorization.json").write_text(
            json.dumps(authorize(card, "AUTHORIZE").to_dict()), encoding="utf-8"
        )
        original_read_text = Path.read_text

        def fail_attestation_read(path, *args, **kwargs):
            if path == attestation_path:
                raise PermissionError("temporary attestation read failure")
            return original_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", fail_attestation_read):
            result = run_cli(
                [
                    "resume",
                    "--plan", "probe-plan",
                    "--run-id", snapshot.run_id,
                    "--json",
                ],
                root,
                runner=FakeRunner(),
            )

        self.assertEqual(result.payload.get("status"), "unknown", result.payload)
        self.assertIn("execution_engine_unverified", result.payload.get("reason", ""))
        self.assertFalse((directory / "authorization-invalidated.json").exists())

    def test_workflow_artifact_content_drift_invalidates_authorization(self):
        root = publish_complex_probe(self)
        paths = ProjectPaths(root)
        authorized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            root,
        )
        self.assertEqual(authorized.payload.get("status"), "ok", authorized.payload)
        directory, plan, nodes, card = _load_plan(paths, "probe-plan")
        record = authorize(card, "AUTHORIZE")
        runner = FakeRunner()
        snapshot = Monitor(paths, plan, nodes).start(record, runner)
        (directory / "authorization.json").write_text(
            json.dumps(record.to_dict()), encoding="utf-8"
        )
        prd = directory / "prd.md"
        prd.write_bytes(prd.read_bytes() + b"\nActual PRD content drift.\n")

        result = run_cli(
            [
                "resume",
                "--plan", "probe-plan",
                "--run-id", snapshot.run_id,
                "--json",
            ],
            root,
            runner=runner,
        )

        self.assertEqual(result.payload.get("status"), "blocked_design", result.payload)
        self.assertIn("workflow_evidence_stale", result.payload.get("reason", ""))
        self.assertTrue((directory / "authorization-invalidated.json").exists())


class DispatchTopologyRulingTests(unittest.TestCase):
    """``(C)`` — one ruling, shared by the card and the dispatcher."""

    def setUp(self):
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")

    def _card(self, nodes):
        plan = Plan("plan-1", 1, "docs/prd.md", [item.id for item in nodes], "draft")
        return plan, build_authorization_card(plan, nodes, self.capabilities)

    @staticmethod
    def _by_id(card):
        return {entry["node_id"]: entry for entry in card.workers}

    def test_refresh_rerules_visible_topology_from_the_live_ruling(self):
        nodes = [_business_node("alpha"), _business_node("beta")]
        plan, card = self._card(nodes)
        self.assertEqual(card.topology_summary["by_topology"]["visible-sdd"], 0)

        refreshed = refresh_authorization_card(
            plan, nodes, card, topology_rulings={"workbuddy": "in_session_sdd"}
        )

        entries = self._by_id(refreshed)
        for node_id in ("alpha", "beta"):
            self.assertEqual(entries[node_id]["topology"], "visible-sdd")
            self.assertEqual(entries[node_id]["mode"], "visible")
            self.assertEqual(entries[node_id]["role"], "developer")
        self.assertEqual(refreshed.topology_summary["by_topology"]["visible-sdd"], 2)
        self.assertEqual(refreshed.topology_summary["by_topology"]["dual-visible"], 0)

    def test_refresh_keeps_the_conservative_topology_without_a_ruling(self):
        nodes = [_business_node("alpha")]
        plan, card = self._card(nodes)

        refreshed = refresh_authorization_card(plan, nodes, card, topology_rulings={})

        self.assertEqual(self._by_id(refreshed)["alpha"]["topology"], "dual-visible")

    def test_first_publication_rules_workers_from_the_live_ruling(self):
        """The first card, not only a refresh, must describe the real dispatch.

        ``_normalize_workers_schema`` defaults every undeclared entry to the
        conservative topology, so without a ruling the published card would
        advertise ``dual-visible`` for a node the supervisor dispatches as
        ``visible-sdd`` -- the same split, one publication earlier.
        """
        nodes = [_business_node("alpha"), _business_node("beta")]
        plan = Plan("plan-1", 1, "docs/prd.md", ["alpha", "beta"], "draft")

        without_ruling = build_authorization_card(plan, nodes, self.capabilities)
        self.assertEqual(
            without_ruling.topology_summary["by_topology"]["visible-sdd"], 0
        )

        ruled = build_authorization_card(
            plan,
            nodes,
            self.capabilities,
            topology_rulings={"workbuddy": "in_session_sdd"},
        )

        entries = self._by_id(ruled)
        for node_id in ("alpha", "beta"):
            self.assertEqual(entries[node_id]["topology"], "visible-sdd")
            self.assertEqual(entries[node_id]["mode"], "visible")
            self.assertEqual(entries[node_id]["role"], "developer")
        self.assertEqual(ruled.topology_summary["by_topology"]["visible-sdd"], 2)

    def test_first_publication_stays_conservative_without_a_ruling(self):
        nodes = [_business_node("alpha")]
        plan = Plan("plan-1", 1, "docs/prd.md", ["alpha"], "draft")

        card = build_authorization_card(
            plan, nodes, self.capabilities, topology_rulings={}
        )

        self.assertEqual(self._by_id(card)["alpha"]["topology"], "dual-visible")

    def test_refresh_never_silently_upgrades_a_disclosed_background_worker(self):
        nodes = [_business_node("alpha")]
        plan = Plan("plan-1", 1, "docs/prd.md", ["alpha"], "draft")
        card = build_authorization_card(
            plan,
            nodes,
            self.capabilities,
            workers={
                "alpha": {
                    "mode": "background",
                    "limitations": list(BACKGROUND_MODE_DISCLOSURES),
                }
            },
        )

        refreshed = refresh_authorization_card(
            plan, nodes, card, topology_rulings={"workbuddy": "in_session_sdd"}
        )

        entry = self._by_id(refreshed)["alpha"]
        self.assertEqual(entry["topology"], "background")
        self.assertEqual(entry["mode"], "background")
        self.assertEqual(entry["limitations"], tuple(BACKGROUND_MODE_DISCLOSURES))

    def test_integration_review_is_never_ruled_into_the_single_session_topology(self):
        """The closeout node keeps its reviewer-role dispatch under any ruling.

        Removing this pin was tried and measured (2026-10-07).  With the pin
        gone the monitor dispatched ``integration-review`` with
        ``role=developer`` like any other node; the run-level
        ``integration_review_evidence`` package is only ever derived from a
        *reviewer* acceptance claim (``monitor._derive_integration_acceptance``),
        so it stayed empty and the run stalled on ``integration review evidence
        is missing`` instead of reaching ``complete``.  With the pin in place the
        node is dispatched twice -- once through the ruling, once as a reviewer
        -- and the second dispatch is what closes the run out.
        ``tests/test_integration_review_closeout_entry.py`` pins the same
        behaviour end to end.
        """
        node = _business_node("integration-review")

        self.assertEqual(
            dispatch_topology_for_node(node, {"workbuddy": "in_session_sdd"}),
            "dual-visible",
        )
        # A plan that genuinely needs the two-visible-session shape states it in
        # the node contract, which still wins over the ruling.
        self.assertEqual(
            dispatch_topology_for_node(
                _business_node("integration-review", dispatch_topology="dual-visible"),
                {"workbuddy": "in_session_sdd"},
            ),
            "dual-visible",
        )

    def test_integration_review_rejects_developer_only_persisted_topology(self):
        node = _business_node("integration-review", dispatch_topology="visible-sdd")
        with self.assertRaisesRegex(ValueError, "integration.*reviewer"):
            dispatch_topology_for_node(node, {"workbuddy": "in_session_sdd"})

    def test_integration_review_keeps_explicit_background_topology(self):
        node = _business_node("integration-review", dispatch_topology="background")
        self.assertEqual(dispatch_topology_for_node(node), "background")

    def test_persisted_contract_ruling_wins_over_the_live_ruling(self):
        node = _business_node("alpha", dispatch_topology="dual-visible")

        self.assertEqual(
            dispatch_topology_for_node(node, {"workbuddy": "in_session_sdd"}),
            "dual-visible",
        )

    def test_unknown_ruling_never_upgrades_to_visible_sdd(self):
        node = _business_node("alpha")

        for ruling in (None, "", "unknown", "visible", "dual-visible"):
            with self.subTest(ruling=ruling):
                self.assertEqual(
                    dispatch_topology_for_node(node, {"workbuddy": ruling}),
                    "dual-visible",
                )

    def test_card_ruling_matches_the_dispatcher_ruling(self):
        cases = (
            ("in_session_sdd", "visible-sdd"),
            ("dual-visible", "dual-visible"),
            ("unknown", "dual-visible"),
            ("", "dual-visible"),
        )
        for ruling, expected in cases:
            with self.subTest(ruling=ruling):
                nodes = [_business_node("alpha"), _business_node("beta")]
                plan, card = self._card(nodes)
                rulings = {"workbuddy": ruling}
                refreshed = refresh_authorization_card(
                    plan, nodes, card, topology_rulings=rulings
                )
                dispatcher = object.__new__(Monitor)
                dispatcher._topology_rulings = dict(rulings)

                entries = self._by_id(refreshed)
                for node in nodes:
                    self.assertEqual(
                        entries[node.id]["topology"],
                        dispatcher._node_dispatch_topology(node),
                        "card and dispatcher disagree for " + node.id,
                    )
                self.assertEqual(entries["alpha"]["topology"], expected)

    def test_dispatcher_and_card_agree_on_the_integration_review_closeout_node(self):
        node = _business_node("integration-review")
        rulings = {"workbuddy": "in_session_sdd"}
        dispatcher = object.__new__(Monitor)
        dispatcher._topology_rulings = dict(rulings)

        self.assertEqual(
            dispatcher._node_dispatch_topology(node),
            dispatch_topology_for_node(node, rulings),
        )


class ReauthorizeComplexProjectionTests(unittest.TestCase):
    """``(a)`` and ``(d)`` — observed on a real complex run."""

    def setUp(self):
        self.root = publish_complex_probe(self, dispatch_mode="visible")
        self.paths = ProjectPaths(self.root)
        authorized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )
        assert authorized.payload.get("status") == "ok", authorized.payload
        self.directory, self.plan, self.nodes, self.card = _load_plan(
            self.paths, "probe-plan"
        )
        self.attestation = json.loads(
            (self.directory / "engine-attestation.json").read_text(encoding="utf-8")
        )
        # Park the only business node at the brief gate: that is how a real run
        # reaches reauthorization with `developer_generation == 0` and an empty
        # recorded ready set, which is the state the transition then moves.
        self._apply_brief_gate(self.nodes)
        record = authorize(self._recard(self.plan, self.nodes), "AUTHORIZE")
        self.snapshot = Monitor(self.paths, self.plan, self.nodes).start(
            record, FakeRunner()
        )
        self.assertEqual(self.snapshot.nodes["probe-node-a"]["status"], "brief_pending")
        self.assertEqual(self.snapshot.nodes["probe-node-a"]["developer_generation"], 0)
        self.assertEqual(list(self.snapshot.ready_set), [])
        # The CLI flips plan.json to `authorized` after a successful monitor
        # run; a refresh is only reachable on such a plan.
        published = json.loads((self.directory / "plan.json").read_text(encoding="utf-8"))
        published["status"] = "authorized"
        (self.directory / "plan.json").write_text(
            json.dumps(published), encoding="utf-8"
        )

    @staticmethod
    def _apply_brief_gate(nodes):
        for node in nodes:
            if node.id == "probe-node-a":
                node.contract["brief_required"] = True
                # Deliberately missing `base_sha`: the gate holds the node
                # before any writer lease or provider start.
                node.contract["implementation_brief"] = {
                    "issue_id": node.id,
                    "goal": "goal",
                    "non_goals": [],
                    "owned_paths": [node.id + ".py"],
                    "read_paths": [],
                    "call_chain": [node.id + ".py:missing"],
                    "invariants": [
                        {
                            "id": "I1",
                            "entrypoint": node.id + ".py:missing",
                            "positive_case": "ok",
                            "negative_case": "bad",
                            "test_command": "python -m unittest",
                        }
                    ],
                    "plan_revision": 1,
                    "execution_epoch": 0,
                    "evidence_ref": "brief.json",
                }

    def _recard(self, plan, nodes):
        """Rebuild the authorized card for a mutated node set.

        The on-disk engine attestation is carried through explicitly: a complex
        plan still in ``confirmed_pending_authorization`` refuses a card
        without it.
        """
        return build_authorization_card(
            plan,
            nodes,
            AgentCapabilities(self.card.agent_id, False, False, False, False, False, "guide"),
            active_pair_limit=self.card.active_pair_limit,
            allowed_actions=self.card.allowed_actions,
            remote_git_actions=self.card.remote_git_actions,
            required_workflow=self.card.required_workflow,
            skipped_nodes=self.card.skipped_nodes,
            integration_node_id=self.card.integration_node_id,
            integration_review_scope=self.card.integration_review_scope,
            workflow=load_live_workflow(self.paths, plan.plan_id),
            execution_engine=self.card.execution_engine,
            engine_mode=self.card.engine_mode,
            engine_evidence_ref=self.card.engine_evidence_ref,
            engine_attestation=self.attestation,
            explicit_execution_mode_override=self.card.explicit_execution_mode_override,
            workers=self.card.workers,
        )

    def _rebound(self):
        """Return ``(monitor, record)`` after a contract change forces a re-pin."""
        _, plan, nodes, _card = _load_plan(self.paths, "probe-plan")
        self._apply_brief_gate(nodes)
        for node in nodes:
            if node.id == "probe-node-a":
                node.contract["acceptance_example"] = "verified_fact: README 含两行文本"
        return Monitor(self.paths, plan, nodes), authorize(
            self._recard(plan, nodes), "AUTHORIZE"
        )

    def test_reauthorize_dispatches_after_refreshing_the_stale_ready_set(self):
        monitor, record = self._rebound()
        self.assertEqual(list(self.snapshot.ready_set), [])

        rebound = monitor.reauthorize(
            self.snapshot.run_id, record, FakeRunner(), "executable_contract_changed"
        )

        self.assertEqual(rebound.authorization_digest, record.digest)
        # The transition moved the ready set from empty to the reset node; the
        # projection must have been re-recorded for the validator to accept it.
        names = [event["event"] for event in load_events(self.paths, rebound.run_id)]
        transition = names.index("authorization_reauthorized")
        self.assertIn("execution_topology_observed", names[transition + 1 :])
        # A follow-up tick re-runs the same validator against the persisted
        # snapshot, so a stale projection cannot survive here either.
        monitor.tick(rebound.run_id, FakeRunner())

    def test_without_the_projection_refresh_reauthorize_fails_closed(self):
        """Negative control: removing the refresh reproduces the original block."""
        monitor, record = self._rebound()
        original = Monitor._record_topology_projection

        def only_other_entries(inner_self, snapshot, entry):
            if entry == "monitor.reauthorize":
                return
            return original(inner_self, snapshot, entry)

        with patch.object(Monitor, "_record_topology_projection", only_other_entries):
            with self.assertRaisesRegex(
                PermissionError, "ready-set drift"
            ):
                monitor.reauthorize(
                    self.snapshot.run_id,
                    record,
                    FakeRunner(),
                    "executable_contract_changed",
                )

    def test_reauthorize_repins_an_edited_prd_and_resume_recovers(self):
        prd = self.root / ".vibe" / "plans" / "probe-plan" / "prd.md"
        prd.write_text(
            prd.read_text(encoding="utf-8") + "\n\n新增一段说明。\n", encoding="utf-8"
        )
        new_digest = hashlib.sha256(prd.read_bytes()).hexdigest()
        self.assertNotEqual(new_digest, self.snapshot.prd_digest)

        # The legal re-materialization entry has to run first: with stale
        # workflow evidence `resume` refuses before it reaches lineage at all.
        rematerialized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )
        self.assertEqual(rematerialized.payload.get("status"), "ok")
        with self.assertRaisesRegex(PermissionError, "PRD/Spec source drift"):
            Monitor(self.paths, self.plan, self.nodes).resume(
                self.snapshot.run_id, FakeRunner()
            )

        monitor, record = self._rebound()
        rebound = monitor.reauthorize(
            self.snapshot.run_id, record, FakeRunner(), "executable_contract_changed"
        )

        self.assertEqual(rebound.prd_digest, new_digest)
        self.assertEqual(rebound.spec_digest, new_digest)
        transition = next(
            event
            for event in load_events(self.paths, rebound.run_id)
            if event["event"] == "authorization_reauthorized"
        )
        self.assertEqual(
            transition["data"]["previous_prd_digest"], self.snapshot.prd_digest
        )
        self.assertEqual(transition["data"]["prd_digest"], new_digest)
        # The durable snapshot, not just the in-memory one, carries the re-pin.
        self.assertEqual(
            load_snapshot(self.paths, rebound.run_id).prd_digest, new_digest
        )

        monitor.resume(rebound.run_id, FakeRunner())


class FailedReauthorizationCardPublicationTests(unittest.TestCase):
    """``(b)`` — the card may only be published once reauthorize succeeded."""

    def setUp(self):
        self.root = publish_complex_probe(self, dispatch_mode="visible")
        self.paths = ProjectPaths(self.root)
        authorized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )
        assert authorized.payload.get("status") == "ok", authorized.payload
        self.directory, plan, nodes, card = _load_plan(self.paths, "probe-plan")
        record = authorize(card, "AUTHORIZE")
        self.snapshot = Monitor(self.paths, plan, nodes).start(record, FakeRunner())
        # Mirror the state a successful monitor run leaves behind, so the
        # `elif _current_run_path(directory).exists()` branch is selected.
        published = json.loads((self.directory / "plan.json").read_text(encoding="utf-8"))
        published["status"] = "authorized"
        (self.directory / "plan.json").write_text(
            json.dumps(published), encoding="utf-8"
        )
        (self.directory / "current-run.json").write_text(
            json.dumps({"run_id": self.snapshot.run_id}), encoding="utf-8"
        )
        # A delivered contract correction, so the refreshed card digest moves
        # and a premature write would be observable.
        nodes_path = self.directory / "nodes.json"
        payload = json.loads(nodes_path.read_text(encoding="utf-8"))
        target = payload["nodes"][0] if isinstance(payload, dict) else payload[0]
        target["contract"]["acceptance_example"] = "verified_fact: README 含两行文本"
        nodes_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        self.card_path = self.directory / "authorization-card.json"

    def test_failed_reauthorization_leaves_the_published_card_untouched(self):
        before = self.card_path.read_bytes()
        # Non-vacuousness: the refresh this run would have published really is
        # a different card, so leaving the file alone is a real assertion.
        _, plan, nodes, card = _load_plan(self.paths, "probe-plan")
        refreshed = refresh_authorization_card(
            plan, nodes, card, workflow=load_live_workflow(self.paths, plan.plan_id)
        )
        self.assertNotEqual(refreshed.digest, card.digest)

        def explode(*_args, **_kwargs):
            raise RuntimeError("reauthorize refused by test")

        with patch.object(Monitor, "reauthorize", explode):
            result = run_cli(
                ["monitor", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
                self.root,
            )

        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertEqual(self.card_path.read_bytes(), before)


class EngineEvidenceStagingTests(unittest.TestCase):
    """The evidence a reauthorization names must be on disk before it runs.

    ``Monitor._require_record`` re-reads ``engine-attestation.json``, while the
    refreshed card is deliberately published only after ``reauthorize``
    succeeds.  Those two orderings cannot both be "after", so the evidence is
    staged for the duration of the call and put back when it raises.  A
    reauthorization that fails must leave the plan exactly as it found it.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vg-staged-attestation-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = self.root / "engine-attestation.json"

    def test_a_raise_restores_the_previous_bytes(self):
        original = b'{"previous": true}'
        self.path.write_bytes(original)

        with self.assertRaises(RuntimeError):
            with _staged_engine_attestation(self.path, {"fresh": True}):
                self.assertEqual(
                    json.loads(self.path.read_text(encoding="utf-8")), {"fresh": True}
                )
                raise RuntimeError("reauthorize refused by test")

        self.assertEqual(self.path.read_bytes(), original)

    def test_a_raise_removes_a_file_that_did_not_exist_before(self):
        with self.assertRaises(RuntimeError):
            with _staged_engine_attestation(self.path, {"fresh": True}):
                raise RuntimeError("reauthorize refused by test")

        self.assertFalse(self.path.exists())

    def test_a_success_leaves_the_staged_evidence_in_place(self):
        self.path.write_bytes(b'{"previous": true}')

        with _staged_engine_attestation(self.path, {"fresh": True}) as staged:
            self.assertEqual(staged, {"fresh": True})

        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")), {"fresh": True}
        )


class _StubCard:
    """Stand-in carrying only the serialization shape ``_card_field_diff`` reads."""

    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return self._payload


class CardDiffRepresentationTests(unittest.TestCase):
    """A diff between a loaded card and a derived one must be a real diff.

    ``AuthorizationCard.to_dict()`` hands back tuples; the same card read from
    ``authorization-card.json`` carries lists.  Comparing them raw reported
    ``integration_review_scope``, ``required_workflow`` and ``skipped_nodes``
    as changed on every preview and buried the fields a reviewer has to read.
    """

    def setUp(self):
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")

    def _card(self):
        nodes = [_business_node("alpha")]
        plan = Plan("plan-1", 1, "docs/prd.md", ["alpha"], "draft")
        return build_authorization_card(plan, nodes, self.capabilities)

    def test_a_list_and_a_tuple_representation_of_one_card_are_not_a_diff(self):
        derived = self._card().to_dict()
        reloaded = json.loads(json.dumps(derived, ensure_ascii=False))

        self.assertEqual(
            _card_field_diff(_StubCard(reloaded), _StubCard(derived)), {}
        )

    def test_a_real_change_is_still_reported(self):
        card = self._card()
        other = build_authorization_card(
            Plan("plan-1", 2, "docs/prd.md", ["alpha"], "draft"),
            [_business_node("alpha")],
            self.capabilities,
        )

        changed = _card_field_diff(card, other)

        self.assertIn("plan_version", changed)
        self.assertNotIn("required_workflow", changed)


class RefreshNamesFreshEngineEvidenceTests(unittest.TestCase):
    """``refresh_authorization_card`` must be able to re-sign new evidence.

    Run on the published probe plan rather than a hand-built card: a complex
    plan refuses a card that does not carry the complete required workflow and
    the plan's own integration contract, so a hand-built stand-in would test
    the stand-in.
    """

    def setUp(self):
        self.root = publish_complex_probe(self, dispatch_mode="visible")
        self.paths = ProjectPaths(self.root)
        authorized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )
        assert authorized.payload.get("status") == "ok", authorized.payload
        _directory, self.plan, self.nodes, self.card = _load_plan(self.paths, "probe-plan")
        # A refresh that names no new evidence is only reachable once the plan
        # is past publication confirmation; before that the card build refuses
        # a complex plan without a verified attestation.
        self.plan.status = "authorized"

    def _attestation(self, now):
        return create_engine_attestation(
            self.plan.plan_id, self.plan.version, "vibeguide_monitor", "dag",
            self.card.agent_id, {self.card.agent_id + ".subprocess": True}, "test", now,
        )

    @staticmethod
    def _fresh_timestamp():
        """A timestamp that is fresh whenever the suite runs, not just today.

        ``validate_engine_attestation`` refuses evidence older than 72 hours, so a
        literal such as ``2026-10-07T00:00:00Z`` makes these tests expire at the
        end of the window: they passed on the day they were written and failed from
        the following day on, which is indistinguishable from a real regression
        until you notice the clock.
        """
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _refresh(self, **kwargs):
        # A complex plan refuses a refresh without the workflow evidence the
        # published card preserved.
        return refresh_authorization_card(
            self.plan,
            self.nodes,
            self.card,
            workflow=load_live_workflow(self.paths, self.plan.plan_id),
            **kwargs
        )

    def test_without_new_evidence_the_previous_reference_is_carried(self):
        refreshed = self._refresh()

        self.assertEqual(
            refreshed.engine_evidence_ref, self.card.engine_evidence_ref
        )

    def test_new_evidence_replaces_the_carried_reference(self):
        fresh = self._attestation(self._fresh_timestamp())
        self.assertNotEqual(fresh["evidence_ref"], self.card.engine_evidence_ref)

        refreshed = self._refresh(engine_attestation=fresh)

        self.assertEqual(refreshed.engine_evidence_ref, fresh["evidence_ref"])
        self.assertEqual(refreshed.engine_authorization_digest, refreshed.digest)

    def test_the_carried_reference_is_not_re_passed_beside_new_evidence(self):
        """Both at once is a contradiction, and must not be silently resolved.

        ``build_authorization_card`` refuses a reference that disagrees with
        the attestation it was handed, so the refresh clears the carried
        reference whenever it has fresh evidence to name instead.  Passing
        both is what the refresh must not do.
        """
        fresh = self._attestation(self._fresh_timestamp())

        with self.assertRaisesRegex(ValueError, "does not match attestation"):
            build_authorization_card(
                self.plan, self.nodes, self._capabilities(),
                engine_evidence_ref=self.card.engine_evidence_ref,
                engine_attestation=fresh,
                required_workflow=self.card.required_workflow,
                integration_node_id=self.card.integration_node_id,
                integration_review_scope=self.card.integration_review_scope,
                workflow=load_live_workflow(self.paths, self.plan.plan_id),
                execution_engine=self.card.execution_engine,
                engine_mode=self.card.engine_mode,
                active_pair_limit=self.card.active_pair_limit,
                allowed_actions=self.card.allowed_actions,
                remote_git_actions=self.card.remote_git_actions,
            )

    def _capabilities(self):
        return AgentCapabilities(
            self.card.agent_id, False, False, False, False, False, "guide"
        )


class ExpiredEngineEvidenceReauthorizationTests(unittest.TestCase):
    """A plan older than the evidence window must still be reauthorizable.

    ``validate_engine_attestation`` refuses evidence older than 72 hours, and the
    only writer of ``engine-attestation.json`` is publication, which refuses to
    run for a plan that already exists.  A plan published three days ago therefore
    had no supported way back: the refreshed card kept naming the expired
    reference and ``Monitor._require_record`` blocked before any dispatch was
    considered.

    Time is what ages the evidence, so time is what the test moves:
    ``_aged_evidence`` makes the validator see the already-published
    attestation as a day past its window, and nothing else.
    """

    def setUp(self):
        self.root = publish_complex_probe(self, dispatch_mode="visible")
        self.paths = ProjectPaths(self.root)
        authorized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )
        assert authorized.payload.get("status") == "ok", authorized.payload
        self.directory, self.plan, nodes, card = _load_plan(self.paths, "probe-plan")
        self.attestation_path = self.directory / "engine-attestation.json"
        self.published = json.loads(self.attestation_path.read_text(encoding="utf-8"))
        self.card_path = self.directory / "authorization-card.json"
        self.card_before = json.loads(self.card_path.read_text(encoding="utf-8"))
        self.snapshot = Monitor(self.paths, self.plan, nodes).start(
            authorize(card, "AUTHORIZE"), FakeRunner()
        )
        # A delivered contract correction, so the refreshed card really is a
        # different card and the assertions below are not vacuous.  It has to
        # land after ``start``: the run verifies the published workflow
        # evidence against ``nodes.json`` and would refuse a node set that no
        # longer matches what was recorded.
        self._edit_a_node_contract()
        # Mirror the state a successful monitor run leaves behind, so the
        # `elif _current_run_path(directory).exists()` branch is selected.
        published = json.loads((self.directory / "plan.json").read_text(encoding="utf-8"))
        published["status"] = "authorized"
        (self.directory / "plan.json").write_text(json.dumps(published), encoding="utf-8")
        (self.directory / "current-run.json").write_text(
            json.dumps({"run_id": self.snapshot.run_id}), encoding="utf-8"
        )

    def _edit_a_node_contract(self):
        nodes_path = self.directory / "nodes.json"
        payload = json.loads(nodes_path.read_text(encoding="utf-8"))
        target = payload["nodes"][0] if isinstance(payload, dict) else payload[0]
        target["contract"]["acceptance_example"] = "verified_fact: README 含两行文本"
        nodes_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _aged_evidence(self):
        real = monitor_module.validate_engine_attestation
        beyond = (datetime.now(timezone.utc) + timedelta(days=4)).isoformat().replace(
            "+00:00", "Z"
        )

        def validate(candidate, plan_id, plan_revision, **kwargs):
            if candidate.get("digest") == self.published["digest"]:
                kwargs["now"] = beyond
            return real(candidate, plan_id, plan_revision, **kwargs)

        return patch.object(monitor_module, "validate_engine_attestation", validate)

    def _reauthorize(self):
        return run_cli(
            ["monitor", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )

    def test_the_published_evidence_is_what_expires(self):
        """Non-vacuousness: the fixture really is past the window."""
        with self._aged_evidence():
            with self.assertRaisesRegex(ValueError, "expired"):
                monitor_module.validate_engine_attestation(
                    self.published, "probe-plan", self.plan.version
                )

    def test_reauthorization_replaces_the_expired_evidence(self):
        with self._aged_evidence():
            self._reauthorize()

        # The run's own event log is the signal: a refused reauthorization
        # leaves no transition behind, whatever status the snapshot reports.
        names = [event["event"] for event in load_events(self.paths, self.snapshot.run_id)]
        self.assertIn("authorization_reauthorized", names)
        refreshed = json.loads(self.attestation_path.read_text(encoding="utf-8"))
        self.assertNotEqual(refreshed["digest"], self.published["digest"])
        # The replacement is real evidence, not a re-typed copy: it validates
        # against the plan binding with the clock left alone.
        validate_engine_attestation(refreshed, "probe-plan", self.plan.version)
        # The card names the evidence that is actually on disk.
        card = json.loads(self.card_path.read_text(encoding="utf-8"))
        self.assertEqual(card["engine_evidence_ref"], refreshed["evidence_ref"])
        self.assertNotEqual(card["digest"], self.card_before["digest"])

    def test_carrying_the_expired_reference_forward_still_fails_closed(self):
        """Negative control: the pre-fix behaviour is still a refusal.

        Returning the already-published attestation is exactly what the old
        path did by carrying ``previous.engine_evidence_ref`` through, so this
        is the block the fix removes -- not a block it merely renames.
        """
        before = self.card_path.read_bytes()
        with self._aged_evidence():
            with patch.object(
                cli_module,
                "_observed_engine_attestation",
                lambda _paths, _plan: dict(self.published),
            ):
                result = self._reauthorize()

        self.assertIn("execution_engine_unverified", result.payload.get("reason", ""))
        self.assertEqual(self.card_path.read_bytes(), before)
        names = [event["event"] for event in load_events(self.paths, self.snapshot.run_id)]
        self.assertNotIn("authorization_reauthorized", names)

    def test_reauthorization_refreshes_workflow_before_public_resume(self):
        state_path = self.paths.vibe / "state.json"
        before = json.loads(state_path.read_text(encoding="utf-8"))
        before["task_workflow"]["other-plan"] = {"preserved": True}
        state_path.write_text(json.dumps(before), encoding="utf-8")
        with self._aged_evidence():
            self._reauthorize()

        card = json.loads(self.card_path.read_text(encoding="utf-8"))
        workflow = load_live_workflow(self.paths, "probe-plan")
        self.assertEqual(
            workflow["node_records"]["plan_confirmation"]["output"]["authorization_digest"],
            card["digest"],
        )
        after = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(after["task_workflow"]["other-plan"], {"preserved": True})
        for key in set(before) - {"task_workflow"}:
            self.assertEqual(after[key], before[key])
        resumed = run_cli(
            ["resume", "--plan", "probe-plan", "--run-id", self.snapshot.run_id, "--json"],
            self.root,
        )
        self.assertNotIn("workflow_evidence_stale", resumed.payload.get("reason", ""))
        self.assertNotEqual(resumed.payload.get("status"), "blocked_design")
        current = load_snapshot(self.paths, self.snapshot.run_id)
        self.assertEqual(current.run_id, self.snapshot.run_id)
        self.assertEqual(current.authorization_digest, card["digest"])

    def test_workflow_refresh_failure_retains_invalidation_and_can_retry(self):
        invalidation = self.directory / "authorization-invalidated.json"
        invalidation.write_text(json.dumps({"reason": "contract correction"}), encoding="utf-8")
        before = (self.paths.vibe / "state.json").read_bytes()
        with patch.object(
            cli_module, "materialize_workflow_evidence",
            side_effect=OSError("workflow publication refused by test"),
        ):
            result = self._reauthorize()
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertTrue(invalidation.exists())
        self.assertEqual((self.paths.vibe / "state.json").read_bytes(), before)
        self._reauthorize()
        self.assertFalse(invalidation.exists())
        current = load_snapshot(self.paths, self.snapshot.run_id)
        self.assertEqual(current.run_id, self.snapshot.run_id)
        resumed = run_cli(
            ["resume", "--plan", "probe-plan", "--run-id", self.snapshot.run_id, "--json"],
            self.root,
        )
        self.assertNotIn("workflow_evidence_stale", resumed.payload.get("reason", ""))

    def test_a_plan_without_an_engine_binding_stages_nothing(self):
        """Non-complex plans must not acquire evidence they cannot carry.

        The predicate is stubbed so the real call site runs with
        ``engine_attestation=None``; the predicate itself is covered for every
        band in ``EngineEvidenceScopeTests``.
        """
        before = self.attestation_path.read_bytes()

        with patch.object(cli_module, "_engine_evidence_required", lambda _plan: False):
            self._reauthorize()

        names = [event["event"] for event in load_events(self.paths, self.snapshot.run_id)]
        self.assertIn("authorization_reauthorized", names)
        self.assertEqual(self.attestation_path.read_bytes(), before)

    def test_a_failed_card_publication_restores_the_evidence(self):
        """The staged window must cover the card write, not stop before it.

        With the card published after the window closed, a failing write left
        freshly observed evidence on disk beside a card still naming the old
        reference -- the exact pairing the staging exists to prevent.
        """
        before_evidence = self.attestation_path.read_bytes()
        before_card = self.card_path.read_bytes()
        real = cli_module._atomic_json

        def explode_on_the_card(path, payload):
            if Path(path).name == "authorization-card.json":
                raise OSError("card publication refused by test")
            return real(path, payload)

        with patch.object(cli_module, "_atomic_json", explode_on_the_card):
            result = self._reauthorize()

        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertEqual(self.attestation_path.read_bytes(), before_evidence)
        self.assertEqual(self.card_path.read_bytes(), before_card)

    def test_a_refused_reauthorization_leaves_the_published_card_alone(self):
        before = self.card_path.read_bytes()

        def explode(*_args, **_kwargs):
            raise RuntimeError("reauthorize refused by test")

        with self._aged_evidence():
            with patch.object(Monitor, "reauthorize", explode):
                result = self._reauthorize()

        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertEqual(self.card_path.read_bytes(), before)
        names = [event["event"] for event in load_events(self.paths, self.snapshot.run_id)]
        self.assertNotIn("authorization_reauthorized", names)


class EngineEvidenceScopeTests(unittest.TestCase):
    """Only a complex plan carries an engine binding, so only it may be staged.

    Observing evidence for a simple or light plan would hard-require a
    capability store that non-complex publication never reads -- turning a
    reauthorization that used to work into ``retry_pending`` -- and would leave
    behind an ``engine-attestation.json`` the plan is not allowed to carry.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vg-engine-scope-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = self.root / "engine-attestation.json"

    @staticmethod
    def _plan(band):
        return Plan("p", 1, "prd.md", [], "draft", complexity_band=band)

    def test_only_a_complex_plan_requires_engine_evidence(self):
        for band in ("simple", "light_plan", ""):
            with self.subTest(band=band):
                self.assertFalse(_engine_evidence_required(self._plan(band)))
        self.assertTrue(_engine_evidence_required(self._plan("complex")))

    def test_staging_none_creates_nothing(self):
        with _staged_engine_attestation(self.path, None) as staged:
            self.assertIsNone(staged)
            self.assertFalse(self.path.exists())

        self.assertFalse(self.path.exists())

    def test_staging_none_leaves_an_existing_file_untouched(self):
        original = b'{"previous": true}'
        self.path.write_bytes(original)

        with _staged_engine_attestation(self.path, None) as staged:
            self.assertIsNone(staged)

        self.assertEqual(self.path.read_bytes(), original)


class CardPreviewTests(unittest.TestCase):
    """``monitor --preview-card`` is the read-only half of the approval loop.

    Publishing the refreshed card on its own is not a representable state: the
    execution gate binds the on-disk card to the run snapshot's authorized
    digest, so a refreshed card with the previous authorization beside it fails
    closed with ``plan-confirmation.invalid``.  The preview derives the same
    card in memory instead, which is the only way to show a reviewer what a
    reauthorization would sign before it is signed.
    """

    def setUp(self):
        self.root = publish_complex_probe(self, dispatch_mode="visible")
        self.paths = ProjectPaths(self.root)
        authorized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )
        assert authorized.payload.get("status") == "ok", authorized.payload
        self.directory, plan, nodes, card = _load_plan(self.paths, "probe-plan")
        self.snapshot = Monitor(self.paths, plan, nodes).start(
            authorize(card, "AUTHORIZE"), FakeRunner()
        )
        (self.directory / "current-run.json").write_text(
            json.dumps({"run_id": self.snapshot.run_id}), encoding="utf-8"
        )
        # A delivered contract correction, so the preview has something real to
        # report rather than an empty diff.  It has to land after ``start``:
        # the run verifies the published workflow evidence against
        # ``nodes.json`` and would refuse a node set that no longer matches.
        nodes_path = self.directory / "nodes.json"
        payload = json.loads(nodes_path.read_text(encoding="utf-8"))
        target = payload["nodes"][0] if isinstance(payload, dict) else payload[0]
        target["contract"]["acceptance_example"] = "verified_fact: README 含两行文本"
        nodes_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        # The CLI flips plan.json to `authorized` after a successful monitor
        # run; a refresh is only reachable on such a plan.
        published = json.loads((self.directory / "plan.json").read_text(encoding="utf-8"))
        published["status"] = "authorized"
        (self.directory / "plan.json").write_text(json.dumps(published), encoding="utf-8")

    def _fingerprint(self):
        return {
            path.relative_to(self.root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def _preview(self, extra=()):
        return run_cli(
            ["monitor", "--plan", "probe-plan", "--preview-card", "--json", *extra],
            self.root,
        )

    def test_the_preview_writes_nothing(self):
        before = self._fingerprint()

        result = self._preview()

        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        self.assertEqual(self._fingerprint(), before)

    def test_the_preview_needs_no_authorize_token(self):
        """Reading a card must not require the token that signs it."""
        result = self._preview()

        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        self.assertEqual(result.payload["publication"], "same_run_reauthorization")

    def test_the_preview_reports_a_missing_run_instead_of_raising(self):
        """The preview sits outside the monitor command's own handler.

        A dangling ``current-run.json`` used to raise straight out of the CLI:
        the call is made before the ``monitor`` try block, so nothing catches
        it.  Reading a card must never be the thing that crashes the process.
        """
        (self.directory / "current-run.json").write_text(
            json.dumps({"run_id": "run-that-does-not-exist"}), encoding="utf-8"
        )
        before = self._fingerprint()

        result = self._preview()

        self.assertEqual(result.payload.get("status"), "blocked_design", result.payload)
        self.assertTrue(result.payload.get("reason"))
        self.assertEqual(self._fingerprint(), before)

    def test_the_preview_discloses_that_its_digest_will_change(self):
        """A ``--json`` consumer must not read the preview digest as a credential."""
        result = self._preview()

        self.assertFalse(result.payload["preview_card_digest_matches_publication"])
        self.assertNotEqual(
            result.payload["preview_card_digest"],
            json.loads(
                (self.directory / "authorization-card.json").read_text(encoding="utf-8")
            )["digest"],
        )

    def test_the_preview_reports_the_fields_the_reauthorization_will_change(self):
        result = self._preview()

        changed = result.payload["changed_fields"]
        # The reviewer-facing diff is against the card that was approved, so
        # the contract correction shows up rather than only the engine fields.
        self.assertIn("node_contract_digest", changed)
        self.assertIn("digest", changed)
        self.assertEqual(
            changed["engine_evidence_ref"]["previous"],
            json.loads(
                (self.directory / "authorization-card.json").read_text(encoding="utf-8")
            )["engine_evidence_ref"],
        )
        # Representation-only fields must not appear: a tuple on one side and a
        # list on the other is not a change a reviewer has to consider.
        for noisy in ("required_workflow", "skipped_nodes", "integration_review_scope"):
            self.assertNotIn(noisy, changed)
        # The narrower self-check -- a refresh with old evidence against a
        # refresh with new evidence -- must stay clean.
        self.assertEqual(result.payload["engine_refresh_drift"], [])

    def test_the_preview_reports_a_topology_move(self):
        """The fixture rules ``dual-visible``; a passing probe must move it.

        The card-level half of the preview's job: the business node follows the
        new ruling, while the closeout node keeps the reviewer-role dispatch
        that rule 2 of ``dispatch_topology_for_node`` reserves for it -- the run
        cannot close out without it.
        """
        self._probe_in_session_sdd()

        changed = self._preview().payload["changed_fields"]

        self.assertIn("workers", changed)
        self.assertIn("topology_summary", changed)
        workers = changed["workers"]["next"]
        self.assertEqual(
            {entry["node_id"]: entry["topology"] for entry in workers},
            {"probe-node-a": "visible-sdd", "integration-review": "dual-visible"},
        )

    def _probe_in_session_sdd(self):
        store = self.root / ".vibe" / "provider-actions" / "capabilities.json"
        payload = json.loads(store.read_text(encoding="utf-8"))
        payload["facts"]["claude-code.in_session_sdd"] = True
        store.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_an_underivable_self_check_is_unknown_rather_than_clean(self):
        """``unknown`` must never be reported as "no drift found".

        A plan whose lifecycle never advanced past
        ``confirmed_pending_authorization`` has a run but no refresh that names
        the old evidence, so the drift comparison has nothing to compare
        against.  The card is still shown -- that is the point of a read-only
        preview -- and the missing check is disclosed.
        """
        plan_path = self.directory / "plan.json"
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
        payload["status"] = "confirmed_pending_authorization"
        plan_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        result = self._preview()

        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        self.assertIsNone(result.payload["engine_refresh_drift"])
        self.assertTrue(result.payload["engine_refresh_carry_error"])
        self.assertTrue(
            result.payload["card"]["engine_evidence_ref"].startswith(
                "engine-attestation:"
            )
        )

    def test_the_preview_card_is_the_card_a_reauthorization_publishes(self):
        preview = self._preview().payload["card"]
        before = self._fingerprint()

        run_cli(
            ["monitor", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )

        self.assertNotEqual(self._fingerprint(), before)
        names = [event["event"] for event in load_events(self.paths, self.snapshot.run_id)]
        self.assertIn("authorization_reauthorized", names)
        published = json.loads(
            (self.directory / "authorization-card.json").read_text(encoding="utf-8")
        )
        # Everything a reviewer can act on is identical; only the fields that
        # carry the re-observed attestation's timestamp may differ.  Both sides
        # go through the JSON shape so a tuple on one side and a list on the
        # other is not mistaken for a difference.
        self.assertEqual(
            {
                key: _normalize_card_value(value)
                for key, value in preview.items()
                if key not in _VOLATILE_CARD_FIELDS
            },
            {
                key: _normalize_card_value(value)
                for key, value in published.items()
                if key not in _VOLATILE_CARD_FIELDS
            },
        )


class FirstPublicationPreviewTests(unittest.TestCase):
    """Before a run exists, the preview digest *is* the digest that gets signed.

    That is the one case where the preview is a literal approval credential, so
    the flag that says so has to be right in both directions.
    """

    def setUp(self):
        self.root = publish_complex_probe(self, dispatch_mode="visible")
        self.paths = ProjectPaths(self.root)
        authorized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )
        assert authorized.payload.get("status") == "ok", authorized.payload

    def test_the_first_publication_digest_is_the_one_that_gets_signed(self):
        result = run_cli(
            ["monitor", "--plan", "probe-plan", "--preview-card", "--json"], self.root
        )

        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        self.assertEqual(result.payload["publication"], "first")
        self.assertTrue(result.payload["preview_card_digest_matches_publication"])
        on_disk = json.loads(
            (
                self.paths.vibe / "plans" / "probe-plan" / "authorization-card.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(result.payload["preview_card_digest"], on_disk["digest"])


class ObservedAdapterAdmissionTests(unittest.TestCase):
    """The publish/dispatch gate must admit exactly what the ruling admits.

    ``_observed_adapter`` decides "is this platform dispatchable?" from the
    capability report.  It used to require a verified visible lifecycle
    outright, which refused the in-session SDD shape entirely; now it admits
    that shape -- but only for platforms whose matrix row actually rules it,
    so the gate can never be looser than the dispatcher.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vg-observed-adapter-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = ProjectPaths(self.root)
        self.assertEqual(
            run_cli(["init", "--confirm", "--json"], self.root).payload["status"], "ok"
        )

    def write(self, adapter_id, **facts):
        store = self.root / ".vibe" / "provider-actions"
        store.mkdir(parents=True, exist_ok=True)
        (store / "capabilities.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "adapter_id": adapter_id,
                    "facts": facts,
                    "provenance": "test",
                }
            ),
            encoding="utf-8",
        )

    def test_admits_the_in_session_sdd_shape(self):
        self.write("workbuddy", **{"workbuddy.subprocess": True, "workbuddy.in_session_sdd": True})

        result = _observed_adapter(self.paths, "workbuddy")

        self.assertEqual(result.capabilities.mode, "in_session_sdd")
        self.assertTrue(result.capabilities.in_session_sdd)
        self.assertFalse(result.capabilities.visible_automation)

    def test_refuses_a_row_that_never_upgrades(self):
        # Byte-for-byte the same fact shape that workbuddy is admitted on;
        # only grok's matrix row differs (``probe_pass`` stays conservative).
        self.write("grok", **{"grok.subprocess": True, "grok.in_session_sdd": True})

        with self.assertRaises(ValueError) as caught:
            _observed_adapter(self.paths, "grok")

        self.assertIn("dispatch lifecycle", str(caught.exception))

    def test_refuses_a_pass_without_provenance(self):
        store = self.root / ".vibe" / "provider-actions"
        store.mkdir(parents=True, exist_ok=True)
        (store / "capabilities.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "adapter_id": "workbuddy",
                    "facts": {
                        "workbuddy.subprocess": True,
                        "workbuddy.in_session_sdd": True,
                    },
                    "provenance": "",
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(ValueError) as caught:
            _observed_adapter(self.paths, "workbuddy")

        self.assertIn("dispatch lifecycle", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class _PendingForBRunner(FakeRunner):
    """The desktop bridge has not answered node b's create yet."""

    def task_binding(self, contract, worktree, run_id, status):
        from vibe_guide.adapters.task_provider import ProviderPending

        from vibe_guide.task_registry import TaskBinding

        if contract.get("node_id") == "b":
            raise ProviderPending("provider capability bridge is pending")
        return TaskBinding(
            provider="runner", mode="background", issue_id=contract["node_id"],
            role=contract["role"], task_id=contract["task_id"], host=None,
            worktree=str(worktree), branch=str(contract.get("branch", "")),
            status_file="", handoff_file="", run_id=run_id, status=status,
            generation=contract["generation"],
            allowlist=list(contract.get("files", [])),
            capability_contract_digest=contract.get("capability_contract_digest"),
            topology=str(contract.get("topology")),
            limitations=list(contract.get("dispatch_limitations", [])),
        )


class PlatformHandoffUnservedLeaseTests(unittest.TestCase):
    """A just-ready node whose create nobody claimed must survive a handoff."""

    setUp = ScopeOnlyReauthorizationTests.setUp
    tearDown = ScopeOnlyReauthorizationTests.tearDown
    _nodes = staticmethod(ScopeOnlyReauthorizationTests._nodes)
    _handoff_nodes = ScopeOnlyReauthorizationTests._handoff_nodes
    _switch_platform = ScopeOnlyReauthorizationTests._switch_platform
    _handoff = ScopeOnlyReauthorizationTests._handoff

    def _a_accepted_b_unclaimed(self):
        nodes = self._nodes()
        plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")
        record = authorize(
            build_authorization_card(plan, nodes, self.capabilities), "AUTHORIZE"
        )
        runner = _PendingForBRunner(events={
            ("a", "developer"): [("complete", {"evidence": "a"})],
            ("a", "reviewer"): [("accepted", {"evidence": "a"})],
        })
        monitor = Monitor(self.paths, plan, nodes)
        snapshot = monitor.start(record, runner)
        for _ in range(6):
            snapshot = monitor.tick(snapshot.run_id, runner)
        return snapshot

    _run_id = None

    def _b_leases(self):
        return [(path, json.loads(path.read_text()))
                for path in sorted((self.paths.vibe / "leases").glob("*.json"))
                if json.loads(path.read_text())["node_id"] == "b"]

    def _handoff_with(self, snapshot, runner=None):
        self._switch_platform()
        return self._handoff(snapshot, self._handoff_nodes(), runner or FakeRunner())

    def _old_create(self, snapshot):
        from vibe_guide.adapters.task_provider import ProviderActionStore

        with patch("vibe_guide.adapters.task_provider.require_entry"):
            return ProviderActionStore(self.paths).request(
                operation="create", provider="workbuddy", run_id=snapshot.run_id,
                issue_id="b", role="developer", generation=1, native_tool="native",
                request={"authorization_digest": snapshot.authorization_digest},
            )

    def test_fixture_leaves_b_leased_but_unclaimed(self):
        snapshot = self._a_accepted_b_unclaimed()
        self.assertEqual(snapshot.nodes["a"]["status"], "accepted")
        self.assertEqual(len(self._b_leases()), 1)
        self.assertFalse(snapshot.nodes["b"].get("developer_identity"))
        self.assertNotIn("b", snapshot.handles)
        with self.assertRaises(FileNotFoundError):
            load_task_binding(self.paths, "b", "developer", run_id=snapshot.run_id)

    def test_handoff_rebinds_the_unclaimed_lease_and_the_new_platform_claims_b(self):
        snapshot = self._a_accepted_b_unclaimed()
        old_digest = self._b_leases()[0][1]["intent_digest"]
        runner = FakeRunner(events={("b", "developer"): [("complete", {"evidence": "b"})],
                                    ("b", "reviewer"): [("accepted", {"evidence": "b"})]})
        monitor, _record, rebound = self._handoff_with(snapshot, runner)
        self.assertNotEqual(rebound.nodes["b"]["status"], "blocked_unknown")
        for _ in range(6):
            rebound = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(rebound.nodes["b"]["status"], "accepted")
        self.assertEqual(len([c for c in runner.start_calls if c["node_id"] == "b" and c["role"] == "developer"]), 1)
        event = [e for e in load_events(self.paths, snapshot.run_id) if e["event"] == "handoff_lease_rebound"]
        self.assertEqual(len(event), 1)
        data = event[0]["data"]
        self.assertEqual((data["node_id"], data["role"], data["generation"]), ("b", "developer", 1))
        self.assertNotEqual(data["intent_digest"], old_digest)
        self.assertIn("superseded_creates", data)

    def test_rebind_keeps_exactly_one_active_lease(self):
        snapshot = self._a_accepted_b_unclaimed()
        old_path = self._b_leases()[0][0]
        self._handoff_with(snapshot)
        leases = self._b_leases()
        self.assertEqual(len(leases), 1)
        self.assertNotEqual(leases[0][0], old_path)
        self.assertEqual(leases[0][1]["status"], "active")
        self.assertEqual(leases[0][1]["run_id"], snapshot.run_id)

    def test_old_platform_create_is_superseded_and_not_pending(self):
        from vibe_guide.adapters.task_provider import ProviderActionStore

        snapshot = self._a_accepted_b_unclaimed()
        old = self._old_create(snapshot)
        self.assertEqual([a["action_id"] for a in ProviderActionStore(self.paths).pending(snapshot.run_id)],
                         [old["action_id"]])
        self._handoff_with(snapshot)
        self.assertEqual(ProviderActionStore(self.paths).pending(snapshot.run_id), [])
        marker = json.loads((self.paths.vibe / "provider-actions" / "stale" / (old["action_id"] + ".json")).read_text())
        self.assertEqual(marker["reason"], "platform_handoff_superseded")
        self.assertEqual(marker["request_digest"], old["request_digest"])
        # Immutable request stays, and no provider result was forged.
        self.assertTrue((self.paths.vibe / "provider-actions" / "requests" / (old["action_id"] + ".json")).exists())
        self.assertFalse((self.paths.vibe / "provider-actions" / "results" / (old["action_id"] + ".json")).exists())

    def test_a_claimed_create_is_never_superseded(self):
        snapshot = self._a_accepted_b_unclaimed()
        old = self._old_create(snapshot)
        results = self.paths.vibe / "provider-actions" / "results"
        results.mkdir(parents=True, exist_ok=True)
        (results / (old["action_id"] + ".json")).write_text("{}")
        _monitor, _record, rebound = self._handoff_with(snapshot)
        self.assertEqual(rebound.nodes["b"]["status"], "blocked_unknown")
        self.assertFalse((self.paths.vibe / "provider-actions" / "stale" / (old["action_id"] + ".json")).exists())

    def _assert_still_blocked(self, snapshot):
        _monitor, _record, rebound = self._handoff_with(snapshot)
        self.assertEqual(rebound.nodes["b"]["status"], "blocked_unknown")
        self.assertIn("another run", rebound.nodes["b"]["reason"])
        self.assertFalse([e for e in load_events(self.paths, snapshot.run_id)
                          if e["event"] == "handoff_lease_rebound"])

    def _edit_b_lease(self, **changes):
        path, payload = self._b_leases()[0]
        payload.update(changes)
        path.write_text(json.dumps(payload, sort_keys=True))

    def test_a_lease_of_another_run_still_blocks(self):
        snapshot = self._a_accepted_b_unclaimed()
        self._edit_b_lease(run_id="run-someone-else")
        self._assert_still_blocked(snapshot)

    def test_a_lease_of_another_generation_is_never_rebound(self):
        # The acquire rule never treated a different generation as a clash;
        # the rebind must not touch such a lease either.
        snapshot = self._a_accepted_b_unclaimed()
        self._edit_b_lease(generation=7)
        before = self._b_leases()
        self._handoff_with(snapshot)
        self.assertIn(before[0], self._b_leases())
        self.assertFalse([e for e in load_events(self.paths, snapshot.run_id)
                          if e["event"] == "handoff_lease_rebound"])

    def test_a_node_with_a_task_binding_still_blocks(self):
        snapshot = self._a_accepted_b_unclaimed()
        save_task_binding(self.paths, TaskBinding(
            provider="runner", mode="background", issue_id="b", role="developer",
            task_id="developer:b", host=None, worktree=".worktrees/b", branch="branch-b",
            status_file="", handoff_file="", run_id=snapshot.run_id, status="running",
            generation=1, allowlist=["b.py"],
        ))
        self._assert_still_blocked(snapshot)

    def test_a_node_with_a_start_confirmed_event_still_blocks(self):
        snapshot = self._a_accepted_b_unclaimed()
        monitor = Monitor(self.paths, Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft"), self._nodes())
        live = load_snapshot(self.paths, snapshot.run_id)
        monitor._record(live, "start_confirmed", {"run_id": live.run_id, "node_id": "b", "role": "developer"})
        monitor._save_snapshot(live)
        self._assert_still_blocked(live)

    def test_without_a_platform_handoff_the_lease_is_never_rebound(self):
        snapshot = self._a_accepted_b_unclaimed()
        monitor = Monitor(self.paths, Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft"), self._nodes())
        live = load_snapshot(self.paths, snapshot.run_id)
        self.assertFalse(monitor._adopt_unserved_handoff_lease(live, "b", "developer", 1, "f" * 64))
        self.assertEqual(self._b_leases()[0][1]["intent_digest"] != "f" * 64, True)

    # -- review round 2: claim marks, handoff boundary, crash order ---------

    def _mark(self, kind, snapshot, old):
        root = self.paths.vibe / "provider-actions"
        run_id = snapshot.run_id
        if kind == "lock":
            target, text = root / "locks" / (old["action_id"] + ".lock"), ""
        elif kind == "observation":
            target, text = root / "observations" / (old["action_id"] + "-x.json"), "{}"
        elif kind == "handle":
            target = root / "handles" / "bridge-x.json"
            text = json.dumps({"run_id": run_id, "node_id": "b"})
        elif kind == "session":
            target = root / "claude-bg-sessions" / "claude-bg-b-x.json"
            text = json.dumps({"claims": {"run_id": run_id, "node_id": "b"}})
        elif kind == "delivery":
            target, text = root / "deliveries" / (run_id + "-b-developer-g1.json"), "{}"
        elif kind == "prompt":
            target, text = root / "prompts" / (run_id + "-b-developer-g1.md"), "x"
        else:
            raise AssertionError(kind)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def _assert_marked_stays_blocked(self, kind):
        from vibe_guide.adapters.task_provider import ProviderActionStore

        snapshot = self._a_accepted_b_unclaimed()
        old = self._old_create(snapshot)
        self._mark(kind, snapshot, old)
        self._assert_still_blocked(snapshot)
        self.assertFalse((self.paths.vibe / "provider-actions" / "stale" / (old["action_id"] + ".json")).exists())
        self.assertEqual([a["action_id"] for a in ProviderActionStore(self.paths).pending(snapshot.run_id)],
                         [old["action_id"]])

    def test_a_completion_lock_marks_the_create_as_claimed(self):
        self._assert_marked_stays_blocked("lock")

    def test_a_conflicting_observation_marks_the_create_as_claimed(self):
        self._assert_marked_stays_blocked("observation")

    def test_a_bridge_handle_marks_the_node_as_claimed(self):
        self._assert_marked_stays_blocked("handle")

    def test_a_claude_bg_session_record_marks_the_node_as_claimed(self):
        self._assert_marked_stays_blocked("session")

    def test_a_delivery_file_marks_the_node_as_claimed(self):
        self._assert_marked_stays_blocked("delivery")

    def test_a_prompt_file_marks_the_node_as_claimed(self):
        self._assert_marked_stays_blocked("prompt")

    def test_marks_of_another_node_do_not_count(self):
        from vibe_guide.adapters.task_provider import ProviderActionStore

        snapshot = self._a_accepted_b_unclaimed()
        root = self.paths.vibe / "provider-actions"
        (root / "handles").mkdir(parents=True)
        (root / "handles" / "bridge-a.json").write_text(
            json.dumps({"run_id": snapshot.run_id, "node_id": "a"}))
        (root / "deliveries").mkdir(parents=True)
        (root / "deliveries" / (snapshot.run_id + "-b-extra-developer-g1.json")).write_text("{}")
        self.assertIsNone(ProviderActionStore(self.paths).claim_evidence(snapshot.run_id, "b"))

    def test_an_unreadable_mark_counts_as_claimed(self):
        from vibe_guide.adapters.task_provider import ProviderActionStore

        snapshot = self._a_accepted_b_unclaimed()
        handles = self.paths.vibe / "provider-actions" / "handles"
        handles.mkdir(parents=True)
        (handles / "bridge-bad.json").write_text("{not json")
        self.assertEqual(ProviderActionStore(self.paths).claim_evidence(snapshot.run_id, "b"), "unreadable")

    def _new_platform_create(self, auth_digest):
        from vibe_guide.adapters.task_provider import ProviderActionStore

        with patch("vibe_guide.adapters.task_provider.require_entry"):
            return ProviderActionStore(self.paths).request(
                operation="create", provider="cursor", run_id=self._run_id,
                issue_id="b", role="developer", generation=1, native_tool="native",
                request={"authorization_digest": auth_digest},
            )

    def test_a_later_reauthorization_never_hides_the_new_platforms_create(self):
        from vibe_guide.adapters.task_provider import ProviderActionStore

        snapshot = self._a_accepted_b_unclaimed()
        self._run_id = snapshot.run_id
        self._old_create(snapshot)
        pending_runner = _PendingForBRunner()
        self._switch_platform()
        monitor, record, rebound = self._handoff(snapshot, self._handoff_nodes(), pending_runner)
        self.assertEqual(rebound.nodes["b"]["status"], "running")
        fresh = self._new_platform_create(record.digest)
        # An ordinary reauthorization follows the handoff (scope edit).
        nodes = self._handoff_nodes(a_files=["a.py", "tests/test_a.py"])
        plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")
        later = authorize(build_authorization_card(plan, nodes, self.capabilities), "AUTHORIZE")
        self.assertNotEqual(later.digest, record.digest)
        after = Monitor(self.paths, plan, nodes).reauthorize(
            snapshot.run_id, later, pending_runner, "executable_contract_changed")
        pending = [a["action_id"] for a in ProviderActionStore(self.paths).pending(snapshot.run_id)]
        self.assertEqual(pending, [fresh["action_id"]])
        self.assertFalse([e for e in load_events(self.paths, snapshot.run_id)
                          if e["event"] == "handoff_lease_rebound" and e["data"]["intent_digest"] != self._b_leases()[0][1]["intent_digest"]
                          and e["sequence"] > after.event_sequence])

    # -- crash windows: every interruption converges on one create, one lease

    def _crash_at(self, target, attribute, only_event=None):
        snapshot = self._a_accepted_b_unclaimed()
        old = self._old_create(snapshot)
        self._switch_platform()
        nodes = self._handoff_nodes()
        plan = Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft")
        record = authorize(build_authorization_card(plan, nodes, self.capabilities), "AUTHORIZE")
        monitor = Monitor(self.paths, plan, nodes)
        original = getattr(target, attribute)

        def crash(*args, **kwargs):
            if only_event is not None and (not args or only_event not in args):
                return original(*args, **kwargs)
            raise RuntimeError("injected crash")

        with patch.object(target, attribute, crash):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                monitor.reauthorize(snapshot.run_id, record, FakeRunner(), "platform_handoff")
        # The operator re-runs the same handoff; the run must converge.
        recovered = Monitor(self.paths, plan, nodes).reauthorize(
            snapshot.run_id, record, FakeRunner(), "platform_handoff")
        self._assert_converged(recovered, old)

    def _assert_converged(self, recovered, old):
        from vibe_guide.adapters.task_provider import ProviderActionStore

        self.assertNotEqual(recovered.nodes["b"]["status"], "blocked_unknown")
        self.assertEqual(ProviderActionStore(self.paths).pending(recovered.run_id), [])
        self.assertTrue((self.paths.vibe / "provider-actions" / "stale" / (old["action_id"] + ".json")).exists())
        leases = self._b_leases()
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases[0][1]["status"], "active")

    def test_a_crash_while_hiding_the_old_create_converges(self):
        from vibe_guide.adapters.task_provider import ProviderActionStore

        self._crash_at(ProviderActionStore, "supersede_unclaimed_creates")

    def test_a_crash_while_moving_the_lease_converges(self):
        self._crash_at(monitor_module, "rebind_writer_lease")

    def test_a_crash_while_recording_the_event_converges(self):
        self._crash_at(Monitor, "_record", only_event="handoff_lease_rebound")

    # -- replay of the evidence event ----------------------------------------

    def _replay_with(self, mutate):
        snapshot = self._a_accepted_b_unclaimed()
        self._switch_platform()
        self._handoff(snapshot, self._handoff_nodes(), _PendingForBRunner())
        replay = load_snapshot(self.paths, snapshot.run_id)
        events = load_events(self.paths, snapshot.run_id)
        target = next(e for e in events if e["event"] == "handoff_lease_rebound")
        replay.event_sequence = target["sequence"] - 1
        monitor = Monitor(self.paths, Plan("plan-1", 1, "docs/prd.md", ["a", "b"], "draft"), self._handoff_nodes())
        mutated = mutate([json.loads(json.dumps(e)) for e in events])
        with patch.object(monitor_module, "load_events", lambda *a, **k: mutated):
            monitor._reconcile_unapplied_events(replay)
        return replay, target

    def test_the_rebound_event_replays_as_evidence_only(self):
        def upto_the_event(events):
            index = next(i for i, e in enumerate(events) if e["event"] == "handoff_lease_rebound")
            return events[: index + 1]

        replay, target = self._replay_with(upto_the_event)
        self.assertEqual(replay.event_sequence, target["sequence"])

    def test_a_rebound_event_without_system_provenance_is_refused(self):
        def mutate(events):
            for e in events:
                if e["event"] == "handoff_lease_rebound":
                    e["provenance"]["role"] = "developer"
            return events

        with self.assertRaisesRegex(ValueError, "handoff lease rebound replay provenance"):
            self._replay_with(mutate)

    def test_a_rebound_event_in_a_run_without_a_handoff_is_refused(self):
        def mutate(events):
            for e in events:
                if e["event"] == "authorization_reauthorized":
                    e["data"]["change_reason"] = "executable_contract_changed"
            return events

        with self.assertRaisesRegex(ValueError, "handoff lease rebound replay provenance"):
            self._replay_with(mutate)

    # -- the helper's own gates, one at a time --------------------------------

    def _post_handoff_probe(self):
        snapshot = self._a_accepted_b_unclaimed()
        self._switch_platform()
        monitor, _record, _rebound = self._handoff(snapshot, self._handoff_nodes(), _PendingForBRunner())
        live = load_snapshot(self.paths, snapshot.run_id)
        digest = self._b_leases()[0][1]["intent_digest"]
        return monitor, live, digest

    def test_the_helper_is_idempotent_once_the_lease_already_moved(self):
        monitor, live, digest = self._post_handoff_probe()
        self.assertTrue(monitor._adopt_unserved_handoff_lease(live, "b", "developer", 1, digest))

    def test_the_helper_refuses_when_the_snapshot_left_the_handoffs_authorization(self):
        # Defence in depth beside "the latest reauthorization must be the
        # handoff": the digest the handoff produced must still be the run's.
        monitor, live, digest = self._post_handoff_probe()
        live.authorization_digest = "d" * 64
        self.assertFalse(monitor._adopt_unserved_handoff_lease(live, "b", "developer", 1, digest))

    def test_the_helper_refuses_when_a_later_reauthorization_followed_the_handoff(self):
        monitor, live, digest = self._post_handoff_probe()
        events = load_events(self.paths, live.run_id)
        later = json.loads(json.dumps(next(
            e for e in reversed(events) if e["event"] == "authorization_reauthorized")))
        later["data"]["change_reason"] = "executable_contract_changed"
        with patch.object(monitor_module, "load_events", lambda *a, **k: events + [later]):
            live.event_sequence = len(events) + 1
            self.assertFalse(monitor._adopt_unserved_handoff_lease(live, "b", "developer", 1, digest))

    def test_the_helper_refuses_a_node_with_a_task_identity(self):
        # Defence in depth: identity is normally set together with a binding.
        monitor, live, digest = self._post_handoff_probe()
        live.nodes["b"]["developer_identity"] = "developer:b"
        self.assertFalse(monitor._adopt_unserved_handoff_lease(live, "b", "developer", 1, digest))

    def test_the_helper_refuses_a_node_with_an_active_task(self):
        monitor, live, digest = self._post_handoff_probe()
        live.nodes["b"]["active_task"] = {"task_id": "developer:b"}
        self.assertFalse(monitor._adopt_unserved_handoff_lease(live, "b", "developer", 1, digest))

    def test_the_helper_refuses_a_node_with_a_live_handle(self):
        monitor, live, digest = self._post_handoff_probe()
        live.handles["b"] = "handle-b"
        self.assertFalse(monitor._adopt_unserved_handoff_lease(live, "b", "developer", 1, digest))


class RebindWriterLeaseTests(unittest.TestCase):
    """state.rebind_writer_lease, as the handoff uses it (active -> new intent)."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vg-rebind-")
        self.addCleanup(self.temporary.cleanup)
        self.paths = ProjectPaths(Path(self.temporary.name))
        self.run_id = "run-" + "1" * 32
        self.other_run = "run-" + "2" * 32

    def _lease(self, intent, run_id=None, role="developer", generation=1, status=None):
        from vibe_guide.state import acquire_writer_lease

        self.assertTrue(acquire_writer_lease(
            self.paths, "b", ".worktrees/b", run_id or self.run_id, intent, role, generation))
        if status:
            for path in (self.paths.vibe / "leases").glob("*.json"):
                payload = json.loads(path.read_text())
                if payload["intent_digest"] == intent:
                    payload["status"] = status
                    path.write_text(json.dumps(payload))

    def _rebind(self, new="n" * 64, run_id=None, role="developer", generation=1, status="active"):
        from vibe_guide.state import rebind_writer_lease

        return rebind_writer_lease(
            self.paths, "b", ".worktrees/b", run_id or self.run_id, role, generation,
            None, new, previous_status=status)

    def _intents(self):
        return sorted(json.loads(p.read_text())["intent_digest"]
                      for p in (self.paths.vibe / "leases").glob("*.json"))

    def test_one_active_lease_moves_to_the_new_intent(self):
        self._lease("o" * 64)
        self.assertTrue(self._rebind())
        self.assertEqual(self._intents(), ["n" * 64])

    def test_a_quarantined_lease_is_not_taken_over_as_active(self):
        self._lease("o" * 64, status="quarantined")
        self.assertFalse(self._rebind())
        self.assertEqual(self._intents(), ["o" * 64])

    def test_two_candidate_old_leases_refuse_the_move(self):
        self._lease("o" * 64)
        first = next((self.paths.vibe / "leases").glob("*.json"))
        twin = json.loads(first.read_text())
        twin["intent_digest"] = "p" * 64
        (self.paths.vibe / "leases" / ("twin-" + first.name)).write_text(json.dumps(twin))
        self.assertFalse(self._rebind())
        self.assertEqual(self._intents(), ["o" * 64, "p" * 64])

    def test_another_run_is_refused(self):
        self._lease("o" * 64)
        self.assertFalse(self._rebind(run_id=self.other_run))
        self.assertEqual(self._intents(), ["o" * 64])

    def test_another_role_is_refused(self):
        self._lease("o" * 64)
        self.assertFalse(self._rebind(role="reviewer"))
        self.assertEqual(self._intents(), ["o" * 64])

    def test_another_generation_is_refused(self):
        self._lease("o" * 64)
        self.assertFalse(self._rebind(generation=2))
        self.assertEqual(self._intents(), ["o" * 64])

    def test_the_quarantine_default_still_moves_only_a_quarantined_lease(self):
        from vibe_guide.state import rebind_writer_lease

        self._lease("o" * 64)
        self.assertFalse(rebind_writer_lease(
            self.paths, "b", ".worktrees/b", self.run_id, "developer", 1, "o" * 64, "n" * 64))
        self.assertEqual(self._intents(), ["o" * 64])


class SupersedeUnclaimedCreatesTests(unittest.TestCase):
    """Only the create the handoff replaced, for exactly this role slot, is hidden."""

    setUp = ScopeOnlyReauthorizationTests.setUp
    tearDown = ScopeOnlyReauthorizationTests.tearDown
    OLD = "a" * 64
    CURRENT = "c" * 64
    RUN = "run-" + "3" * 32

    def _request(self, issue="b", role="developer", generation=1, operation="create", auth=None):
        from vibe_guide.adapters.task_provider import ProviderActionStore

        with patch("vibe_guide.adapters.task_provider.require_entry"):
            return ProviderActionStore(self.paths).request(
                operation=operation, provider="workbuddy", run_id=self.RUN, issue_id=issue,
                role=role, generation=generation, native_tool="native",
                request={"authorization_digest": auth or self.OLD},
            )

    def _supersede(self):
        from vibe_guide.adapters.task_provider import ProviderActionStore

        return ProviderActionStore(self.paths).supersede_unclaimed_creates(
            self.RUN, "b", "developer", 1, self.OLD)

    def test_the_replaced_create_is_hidden(self):
        action = self._request()
        self.assertEqual(self._supersede(), [action["action_id"]])

    def test_a_create_of_the_current_authorization_is_not_hidden(self):
        self._request(auth=self.CURRENT)
        self.assertEqual(self._supersede(), [])

    def test_another_role_is_not_hidden(self):
        self._request(role="reviewer")
        self.assertEqual(self._supersede(), [])

    def test_another_operation_is_not_hidden(self):
        self._request(operation="resume")
        self.assertEqual(self._supersede(), [])

    def test_another_generation_is_not_hidden(self):
        self._request(generation=2)
        self.assertEqual(self._supersede(), [])

    def test_another_issue_is_not_hidden(self):
        self._request(issue="a")
        self.assertEqual(self._supersede(), [])

    def test_a_create_that_already_has_a_result_is_not_hidden(self):
        action = self._request()
        results = self.paths.vibe / "provider-actions" / "results"
        results.mkdir(parents=True, exist_ok=True)
        (results / (action["action_id"] + ".json")).write_text("{}")
        self.assertEqual(self._supersede(), [])

    def test_hiding_twice_is_idempotent(self):
        action = self._request()
        self.assertEqual(self._supersede(), [action["action_id"]])
        self.assertEqual(self._supersede(), [])
