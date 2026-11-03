import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vibe_guide import supervisor as sv
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import run_dir

RUN = "run-" + "d" * 32
NODES = ("a", "b", "c")


def node():
    return {
        "status": "running", "user_status": "自动修复中", "developer_generation": 1,
        "review_generation": 0, "active_task": None, "evidence": [], "handoff_file": "",
        "status_file": "",
        "retryable_action": {"attempt": 1, "next_retry_at": 1.0, "last_observation_ref": "provider-pending"},
    }


class MailboxWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name), vibe_home=Path(self.tmp.name) / "home")
        (Path(self.tmp.name) / "home").mkdir()
        run_dir(self.paths, RUN, create=True)
        self.requests = self.paths.vibe / "provider-actions/requests"
        self.requests.mkdir(parents=True)
        self.snapshot = SimpleNamespace(nodes={n: node() for n in NODES}, tasks={})
        self.now = 1_800_000_000.0

    def request(self, node_id, provider, age_seconds=60, operation="create"):
        name = "action-{}-{}".format(provider, node_id)
        path = self.requests / (name + ".json")
        path.write_text(json.dumps({
            "schema_version": 1, "action_id": name, "operation": operation, "provider": provider,
            "run_id": RUN, "issue_id": node_id, "role": "developer", "generation": 1, "sequence": 0,
            "native_tool": "x", "request": {}, "request_digest": "d" * 64}), encoding="utf-8")
        os.utime(path, (self.now - age_seconds, self.now - age_seconds))

    def beat(self, **kwargs):
        with mock.patch.object(sv, "load_snapshot", return_value=self.snapshot), \
                mock.patch.object(sv, "load_events", return_value=[]):
            report = sv.beat_stall_report(self.paths, RUN, now=self.now, **kwargs)
            text = sv.render_stall_beat_text(report, RUN)
            sv.mailbox_wait_mark_reported(self.paths, RUN, report)
        return report, text

    def test_pending_requests_never_read_as_stalled(self):
        for provider in ("workbuddy-visible", "codex-app-visible"):
            for n in NODES:
                self.request(n, provider)
            for _ in range(6):
                report, text = self.beat()
                self.assertEqual(report["nodes"], {})
                self.assertNotIn("需要人处理", text)
                self.assertNotIn("需要监工排查", text)
                for n in NODES:
                    self.assertIn("信箱待服务：节点 {} 有 1 条请求".format(n), text)
                self.assertIn("已等 1 分钟", text)
            for n in NODES:
                (self.requests / "action-{}-{}.json".format(provider, n)).unlink()

    def test_sixty_minutes_escalates_once_without_repair(self):
        self.request("a", "workbuddy-visible", age_seconds=3600)
        report, text = self.beat()
        self.assertIn("已等满 60 分钟，需要监工服务信箱", text)
        self.assertTrue(text.startswith("信箱待服务"))
        self.assertEqual(report["nodes"], {})
        _, again = self.beat()
        self.assertIn("信箱待服务", again)
        self.assertNotIn("已等满 60 分钟", again)

    def test_unreadable_mailbox_is_unknown(self):
        with mock.patch("vibe_guide.adapters.task_provider.ProviderActionStore.pending",
                        side_effect=ValueError("boom")):
            _, text = self.beat()
        self.assertIn("读不到信箱", text)

    def test_background_requests_keep_the_stall_path(self):
        self.request("a", "claude-code-background")
        outcomes = [self.beat()[0] for _ in range(4)]
        self.assertEqual(outcomes[-1]["mailbox_waiting"], {})
        self.assertIn("a", outcomes[-1]["nodes"])


if __name__ == "__main__":
    unittest.main()
