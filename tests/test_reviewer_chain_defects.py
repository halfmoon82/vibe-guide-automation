"""Reviewer-chain defects D1-D4: each test failed before its fix."""
import hashlib
import unittest
from pathlib import Path

from vibe_guide.dag import INTEGRATION_REVIEW_NODE_ID
from vibe_guide.models import DAGNode, Plan

from tests.test_integrated_prd_acceptance import FULL, V42_STATE, _ProjectCase
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.monitor import Monitor
from vibe_guide.runners.fake import FakeRunner


class IntegrationReviewTierTests(_ProjectCase):
    state = V42_STATE

    def test_profileless_integration_node_resolves_deep_model(self):
        """D1: no worker_profile dict must not fall back to the default model."""
        node = DAGNode(
            INTEGRATION_REVIEW_NODE_ID, "review", [], [], "integration",
            {"files": ["a.py"], "adapter_id": "claude-code",
             "worker": "integration-reviewer", "worktree": ".worktrees/ir"},
            "ready",
        )
        plan = Plan("plan-1", 1, "docs/prd.md", [node.id], "draft",
                    complexity_band="complex")
        monitor = Monitor(self.paths, plan, [node])
        contract = dict(node.contract)
        monitor._apply_model_tier(None, node, contract, {}, "reviewer")
        profile = contract.get("worker_profile")
        self.assertIsInstance(profile, dict)
        self.assertEqual(profile["model"], "opus")
        self.assertEqual(profile["selection_basis"]["model_tier"], "deep")


if __name__ == "__main__":
    unittest.main()


class EvidenceWordingTests(unittest.TestCase):
    SENTENCE = (
        "evidence 本身就是这四个键：findings、iteration_compatibility、"
        "test_runtime_delivery、out_of_scope，不再套一层 claim。"
    )

    def _contract_output(self):
        from vibe_guide.dag import append_integration_review_node
        business = DAGNode("n1", "n1", [], [], "g1", {"files": ["a.py"]}, "ready")
        plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft",
                    spec_path="spec.md", complexity_band="complex", nodes=[business],
                    integration_contract={
                        "iteration_context": {"kind": "iteration", "based_on": ["V4"]},
                        "compatibility_scope": ["V4 API"],
                        "agentsmd_acceptance_refs": ["AGENTS.md#8"],
                        "integration_acceptance_contract": {"checks": ["all"]},
                        "unverified_or_excluded": ["real provider lifecycle"],
                    })
        plan = append_integration_review_node(plan)
        node = [n for n in plan.nodes if n.id == INTEGRATION_REVIEW_NODE_ID][0]
        return node.contract["output"]

    def test_dag_contract_and_prd_guide_use_the_same_sentence(self):
        """D3: the contract must not read as evidence.claim.{...}."""
        guide = (Path(__file__).resolve().parents[1]
                 / "vibe_guide" / "protocols" / "prd-guide.md").read_text(encoding="utf-8")
        output = self._contract_output()
        self.assertIn(self.SENTENCE, output)
        self.assertIn(self.SENTENCE, guide)
        self.assertNotIn("a claim with", output)


# ---------------------------------------------------------------------------
# D2 / D4 share a one-node dual-visible fixture.
# ---------------------------------------------------------------------------
import tempfile
from unittest import mock

from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.models import AgentCapabilities
from vibe_guide.contracts import RunHandle
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import load_events, load_snapshot

from tests.test_acceptance_rejection_escalation import _business_node, _reviewer_evidence

REVIEWER_SECRET = "REVIEWER-SUPPLIED-TEXT-7f3a"


class NoticeRunner(FakeRunner):
    """FakeRunner plus the optional session-notice hook."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.notices = []

    def send_notice(self, handle, message):
        self.notices.append((handle.run_id, message))


class _OneNodeCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = ProjectPaths(Path(temporary.name))
        (self.paths.vibe).mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8"
        )
        save_contract(self.paths, build_contract(self.paths.root, provider="fake", host_id="local"))
        caps = AgentCapabilities("fake", True, True, True, True, True, "full")
        nodes = [_business_node("n1")]
        plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        card = build_authorization_card(plan, nodes, caps)
        self.monitor = Monitor(self.paths, plan, nodes)
        self.record = authorize(card, "AUTHORIZE")

    def _to_review(self, reviewer_events):
        runner = NoticeRunner(events={
            ("n1", "developer"): [("complete", {"evidence": "delivery"})],
            ("n1", "reviewer"): list(reviewer_events),
        })
        snapshot = self.monitor.start(self.record, runner)
        snapshot = self.monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["n1"]["status"], "review")
        return snapshot, runner


class RejectionNoticeTests(_OneNodeCase):
    def test_rejected_delivery_notifies_same_reviewer_session_with_fixed_text(self):
        """D2: a format rejection reaches the reviewer and is visible to the supervisor."""
        snapshot, runner = self._to_review([
            ("accepted", {"delivery_evidence": {"thread_status": "complete",
                                                "note": REVIEWER_SECRET}}),
        ])
        handle = snapshot.handles["n1"]
        snapshot = self.monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["n1"]["status"], "review")
        self.assertEqual(len(runner.notices), 1)
        target, message = runner.notices[0]
        self.assertEqual(target, handle)
        self.assertNotIn(REVIEWER_SECRET, message)
        self.assertIn("out_of_scope", message)
        from vibe_guide.monitor import ACCEPTANCE_REJECTION_NOTICE
        self.assertEqual(message, ACCEPTANCE_REJECTION_NOTICE)
        events = [e for e in load_events(self.paths, snapshot.run_id)
                  if e["event"] == "acceptance_correction_notified"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["node_id"], "n1")
        self.assertNotIn(REVIEWER_SECRET, str(events[0]))


class RepairAfterRejectionTests(_OneNodeCase):
    def test_repair_after_one_rejection_with_dead_reviewer_dispatches_replacement(self):
        """D4: below the escalation limit repair must not park the node blocked_unknown."""
        from vibe_guide.task_registry import load_task_binding
        snapshot, runner = self._to_review([
            ("accepted", {"delivery_evidence": {"thread_status": "complete"}}),
        ])
        snapshot = self.monitor.tick(snapshot.run_id, runner)
        current = snapshot.nodes["n1"]
        self.assertEqual(current["acceptance_format_rejections"]["count"], 1)
        # The reviewer session ended after delivering, but nothing wrote that
        # to the registry: the binding still reads "review" (real-run shape).
        binding = load_task_binding(self.paths, "n1", "reviewer", run_id=snapshot.run_id)
        self.assertEqual(binding.status, "review")
        with mock.patch("vibe_guide.worker_liveness.session_ended", return_value=True):
            result = self.monitor.repair_node("n1", snapshot.run_id)

        after = load_snapshot(self.paths, snapshot.run_id).nodes["n1"]
        self.assertEqual(after["status"], "rework", result)
        self.assertNotEqual(after.get("recovery_action"), "reconcile_binding")
        retry = after.get("retryable_action") or {}
        self.assertEqual(retry.get("role"), "reviewer")
        self.assertIs(retry.get("successor"), True)
        self.assertIs(retry.get("pending_schedule"), True)

    def test_repair_keeps_reconcile_when_reviewer_session_may_still_be_live(self):
        """A reviewer not proven ended still parks for reconcile_binding."""
        snapshot, runner = self._to_review([
            ("accepted", {"delivery_evidence": {"thread_status": "complete"}}),
        ])
        snapshot = self.monitor.tick(snapshot.run_id, runner)
        with mock.patch("vibe_guide.worker_liveness.session_ended", return_value=False):
            self.monitor.repair_node("n1", snapshot.run_id)
        after = load_snapshot(self.paths, snapshot.run_id).nodes["n1"]
        self.assertEqual(after["status"], "blocked_unknown")
        self.assertEqual(after.get("recovery_action"), "reconcile_binding")


class RealRunnerNoticeTests(unittest.TestCase):
    """P1: the notice must exist on the runners that run in production."""

    def test_claude_bg_runner_resumes_same_session_without_bumping_generation(self):
        import tempfile
        from vibe_guide.runners.claude_bg import ClaudeBgRunner
        from tests.test_claude_bg_runner import FakeLauncher, UUID

        with tempfile.TemporaryDirectory() as tmp:
            launcher = FakeLauncher(state="idle")
            runner = ClaudeBgRunner(
                lambda c, w: launcher, delivery_reader=lambda claims: None,
                paths=ProjectPaths(Path(tmp)),
            )
            handle = runner.start(
                {"node_id": "n1", "role": "reviewer", "run_id": "r1",
                 "generation": 1, "contract_path": "/c.md"}, Path("/tmp"))
            runner.send_notice(handle, "fixed text")
            self.assertEqual(launcher.calls[-1], ("resume", UUID, "fixed text"))
            self.assertEqual(runner._session(handle)["claims"]["generation"], 1)

    def test_claude_bg_runner_does_not_interrupt_a_working_session(self):
        import tempfile
        from vibe_guide.runners.claude_bg import ClaudeBgRunner
        from tests.test_claude_bg_runner import FakeLauncher

        with tempfile.TemporaryDirectory() as tmp:
            launcher = FakeLauncher(state="working")
            runner = ClaudeBgRunner(
                lambda c, w: launcher, delivery_reader=lambda claims: None,
                paths=ProjectPaths(Path(tmp)),
            )
            handle = runner.start(
                {"node_id": "n1", "role": "reviewer", "run_id": "r1",
                 "generation": 1, "contract_path": "/c.md"}, Path("/tmp"))
            with self.assertRaises(Exception):
                runner.send_notice(handle, "fixed text")
            self.assertFalse([c for c in launcher.calls if c[0] in ("resume", "stop")])

    def test_provider_action_runner_queues_resume_action_without_touching_wait_state(self):
        import tempfile
        from unittest.mock import patch
        from vibe_guide.runners.provider_action import ProviderActionRunner

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".vibe").mkdir()
            (root / ".vibe" / "state.json").write_text("{}", encoding="utf-8")
            with patch("vibe_guide.adapters.task_provider.require_entry"):
                runner = ProviderActionRunner(ProjectPaths(root), "claude-code", "claude-code-visible")
                handle = RunHandle("h1")
                metadata = {
                    "node_id": "n1", "role": "reviewer", "run_id": "r1", "task_id": "t1",
                    "host": "local", "generation": 1, "pending_action": None,
                    "pending_operation": None,
                }
                runner.store._atomic(runner._handle_path("h1"), dict(metadata))
                runner.send_notice(handle, "fixed text")
                runner.send_notice(handle, "fixed text")
                after = runner.store._read(runner._handle_path("h1"))
            self.assertIsNone(after["pending_action"])
            requests = list((root / ".vibe" / "provider-actions").rglob("*.json"))
            texts = [p.read_text(encoding="utf-8") for p in requests]
            self.assertTrue(any("fixed text" in t and "t1" in t for t in texts))
