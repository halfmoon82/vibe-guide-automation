import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.contracts import RunEvent
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor, classify_provider_failure
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.supervisor import Supervisor
from vibe_guide.adapters.task_provider import ProviderPending, ProviderUnavailable
from vibe_guide.adapters.task_provider import ProviderActionStore


def _node(node_id="n1"):
    return DAGNode(
        node_id, node_id, [], [], "control",
        {"files": [node_id + ".py"], "worker": "worker-" + node_id,
         "worktree": ".worktrees/" + node_id, "branch": "branch-" + node_id},
        "ready",
    )


class ProviderSelfHealingTests(unittest.TestCase):
    def test_failure_classification_covers_engineering_and_external(self):
        engineering = ["timeout", "429 rate limit", "pending setup", "worker exited",
                       "snapshot interrupted", "disconnect", "empty response", "branch drift"]
        for value in engineering:
            with self.subTest(value=value):
                self.assertEqual(classify_provider_failure({"reason": value})["kind"], "engineering")
        external = ["login required", "credential missing", "system permission denied",
                    "remote approval required"]
        for value in external:
            with self.subTest(value=value):
                self.assertEqual(classify_provider_failure({"reason": value})["kind"], "external")
        self.assertEqual(classify_provider_failure({"reason": "unexplained"})["kind"], "unknown")

    def test_model_reasoning_validation_is_engineering(self):
        result = classify_provider_failure({
            "reason": (
                'create_thread could not validate reasoning effort "high" '
                'for model "gpt-6.1-sol"'
            )
        })
        self.assertEqual(result["kind"], "engineering")

    def test_create_parameter_failure_retries_without_blocking(self):
        with tempfile.TemporaryDirectory() as root:
            paths, node, plan, record = self._authorized(root)

            class ParameterRejectingRunner(FakeRunner):
                def start(self, contract, worktree):
                    raise ProviderUnavailable(
                        'create_thread could not validate reasoning effort "high" '
                        'for model "gpt-6.1-sol"'
                    )

            monitor = Monitor(paths, plan, [node])
            with patch("vibe_guide.monitor.require_entry", return_value=None):
                snapshot = monitor.start(record, ParameterRejectingRunner())
            current = snapshot.nodes[node.id]
            self.assertEqual(current["status"], "retry_pending")
            self.assertEqual(current["retryable_action"]["role"], "developer")
            self.assertTrue(current["retryable_action"]["same_task_required"])
            self.assertFalse(current["retryable_action"]["successor"])

    def test_stale_resume_action_is_converged_after_newer_acceptance(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            store = ProviderActionStore(paths)
            action = store.request(
                operation="resume",
                provider="codex-app-visible",
                run_id="run-stale",
                issue_id="n1",
                role="developer",
                generation=3,
                native_tool="codex_app__send_message_to_thread",
                request={"authorization_digest": "a" * 64, "threadId": "thread-1"},
            )
            evidence = {
                "run_id": "run-stale",
                "authorization_digest": "a" * 64,
                "nodes": {
                    "n1": {
                        "status": "accepted",
                        "accepted_generation": 4,
                        "roles": {
                            "developer": {"generation": 4, "accepted_generation": 4},
                            "reviewer": {"generation": 0, "accepted_generation": 0},
                        },
                    }
                },
            }
            changed = store.reconcile_stale("run-stale", evidence)
            self.assertEqual(changed, [action["action_id"]])
            self.assertEqual(store.pending(run_id="run-stale"), [])
            audit = json.loads((paths.vibe / "provider-actions" / "stale" / (action["action_id"] + ".json")).read_text())
            self.assertEqual(audit["reason"], "accepted_generation_superseded")

    def _authorized(self, root):
        paths = ProjectPaths(Path(root))
        paths.vibe.mkdir(parents=True, exist_ok=True)
        (paths.vibe / "state.json").write_text(
            '{"workflow_version": 4, "execution_mode": "sdd_first", '
            '"session_gate": "s0_required", "capability_contract_required": true}\n'
        )
        save_contract(paths, build_contract(paths.root, provider="fake", host_id="local"))
        node = _node()
        plan = Plan("v42-self-heal", 1, "docs/prd.md", [node.id], "draft")
        caps = AgentCapabilities("fake", True, True, True, True, True, "full")
        record = authorize(build_authorization_card(plan, [node], caps), "AUTHORIZE")
        return paths, node, plan, record

    def test_timeout_retries_same_task_and_cursor(self):
        with tempfile.TemporaryDirectory() as root:
            paths, node, plan, record = self._authorized(root)
            runner = FakeRunner(events={(node.id, "developer"): [("timeout", {"reason": "provider timeout"})]})
            monitor = Monitor(paths, plan, [node])
            with patch("vibe_guide.monitor.require_entry", return_value=None):
                first = monitor.start(record, runner)
                task_id = first.nodes[node.id]["active_task"]["task_id"]
                second = monitor.tick(first.run_id, runner)
            action = second.nodes[node.id]["retryable_action"]
            self.assertEqual(second.nodes[node.id]["status"], "retry_pending")
            self.assertEqual(action["task_id"], task_id)
            self.assertTrue(action["same_task_required"])
            self.assertIn("attempt", action)
            self.assertIn("next_retry_at", action)
            self.assertIn("binding_digest", action)
            self.assertIn("last_observation_ref", action)

    def test_pending_client_thread_never_becomes_formal_thread(self):
        with tempfile.TemporaryDirectory() as root:
            paths, node, plan, record = self._authorized(root)
            class PendingRunner(FakeRunner):
                def start(self, contract, worktree):
                    raise ProviderPending("pending setup clientThreadId=setup-1")
            runner = PendingRunner()
            monitor = Monitor(paths, plan, [node])
            with patch("vibe_guide.monitor.require_entry", return_value=None):
                snapshot = monitor.start(record, runner)
            self.assertIsNone(snapshot.nodes[node.id].get("active_task"))
            self.assertIsNone(snapshot.nodes[node.id].get("thread_id"))
            self.assertIsNotNone(snapshot.nodes[node.id].get("retryable_action"))

    def test_worker_exit_is_repaired_without_successor(self):
        with tempfile.TemporaryDirectory() as root:
            paths, node, plan, record = self._authorized(root)
            runner = FakeRunner(events={(node.id, "developer"): [("failed", {"reason": "worker exited"}), ("timeout", {"reason": "retry"})]})
            monitor = Monitor(paths, plan, [node])
            with patch("vibe_guide.monitor.require_entry", return_value=None):
                started = monitor.start(record, runner)
                snapshot = Supervisor(paths, monitor, runner, started.run_id).watch(interval=0, max_cycles=2)
            self.assertEqual(snapshot.nodes[node.id]["developer_identity"], started.nodes[node.id]["developer_identity"])
            self.assertFalse(snapshot.nodes[node.id].get("successor_created", False))

    def test_external_auth_boundary_does_not_consume_engineering_retry(self):
        with tempfile.TemporaryDirectory() as root:
            paths, node, plan, record = self._authorized(root)
            runner = FakeRunner(events={(node.id, "developer"): [("failed", {"reason": "login required", "credential_required": True})]})
            monitor = Monitor(paths, plan, [node])
            with patch("vibe_guide.monitor.require_entry", return_value=None):
                started = monitor.start(record, runner)
                snapshot = monitor.tick(started.run_id, runner)
            self.assertEqual(snapshot.nodes[node.id]["status"], "blocked_unknown")
            self.assertIsNone(snapshot.nodes[node.id]["retryable_action"])


if __name__ == "__main__":
    unittest.main()
