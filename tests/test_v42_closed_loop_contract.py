import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.initializer import init_project
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.workflow_gate import require_entry, require_v42_sdd_first


def _node(node_id="n1"):
    return DAGNode(
        node_id,
        node_id,
        [],
        [],
        "control",
        {
            "files": [node_id + ".py"],
            "worker": "worker-" + node_id,
            "worktree": ".worktrees/" + node_id,
            "branch": "branch-" + node_id,
        },
        "ready",
    )


class V42ClosedLoopContractTests(unittest.TestCase):
    def test_v42_state_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            paths.vibe.mkdir()
            (paths.vibe / "state.json").write_text(
                json.dumps({
                    "workflow_version": 4,
                    "execution_mode": "sdd_first",
                    "session_gate": "s0_required",
                    "capability_contract_required": True,
                    "legacy": True,
                }),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(PermissionError, "^v42_state_required$"):
                require_v42_sdd_first(paths)

    def test_init_materializes_v42_sdd_first_state(self):
        with tempfile.TemporaryDirectory() as root:
            project_root = Path(root)
            paths = ProjectPaths(project_root)

            init_project(paths, True)

            state = json.loads((project_root / ".vibe/state.json").read_text())
            self.assertEqual(
                state,
                {
                    "workflow_version": 4,
                    "execution_mode": "sdd_first",
                    "session_gate": "s0_required",
                    "capability_contract_required": True,
                },
            )

    def test_provider_timeout_is_retryable_without_new_authorization(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            paths.vibe.mkdir()
            (paths.vibe / "state.json").write_text(
                '{"workflow_version": 4, "execution_mode": "sdd_first", '
                '"session_gate": "s0_required", "capability_contract_required": true}\n'
            )
            save_contract(paths, build_contract(paths.root, provider="fake", host_id="local"))
            node = _node()
            plan = Plan("v42-timeout", 1, "docs/prd.md", [node.id], "draft")
            capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")
            record = authorize(build_authorization_card(plan, [node], capabilities), "AUTHORIZE")
            runner = FakeRunner(
                events={(node.id, "developer"): [("timeout", {"reason": "provider timeout"})]}
            )
            monitor = Monitor(paths, plan, [node])

            # Keep this lifecycle test focused on provider recovery while the
            # separate entry-gate test owns V2/V4 state rejection.
            with patch("vibe_guide.monitor.require_entry", return_value=None):
                started = monitor.start(record, runner)
            original_task_id = started.nodes[node.id]["active_task"]["task_id"]
            snapshot = monitor.tick(started.run_id, runner)

            self.assertEqual(snapshot.nodes[node.id]["status"], "retry_pending")
            self.assertEqual(
                snapshot.nodes[node.id]["active_task"]["task_id"], original_task_id
            )

    def test_external_permission_is_boundary_not_engineering_failure(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            paths.vibe.mkdir()
            (paths.vibe / "state.json").write_text(
                '{"workflow_version": 4, "execution_mode": "sdd_first", '
                '"session_gate": "s0_required", "capability_contract_required": true}\n'
            )
            save_contract(paths, build_contract(paths.root, provider="fake", host_id="local"))
            node = _node()
            plan = Plan("v42-permission", 1, "docs/prd.md", [node.id], "draft")
            capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")
            record = authorize(build_authorization_card(plan, [node], capabilities), "AUTHORIZE")
            runner = FakeRunner(
                events={
                    (node.id, "developer"): [
                        (
                            "failed",
                            {
                                "reason": "external permission denied",
                                "permission_denied": True,
                            },
                        )
                    ]
                }
            )
            monitor = Monitor(paths, plan, [node])

            with patch("vibe_guide.monitor.require_entry", return_value=None):
                started = monitor.start(record, runner)
                snapshot = monitor.tick(started.run_id, runner)

            self.assertEqual(snapshot.nodes[node.id]["status"], "blocked_unknown")
            self.assertIsNone(snapshot.nodes[node.id]["retryable_action"])


if __name__ == "__main__":
    unittest.main()
