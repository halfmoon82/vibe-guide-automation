import unittest
import tempfile
from pathlib import Path

from vibe_guide.cli import run_v4_sdd


class V4CliSddModeTests(unittest.TestCase):
    def test_sdd_first_selection_and_four_user_statuses(self):
        for internal, expected in (
            ("planned", "准备中"),
            ("running", "已启动"),
            ("retry_pending", "自动修复中"),
            ("blocked_design", "需要你决定"),
        ):
            result = run_v4_sdd({"workflow_version": 4, "status": internal})
            self.assertEqual(result["execution_mode"], "sdd_first")
            self.assertEqual(result["user_status"], expected)

    def test_optional_binding_evidence_does_not_create_repeated_prompts(self):
        result = run_v4_sdd({"workflow_version": 4, "status": "running", "nodes": [{"id": "n1", "status": "running"}]})
        self.assertEqual(result["prompts"], [])
        self.assertEqual(result["required_inputs"], [])

    def test_json_and_text_projection_share_same_payload(self):
        request = {"workflow_version": 4, "execution_mode": "sdd_first", "status": "accepted"}
        self.assertEqual(run_v4_sdd(request, False), run_v4_sdd(request, True))

    def test_local_orchestration_admits_ready_nodes_and_persists_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            request = {
                "workflow_version": 4, "execution_mode": "sdd_first", "orchestrate": True,
                "run_id": "cli-seam", "state_dir": directory,
                "nodes": [
                    {"id": "ready", "status": "ready", "developer_task_id": "dev", "reviewer_task_id": "rev"},
                    {"id": "timeout", "status": "running", "task_id": "dev-timeout"},
                ],
                "provider_observations": [{"node_id": "timeout", "kind": "provider_timeout"}],
            }
            result = run_v4_sdd(request)
            self.assertEqual(result["admitted_nodes"], ["ready"])
            self.assertEqual(result["node_effects"]["timeout"], "unknown")
            self.assertTrue((Path(directory) / "cli-seam" / "state.json").is_file())
            self.assertTrue((Path(directory) / "cli-seam" / "events.jsonl").is_file())
            self.assertEqual(result["nodes"][1]["status"], "blocked_unknown")
            persisted = (Path(directory) / "cli-seam" / "state.json").read_text(encoding="utf-8")
            self.assertIn('"status": "blocked_unknown"', persisted)

    def test_timeout_status_isolated_in_final_payload_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_v4_sdd({
                "workflow_version": 4, "orchestrate": True, "run_id": "cli-timeout", "state_dir": directory,
                "nodes": [{"id": "n1", "status": "running", "task_id": "dev"}],
                "provider_observations": [{"node_id": "n1", "status": "timeout"}],
            })
            self.assertEqual(result["nodes"][0]["status"], "blocked_unknown")
            self.assertIn("blocked_unknown", (Path(directory) / "cli-timeout" / "state.json").read_text(encoding="utf-8"))
            events = (Path(directory) / "cli-timeout" / "events.jsonl").read_text(encoding="utf-8").splitlines()
            isolation_events = [line for line in events if '"event": "v4_node_isolated"' in line and '"node_id": "n1"' in line]
            self.assertEqual(len(isolation_events), 1)
            self.assertFalse(any('"node_id": "other"' in line for line in events))

    def test_repeated_orchestration_keeps_one_isolation_event_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            request = {
                "workflow_version": 4, "orchestrate": True, "run_id": "cli-idempotent", "state_dir": directory,
                "nodes": [{"id": "n1", "status": "running", "task_id": "dev"}],
                "provider_observations": [{"node_id": "n1", "status": "timeout"}],
            }
            run_v4_sdd(request)
            run_v4_sdd(request)
            events = (Path(directory) / "cli-idempotent" / "events.jsonl").read_text(encoding="utf-8").splitlines()
            isolation_events = [line for line in events if '"event": "v4_node_isolated"' in line and '"node_id": "n1"' in line]
            self.assertEqual(len(isolation_events), 1)
            self.assertGreaterEqual(len(events), 3)
            self.assertEqual(sum('"event": "v4_admission"' in line for line in events), 2)


if __name__ == "__main__":
    unittest.main()
