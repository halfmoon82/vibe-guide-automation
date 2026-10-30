import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_claude_bg_launcher import FakeClaude, LauncherCase
from vibe_guide.adapters.claude_bg import (
    ClaudeBgUnknown,
    ClaudeBgWorkspaceUntrusted,
    CommandResult,
    WORKSPACE_NOT_TRUSTED,
)
from vibe_guide.monitor import Monitor, classify_provider_failure

NOT_TRUSTED = ("Workspace not trusted. Run `claude` in {path} once and accept the trust "
               "prompt, then retry.")


class UntrustedLaunchTests(LauncherCase):
    def untrusted(self):
        text = NOT_TRUSTED.format(path=self.worktree)
        original = self.fake.__call__

        def run(argv, cwd, timeout):
            if "--bg" in argv:
                self.fake.calls.append((list(argv), Path(cwd)))
                return CommandResult(1, "", text)
            return original(argv, cwd, timeout)
        self.fake = mock.Mock(side_effect=run)
        self.fake.calls = []
        self.fake.__call__ = run

    def launch(self):
        self.untrusted()
        return self.launcher().launch("developer", "n1", self.contract)

    def test_untrusted_workspace_is_a_named_failure_not_a_generic_unknown(self):
        with self.assertRaises(ClaudeBgWorkspaceUntrusted) as caught:
            self.launch()
        error = caught.exception
        self.assertEqual(error.reason_code, WORKSPACE_NOT_TRUSTED)
        self.assertEqual(error.worktree, str(self.worktree))
        self.assertIsInstance(error, ClaudeBgUnknown)
        self.assertIn("cd %s && claude" % self.worktree, error.command)

    def test_message_names_the_directory_and_the_one_step(self):
        with self.assertRaises(ClaudeBgWorkspaceUntrusted) as caught:
            self.launch()
        self.assertIn(str(self.worktree), caught.exception.reason)
        self.assertIn("trust", caught.exception.reason.lower())

    def test_other_launch_failures_stay_generic(self):
        self.fake = FakeClaude(self.worktree)
        self.fake.returncodes["bg"] = 1
        self.fake.launch_out = "something else broke"
        with self.assertRaises(ClaudeBgUnknown) as caught:
            self.launcher().launch("developer", "n1", self.contract)
        self.assertNotIsInstance(caught.exception, ClaudeBgWorkspaceUntrusted)

    def test_structured_observation_carries_no_provider_text(self):
        with self.assertRaises(ClaudeBgWorkspaceUntrusted) as caught:
            self.launch()
        self.assertEqual(set(caught.exception.observation), {"reason_code", "worktree", "branch"})
        self.assertEqual(classify_provider_failure(caught.exception)["kind"], "binding_unknown")


class UntrustedDispatchPersistsTheReasonTests(unittest.TestCase):
    """The monitor must keep the fixed code even though free text is redacted."""

    def test_dispatch_failure_dict_carries_the_reason_code(self):
        from vibe_guide.adapters.claude_bg import ClaudeBgWorkspaceUntrusted
        error = ClaudeBgWorkspaceUntrusted(WORKSPACE_NOT_TRUSTED, "x", "/w", "node/a")
        monitor = Monitor.__new__(Monitor)
        runner = mock.Mock()
        runner.start.side_effect = error
        contract = {"worktree": "/w", "binding_contract_version": "4.4"}
        result = monitor._dispatch_with_intent(runner, contract, {"intent_id": "i"})
        self.assertEqual(result["reason_code"], WORKSPACE_NOT_TRUSTED)
        self.assertEqual(result["reason_class"], "binding_unknown")
        self.assertIn("/w", result["worktree"])


if __name__ == "__main__":
    unittest.main()


class UntrustedThroughTheMonitorTests(unittest.TestCase):
    """The sandbox symptom end to end: the node and its event name the cause."""

    def test_start_failure_leaves_a_readable_reason_on_node_and_event(self):
        from vibe_guide.authorization import authorize, build_authorization_card
        from vibe_guide.capability_contract import build_contract, save_contract
        from vibe_guide.models import AgentCapabilities, DAGNode, Plan
        from vibe_guide.paths import ProjectPaths
        from vibe_guide.runners.fake import FakeRunner
        from vibe_guide.state import load_events

        class Untrusted(FakeRunner):
            def start(self, contract, worktree):
                raise ClaudeBgWorkspaceUntrusted(
                    WORKSPACE_NOT_TRUSTED, "Workspace not trusted. Run `claude` in %s once" % worktree,
                    str(worktree), "branch-n1")

        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            paths.vibe.mkdir(parents=True, exist_ok=True)
            (paths.vibe / "state.json").write_text(
                '{"workflow_version": 4, "execution_mode": "sdd_first", '
                '"session_gate": "s0_required", "capability_contract_required": true}\n')
            save_contract(paths, build_contract(paths.root, provider="fake", host_id="local"))
            node = DAGNode("n1", "n1", [], [], "control",
                           {"files": ["n1.py"], "worker": "worker-n1", "worktree": ".worktrees/n1",
                            "branch": "branch-n1"}, "ready")
            plan = Plan("untrusted", 1, "docs/prd.md", [node.id], "draft")
            caps = AgentCapabilities("fake", True, True, True, True, True, "full")
            record = authorize(build_authorization_card(plan, [node], caps), "AUTHORIZE")
            monitor = Monitor(paths, plan, [node])
            with mock.patch("vibe_guide.monitor.require_entry", return_value=None):
                snapshot = monitor.start(record, Untrusted())
            current = snapshot.nodes["n1"]
            self.assertEqual(current["status"], "blocked_unknown")
            self.assertEqual(current["reason_code"], WORKSPACE_NOT_TRUSTED)
            event = [e for e in load_events(paths, snapshot.run_id) if e["event"] == "binding_unknown"][-1]
            self.assertEqual(event["data"]["reason_code"], WORKSPACE_NOT_TRUSTED)
            self.assertIn(".worktrees/n1", event["data"]["worktree"])


class UntrustedNodeReachesTheProductManagerTests(unittest.TestCase):
    def test_a_node_stuck_on_an_untrusted_workspace_climbs_the_ladder(self):
        from vibe_guide import stall
        node = {
            "status": "blocked_unknown", "user_status": "自动修复中", "binding_phase": "binding_unknown",
            "developer_generation": 1, "review_generation": 0, "active_task": None, "evidence": [],
            "reason_code": WORKSPACE_NOT_TRUSTED,
            "retryable_action": {"attempt": 1, "reason_class": "binding_unknown", "role": "developer",
                                 "next_retry_at": 1.0, "last_observation_ref": "provider-pending"},
        }
        previous = None
        for generation in range(1, 4):
            node["developer_generation"] = generation
            outcome = stall.assess_stalls({"n1": node}, {}, [], previous)
            previous = outcome["record"]
        self.assertEqual(outcome["nodes"]["n1"]["tier"], stall.TIER_INVESTIGATE)
