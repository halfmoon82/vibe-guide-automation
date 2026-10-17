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
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from vibe_guide import cli as vibe_cli
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.cli import _observed_topology_rulings, _public_runner, run_cli
from vibe_guide.deploy import DeployManifest
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor, ScopeDecisionRequired, VISIBLE_SDD_PROTOCOL_REF
from vibe_guide.paths import ProjectPaths
from vibe_guide.planner import resolve_scope_expansion
from vibe_guide.runners.claude_bg import ClaudeBgRunner
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.runners.provider_action import ProviderActionRunner
from vibe_guide.state import load_events, load_snapshot, map_user_status

from tests.support_v45_authorize import publish_complex_probe
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
CLAUDE_FULL = AgentCapabilities("claude-code", True, True, True, True, True, "full")

#: Native visible-task facts (same set as tests/support_v45_authorize.py).
_CLAUDE_VISIBLE_FACTS = {
    "claude-code." + name: True
    for name in ("agent", "shell", "subprocess", "worktree",
                 "visible_task.create", "visible_task.enter",
                 "visible_task.resume", "visible_task.wait")
}


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


def _isolate_user_dirs(case):
    """Point HOME and VIBE_HOME at throwaway directories for this test."""
    home = tempfile.TemporaryDirectory(prefix="vg-integrated-home-")
    vibe_home = tempfile.TemporaryDirectory(prefix="vg-integrated-vibe-home-")
    case.addCleanup(home.cleanup)
    case.addCleanup(vibe_home.cleanup)
    patcher = patch.dict(os.environ, {"HOME": home.name, "VIBE_HOME": vibe_home.name})
    patcher.start()
    case.addCleanup(patcher.stop)


#: Approved naming decision the consistency resolver checks findings against
#: (same shape as tests/test_auto_scope_files.py).
NAMING_DECISION = {
    "id": "decision-naming",
    "field": "naming",
    "revision": 1,
    "status": "approved",
    "selected": "approved-name",
    "question": "canonical name",
}


class _ProjectCase(unittest.TestCase):
    """Temporary project root with a fake capability contract."""

    state = V2_STATE

    def setUp(self):
        _isolate_user_dirs(self)
        self._make_project()

    def _make_project(self):
        temporary = tempfile.TemporaryDirectory(prefix="vg-integrated-prd-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = ProjectPaths(self.root)
        self.paths.vibe.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text(
            json.dumps(self.state) + "\n", encoding="utf-8"
        )
        save_contract(
            self.paths, build_contract(self.root, provider="fake", host_id="local")
        )

    def _start(self, nodes, events, topology_rulings=None, card_kwargs=None,
               plan_kwargs=None):
        plan = Plan(
            "plan-1", 1, "docs/prd.md", [n.id for n in nodes], "draft",
            **(plan_kwargs or {})
        )
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
                self._make_project()
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
        """Protocol anchor (辅助): §7 lists each action and a requeue entry.

        Facts are anchored to the line that owns them, not the whole section.
        """
        text = (PROTOCOLS / "prd-guide.md").read_text(encoding="utf-8")
        section = text.split("## 7.", 1)[1].split("\n## ", 1)[0]
        lines = section.splitlines()
        for action in ("repair_node", "requeue_same_task", "reconcile_binding",
                       "await_external_decision"):
            self.assertTrue(
                any(l.lstrip().startswith("- `" + action + "`") for l in lines),
                "prd-guide §7 must give {} its own bullet".format(action),
            )
        self.assertEqual(
            run_cli(["requeue", "--help"], self.root).exit_code, 0,
            "#153: the requeue entry named in prd-guide §7 must exist as a command",
        )
        entry_line = next((l for l in lines if "requeue 入口" in l), "")
        self.assertIn(
            "`reconcile_binding`", entry_line,
            "prd-guide §7: the repair/requeue entry must sit on the reconcile_binding row",
        )
        self.assertIn("vibe repair --plan", entry_line)
        loop_line = next((l for l in lines if "三轮返工仍不收敛" in l), "")
        self.assertTrue(loop_line, "prd-guide §7 must state the 3-round rule")
        for fact in ("换 reviewer", "拆节点", "唯一 writer", "PRD 验收判定"):
            self.assertIn(
                fact, loop_line,
                "#153: the 3-round line of prd-guide §7 must carry {!r}".format(fact),
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

    def test_ac_b6_replacement_reviewer_keeps_identity_on_next_round(self):
        """P2-1 of the PR #238 review: the replacement gate must close.

        After the scheduled successor review starts, the next delivery must
        continue that same replacement reviewer.  A gate left open forces a
        third fresh identity on every round, so the review lineage the
        visible-sdd protocol requires never forms.
        """
        monitor, runner, snapshot = self._three_rounds(
            {"finding": "still wrong", "severity": "P1"}
        )
        old_reviewer = {
            c["task_id"] for c in runner.start_calls if c["role"] == "reviewer"
        }
        monitor.repair_node("n1", snapshot.run_id)
        # The successor gets a fresh three-finding budget: its first finding
        # reworks instead of parking, and the re-review continues it.
        runner.events[("n1", "reviewer")] = [
            ("review_finding", {"finding": "still wrong", "severity": "P1"}),
            ("accepted", {"evidence": "fresh review"}),
        ]
        runner.events[("n1", "developer")] = [("complete", {"evidence": "d"})] * 6
        before = len(runner.start_calls)
        with patch("time.time", return_value=time.time() + 3600):
            snapshot = self._tick_until(
                monitor, runner, snapshot, "n1",
                {"accepted", "blocked_unknown", "blocked_design"}, limit=20,
            )
        new_reviews = [
            c for c in runner.start_calls[before:] if c["role"] == "reviewer"
        ]
        self.assertEqual(
            len(new_reviews), 2,
            "PR #238 P2-1: expected exactly the successor review and its "
            "continuation, got {}".format(
                [(c["task_id"], bool(c.get("successor")), bool(c.get("continuation"))) for c in new_reviews]
            ),
        )
        successor, again = new_reviews
        self.assertTrue(successor["successor"])
        self.assertFalse(successor["continuation"])
        self.assertNotIn(successor["task_id"], old_reviewer)
        self.assertEqual(again["task_id"], successor["task_id"])
        self.assertTrue(again["continuation"])
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")

    def test_ac_b6_review_loop_limit_survives_event_replay(self):
        """P2-2 of the PR #238 review: the park must replay as itself.

        A crash between appending ``review_loop_limit_reached`` and saving
        the snapshot must not degrade the node to the generic
        "unapplied event needs manual reconciliation" state: the recovery
        entry has to stay executable.
        """
        monitor, runner, snapshot = self._three_rounds(
            {"finding": "still wrong", "severity": "P1"}
        )
        events = load_events(self.paths, snapshot.run_id)
        park = [r for r in events if r["event"] == "review_loop_limit_reached"][-1]
        stale = load_snapshot(self.paths, snapshot.run_id)
        stale.event_sequence = park["sequence"] - 1
        node = stale.nodes["n1"]
        # Stand in for the pre-park snapshot: drop every park marker the
        # replay is supposed to restore from the event.
        node.pop("reviewer_replacement_required", None)
        node.pop("review_loop_rounds", None)
        node["recovery_action"] = None
        node["reviewer_identity"] = None
        monitor._reconcile_unapplied_events(stale)
        current = stale.nodes["n1"]
        self.assertEqual(current["status"], "blocked_unknown")
        self.assertEqual(
            current.get("reason"),
            "review loop limit reached; change reviewer or split node required",
            "PR #238 P3-1: the replay must restore the vibe-authored park "
            "reason, not the [REDACTED_PROVIDER_TEXT] placeholder the "
            "top-level reason key is redacted to on write",
        )
        self.assertEqual(
            current.get("recovery_action"), "change_reviewer_or_split_node"
        )
        self.assertIs(current.get("reviewer_replacement_required"), True)
        self.assertIs(current.get("reviewer_started"), False)
        self.assertEqual(
            current.get("reviewer_identity"),
            park["data"]["proof"]["reviewer_identity"],
        )
        self.assertEqual(
            current.get("review_loop_rounds"),
            park["data"]["proof"]["review_loop_rounds"],
        )

    def test_ac_b6_three_rounds_escalate_only_when_acceptance_changes(self):
        """#153: 只有分歧点改变验收判定才升级.

        Driven directly (not via ``_three_rounds``): an escalation may stop
        the loop before three reviews ran, which is allowed.
        """
        finding = {
            "finding": "the approved acceptance example itself is wrong",
            "severity": "P1",
            "changes_prd_acceptance": True,
        }
        monitor, runner, snapshot = self._start([_node("n1")], {
            ("n1", "developer"): [("complete", {"evidence": "d"})] * 5,
            ("n1", "reviewer"): [("review_finding", dict(finding))] * 5,
        })
        snapshot = self._tick_until(
            monitor, runner, snapshot, "n1",
            {"blocked_unknown", "blocked_design"},
        )
        reviews = [c for c in runner.start_calls if c["role"] == "reviewer"]
        current = snapshot.nodes["n1"]
        self.assertLessEqual(
            len(reviews), 3,
            "#153: at most three review rounds before the loop stops",
        )
        self.assertEqual(
            map_user_status(current), PM_INTERRUPT,
            "#153: a disagreement that changes PRD acceptance must escalate to "
            "the PM; main ended status={!r} recovery_action={!r} after {} "
            "review rounds -- Monitor._finding_changes_prd_acceptance "
            "(monitor.py:6233) has no caller".format(
                current["status"], current.get("recovery_action"), len(reviews)
            ),
        )


# ---------------------------------------------------------------------------
# AC-B3 (B1 counter-examples)
# ---------------------------------------------------------------------------


class ACB3ThirdPartyDependencyTests(_ProjectCase):
    """AC-B3：新增第三方依赖仍然打断产品经理（即使验收判定不变、规则已批准）。"""

    def setUp(self):
        super().setUp()
        self._approve_acceptance_rule()

    def _approve_acceptance_rule(self):
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

    def test_ac_b3_dependency_manifest_under_tests_may_auto_expand(self):
        """PM decision 2 (2026-10-15): a manifest under tests/ may auto-expand.

        The PM ruled that a dependency manifest placed under ``tests/`` does
        not count as a new third-party dependency, so the mechanical
        ``tests_dir`` rule and an approved claim both widen scope for it.
        Manifests elsewhere are the product dependency fact itself and stay
        blocked (see the root-path case above).  This test used to assert
        the opposite; the ruling said the assertion was what had to change
        (run-9edb07bb integration-review reports g2/g3).
        """
        for path in ("tests/requirements.txt", "tests/package.json",
                     "tests/pyproject.toml"):
            with self.subTest(path=path):
                mechanical = resolve_scope_expansion([path], ["n1.py"], ["n1.py"])
                self.assertIsNotNone(
                    mechanical,
                    "PM decision 2: {} under tests/ must auto-expand via the "
                    "tests_dir rule".format(path),
                )
                self.assertEqual(
                    mechanical.scope_rules,
                    [{"path": path, "rule": "tests_dir"}],
                )
                claimed = resolve_scope_expansion(
                    _scope_claim([path]), ["n1.py"], ["n1.py"],
                    acceptance_rule_approved=True,
                )
                self.assertIsNotNone(
                    claimed,
                    "PM decision 2: an approved claim must also widen {} "
                    "under tests/".format(path),
                )
                self.assertEqual(
                    claimed.scope_rules,
                    [{"path": path, "rule": "tests_dir"}],
                )

    def _review_finding_run(self, files, adds_dependency):
        """Ordinary rework path: a finding whose fix needs files outside scope.

        The plan carries the approved naming decision and the node contract
        the approved value, so the consistency resolver actually evaluates
        the claim instead of refusing an unbound decision outright.
        """
        node = _node("n1", naming="approved-name")
        monitor, runner, snapshot = self._start(
            [node],
            {("n1", "developer"): [("complete", {"evidence": "d"})] * 2},
            plan_kwargs={"decisions": [dict(NAMING_DECISION)]},
        )
        snapshot = monitor.tick(snapshot.run_id, runner)
        binding = [c for c in runner.start_calls if c["node_id"] == "n1"][-1][
            "consistency_binding"
        ]
        runner.events[("n1", "reviewer")] = [("review_finding", {
            "finding": "needs a change outside the node scope",
            "in_contract": False,
            "consistency": {
                "field": "naming", "action": "rework", "files": list(files),
                "candidates": [
                    {"source": "approved_prd", "value": "approved-name",
                     "binding": binding, "decision": dict(NAMING_DECISION)},
                    {"source": "implementation", "value": "stale-name"},
                ],
                "scope_expansion": _scope_claim(
                    files, adds_third_party_dependency=adds_dependency
                ),
            },
        })]
        snapshot = monitor.tick(snapshot.run_id, runner)
        expanded = [
            r for r in load_events(self.paths, snapshot.run_id)
            if r["event"] == "scope_auto_expanded"
        ]
        return snapshot.nodes["n1"], expanded

    def test_ac_b3_review_finding_adding_dependency_blocks_for_pm(self):
        cases = {
            "manifest_file": ["n1.py", "requirements.txt"],
            # Only the flag differs from the control case below.
            "flag_only": ["n1.py", "tests/test_n1.py"],
        }
        for label, files in cases.items():
            with self.subTest(case=label):
                self._make_project()
                self._approve_acceptance_rule()
                current, expanded = self._review_finding_run(files, adds_dependency=True)
                self.assertEqual(
                    map_user_status(current), PM_INTERRUPT,
                    "AC-B3: a rework finding that adds a dependency ({}) ended "
                    "status={!r} reason={!r}".format(
                        label, current["status"], current.get("reason")
                    ),
                )
                self.assertEqual(expanded, [])

    def test_ac_b3_review_finding_without_dependency_reworks_without_pm(self):
        """Control: the same finding minus the dependency is auto-handled."""
        current, expanded = self._review_finding_run(
            ["n1.py", "tests/test_n1.py"], adds_dependency=False
        )
        self.assertNotEqual(map_user_status(current), PM_INTERRUPT)
        self.assertEqual(
            current["status"], "rework",
            "control: a test-file-only expansion is ordinary rework, got "
            "status={!r} reason={!r}".format(current["status"], current.get("reason")),
        )
        self.assertEqual(len(expanded), 1, "§5.8: the expansion leaves a record")


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

    def test_ac_b3_cli_dispatch_keeps_visible_runner_for_a_visible_card(self):
        """The CLI dispatch path (``_public_runner``) never picks the
        background runner for a card signed ``visible``."""
        self.assertEqual(run_cli(["init", "--confirm", "--json"], self.root).exit_code, 0)
        store = self.root / ".vibe" / "provider-actions"
        store.mkdir(parents=True, exist_ok=True)
        (store / "capabilities.json").write_text(json.dumps({
            "schema_version": 1, "adapter_id": "claude-code",
            "facts": dict(_CLAUDE_VISIBLE_FACTS,
                          **{"claude-code.background_task.launch": True}),
            "provenance": "live:test",
        }), encoding="utf-8")
        node = self._node()
        card = build_authorization_card(
            Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft"), [node], CLAUDE_FULL
        )
        self.assertEqual(card.workers[0]["mode"], "visible")
        self.assertEqual(
            vibe_cli._observed_adapter(self.paths, "claude-code").capabilities.mode,
            "background",
            "fixture precondition: the recorded facts make background available",
        )
        runner = _public_runner(self.paths, card, [node])
        self.assertNotIsInstance(runner, ClaudeBgRunner)
        self.assertIsInstance(runner, ProviderActionRunner)
        self.assertNotIn(
            "background", set(_observed_topology_rulings(self.paths, card).values())
        )


class ACB3VisibilityDowngradePreviewTests(unittest.TestCase):
    """AC-B3（PRD 可观察面）：已签可见卡后可见任务能力消失、只剩后台能力
    （真正的可见→后台降级条件），预览必须给出结构化的重新授权信号，
    而不是静默沿用已签的可见卡。"""

    def setUp(self):
        _isolate_user_dirs(self)

    def test_ac_b3_preview_asks_pm_to_authorize_a_visibility_downgrade(self):
        root = publish_complex_probe(self)
        before = run_cli(
            ["monitor", "--plan", "probe-plan", "--preview-card", "--json"], root
        ).payload
        self.assertEqual(before.get("status"), "ok", before)
        self.assertEqual(
            before["topology_summary"]["background"], 0,
            "fixture precondition: the signed card is all visible",
        )
        capabilities = root / ".vibe" / "provider-actions" / "capabilities.json"
        recorded = json.loads(capabilities.read_text(encoding="utf-8"))
        for name in list(recorded["facts"]):
            if name.startswith("claude-code.visible_task."):
                recorded["facts"][name] = False
        recorded["facts"]["claude-code.background_task.launch"] = True
        capabilities.write_text(json.dumps(recorded), encoding="utf-8")

        payload = run_cli(
            ["monitor", "--plan", "probe-plan", "--preview-card", "--json"], root
        ).payload
        summary = payload.get("topology_summary") or {}
        reason = str(payload.get("reason", "")).casefold()
        signals = {
            "background_count_changed":
                summary.get("background") != before["topology_summary"]["background"],
            "digest_differs_from_publication":
                payload.get("preview_card_digest_matches_publication") is False,
            "changed_fields": bool(payload.get("changed_fields")),
            "refused_for_reauthorization": payload.get("status") != "ok" and any(
                word in reason for word in ("reauthoriz", "authoriz", "授权")
            ),
        }
        self.assertTrue(
            any(signals.values()),
            "AC-B3: visible card signed, then every claude-code visible_task.* "
            "fact became False and background_task.launch True; `vibe monitor "
            "--preview-card` still reports topology {} with digest matching "
            "the publication {!r}, changed_fields {!r}, status {!r} -- no "
            "structured reauthorization signal for the visible→background "
            "downgrade".format(
                summary, payload.get("preview_card_digest_matches_publication"),
                payload.get("changed_fields"), payload.get("status"),
            ),
        )


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

    def setUp(self):
        _isolate_user_dirs(self)

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
                if hasattr(tarfile, "data_filter"):
                    bundle.extractall(tmp, filter="data")
                else:
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

_TRIAGE_REQUESTS = {
    # request -> route fixed by the S1 rules (S1=5 / S1=17 on this branch).
    "修复 README 里的一个错别字": "simple",
    "重构支付系统：迁移数据库、改造三个微服务接口、接入新的风控规则并上线到生产": "complex",
}
_DEGRADED_MARKERS = ("degraded", "降级")
_REASON_MARKERS = ("reason", "原因")
_TRIAGE_MARKERS = ("triage", "分诊", "s1")


def _mentions(texts, markers):
    return any(m in t for t in texts for m in markers)


def _has_degradation_record(value, context=()):
    """True when some mapping records a triage degradation with its reason.

    Field names are deliberately not fixed: a dict counts when its keys or
    string values mention ``degraded``/``降级``, it has a non-empty
    ``reason``/``原因``-like field, and ``triage``/``分诊``/``s1`` appears in
    its keys, string values or the keys leading to it (so an unrelated
    downgrade disclosure does not count).
    """
    if isinstance(value, dict):
        texts = [str(k).casefold() for k in value] + [
            v.casefold() for v in value.values() if isinstance(v, str)
        ]
        reasoned = any(
            any(m in str(k).casefold() for m in _REASON_MARKERS) and v
            for k, v in value.items()
        )
        if (
            reasoned
            and _mentions(texts, _DEGRADED_MARKERS)
            and _mentions(texts + list(context), _TRIAGE_MARKERS)
        ):
            return True
        return any(
            _has_degradation_record(v, context + (str(k).casefold(),))
            for k, v in value.items()
        )
    if isinstance(value, list):
        return any(_has_degradation_record(v, context) for v in value)
    return False


def _file_has_degradation_record(path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    try:
        return _has_degradation_record(json.loads(text))
    except ValueError:
        pass
    for line in text.splitlines():
        try:
            if _has_degradation_record(json.loads(line)):
                return True
        except ValueError:
            folded = [line.casefold()]
            if all(_mentions(folded, markers) for markers in (
                _DEGRADED_MARKERS, _REASON_MARKERS, _TRIAGE_MARKERS
            )):
                return True
    return False


def _vibe_fingerprint(root):
    vibe = root / ".vibe"
    return {
        p: p.read_bytes() for p in vibe.rglob("*") if p.is_file()
    } if vibe.is_dir() else {}


class _OutboundRecorder:
    """Records every outbound connection attempt and refuses it (service off)."""

    def __init__(self):
        self.calls = []

    def create_connection(self, address, *args, **kwargs):
        self.calls.append(("socket.create_connection", address))
        raise ConnectionRefusedError("triage gateway is off (test)")

    def urlopen(self, url, *args, **kwargs):
        self.calls.append(("urllib.request.urlopen", getattr(url, "full_url", url)))
        raise OSError("triage gateway is off (test)")


class ACE1EntryTriageTests(unittest.TestCase):
    """AC-E1：关掉分诊服务后入口请求按 S1 规则完成分诊并留降级记录；
    评测报告给出 accuracy、ECE、p95 三项数值，ECE ≤ 0.05。

    "Service off" is modelled by refusing every outbound connection."""

    def setUp(self):
        _isolate_user_dirs(self)
        temporary = tempfile.TemporaryDirectory(prefix="vg-triage-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assertEqual(run_cli(["init", "--confirm", "--json"], self.root).exit_code, 0)

    def _plan(self, request):
        recorder = _OutboundRecorder()
        with patch.object(socket, "create_connection", recorder.create_connection), \
                patch.object(urllib.request, "urlopen", recorder.urlopen):
            result = run_cli(["plan", "--request", request, "--json"], self.root)
        return result, recorder

    def test_ac_e1_gateway_down_entry_still_triages_by_s1_rules(self):
        for request, expected in _TRIAGE_REQUESTS.items():
            with self.subTest(request=request):
                result, _recorder = self._plan(request)
                self.assertEqual(result.exit_code, 0, result.payload)
                self.assertEqual(result.payload["route"], expected)

    def test_ac_e1_gateway_down_leaves_a_degradation_record(self):
        before = _vibe_fingerprint(self.root)
        result, recorder = self._plan(next(iter(_TRIAGE_REQUESTS)))
        after = _vibe_fingerprint(self.root)
        touched = sorted(p for p, data in after.items() if before.get(p) != data)
        on_disk = [p for p in touched if _file_has_degradation_record(p)]
        self.assertTrue(
            _has_degradation_record(result.payload) or on_disk,
            "AC-E1: no degradation record -- `vibe plan` with the triage "
            "service off returned no triage/分诊/S1 degraded/降级 entry with a reason "
            "(payload keys {}) and wrote none under .vibe (files written: {}; "
            "outbound attempts: {})".format(
                sorted(result.payload),
                [p.relative_to(self.root).as_posix() for p in touched],
                len(recorder.calls),
            ),
        )

    def test_ac_e1_evaluation_scaffold_reports_accuracy_ece_p95(self):
        scripts = REPO_ROOT / "scripts"
        candidates = sorted(
            p for p in (scripts.rglob("*") if scripts.is_dir() else [])
            if p.is_file() and "triage" in p.name.casefold()
            and "__pycache__" not in p.parts
        )
        self.assertEqual(
            len(candidates), 1,
            "AC-E1: expected exactly one triage evaluation scaffold under "
            "scripts/ (*triage*), found {}: {}; cannot produce "
            "accuracy/ECE/p95".format(
                len(candidates),
                [p.relative_to(REPO_ROOT).as_posix() for p in candidates],
            ),
        )
        completed = subprocess.run(
            [sys.executable, str(candidates[0]), "--json"], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        for key in ("accuracy", "ece", "p95"):
            self.assertIsInstance(report.get(key), (int, float), key)
        self.assertLessEqual(report["ece"], 0.05)


if __name__ == "__main__":
    unittest.main()
