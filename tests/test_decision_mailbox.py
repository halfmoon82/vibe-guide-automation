import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.decision_boundary import (
    DecisionMailbox,
    NotificationBridge,
    ingest_worker_decisions,
)
from vibe_guide.cli import UNKNOWN, run_cli
from vibe_guide.paths import ProjectPaths


class DecisionMailboxTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.home = Path(tempfile.mkdtemp())
        env = patch.dict(os.environ, {"VIBE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.paths = ProjectPaths.from_cwd(self.root)
        self.mailbox = DecisionMailbox(self.paths)

    def write_bridge(self, project, user=None):
        config_path = self.root / ".vibe" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({"notify_bridge": project}), encoding="utf-8")
        if user is not None:
            (self.home / "config.json").write_text(
                json.dumps({"notify_bridge": user}), encoding="utf-8"
            )

    def test_raise_from_supervisor_and_worker_share_one_durable_request(self):
        first = self.mailbox.raise_decision(
            run_id="run-1",
            node_id="issue-1",
            question="Use A or B?",
            options=[{"id": "A", "label": "A", "impact": "fast"}, {"id": "B", "label": "B", "impact": "safe"}],
            recommendation="A",
            evidence={"source": "supervisor"},
        )
        second = self.mailbox.ingest_worker_requests(
            "run-1",
            "issue-1",
            [{
                "question": "Use A or B?",
                "options": first["options"],
                "recommendation": "A",
                "evidence": {"source": "worker"},
            }],
        )[0]
        self.assertEqual(first["decision_id"], second["decision_id"])
        self.assertEqual(len(self.mailbox.list_pending("run-1")), 1)
        self.assertEqual(len(list((self.root / ".vibe" / "decisions" / "requests").glob("*.json"))), 1)

    def test_answer_is_rejected_when_option_digest_changes(self):
        request = self.mailbox.raise_decision(
            run_id="run-1", node_id="issue-1", question="Pick", options=["A", "B"]
        )
        path = self.root / ".vibe" / "decisions" / "requests" / (request["decision_id"] + ".json")
        changed = json.loads(path.read_text())
        changed["options"] = [{"id": "A", "label": "Changed", "impact": "new"}, {"id": "B", "label": "B", "impact": ""}]
        path.write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            self.mailbox.answer(request["decision_id"], "A", request_digest=request["request_digest"])

    def test_notify_bridge_is_disabled_by_default_and_failure_is_unknown(self):
        bridge = NotificationBridge(self.paths)
        self.assertEqual(bridge.notify({"decision_id": "dec-1"})["status"], "disabled")
        bridge = NotificationBridge(self.paths, {"enabled": True, "command": "/definitely/missing"})
        result = bridge.notify({"decision_id": "dec-1"})
        self.assertEqual(result["status"], "unknown")

    def test_malformed_result_is_not_treated_as_answered(self):
        request = self.mailbox.raise_decision(
            run_id="run-1", node_id="issue-1", question="Pick", options=["A", "B"]
        )
        result_path = self.root / ".vibe" / "decisions" / "results" / (request["decision_id"] + ".json")
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps({"choice": "A"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.mailbox.list_pending("run-1")

    def test_result_validation_rejects_stale_digest_and_invalid_choice(self):
        request = self.mailbox.raise_decision(
            run_id="run-1", node_id="issue-1", question="Pick", options=["A", "B"]
        )
        result_path = self.root / ".vibe" / "decisions" / "results" / (request["decision_id"] + ".json")
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps({
                "schema_version": 1,
                "decision_id": request["decision_id"],
                "request_digest": "stale",
                "choice": "C",
                "answered_at": 1.0,
                "answered_via": "test",
            }),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            self.mailbox.result(request["decision_id"])

    def test_worker_originated_decision_uses_configured_notification_bridge(self):
        self.write_bridge({"enabled": True}, {"command": "/usr/bin/true", "timeout_seconds": 10})
        with patch("vibe_guide.decision_boundary.subprocess.run") as run:
            run.return_value.returncode = 0
            records, notifications = ingest_worker_decisions(
                self.paths,
                "run-1",
                "issue-1",
                [{"question": "Pick", "options": ["A", "B"]}],
            )
        self.assertEqual(len(records), 1)
        self.assertEqual(notifications, [{"status": "accepted", "evidence": "notify_bridge:exit0"}])
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/true")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_project_config_cannot_name_the_bridge_command(self):
        # Integrated PRD N8 / AC-D6: a cloned repository must not make this
        # machine run a program.
        self.write_bridge({"enabled": True, "command": "/usr/bin/true"},
                          {"command": "/usr/bin/true"})
        with patch("vibe_guide.decision_boundary.subprocess.run") as run:
            _records, notifications = ingest_worker_decisions(
                self.paths, "run-1", "issue-1",
                [{"question": "Pick", "options": ["A", "B"]}],
            )
        run.assert_not_called()
        self.assertEqual(notifications[0]["status"], "unknown")
        self.assertIn("user-level", notifications[0]["reason"])

    def test_user_command_needs_project_switch(self):
        self.write_bridge({"enabled": False}, {"command": "/usr/bin/true"})
        self.assertEqual(NotificationBridge(self.paths).notify({"decision_id": "dec-1"})["status"], "disabled")
        (self.root / ".vibe" / "config.json").write_text("{}", encoding="utf-8")
        self.assertEqual(NotificationBridge(self.paths).notify({"decision_id": "dec-1"})["status"], "disabled")

    def test_enabled_without_user_command_is_unknown(self):
        self.write_bridge({"enabled": True})
        result = NotificationBridge(self.paths).notify({"decision_id": "dec-1"})
        self.assertEqual(result["status"], "unknown")

    def test_cli_reports_notification_bridge_failure_as_unknown_while_request_stays_pending(self):
        (self.root / ".project-root").write_text("fixture\n", encoding="utf-8")
        self.write_bridge({"enabled": True}, {"command": "/definitely/missing"})

        result = run_cli(
            [
                "decision-raise", "--json", "--run-id", "run-1", "--node", "issue-1",
                "--question", "Pick", "--options", "A,B",
            ],
            self.root,
        )

        self.assertEqual(result.exit_code, UNKNOWN)
        self.assertEqual(result.payload["status"], "unknown")
        self.assertEqual(result.payload["notification"]["status"], "unknown")
        self.assertEqual(len(self.mailbox.list_pending("run-1")), 1)


if __name__ == "__main__":
    unittest.main()
