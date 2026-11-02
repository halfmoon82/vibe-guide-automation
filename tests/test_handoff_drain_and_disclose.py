"""Platform handoff: drain the old platform first, disclose the dispatch change after.

PRD 5.2.6 card-and-handoff (decisions D1, D2).  Every test drives the public
CLI against the real provider mailbox, the way a supervisor would on the new
platform; nothing hand-edits ``.vibe/`` except where a test deliberately
builds a foreign project or an unstamped (pre-5.2.6) plan.
"""
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide.authorization import DISPATCH_TOPOLOGY_KEY
from vibe_guide.capability_contract import build_contract
from vibe_guide.handoff_rebind import STOP_EVIDENCE_FIELDS, project_identity_problem
from vibe_guide.protocols import SHIPPED_PROTOCOL_DIGESTS, load_protocol, published_protocol_digest
from vibe_guide.state import load_events
from vibe_guide.task_registry import TERMINAL_TASK_STATUSES, load_task_binding

from tests import test_integration_review_closeout_entry as closeout
from tests.test_platform_handoff_rebind import FIRST, PLAN, SECOND, HandoffCase

ROOT = Path(__file__).resolve().parent.parent


class DrainCase(HandoffCase):
    def second_running_on(self, adapter):
        """First node accepted; the second node's worker created and running on ``adapter``."""
        self.published_on(adapter)
        for _ in range(3):  # create, locate, visibility: the session is then running
            for action in self.store.pending():
                if action["issue_id"] == SECOND and action["operation"] != "wait":
                    self.store.complete(action["action_id"], closeout.MailboxClosesTheRunTests.reply(
                        action, self.waits, closeout.CLEARED_CLAIM))
            self.cli("resume", "--plan", PLAN)
        snapshot = self.snapshot()
        self.assertTrue(snapshot.nodes[SECOND].get("developer_identity"), snapshot.nodes[SECOND])
        return snapshot

    def files(self):
        names = ("state.json", "tasks.json", "events.jsonl")
        run = self.root / ".vibe" / "runs" / self.run_id
        plan = self.root / ".vibe" / "plans" / PLAN
        return {name: (run / name).read_bytes() for name in names} | {
            name: (plan / name).read_bytes() for name in ("nodes.json", "authorization-card.json", "authorization.json")
        }

    def register_stop(self, refusal, how="stopped_on_old_platform", **override):
        values = dict({"how_stopped": how, "note": "旧平台会话已在 2026-11-03 停止"}, **override)
        template = dict(refusal.payload["stop_evidence_templates"][0], **values)
        (self.root / ".vibe" / "handoff-stop.json").write_text(
            json.dumps(template, ensure_ascii=False), encoding="utf-8")
        return self.cli("reconcile", "--plan", PLAN, "--run-id", self.run_id,
                        "--evidence", ".vibe/handoff-stop.json")

    def live_bindings(self, node_id):
        tasks = json.loads((self.root / ".vibe" / "runs" / self.run_id / "tasks.json").read_text(encoding="utf-8"))
        return [item for item in tasks["bindings"]
                if item["issue_id"] == node_id and item["status"] not in TERMINAL_TASK_STATUSES]


class LiveWorkerRefusesTheHandoffTests(DrainCase):
    """Acceptance 2 and 3."""

    def test_refusal_names_the_node_both_drains_and_the_command_and_changes_nothing(self):
        before_snapshot = self.second_running_on("workbuddy")
        self.switch_platform("claude-code")
        before = self.files()
        refusal = self.handoff()
        self.assertEqual(refusal.payload["status"], "handoff_refused", refusal.payload)
        self.assertEqual(refusal.payload["reason_code"], "handoff_not_drained")
        self.assertEqual(refusal.payload["nodes"], [SECOND])
        worker = refusal.payload["live_workers"][0]
        self.assertEqual((worker["node_id"], worker["role"], worker["task_id"]),
                         (SECOND, "developer", before_snapshot.nodes[SECOND]["developer_identity"]))
        self.assertEqual([item["option"] for item in refusal.payload["drain_options"]],
                         ["wait_for_delivery", "register_stop_evidence"])
        command = refusal.payload["stop_evidence_command"]
        self.assertTrue(command.startswith("vibe reconcile --project-root "), command)
        self.assertIn("--plan " + PLAN, command)
        self.assertIn("--run-id " + self.run_id, command)
        self.assertEqual(refusal.payload["stop_evidence_fields"], list(STOP_EVIDENCE_FIELDS))
        self.assertIn(command, refusal.payload["reason"])
        # Nothing moved: run, task registry, nodes and card are byte-identical.
        self.assertEqual(self.files(), before)
        self.assertEqual(self.plan_card().agent_id, "workbuddy")

    def test_registering_stop_evidence_lets_the_rerun_succeed_with_one_live_binding(self):
        self.second_running_on("workbuddy")
        self.switch_platform("claude-code")
        refusal = self.handoff()
        registered = self.register_stop(refusal)
        self.assertNotEqual(registered.payload.get("status"), "blocked_unknown", registered.payload)
        old = load_task_binding(self.paths, SECOND, "developer", run_id=self.run_id)
        self.assertEqual(old.status, "stopped")
        stops = [e for e in load_events(self.paths, self.run_id) if e["event"] == "handoff_worker_stopped"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["data"]["predecessor_task_id"], old.task_id)

        result = self.handoff()
        self.assertNotEqual(result.payload["status"], "handoff_refused", result.payload)
        self.assertEqual(self.plan_card().agent_id, "claude-code")
        second = self.snapshot().nodes[SECOND]
        # Waiting on the new platform's successor create: a retry, not a doubt.
        self.assertEqual(second["handoff_superseded"], {"developer": old.task_id})
        self.assertNotIn("predecessor cannot be proven", str(second.get("reason")))
        self.assertEqual([(a["issue_id"], a["provider"], a["operation"]) for a in self.store.pending()],
                         [(SECOND, "claude-code-visible", "create")])
        # The successor create goes out on the new platform; serve it.
        self.serve(SECOND)
        self.assertEqual(self.snapshot().nodes[SECOND]["status"], "accepted")
        live = self.live_bindings(SECOND)
        self.assertLessEqual(len(live), 1, live)

    def test_successor_is_the_only_live_writer_right_after_the_handoff(self):
        self.second_running_on("workbuddy")
        self.switch_platform("claude-code")
        self.register_stop(self.handoff())
        self.handoff()
        for _ in range(4):  # successor create, locate, visibility
            for action in self.store.pending():
                if action["issue_id"] == SECOND and action["operation"] != "wait":
                    self.assertEqual(action["provider"], "claude-code-visible")
                    self.store.complete(action["action_id"], closeout.MailboxClosesTheRunTests.reply(
                        action, self.waits, closeout.CLEARED_CLAIM))
            self.cli("resume", "--plan", PLAN)
        live = self.live_bindings(SECOND)
        self.assertEqual(len(live), 1, live)
        self.assertEqual(live[0]["provider"], "claude-code-visible")

    def test_old_platform_unreachable_is_a_way_out(self):
        self.second_running_on("workbuddy")
        self.switch_platform("claude-code")
        refusal = self.handoff()
        registered = self.register_stop(refusal, how="old_platform_unreachable",
                                        note="WorkBuddy 已卸载，旧会话无法打开")
        self.assertNotEqual(registered.payload.get("status"), "blocked_unknown", registered.payload)
        result = self.handoff()
        self.assertNotEqual(result.payload["status"], "handoff_refused", result.payload)

    def test_stop_evidence_must_be_complete_and_name_the_current_task(self):
        self.second_running_on("workbuddy")
        self.switch_platform("claude-code")
        refusal = self.handoff()
        unfilled = self.register_stop(refusal, how="stopped_on_old_platform|old_platform_unreachable")
        self.assertEqual(unfilled.payload["status"], "blocked_unknown")
        wrong = self.register_stop(refusal, task_id="some-other-task")
        self.assertEqual(wrong.payload["status"], "blocked_unknown")
        self.assertIn("task_id", wrong.payload["reason"])
        template = dict(refusal.payload["stop_evidence_templates"][0], extra="x")
        (self.root / ".vibe" / "bad.json").write_text(json.dumps(template), encoding="utf-8")
        extra = self.cli("reconcile", "--plan", PLAN, "--run-id", self.run_id, "--evidence", ".vibe/bad.json")
        self.assertEqual(extra.payload["status"], "blocked_unknown")
        # Nothing registered: the rerun is still refused.
        self.assertEqual(self.handoff().payload["status"], "handoff_refused")

    def test_an_unreadable_registry_counts_as_live(self):
        self.second_running_on("workbuddy")
        self.switch_platform("claude-code")
        with mock.patch("vibe_guide.monitor_handoff.load_task_binding", side_effect=ValueError("corrupt")):
            refusal = self.handoff()
        self.assertEqual(refusal.payload["status"], "handoff_refused", refusal.payload)
        self.assertEqual(refusal.payload["live_workers"][0]["status"], "unknown")
        self.assertTrue(refusal.payload["stop_evidence_templates"])

    def test_a_delivered_worker_is_drained(self):
        self.published_on("workbuddy")
        # The ordinary path: the first node was delivered and accepted on the
        # old platform; the second node's create was never claimed.
        self.switch_platform("claude-code")
        result = self.handoff()
        self.assertNotEqual(result.payload["status"], "handoff_refused", result.payload)


class ProjectIdentityTests(DrainCase):
    def test_another_project_root_is_refused_as_a_project_difference(self):
        self.second_running_on("workbuddy")
        copy = Path(str(self.root) + "-copy")
        shutil.copytree(self.root, copy, symlinks=True)
        self.addCleanup(shutil.rmtree, copy, True)
        before = (copy / ".vibe" / "runs" / self.run_id / "state.json").read_bytes()
        from vibe_guide.cli import run_cli

        result = run_cli(["monitor", "--plan", PLAN, "--authorize", "AUTHORIZE",
                          "--platform-handoff", "--json"], copy)
        self.assertEqual(result.payload["status"], "handoff_refused", result.payload)
        self.assertEqual(result.payload["reason_code"], "project_identity_mismatch")
        self.assertIn("项目身份不同", result.payload["reason"])
        self.assertEqual((copy / ".vibe" / "runs" / self.run_id / "state.json").read_bytes(), before)

    def test_worktree_outside_the_root_is_a_project_difference(self):
        self.assertIsNone(project_identity_problem(Path("/p"), ["/p/.worktrees/a", "rel"], True))
        self.assertIn("/q/.worktrees/a", project_identity_problem(Path("/p"), ["/q/.worktrees/a"], True))
        self.assertIn("项目身份不同", project_identity_problem(Path("/p"), [], False))


class DispatchChangeIsDisclosedTests(DrainCase):
    """Acceptance 4: a stamped plan from Codex to Claude Code, visible becomes background."""

    def publish_on_codex(self):
        snapshot = self.published_on("codex")
        nodes = self.plan_nodes()
        self.assertIn(nodes[SECOND].contract[DISPATCH_TOPOLOGY_KEY], {"visible-sdd", "dual-visible"})
        return snapshot

    def to_claude_code_background(self):
        self.switch_platform("claude-code")
        # Claude Code's verified background launch rules background.
        rulings = {"claude-code": "background", "codex": "in_session_sdd"}
        return mock.patch("vibe_guide.cli._observed_topology_rulings",
                          lambda paths, card=None: dict(rulings))

    def test_handoff_succeeds_moves_the_stamp_and_discloses_on_event_and_card(self):
        self.publish_on_codex()
        with self.to_claude_code_background(), \
                mock.patch("vibe_guide.cli._public_runner", return_value=_IdleRunner()):
            result = self.handoff()
        self.assertNotIn(result.payload["status"], {"handoff_refused", "blocked_unknown", "blocked_invalid"},
                         result.payload)
        self.assertNotIn("contract_drift", json.dumps(result.payload, ensure_ascii=False))
        nodes = self.plan_nodes()
        self.assertEqual(nodes[SECOND].contract["adapter_id"], "claude-code")
        self.assertEqual(nodes[SECOND].contract[DISPATCH_TOPOLOGY_KEY], "background")
        self.assertEqual(nodes["integration-review"].contract[DISPATCH_TOPOLOGY_KEY], "background")
        # The accepted node's binding is history: never rewritten.
        self.assertEqual(nodes[FIRST].contract["adapter_id"], "codex")

        events = [e for e in load_events(self.paths, self.run_id) if e["event"] == "platform_handoff_disclosed"]
        self.assertEqual(len(events), 1)
        proof = events[0]["data"]["proof"]
        self.assertEqual((proof["from_platform"], proof["to_platform"]), ("codex", "claude-code"))
        change = next(item for item in proof["changes"] if item["node_id"] == SECOND)
        self.assertEqual(change["to_topology"], "background")
        self.assertIn("可见变后台", change["text"])

        card = self.plan_card()
        self.assertEqual(card.agent_id, "claude-code")
        entry = next(item for item in card.workers if item["node_id"] == SECOND)
        self.assertEqual((entry["topology"], entry["mode"]), ("background", "background"))
        self.assertTrue(any("可见变后台" in item for item in entry["limitations"]), entry["limitations"])
        reauthorized = [e for e in load_events(self.paths, self.run_id)
                        if e["event"] == "authorization_reauthorized"]
        self.assertEqual(reauthorized[-1]["data"]["change_reason"], "platform_handoff")
        self.assertEqual(reauthorized[-1]["data"]["changed_nodes"], [])


class _IdleRunner:
    """A runner that dispatches nothing: this test is about the card and the event."""

    provider = "claude-code-background"

    def start(self, *args, **kwargs):
        raise RuntimeError("not dispatched in this test")

    def __getattr__(self, name):
        raise AttributeError(name)


class RuleTextIsInEveryPlaceTests(unittest.TestCase):
    """The one exception is written where each reader looks (D1)."""

    def test_agents_md_section_6_and_12(self):
        text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        boundary = next(line for line in text.splitlines() if line.startswith("两类边界无论判据结果如何"))
        self.assertIn("跨平台交接", boundary)
        self.assertIn("只把差异写进交接事件和新授权卡", boundary)
        invalidation = next(line for line in text.splitlines() if line.startswith("任何产品设计、执行拓扑或任务可见性要求变化"))
        self.assertIn("跨平台交接引起的派发方式或可见性变化不使授权失效", invalidation)

    def test_prd_guide_section_4_and_7(self):
        text = load_protocol("prd-guide")
        s4 = text[text.index("## 4. 拆节点"):text.index("## 5. 产出与提交")]
        line4 = next(line for line in s4.splitlines() if "两类边界（新增第三方依赖" in line)
        self.assertIn("跨平台交接", line4)
        s7 = text[text.index("## 7. 什么时候才能打断产品经理"):]
        line7 = next(line for line in s7.splitlines() if line.startswith("**已批准判据**"))
        self.assertIn("--platform-handoff", line7)
        self.assertIn("handoff_worker_stopped", line7)

    def test_supervisor_card_decision_boundary(self):
        text = load_protocol("supervisor-card")
        line = next(item for item in text.splitlines() if item.startswith("- 只有改变 PRD 决策"))
        self.assertIn("平台交接改成后台只披露不问", line)

    def test_worker_prompt_carries_the_same_rule(self):
        source = (ROOT / "vibe_guide" / "runners" / "provider_action.py").read_text(encoding="utf-8")
        index = source.index("可见降级为后台不可见时，才在交付里附 decision_requests")
        self.assertIn("跨平台交接把可见改成后台只披露", source[index:index + 200])

    def test_edited_protocols_are_registered(self):
        """Acceptance 6: the digest registry follows the edited protocol files."""
        for name in ("prd-guide", "supervisor-card"):
            digest = published_protocol_digest(name)
            self.assertIn(digest, SHIPPED_PROTOCOL_DIGESTS[name])


if __name__ == "__main__":
    unittest.main()
