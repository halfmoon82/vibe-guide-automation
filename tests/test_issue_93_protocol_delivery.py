"""ISSUE-93: visible-sdd create dispatch carries the shipped protocol text.

The create request prompt must contain the full text of the packaged
``visible-sdd-worker`` protocol, while ``VISIBLE_SDD_PROTOCOL_REF`` stays a
verbatim version pointer.  A missing packaged protocol must fail closed
*before* the create side effect leaves the mailbox.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.monitor import VISIBLE_SDD_PROTOCOL_REF
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.provider_action import ProviderActionRunner
from vibe_guide.protocols import load_protocol

PROTOCOL_NAME = "visible-sdd-worker"

CONTRACT = {
    "node_id": "inline-sdd-protocol",
    "role": "developer",
    "generation": 1,
    "project_id": "project-fixture",
    "topology": "visible-sdd",
    "sdd_protocol": VISIBLE_SDD_PROTOCOL_REF,
    "worktree": ".worktrees/inline-sdd-protocol",
    "branch": "node/inline-sdd-protocol",
    "files": ["vibe_guide/runners/provider_action.py"],
}

RESULTS = {
    "create": {"binding": {"task_id": "task-93", "host": "mac"}},
    "locate": {"located": True},
    "visibility": {"visible": True, "direct_enter": True},
}


class _RecordingRunner(ProviderActionRunner):
    """Capture mailbox create requests and feed canned provider results."""

    def __init__(self, paths):
        super().__init__(paths, "codex", "codex-app-visible")
        self.requests = []

    def _require_result(self, contract, run_id, operation, request):
        self.requests.append((operation, request))
        return dict(RESULTS[operation])


class VisibleSddPromptCarriesProtocolTests(unittest.TestCase):
    def _runner(self, root):
        return _RecordingRunner(ProjectPaths(Path(root)))

    def test_create_prompt_contains_full_protocol_text(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            runner.task_binding(dict(CONTRACT), Path(directory), "run-93", "running")
            creates = [req for op, req in runner.requests if op == "create"]
            self.assertEqual(len(creates), 1, "exactly one create request")
            request = creates[0]
            protocol_text = load_protocol(PROTOCOL_NAME)
            # The full shipped protocol text is inlined into the prompt.
            self.assertIn(protocol_text, request["prompt"])
            # Acceptance example pins: §1 heading and §5 protocol rules text.
            self.assertIn("## 1. 固定流程", request["prompt"])
            self.assertIn("## 5. 交付 payload 契约", request["prompt"])
            # The pointer stays a verbatim version reference, unchanged.
            self.assertEqual(request["sdd_protocol"], VISIBLE_SDD_PROTOCOL_REF)
            self.assertEqual(request["topology"], "visible-sdd")

    def test_missing_protocol_fails_closed_before_create(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            with patch(
                "vibe_guide.runners.provider_action.load_protocol",
                side_effect=FileNotFoundError("missing protocol"),
            ):
                with self.assertRaises(FileNotFoundError):
                    runner.task_binding(
                        dict(CONTRACT), Path(directory), "run-93", "running"
                    )
            self.assertEqual(runner.requests, [], "no provider action may leave")

    def test_non_visible_sdd_topology_keeps_pointer_out_of_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            contract = dict(CONTRACT)
            contract["topology"] = "dual-visible"
            contract.pop("sdd_protocol", None)
            runner.task_binding(contract, Path(directory), "run-93", "running")
            creates = [req for op, req in runner.requests if op == "create"]
            self.assertEqual(len(creates), 1)
            protocol_text = load_protocol(PROTOCOL_NAME)
            self.assertNotIn(protocol_text, creates[0]["prompt"])
            self.assertNotIn("sdd_protocol", creates[0])


if __name__ == "__main__":
    unittest.main()
