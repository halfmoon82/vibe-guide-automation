import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.test_daemon_heal import NODE, RUN, Base, FakeSupervisor, stalled
from vibe_guide import model_catalog
from vibe_guide.models import DAGNode
from vibe_guide.state import load_events


def node(tags):
    return DAGNode(id=NODE, title="t", depends_on=[], integration_after=[], parallel_group=None,
                   contract={"risk_tags": tags}, status="planned")


class Supervisor(FakeSupervisor):
    def __init__(self, tags):
        super().__init__(["running"])
        self.monitor = SimpleNamespace(nodes={NODE: node(tags)})


class TierModelTests(Base):
    def run_daemon(self, tags, **config):
        daemon = self.daemon(Supervisor(tags), assess=lambda: stalled(), **config)
        daemon.run(max_cycles=3)
        return [e for e in load_events(self.paths, RUN) if e["event"] == "model_tier_unresolved"]

    def test_deep_node_gets_the_deep_model(self):
        unresolved = self.run_daemon(["security"])
        self.assertEqual(self.sessions[0][2]["model"], "opus")
        self.assertEqual(unresolved, [])

    def test_standard_node_gets_the_standard_model(self):
        self.run_daemon([])
        self.assertEqual(self.sessions[0][2]["model"], "sonnet")

    def test_command_line_carries_the_tier_model(self):
        from vibe_guide import short_session
        model = self.run_daemon(["security"]) or self.sessions[0][2]["model"]
        command = short_session.build_command("claude-code", "p", model)
        self.assertEqual(command[command.index("--model") + 1], "opus")

    def test_unresolved_tier_uses_platform_default_and_records_once(self):
        choice = model_catalog.ModelChoice("claude-code", "deep", "default", None, "unknown", "probe", "boom")
        with mock.patch.object(model_catalog, "resolve", return_value=choice):
            unresolved = self.run_daemon(["security"], max_sessions=1)
        self.assertIsNone(self.sessions[0][2]["model"])
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["data"]["tier"], "deep")

    def test_resolver_exception_does_not_fail_the_repair(self):
        with mock.patch.object(model_catalog, "resolve", side_effect=RuntimeError("x")):
            unresolved = self.run_daemon(["security"], max_sessions=1)
        self.assertEqual(len(self.sessions), 1)
        self.assertIsNone(self.sessions[0][2]["model"])
        self.assertEqual(len(unresolved), 1)

    def test_explicit_model_wins_over_the_tier(self):
        self.run_daemon(["security"], model="haiku")
        self.assertEqual(self.sessions[0][2]["model"], "haiku")


if __name__ == "__main__":
    unittest.main()
