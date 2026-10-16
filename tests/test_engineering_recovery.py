import tempfile
import unittest
from pathlib import Path

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor, _user_status, classify_provider_failure, recovery_action_for_failure
from vibe_guide.state import map_user_status
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.task_registry import load_task_binding


def _monitor_node(node_id):
    return DAGNode(
        node_id,
        node_id,
        [],
        [],
        "g1",
        {
            "files": [node_id + ".py"],
            "worker": "worker-" + node_id,
            "reviewer_worker": "reviewer-" + node_id,
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


class EngineeringRecoveryClassificationTests(unittest.TestCase):
    def test_five_recovery_classes(self):
        cases = {
            "dirty checkout": "repairable",
            "detached HEAD": "repairable",
            "timeout": "retryable",
            "disconnect": "retryable",
            "task creation failed": "retryable",
            "429 capacity": "capacity_wait",
            "writer identity conflict": "binding_unknown",
            "credential required": "binding_unknown",
            "product scope change": "binding_unknown",
            "deploy required": "binding_unknown",
            "irreversible security action": "binding_unknown",
        }
        for reason, expected in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(classify_provider_failure({"reason": reason})["kind"], expected)

    def test_unknown_fails_closed_as_binding_unknown(self):
        self.assertEqual(classify_provider_failure({"reason": "unexplained"})["kind"], "binding_unknown")

    def test_structured_external_decision_fields(self):
        for value in (
            {"credential_required": True}, {"login_required": True},
            {"permission_required": True}, {"permission_denied": True},
            {"kind": "external_decision"},
        ):
            with self.subTest(value=value):
                self.assertEqual(classify_provider_failure(value)["kind"], "external_decision")

    def test_plain_authorization_and_permission_errors_are_auto_recoverable(self):
        for reason in (
            "authorization digest mismatch",
            "permission denied writing .vibe/x",
        ):
            with self.subTest(reason=reason):
                classification = classify_provider_failure({"reason": reason})
                self.assertEqual(classification["kind"], "repairable")
                self.assertEqual(classification["legacy_kind"], "engineering")

    def test_user_status_uses_state_code_not_reason_keywords(self):
        for mapper in (map_user_status, _user_status):
            with self.subTest(mapper=mapper.__name__):
                record = {
                    "status": "blocked_unknown",
                    "reason": "permission denied writing .vibe/x",
                }
                self.assertEqual(mapper(record), "自动修复中")
                self.assertEqual(
                    mapper({"status": "running", "reason": "authorization digest mismatch"}),
                    "已启动",
                )

    def test_engineering_failure_exposes_a_recoverable_action(self):
        self.assertEqual(
            recovery_action_for_failure({"reason": "contract test execution failed"}),
            "repair_node",
        )
        self.assertEqual(
            recovery_action_for_failure({"reason": "provider timeout"}),
            "requeue_same_task",
        )

    def test_unknown_binding_requires_reconcile_before_dispatch(self):
        self.assertEqual(
            recovery_action_for_failure({"reason": "writer identity conflict"}),
            "reconcile_binding",
        )


class ReviewerReplacementRecoveryTests(unittest.TestCase):
    def test_write_violation_rework_dispatches_reviewer_successor(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary))
            (paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
            (paths.vibe / "state.json").write_text(
                '{"workflow_version": 2, "session_gate": "s0_required"}\n',
                encoding="utf-8",
            )
            save_contract(
                paths,
                build_contract(paths.root, provider="fake", host_id="local"),
            )
            capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")
            target = _monitor_node("n1")
            plan = Plan("plan-1", 1, "docs/prd.md", [target.id], "draft")
            card = build_authorization_card(plan, [target], capabilities)
            monitor = Monitor(paths, plan, [target])
            runner = FakeRunner(
                events={
                    ("n1", "developer"): [
                        ("delivered", {"evidence": "initial"}),
                        ("delivered", {"evidence": "rework"}),
                    ],
                    ("n1", "reviewer"): [
                        (
                            "review_finding",
                            {
                                "reviewer_write_violation": True,
                                "reviewer_modified_files": ["n1.py"],
                            },
                        )
                    ],
                }
            )

            snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
            original_developer = runner.start_calls[0]
            snapshot = monitor.tick(snapshot.run_id, runner)
            original_reviewer = runner.start_calls[1]
            snapshot = monitor.tick(snapshot.run_id, runner)
            snapshot = monitor.tick(snapshot.run_id, runner)

            successor = runner.start_calls[3]
            self.assertEqual(snapshot.nodes["n1"]["status"], "review")
            self.assertNotEqual(successor["task_id"], original_reviewer["task_id"])
            self.assertTrue(successor["successor"])
            self.assertFalse(successor["continuation"])
            self.assertEqual(successor["predecessor_task_id"], original_reviewer["task_id"])
            self.assertEqual(runner.start_calls[2]["task_id"], original_developer["task_id"])
            self.assertTrue(runner.start_calls[2]["continuation"])
            reviewer_binding = load_task_binding(
                paths, "n1", "reviewer", run_id=snapshot.run_id
            )
            self.assertEqual(reviewer_binding.task_id, successor["task_id"])
            self.assertEqual(reviewer_binding.successor_of, original_reviewer["task_id"])
            self.assertFalse(snapshot.nodes["n1"].get("reviewer_replacement_required"))


if __name__ == "__main__":
    unittest.main()
