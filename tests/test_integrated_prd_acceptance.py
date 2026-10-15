"""Automated acceptance for the integrated supervisor PRD (2026-10-13), §6.

Covers the items the gap list marks "未核实":

* AC-B5 (#147)  scope-only reauthorization keeps downstream acceptance;
* AC-B6 (#153)  blocked_unknown dispositions, recovery actions, 3-round loop;
* AC-B3 (B1)    new third-party dependency / visible->background downgrade /
                deploy still interrupt the product manager;
* AC-H2 Codex   the Codex topology ruling is identical to the pre-change
                baseline (main 72456a8, the PRD §4 baseline);
* AC-E1 (#128)  entry triage falls back to S1 with a degradation record and
                an evaluation scaffold reports accuracy / ECE / p95.

Every test drives a real entry point with a concrete input and asserts the
behavior the PRD requires.  A failing test is a real gap on this branch; its
assertion message names what is missing.  Nothing is skipped or xfailed.

"Interrupts the product manager" is observed through the public status
mapping: ``state.map_user_status`` returns ``需要你决定`` exactly for
``blocked_design`` / ``blocked_deploy``.  Entry points that refuse a request
before any state change raise ``ScopeDecisionRequired`` (CLI exit 3,
``blocked_design``) or return ``blocked_deploy``.
"""
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.cli import _observed_topology_rulings, run_cli
from vibe_guide.deploy import DeployManifest
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor, ScopeDecisionRequired, VISIBLE_SDD_PROTOCOL_REF
from vibe_guide.paths import ProjectPaths
from vibe_guide.planner import resolve_scope_expansion, route_task
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.session_entry import default_s1_context
from vibe_guide.state import load_events, load_snapshot, map_user_status

from tests.test_monitor import VisibleSddRunner, load_v46_dispatch_fixture


import vibe_guide

REPO_ROOT = Path(vibe_guide.__file__).resolve().parents[1]
PROTOCOLS = REPO_ROOT / "vibe_guide" / "protocols"

PM_INTERRUPT = "需要你决定"

#: prd-guide §7 standard blocked_unknown recovery actions, plus the #153
#: three-round action named in visible-sdd-worker.md step 4.
DOCUMENTED_RECOVERY_ACTIONS = {
    "repair_node",
    "requeue_same_task",
    "reconcile_binding",
    "await_external_decision",
    "change_reviewer_or_split_node",
}

#: PRD §4 names main 72456a8 as the baseline; it is "改动前" for AC-H2.
CODEX_BASELINE_REF = "72456a8"

V42_STATE = {
    "workflow_version": 4,
    "execution_mode": "sdd_first",
    "session_gate": "s0_required",
    "capability_contract_required": True,
}
V2_STATE = {"workflow_version": 2, "session_gate": "s0_required"}
FULL = AgentCapabilities("fake", True, True, True, True, True, "full")


def _worker_profile(node_id, files):
    return {
        "worker": "codex", "model": "test", "reasoning": "normal", "fallbacks": [],
        "selection_basis": {
            "issue_complexity_ref": node_id, "complexity_band": "standard",
            "risk_tags": [], "availability_evidence": "test",
        },
        "writer": "writer", "worktree": ".worktrees/" + node_id,
        "branch": "branch-" + node_id, "allowlist": list(files),
    }


def _node(node_id, files=None, depends_on=(), **contract):
    files = list(files or [node_id + ".py"])
    body = {
        "files": files,
        "worker": "worker-" + node_id,
        "reviewer_worker": "reviewer-" + node_id,
        "worktree": ".worktrees/" + node_id,
        "worker_profile": _worker_profile(node_id, files),
    }
    body.update(contract)
    return DAGNode(node_id, node_id, list(depends_on), [], "g1", body, "ready")


def _scope_claim(files, **overrides):
    claim = {
        "files": list(files),
        "acceptance_examples_unchanged": True,
        "reason": "fixture only; PRD acceptance examples keep the same result",
        "adds_third_party_dependency": False,
        "visibility_downgrade": False,
    }
    claim.update(overrides)
    return claim


class _ProjectCase(unittest.TestCase):
    """Temporary project root with a fake capability contract."""

    state = V2_STATE

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="vg-integrated-prd-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.paths = ProjectPaths(self.root)
        self.paths.vibe.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text(
            json.dumps(self.state) + "\n", encoding="utf-8"
        )
        save_contract(
            self.paths, build_contract(self.root, provider="fake", host_id="local")
        )

    def _start(self, nodes, events, topology_rulings=None, card_kwargs=None):
        plan = Plan("plan-1", 1, "docs/prd.md", [n.id for n in nodes], "draft")
        card = build_authorization_card(plan, nodes, FULL, **(card_kwargs or {}))
        runner = FakeRunner(events=events)
        monitor = Monitor(self.paths, plan, nodes, topology_rulings=topology_rulings)
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        return monitor, runner, snapshot

    def _tick_until(self, monitor, runner, snapshot, node_id, stop, limit=16):
        for _ in range(limit):
            snapshot = monitor.tick(snapshot.run_id, runner)
            if snapshot.nodes[node_id]["status"] in stop:
                break
        return snapshot


# ---------------------------------------------------------------------------
# AC-B5 (#147)
# ---------------------------------------------------------------------------


class ACB5ScopeOnlyReauthorizationTests(_ProjectCase):
    """AC-B5：已验收 A→B，A 只扩文件清单、验收判定不变，重新授权后 B 保持已验收；
    A 的验收判定变化时 B 照旧失效。另有独立节点 C 证明"只重做受影响节点"。"""

    @staticmethod
    def _nodes(a_files=None, a_acceptance="verified_fact: accepted"):
        a = _node("a", a_files, acceptance_example=a_acceptance)
        b = _node("b", depends_on=["a"])
        c = _node("c")
        return [a, b, c]

    def _accepted_run(self):
        nodes = self._nodes()
        events = {}
        for node_id in ("a", "b", "c"):
            events[(node_id, "developer")] = [("complete", {"evidence": node_id})]
            events[(node_id, "reviewer")] = [("accepted", {"evidence": node_id})]
        monitor, runner, snapshot = self._start(nodes, events)
        for _ in range(8):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(
            {k: v["status"] for k, v in snapshot.nodes.items()},
            {"a": "accepted", "b": "accepted", "c": "accepted"},
            "fixture precondition: A→B and C must all be accepted first",
        )
        return snapshot

    def _reauthorize(self, snapshot, nodes):
        plan = Plan("plan-1", 1, "docs/prd.md", [n.id for n in nodes], "draft")
        record = authorize(build_authorization_card(plan, nodes, FULL), "AUTHORIZE")
        runner = FakeRunner()
        rebound = Monitor(self.paths, plan, nodes).reauthorize(
            snapshot.run_id, record, runner, "executable_contract_changed"
        )
        return rebound, runner

    def test_ac_b5_file_list_only_change_keeps_a_and_b_accepted(self):
        snapshot = self._accepted_run()
        changed = self._nodes(a_files=["a.py", "tests/test_a.py"])

        rebound, runner = self._reauthorize(snapshot, changed)

        for node_id in ("a", "b", "c"):
            self.assertEqual(
                rebound.nodes[node_id]["status"], "accepted",
                "AC-B5: file-list-only change must keep {} accepted".format(node_id),
            )
            self.assertIsNotNone(rebound.nodes[node_id].get("acceptance"))
        self.assertEqual(
            runner.start_calls, [],
            "AC-B5: nothing is affected, so nothing may be redone",
        )

    def test_ac_b5_acceptance_change_invalidates_b_but_not_unrelated_c(self):
        snapshot = self._accepted_run()
        changed = self._nodes(a_acceptance="verified_fact: changed")

        rebound, runner = self._reauthorize(snapshot, changed)

        self.assertEqual(rebound.nodes["a"]["status"], "rework")
        self.assertNotEqual(
            rebound.nodes["b"]["status"], "accepted",
            "AC-B5: B depends on A whose acceptance changed; B must be invalidated",
        )
        self.assertIsNone(rebound.nodes["b"]["acceptance"])
        self.assertEqual(
            rebound.nodes["c"]["status"], "accepted",
            "#147: only affected nodes are redone; independent C stays accepted",
        )
        self.assertEqual(
            sorted({call["node_id"] for call in runner.start_calls}), ["a"],
            "#147: only A is re-dispatched",
        )

    def test_ac_b5_accepted_patch_scope_expansion_keeps_downstream_accepted(self):
        """The in-run entry (``scope-expand`` / accepted_patch) on approved N5."""
        (self.paths.vibe / "config.json").write_text(
            json.dumps({"acceptance_scope_rule_approved": True}), encoding="utf-8"
        )
        nodes = self._nodes()
        events = {}
        for node_id in ("a", "b", "c"):
            events[(node_id, "developer")] = [("complete", {"evidence": node_id})] * 2
            events[(node_id, "reviewer")] = [("accepted", {"evidence": node_id})] * 2
        monitor, runner, snapshot = self._start(nodes, events)
        for _ in range(8):
            snapshot = monitor.tick(snapshot.run_id, runner)

        snapshot = monitor.request_scope_expansion(
            snapshot.run_id, "a", _scope_claim(["tests/test_a.py"])
        )
        self.assertEqual(snapshot.nodes["a"]["status"], "rework")
        self.assertEqual(snapshot.nodes["b"]["status"], "accepted")
        self.assertEqual(snapshot.nodes["c"]["status"], "accepted")
        before = len(runner.start_calls)
        for _ in range(6):
            snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(
            {k: v["status"] for k, v in snapshot.nodes.items()},
            {"a": "accepted", "b": "accepted", "c": "accepted"},
        )
        self.assertEqual(
            {call["node_id"] for call in runner.start_calls[before:]}, {"a"},
            "#147: the scope patch must redo only A, never B or C",
        )
        events_ = [
            r["data"] for r in load_events(self.paths, snapshot.run_id)
            if r["event"] == "scope_auto_expanded"
        ]
        self.assertEqual(len(events_), 1)
        self.assertEqual(events_[0]["files"], ["tests/test_a.py"])
        self.assertTrue(events_[0].get("scope_reason"), "§5.8: event must carry the reason")


# ---------------------------------------------------------------------------
# AC-B6 (#153)
# ---------------------------------------------------------------------------


def _blocked_unknown_review(item, ref="session-delivery#review-round-1"):
    return {
        "protocol": VISIBLE_SDD_PROTOCOL_REF,
        "evidence_ref": ref,
        "clearance": {"p0": 0, "p1": 0, "p2": 0},
        "coverage": {"mode": "ocr", "listed": 3, "reviewed": 3, "skipped": []},
        "environment_facts_ref": "none",
        "blocked_unknowns": [item],
    }


def _clean_review(ref="session-delivery#review-round-2"):
    review = _blocked_unknown_review("x", ref)
    review.pop("blocked_unknowns")
    return review


class ACB6DispositionTests(_ProjectCase):
    """AC-B6：三种处置（confirmed_benign / escalated_to_findings /
    resolved_with_facts）各有判定用例。"""

    ITEM = "无法确认 2.3.7 的 props"

    def _paused(self, deliveries):
        payload, nodes = load_v46_dispatch_fixture()
        plan = Plan("plan-1", 1, "docs/prd.md", [n.id for n in nodes], "draft")
        with patch("vibe_guide.dag._write_scope_conflict_errors", return_value={}):
            card = build_authorization_card(plan, nodes, FULL)
        monitor = Monitor(
            self.paths, plan, nodes, topology_rulings=payload["topology_rulings"]
        )
        runner = VisibleSddRunner(events={
            ("sdd-a", "developer"): [
                ("complete", {"evidence": "delivery", "in_session_review": review})
                for review in deliveries
            ]
        })
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        current = snapshot.nodes["sdd-a"]
        self.assertEqual(current["status"], "blocked_unknown")
        self.assertEqual(current["blocked_unknowns_pending"], [self.ITEM])
        self.assertNotEqual(
            map_user_status(current), PM_INTERRUPT,
            "#153: a blocked_unknown review item is supervisor-handled, not a PM question",
        )
        return monitor, runner, snapshot

    def _disposed(self, snapshot, kind):
        return [
            r for r in load_events(self.paths, snapshot.run_id)
            if r["event"] == "blocked_unknown_disposition"
            and r["data"].get("disposition") == kind
        ]

    def test_ac_b6_confirmed_benign_releases_acceptance(self):
        monitor, runner, snapshot = self._paused(
            [_blocked_unknown_review(self.ITEM)] * 2
        )
        snapshot = monitor.record_blocked_unknown_disposition(
            snapshot.run_id, "sdd-a", self.ITEM, "confirmed_benign",
            note="已核实 props 与 2.3.7 一致",
        )
        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "blocked_unknown")
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "accepted")
        self.assertEqual(len(self._disposed(snapshot, "confirmed_benign")), 1)

    def test_ac_b6_resolved_with_facts_releases_acceptance(self):
        monitor, runner, snapshot = self._paused(
            [_blocked_unknown_review(self.ITEM)] * 2
        )
        snapshot = monitor.record_blocked_unknown_disposition(
            snapshot.run_id, "sdd-a", self.ITEM, "resolved_with_facts",
            note="已补环境事实并复审",
        )
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "accepted")
        self.assertEqual(len(self._disposed(snapshot, "resolved_with_facts")), 1)

    def test_ac_b6_escalated_to_findings_requires_rework_before_acceptance(self):
        monitor, runner, snapshot = self._paused([
            _blocked_unknown_review(self.ITEM),
            _blocked_unknown_review(self.ITEM, "session-delivery#review-round-2"),
            _clean_review("session-delivery#review-round-3"),
        ])
        snapshot = monitor.record_blocked_unknown_disposition(
            snapshot.run_id, "sdd-a", self.ITEM, "escalated_to_findings", note="转 P1",
        )
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(
            snapshot.nodes["sdd-a"]["status"], "blocked_unknown",
            "escalated_to_findings never clears an item the reviewer still reports",
        )
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "accepted")
        self.assertEqual(len(self._disposed(snapshot, "escalated_to_findings")), 1)

    def test_ac_b6_unknown_disposition_kind_is_refused(self):
        monitor, _runner, snapshot = self._paused([_blocked_unknown_review(self.ITEM)])
        with self.assertRaises(ValueError):
            monitor.record_blocked_unknown_disposition(
                snapshot.run_id, "sdd-a", self.ITEM, "ask_product_manager"
            )

    def test_ac_b6_protocol_names_supervisor_disposition_with_three_criteria(self):
        """Protocol anchor (辅助): §5.7 #153 "人工/监工处置" → "监工处置"."""
        text = (PROTOCOLS / "visible-sdd-worker.md").read_text(encoding="utf-8")
        line = next(
            (l for l in text.splitlines() if "`blocked_unknowns`" in l and "处置" in l),
            "",
        )
        self.assertIn("监工处置", line)
        self.assertNotIn("人工", line, "#153: disposition must not be a human step")
        for kind in ("confirmed_benign", "escalated_to_findings", "resolved_with_facts"):
            self.assertIn(kind, line)


class ACB6RecoveryActionTests(_ProjectCase):
    """AC-B6：每种 blocked_unknown 原因都有可执行的恢复动作（prd-guide §7）。"""

    state = V42_STATE

    def _single(self, developer_events):
        node = _node("n1")
        return self._start(
            [node], {("n1", "developer"): list(developer_events)}
        )

    def test_ac_b6_every_blocked_unknown_reason_names_a_documented_recovery_action(self):
        cases = {
            "unexplained provider state": ("unknown", {"reason": "unexplained provider state"}),
            "writer identity conflict": ("unknown", {"reason": "writer identity conflict"}),
            "unrecognized runner event": ("bogus_event", {}),
            "review finding from developer": (
                "review_finding", {"finding": "x", "severity": "P1"}
            ),
        }
        for label, event in cases.items():
            with self.subTest(reason=label):
                self.setUp()
                monitor, runner, snapshot = self._single([event])
                snapshot = monitor.tick(snapshot.run_id, runner)
                current = snapshot.nodes["n1"]
                self.assertEqual(current["status"], "blocked_unknown")
                self.assertIn(
                    current.get("recovery_action"), DOCUMENTED_RECOVERY_ACTIONS,
                    "AC-B6: blocked_unknown ({}) must carry a prd-guide §7 action".format(label),
                )
                self.assertNotEqual(map_user_status(current), PM_INTERRUPT)
                # The named action is executable through the documented entry
                # (`vibe repair` / `vibe requeue` call Monitor.repair_node).
                result = monitor.repair_node("n1", snapshot.run_id)
                self.assertTrue(result.get("preserved_identity"))
                names = [r["event"] for r in load_events(self.paths, snapshot.run_id)]
                self.assertIn("node_repaired", names)

    def test_ac_b6_external_boundary_blocked_unknown_names_await_external_decision(self):
        """prd-guide §7: 命中产品、外部授权或 deploy 边界 → await_external_decision."""
        monitor, runner, snapshot = self._single([
            ("failed", {"kind": "credential_required", "reason": "login needed"})
        ])
        snapshot = monitor.tick(snapshot.run_id, runner)
        current = snapshot.nodes["n1"]
        self.assertEqual(
            current.get("recovery_action"), "await_external_decision",
            "AC-B6: a structured credential_required failure is an external "
            "boundary; prd-guide §7 maps it to await_external_decision, got "
            "status={!r} recovery_action={!r}".format(
                current["status"], current.get("recovery_action")
            ),
        )

    def test_ac_b6_blocked_unknowns_pause_names_its_recovery_entry(self):
        """A paused in-session review is blocked_unknown too; it needs an action."""
        payload, nodes = load_v46_dispatch_fixture()
        plan = Plan("plan-1", 1, "docs/prd.md", [n.id for n in nodes], "draft")
        with patch("vibe_guide.dag._write_scope_conflict_errors", return_value={}):
            card = build_authorization_card(plan, nodes, FULL)
        monitor = Monitor(self.paths, plan, nodes, topology_rulings=payload["topology_rulings"])
        runner = VisibleSddRunner(events={("sdd-a", "developer"): [(
            "complete",
            {"evidence": "d", "in_session_review": _blocked_unknown_review("x")},
        )]})
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        current = snapshot.nodes["sdd-a"]
        self.assertEqual(current["status"], "blocked_unknown")
        self.assertIsNotNone(
            current.get("recovery_action"),
            "AC-B6: blocked_unknown waiting for disposition records no "
            "recovery_action; the disposition entry "
            "(Monitor.record_blocked_unknown_disposition) has no CLI and is "
            "not listed in prd-guide §7",
        )

    def test_ac_b6_transient_failure_requeues_the_same_task_automatically(self):
        """requeue_same_task: timeout → retry_pending → same task resumes."""
        monitor, runner, snapshot = self._single([
            ("unknown", {"reason": "timeout"}),
            ("complete", {"evidence": "delivery"}),
        ])
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["n1"]["status"], "retry_pending")
        retry = snapshot.nodes["n1"]["retryable_action"]
        self.assertTrue(retry["same_task"])
        self.assertFalse(retry["successor"])
        with patch("time.time", return_value=time.time() + 3600):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["n1"]["status"], "review")
        developer_tasks = {
            c["task_id"] for c in runner.start_calls if c["role"] == "developer"
        }
        self.assertEqual(developer_tasks, {"developer:n1"}, "no successor task")

    def test_ac_b6_prd_guide_section_7_lists_recovery_and_requeue_entries(self):
        """Protocol anchor (辅助): §7 lists each action and a requeue entry."""
        text = (PROTOCOLS / "prd-guide.md").read_text(encoding="utf-8")
        section = text.split("## 7.", 1)[1].split("\n## ", 1)[0]
        for action in ("repair_node", "requeue_same_task", "reconcile_binding",
                       "await_external_decision"):
            self.assertIn("`" + action + "`", section)
        self.assertIn("vibe repair --plan", section)
        self.assertIn(
            "vibe requeue", section,
            "#153: prd-guide §7 must give the re-queue entry as a command; it "
            "only says 'repair/requeue 入口'",
        )
        self.assertIn(
            "change_reviewer_or_split_node", section,
            "#153: prd-guide §7 must name the 3-round action",
        )


class ACB6ReworkLoopTests(_ProjectCase):
    """AC-B6：返工 3 轮未收敛的节点进入换 reviewer 或拆节点，而不是问产品经理。"""

    state = V42_STATE

    def _three_rounds(self, finding):
        node = _node("n1")
        monitor, runner, snapshot = self._start([node], {
            ("n1", "developer"): [("complete", {"evidence": "d"})] * 5,
            ("n1", "reviewer"): [("review_finding", dict(finding))] * 5,
        })
        snapshot = self._tick_until(
            monitor, runner, snapshot, "n1",
            {"blocked_unknown", "blocked_design"},
        )
        reviews = [c for c in runner.start_calls if c["role"] == "reviewer"]
        self.assertEqual(len(reviews), 3, "fixture: three review rounds ran")
        return monitor, runner, snapshot

    def test_ac_b6_three_unconverged_rounds_record_change_reviewer_or_split_node(self):
        _monitor, _runner, snapshot = self._three_rounds(
            {"finding": "still wrong", "severity": "P1"}
        )
        current = snapshot.nodes["n1"]
        self.assertNotEqual(
            map_user_status(current), PM_INTERRUPT,
            "AC-B6: an unconverged engineering loop must not ask the PM",
        )
        self.assertEqual(
            current.get("recovery_action"), "change_reviewer_or_split_node",
            "AC-B6/#153: after 3 rounds main records status={!r} "
            "recovery_action={!r} reason={!r}; PRD requires "
            "change_reviewer_or_split_node".format(
                current["status"], current.get("recovery_action"), current.get("reason")
            ),
        )

    def test_ac_b6_three_round_recovery_dispatches_a_replacement_reviewer(self):
        monitor, runner, snapshot = self._three_rounds(
            {"finding": "still wrong", "severity": "P1"}
        )
        old_reviewer = {
            c["task_id"] for c in runner.start_calls if c["role"] == "reviewer"
        }
        monitor.repair_node("n1", snapshot.run_id)
        runner.events[("n1", "reviewer")] = [("accepted", {"evidence": "fresh review"})]
        runner.events[("n1", "developer")] = [("complete", {"evidence": "d"})]
        before = len(runner.start_calls)
        with patch("time.time", return_value=time.time() + 3600):
            for _ in range(4):
                snapshot = monitor.tick(snapshot.run_id, runner)
        new_reviews = [
            c for c in runner.start_calls[before:] if c["role"] == "reviewer"
        ]
        accepted_by = [
            r["data"].get("task_id") for r in load_events(self.paths, snapshot.run_id)
            if r["event"] == "accepted"
        ]
        self.assertTrue(
            new_reviews,
            "AC-B6: executing the recovery entry must re-dispatch a review "
            "(换 reviewer); none was started.  After the loop limit the old "
            "reviewer handle stayed live and the node ended status={!r} "
            "recovery_action={!r}, accepted by {} (all rounds used {})".format(
                snapshot.nodes["n1"]["status"],
                snapshot.nodes["n1"].get("recovery_action"),
                accepted_by, sorted(old_reviewer),
            ),
        )
        self.assertFalse(
            {c.get("task_id") for c in new_reviews} & old_reviewer,
            "AC-B6: the replacement review must use a new reviewer identity",
        )

    def test_ac_b6_three_rounds_escalate_only_when_acceptance_changes(self):
        """#153: 只有分歧点改变验收判定才升级."""
        _monitor, _runner, snapshot = self._three_rounds({
            "finding": "the approved acceptance example itself is wrong",
            "severity": "P1",
            "changes_prd_acceptance": True,
        })
        self.assertEqual(
            map_user_status(snapshot.nodes["n1"]), PM_INTERRUPT,
            "#153: a disagreement that changes PRD acceptance must escalate to "
            "the PM; main looped it as ordinary rework and ended status={!r}".format(
                snapshot.nodes["n1"]["status"]
            ),
        )


# ---------------------------------------------------------------------------
# AC-B3 (B1 counter-examples)
# ---------------------------------------------------------------------------


class ACB3ThirdPartyDependencyTests(_ProjectCase):
    """AC-B3：新增第三方依赖仍然打断产品经理（即使验收判定不变、规则已批准）。"""

    def setUp(self):
        super().setUp()
        (self.paths.vibe / "config.json").write_text(
            json.dumps({"acceptance_scope_rule_approved": True}), encoding="utf-8"
        )

    def _accepted(self):
        node = _node("n1")
        monitor, runner, snapshot = self._start([node], {
            ("n1", "developer"): [("complete", {"evidence": "d"})],
            ("n1", "reviewer"): [("accepted", {"evidence": "r"})],
        })
        for _ in range(2):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        return monitor, snapshot

    def test_ac_b3_declared_new_dependency_is_refused_and_asks_pm(self):
        monitor, snapshot = self._accepted()
        with self.assertRaises(ScopeDecisionRequired):
            monitor.request_scope_expansion(
                snapshot.run_id, "n1",
                _scope_claim(["tests/test_n1.py"], adds_third_party_dependency=True),
            )
        self.assertEqual(
            [r for r in load_events(self.paths, snapshot.run_id)
             if r["event"] == "scope_auto_expanded"], [],
        )

    def test_ac_b3_dependency_manifest_paths_never_auto_expand(self):
        """Dependency manifests are the third-party-dependency fact itself."""
        for path in ("pyproject.toml", "package.json", "requirements.txt",
                     "go.mod", "Cargo.toml", "Pipfile.lock"):
            with self.subTest(path=path):
                self.assertIsNone(resolve_scope_expansion(
                    _scope_claim([path]), ["n1.py"], ["n1.py"],
                    acceptance_rule_approved=True,
                ))

    def test_ac_b3_dependency_manifest_under_tests_still_asks_pm(self):
        """A manifest placed under tests/ still declares a third-party dependency.

        Neither the mechanical ``tests_dir`` rule nor an approved claim may
        auto-expand it: B1 says a new dependency is always a PM question,
        regardless of where the file sits.
        """
        for path in ("tests/requirements.txt", "tests/package.json",
                     "tests/pyproject.toml", "tests/fixtures/package-lock.json"):
            with self.subTest(path=path):
                mechanical = resolve_scope_expansion([path], ["n1.py"], ["n1.py"])
                claimed = resolve_scope_expansion(
                    _scope_claim([path]), ["n1.py"], ["n1.py"],
                    acceptance_rule_approved=True,
                )
                self.assertIsNone(
                    mechanical,
                    "AC-B3: {} auto-expanded via rule {!r} without asking the "
                    "PM".format(path, mechanical and mechanical.scope_rules),
                )
                self.assertIsNone(claimed)

    def test_ac_b3_review_finding_adding_dependency_blocks_for_pm(self):
        """Ordinary rework path: a finding whose fix adds a dependency."""
        node = _node("n1")
        monitor, runner, snapshot = self._start([node], {
            ("n1", "developer"): [("complete", {"evidence": "d"})],
        })
        snapshot = monitor.tick(snapshot.run_id, runner)
        binding = [c for c in runner.start_calls if c["node_id"] == "n1"][-1][
            "consistency_binding"
        ]
        files = ["n1.py", "requirements.txt"]
        decision = {"id": "decision-naming", "field": "naming", "revision": 1,
                    "status": "approved", "selected": "approved-name"}
        runner.events[("n1", "reviewer")] = [("review_finding", {
            "finding": "needs the requests package",
            "in_contract": False,
            "consistency": {
                "field": "naming", "action": "rework", "files": files,
                "candidates": [
                    {"source": "approved_prd", "value": "approved-name",
                     "binding": binding, "decision": decision},
                    {"source": "implementation", "value": "stale-name"},
                ],
                "scope_expansion": _scope_claim(files, adds_third_party_dependency=True),
            },
        })]
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(map_user_status(snapshot.nodes["n1"]), PM_INTERRUPT)


class ACB3VisibilityDowngradeTests(_ProjectCase):
    """AC-B3：执行模式从可见可进入的任务降级为后台不可见，仍然打断产品经理。"""

    def _node(self):
        return _node("n1", adapter_id="claude-code")

    def test_ac_b3_declared_visibility_downgrade_claim_asks_pm(self):
        self.assertIsNone(resolve_scope_expansion(
            _scope_claim(["tests/test_n1.py"], visibility_downgrade=True),
            ["n1.py"], ["n1.py"], acceptance_rule_approved=True,
        ))

    def test_ac_b3_card_cannot_sign_background_without_disclosure(self):
        plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        with self.assertRaisesRegex(ValueError, "downgrade disclosure"):
            build_authorization_card(
                plan, [self._node()], FULL,
                topology_rulings={"claude-code": "background"},
            )

    def test_ac_b3_cli_ruling_keeps_visible_path_for_a_visible_card(self):
        """The CLI ruling walks back a background fact for a visible card."""
        self.assertEqual(run_cli(["init", "--confirm", "--json"], self.root).exit_code, 0)
        store = self.root / ".vibe" / "provider-actions"
        store.mkdir(parents=True, exist_ok=True)
        (store / "capabilities.json").write_text(json.dumps({
            "schema_version": 1, "adapter_id": "claude-code",
            "facts": {"claude-code.subprocess": True,
                      "claude-code.background_task.launch": True},
            "provenance": "live:test",
        }), encoding="utf-8")
        plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        card = build_authorization_card(plan, [self._node()], FULL)
        self.assertEqual(card.workers[0]["mode"], "visible")
        self.assertEqual(
            _observed_topology_rulings(self.paths, card),
            {"claude-code": "dual-visible"},
        )

    def test_ac_b3_monitor_never_dispatches_background_under_a_visible_card(self):
        """The dispatcher itself must stop for the PM, not silently downgrade.

        The card the user signed lists n1 as ``visible``.  A background ruling
        reaching the monitor (any caller that builds ``Monitor`` with the live
        ruling) must not start a background worker; it must stop at a PM
        boundary.
        """
        monitor, runner, snapshot = self._start(
            [self._node()], {}, topology_rulings={"claude-code": "background"}
        )
        topologies = [c.get("topology") for c in runner.start_calls]
        self.assertNotIn(
            "background", topologies,
            "AC-B3: card signed visible, monitor dispatched {} with "
            "limitations {}; a visible→background downgrade must ask the PM "
            "instead".format(
                topologies,
                [c.get("dispatch_limitations") for c in runner.start_calls],
            ),
        )
        self.assertEqual(map_user_status(snapshot.nodes["n1"]), PM_INTERRUPT)


class ACB3DeployTests(_ProjectCase):
    """AC-B3：deploy 仍然打断产品经理（单独授权）。"""

    def _manifest(self):
        manifest = {
            "target": "staging", "commit": "a" * 40,
            "command_allowlist": ["./scripts/deploy-staging"],
            "health_checks": [{"name": "http", "kind": "http", "required": True}],
            "rollback": {"commit": "b" * 40, "command": "./scripts/rollback-staging"},
            "stop_conditions": ["health_check_failed"],
        }
        DeployManifest.from_dict(manifest)
        path = self.root / "deploy-manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path.name

    def test_ac_b3_plan_authorization_does_not_cover_deploy(self):
        plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        card = build_authorization_card(plan, [_node("n1")], FULL)
        self.assertNotIn("deploy", card.allowed_actions)
        self.assertIn("deploy", card.excluded_actions)

    def test_ac_b3_deploy_cli_stops_without_separate_authorization(self):
        manifest = self._manifest()
        observations = self.root / "obs.json"
        observations.write_text(json.dumps({"health": True, "version": "a" * 40}),
                                encoding="utf-8")
        result = run_cli(
            ["deploy", "--manifest", manifest, "--acceptance-state", "accepted",
             "--observations", observations.name, "--json"],
            self.root,
        )
        self.assertEqual(result.payload["status"], "blocked_deploy", result.payload)
        self.assertEqual(map_user_status(result.payload["status"]), PM_INTERRUPT)

    def test_ac_b3_plan_token_is_not_a_deploy_authorization(self):
        result = run_cli(
            ["deploy", "--manifest", self._manifest(), "--acceptance-state",
             "accepted", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )
        self.assertEqual(result.payload["status"], "blocked_deploy", result.payload)

    def test_ac_b3_worker_deploy_signal_reaches_pm(self):
        """A worker reporting ``deploy_required`` must surface to the PM."""
        self.state = V42_STATE
        (self.paths.vibe / "state.json").write_text(json.dumps(V42_STATE), encoding="utf-8")
        monitor, runner, snapshot = self._start([_node("n1")], {
            ("n1", "developer"): [("failed", {"kind": "deploy_required",
                                              "reason": "needs a staging deploy"})],
        })
        snapshot = monitor.tick(snapshot.run_id, runner)
        current = snapshot.nodes["n1"]
        self.assertEqual(
            map_user_status(current), PM_INTERRUPT,
            "AC-B3: structured deploy_required from a worker ended "
            "status={!r} recovery_action={!r} (user sees {!r}); deploy must "
            "interrupt the PM".format(
                current["status"], current.get("recovery_action"),
                map_user_status(current),
            ),
        )


# ---------------------------------------------------------------------------
# AC-H2, Codex part
# ---------------------------------------------------------------------------

_CODEX_VISIBLE = {
    "codex.shell": True, "codex.subprocess": True, "codex.worktree": True,
    "codex.visible_task.create": True, "codex.visible_task.enter": True,
    "codex.visible_task.resume": True, "codex.visible_task.wait": True,
}

#: Real Codex capability fact shapes -> (facts, provenance).
CODEX_CASES = {
    "visible_sdd_pass": (dict(_CODEX_VISIBLE, **{"codex.in_session_sdd": True}),
                         {"codex.in_session_sdd": "live:probe"}),
    "visible_sdd_missing": (dict(_CODEX_VISIBLE), {}),
    "visible_sdd_no_provenance": (dict(_CODEX_VISIBLE, **{"codex.in_session_sdd": True}), {}),
    "visible_sdd_false": (dict(_CODEX_VISIBLE, **{"codex.in_session_sdd": False}),
                          {"codex.in_session_sdd": "live:probe"}),
    "sdd_only_pass": ({"codex.shell": True, "codex.subprocess": True,
                       "codex.worktree": True, "codex.in_session_sdd": True},
                      {"codex.in_session_sdd": "live:probe"}),
    "no_subprocess": ({"codex.in_session_sdd": True}, {"codex.in_session_sdd": "live:probe"}),
    "foreign_background_fact": (dict(_CODEX_VISIBLE, **{"codex.background_launch": True}),
                                {"codex.background_launch": "live:probe"}),
}

#: The script runs against whichever ``vibe_guide`` is first on sys.path, so
#: the identical program is executed on the baseline tree and on this tree.
_CODEX_PROBE = r'''
import json, sys
from vibe_guide.adapters.base import Environment
from vibe_guide.adapters.registry import AdapterRegistry
from vibe_guide.authorization import build_authorization_card, dispatch_topology_for_node
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
cases = json.loads(sys.argv[1])
adapter = AdapterRegistry().get("codex")
out = {}
for name, (facts, provenance) in sorted(cases.items()):
    env = Environment(commands={"codex.agent": True}, facts=facts, provenance=provenance)
    decision = adapter.topology_decision(env).to_dict()
    caps = adapter.detect(env).capabilities
    node = DAGNode("n1", "n1", [], [], "g", {"files": ["n1.py"], "adapter_id": "codex"}, "ready")
    rulings = {"codex": decision["topology"]}
    card = build_authorization_card(
        Plan("p", 1, "prd.md", ["n1"], "draft"), [node],
        AgentCapabilities("codex", True, True, True, True, True, "full"),
        topology_rulings=rulings,
    )
    out[name] = {
        "decision": decision,
        "capabilities": {k: getattr(caps, k) for k in (
            "level", "mode", "provider", "visible_automation", "in_session_sdd")},
        "dispatch": dispatch_topology_for_node(node, rulings),
        "card_worker": {k: card.workers[0][k] for k in ("topology", "mode", "role")},
    }
print(json.dumps(out, sort_keys=True, ensure_ascii=False))
'''


def _run_codex_probe(source_root):
    completed = subprocess.run(
        [sys.executable, "-c", _CODEX_PROBE, json.dumps(CODEX_CASES)],
        cwd=str(source_root), capture_output=True, text=True, timeout=120,
        env={"PYTHONPATH": str(source_root), "PATH": "/usr/bin:/bin"},
    )
    if completed.returncode != 0:
        raise AssertionError(
            "codex probe failed under {}:\n{}".format(source_root, completed.stderr)
        )
    return json.loads(completed.stdout)


class ACH2CodexTopologyTests(unittest.TestCase):
    """AC-H2（Codex 部分）：Codex 判定结果与改动前逐字一致。"""

    def test_ac_h2_codex_ruling_matches_the_documented_outputs(self):
        current = _run_codex_probe(REPO_ROOT)
        expected = {
            "visible_sdd_pass": ("pass", "in_session_sdd", "visible-sdd", "visible"),
            "visible_sdd_missing": ("unknown", "dual-visible", "dual-visible", "visible"),
            "visible_sdd_no_provenance": ("unknown", "dual-visible", "dual-visible", "visible"),
            "visible_sdd_false": ("unsupported", "dual-visible", "dual-visible", "visible"),
            "sdd_only_pass": ("pass", "in_session_sdd", "visible-sdd", "in_session_sdd"),
            "no_subprocess": ("pass", "in_session_sdd", "visible-sdd", "guide"),
            "foreign_background_fact": ("unknown", "dual-visible", "dual-visible", "visible"),
        }
        for name, (status, topology, dispatch, mode) in expected.items():
            with self.subTest(case=name):
                row = current[name]
                self.assertEqual(row["decision"]["probe_status"], status)
                self.assertEqual(row["decision"]["topology"], topology)
                self.assertEqual(row["dispatch"], dispatch)
                self.assertEqual(row["card_worker"]["topology"], dispatch)
                self.assertEqual(row["capabilities"]["mode"], mode)
        self.assertNotIn(
            "background", {row["dispatch"] for row in current.values()},
            "Codex must never be ruled into the Claude Code background path",
        )

    def test_ac_h2_codex_ruling_is_identical_to_the_pre_change_baseline(self):
        with tempfile.TemporaryDirectory(prefix="vg-codex-baseline-") as tmp:
            archive = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "archive", CODEX_BASELINE_REF, "vibe_guide"],
                capture_output=True, timeout=120,
            )
            self.assertEqual(
                archive.returncode, 0,
                "baseline {} is not readable from this checkout: {}".format(
                    CODEX_BASELINE_REF, archive.stderr.decode("utf-8", "replace")
                ),
            )
            with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
                bundle.extractall(tmp)
            baseline = _run_codex_probe(Path(tmp))
        current = _run_codex_probe(REPO_ROOT)
        self.assertEqual(
            json.dumps(current, sort_keys=True, ensure_ascii=False),
            json.dumps(baseline, sort_keys=True, ensure_ascii=False),
            "AC-H2: Codex topology output drifted from " + CODEX_BASELINE_REF,
        )

    def test_ac_h2_codex_cli_ruling_from_recorded_facts(self):
        """The publish/dispatch gate reads recorded Codex facts the same way."""
        cases = (
            ({"codex.subprocess": True, "codex.in_session_sdd": True}, "live:test",
             "in_session_sdd"),
            ({"codex.subprocess": True, "codex.in_session_sdd": True}, "", "dual-visible"),
            ({"codex.subprocess": True, "codex.in_session_sdd": False}, "live:test",
             "dual-visible"),
            ({"codex.subprocess": True}, "live:test", "dual-visible"),
        )
        for facts, provenance, expected in cases:
            with self.subTest(facts=facts, provenance=provenance):
                with tempfile.TemporaryDirectory(prefix="vg-codex-cli-") as tmp:
                    root = Path(tmp)
                    self.assertEqual(
                        run_cli(["init", "--confirm", "--json"], root).exit_code, 0
                    )
                    store = root / ".vibe" / "provider-actions"
                    store.mkdir(parents=True, exist_ok=True)
                    (store / "capabilities.json").write_text(json.dumps({
                        "schema_version": 1, "adapter_id": "codex",
                        "facts": facts, "provenance": provenance,
                    }), encoding="utf-8")
                    self.assertEqual(
                        _observed_topology_rulings(ProjectPaths(root)),
                        {"codex": expected},
                    )


# ---------------------------------------------------------------------------
# AC-E1 (#128 / B5)
# ---------------------------------------------------------------------------

_UNREACHABLE_GATEWAY = "http://127.0.0.1:9/triage"
_TRIAGE_REQUESTS = (
    "修复 README 里的一个错别字",
    "重构支付系统：迁移数据库、改造三个微服务接口、接入新的风控规则并上线到生产",
)


class ACE1EntryTriageTests(unittest.TestCase):
    """AC-E1：关掉分诊服务后入口请求按 S1 规则完成分诊并留降级记录；
    评测报告给出 accuracy、ECE、p95 三项数值，ECE ≤ 0.05。"""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="vg-triage-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.assertEqual(run_cli(["init", "--confirm", "--json"], self.root).exit_code, 0)
        config_path = self.root / ".vibe" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        # Point any triage gateway at a closed port: the service is "off".
        config["triage_gateway"] = {"url": _UNREACHABLE_GATEWAY, "timeout_seconds": 1}
        config_path.write_text(json.dumps(config), encoding="utf-8")

    def _plan(self, request):
        with patch.dict("os.environ", {"VIBE_TRIAGE_GATEWAY_URL": _UNREACHABLE_GATEWAY}):
            return run_cli(["plan", "--request", request, "--json"], self.root)

    def test_ac_e1_gateway_down_entry_still_triages_by_s1_rules(self):
        for request in _TRIAGE_REQUESTS:
            with self.subTest(request=request):
                result = self._plan(request)
                self.assertEqual(result.exit_code, 0, result.payload)
                expected = route_task(default_s1_context(request.lower())).route
                self.assertEqual(result.payload["route"], expected)

    def test_ac_e1_gateway_down_leaves_a_degradation_record(self):
        result = self._plan(_TRIAGE_REQUESTS[0])
        payload_record = result.payload.get("triage")
        on_disk = sorted(
            p.relative_to(self.root).as_posix()
            for p in (self.root / ".vibe").rglob("*")
            if p.is_file() and "triage" in p.name.casefold()
        )
        self.assertTrue(
            isinstance(payload_record, dict)
            and payload_record.get("source") == "s1_rules"
            and payload_record.get("degraded") is True
            and payload_record.get("reason"),
            "AC-E1: no triage hook on this branch -- `vibe plan` never calls a "
            "gateway and records no degradation (payload keys {}, triage files "
            "under .vibe: {})".format(sorted(result.payload), on_disk),
        )

    def test_ac_e1_evaluation_scaffold_reports_accuracy_ece_p95(self):
        candidates = [
            p for p in list((REPO_ROOT / "scripts").glob("*"))
            + list((REPO_ROOT / "vibe_guide").rglob("*.py"))
            if "triage" in p.name.casefold()
        ]
        self.assertTrue(
            candidates,
            "AC-E1: no triage evaluation scaffold exists (searched scripts/ and "
            "vibe_guide/ for *triage*); cannot produce accuracy/ECE/p95",
        )
        script = candidates[0]
        completed = subprocess.run(
            [sys.executable, str(script), "--json"], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        for key in ("accuracy", "ece", "p95"):
            self.assertIsInstance(report.get(key), (int, float), key)
        self.assertLessEqual(report["ece"], 0.05)


if __name__ == "__main__":
    unittest.main()
