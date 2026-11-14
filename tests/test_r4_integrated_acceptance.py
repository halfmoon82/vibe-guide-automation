"""Shared worker entry and monitor acceptance must agree about blockers."""
import json
import tempfile
import unittest
from pathlib import Path

from tests import test_monitor as monitor_fixture
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import load_events
from vibe_guide.task_registry import load_task_binding
from tests.test_integrated_prd_acceptance import _ProjectCase, V42_STATE
from tests.test_model_tiers import _node as tier_node
import os
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.models import Plan, AgentCapabilities
from vibe_guide.monitor import Monitor
from vibe_guide.runners.provider_action import ProviderActionRunner


class BlockedPromptTests(unittest.TestCase):
    def test_original_role_continuation_and_rework_include_executable_blocker_guidance(self):
        for role in ("developer", "reviewer"):
            for generation in (1, 2, 3):
                prompt = ProviderActionRunner._continuation_prompt({
                    "node_id": "n1", "run_id": "run-1", "role": role,
                    "generation": generation})
                self.assertIn("blocked_engineering", prompt)
                self.assertIn("implementation_complete", prompt)
                self.assertIn("blocked-engineering-examples.md", prompt)
                self.assertIn("--role " + role, prompt)


def blocked_payload(event):
    return {"event": event, "implementation_complete": False,
            "status": "blocked", "delivery_evidence": {
                "completion_marker": "DONE", "delivery_path": "delivery.md",
                "thread_status": "completed"}}


class InvalidModelMapDispatchTests(_ProjectCase):
    state = V42_STATE

    def test_invalid_map_after_authorization_cannot_dispatch_a_default_reviewer(self):
        node = tier_node("n1", files=["a.py"], adapter_id="claude-code")
        monitor, runner, snapshot = self._start([node], {
            ("n1", "developer"): [("complete", {"evidence": "developer delivery"})]})
        config = Path(os.environ["VIBE_HOME"]) / "config.json"
        config.write_text(json.dumps({"model_map": {"codex": {"normal": ["invalid-tier"]}}}))
        for _ in range(3):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual([call for call in runner.start_calls if call["role"] == "reviewer"], [])
        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")
        records = [event for event in load_events(self.paths, snapshot.run_id)
                   if event["data"].get("reason_code") == "model_map_invalid"]
        self.assertTrue(records)

    def test_missing_adapter_compatibility_node_uses_the_same_card_and_dispatch_model(self):
        node = tier_node("n1", files=["a.py"])
        config = Path(os.environ["VIBE_HOME"]) / "config.json"
        config.write_text(json.dumps({"model_map": {"codex": {"standard": ["synthetic-writer"]}}}))
        plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        caps = AgentCapabilities("codex", True, True, True, True, True, "full")
        card = build_authorization_card(plan, [node], caps)
        self.assertEqual(card.to_dict()["model_tiers"][0]["resolved_model"], "synthetic-writer")
        runner = FakeRunner()
        Monitor(self.paths, plan, [node]).start(authorize(card, "AUTHORIZE"), runner)
        self.assertEqual(runner.start_calls[0]["worker_profile"]["model"], "synthetic-writer")


class BoundWorkerBlockerTests(unittest.TestCase):
    def entry(self, event, *, missing_root):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.paths = ProjectPaths(root)
        self.store = ProviderActionStore(self.paths)
        self.action = self.store.request(
            operation="wait", provider="codex-app-visible", run_id="run-1",
            issue_id="n1", role="developer", generation=1,
            native_tool="wait_threads", request={
                "targets": [{"threadId": "writer-1", "hostId": "local"}],
                "child_binding": {"parent_run_id": "run-1", "plan_revision": "1",
                    "authorization_digest": "a" * 64, "node_id": "n1",
                    "role": "developer", "writer": "writer-1",
                    "worktree": str(root / "missing") if missing_root else str(root),
                    "branch": "node/n1", "allowlist": ["n1.py"],
                    "worker_profile": {}, "capability_contract_digest": "b" * 64}})
        return run_cli(["worker-deliver", "--run-id", "run-1", "--node", "n1",
                        "--role", "developer", "--generation", "1",
                        "--payload", json.dumps(blocked_payload(event)), "--json"], root)

    def test_bound_blocker_is_consumed_even_when_writer_root_is_missing(self):
        for missing in (False, True):
            with self.subTest(missing_root=missing):
                result = self.entry("blocked_engineering", missing_root=missing)
                self.assertEqual(result.exit_code, 0, result.payload)
                receipt = self.store.result(self.action["action_id"])
                self.assertEqual(receipt["event"], "blocked_engineering")
                self.assertFalse(receipt["implementation_complete"])
                self.assertFalse((self.paths.root / "missing").exists())

    def test_blocked_accepted_is_rejected_before_archive_or_wait_completion(self):
        result = self.entry("accepted", missing_root=False)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIsNone(self.store.result(self.action["action_id"]))
        self.assertIsNone(self.store.archived_payload("run-1", "n1", "developer", 1))

    def test_completed_delivery_still_requires_writer_landing(self):
        # Removing the whole landing gate would make this unsafe payload pass.
        self.entry("blocked_engineering", missing_root=True)
        payload = blocked_payload("delivered")
        del payload["implementation_complete"]
        del payload["status"]
        with self.assertRaisesRegex(ValueError, "worktree"):
            self.store.record_worker_delivery("run-1", "n1", "developer", payload, 1)


class BlockedAcceptanceChainTests(unittest.TestCase):
    def check_blocked_reviewer(self, *, archive_only=False, replay=False):
        fixture = monitor_fixture.MonitorTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        monitor, authorization = fixture.authorized_monitor([
            monitor_fixture.node("up"), monitor_fixture.node("down", ["up"])])
        payload = blocked_payload("accepted")
        payload["evidence"] = "technical clearance"
        projected = {"evidence": "technical clearance"} if archive_only else payload
        runner = FakeRunner({("up", "developer"): [("delivered", {"evidence": "done"})],
                             ("up", "reviewer"): [("accepted", projected)]})
        snapshot = monitor.start(authorization, runner)
        monitor.tick(snapshot.run_id, runner)
        original = load_task_binding(fixture.paths, "up", "reviewer", run_id=snapshot.run_id)
        if archive_only:
            directory = fixture.paths.vibe / "provider-actions" / "deliveries"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / (snapshot.run_id + "-up-reviewer-g1.json")).write_text(json.dumps({
                "run_id": snapshot.run_id, "issue_id": "up", "role": "reviewer",
                "generation": 1, "payload": payload}), encoding="utf-8")
        if replay:
            from vibe_guide.contracts import RunEvent
            from vibe_guide.state import load_snapshot, append_event
            current = load_snapshot(fixture.paths, snapshot.run_id)
            active = current.nodes["up"]["active_task"]
            append_event(fixture.paths, RunEvent("accepted", {
                **projected, "run_id": snapshot.run_id, "node_id": "up",
                "contract_digest": current.nodes["up"]["contract_digest"],
                "authorization_epoch": current.authorization_digest}),
                {**active, "authorization_digest": current.authorization_digest,
                 "node_contract_digest": current.node_contract_digest})
            result = monitor.resume(snapshot.run_id, runner)
        else:
            result = monitor.tick(snapshot.run_id, runner)
        self.assertNotEqual(result.nodes["up"]["status"], "accepted")
        self.assertIsNone(result.nodes["up"]["acceptance"])
        self.assertEqual([call["node_id"] for call in runner.start_calls], ["up", "up"])
        current = load_task_binding(fixture.paths, "up", "reviewer", run_id=snapshot.run_id)
        self.assertEqual((current.task_id, current.generation), (original.task_id, original.generation))
        if not replay:
            self.assertFalse(any(event["event"] == "accepted" for event in load_events(fixture.paths, snapshot.run_id)))

    def test_blocked_reviewer_cannot_accept_or_unlock_downstream(self):
        self.check_blocked_reviewer()

    def test_archived_blocker_survives_runner_projection(self):
        self.check_blocked_reviewer(archive_only=True)

    def test_legacy_blocked_acceptance_replay_cannot_unlock_downstream(self):
        self.check_blocked_reviewer(archive_only=True, replay=True)
