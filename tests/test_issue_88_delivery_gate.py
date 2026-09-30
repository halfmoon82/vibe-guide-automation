"""Issue 88: developer delivery-gate rereport semantics.

The vibeguide_monitor engine must distinguish a worker report that is only
missing delivery-artifact fields (a pure format gap, auditable and
same-session retriable) from missing binding evidence such as task
identity, worktree, branch or cursor (fail-closed blocked_unknown).
"""

import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.dag import append_integration_review_node
from vibe_guide.engine_attestation import create_engine_attestation
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.planner import REQUIRED_COMPLEX_WORKFLOW, TaskContext
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import load_events
from vibe_guide.task_registry import TaskBinding
from vibe_guide.workflow_gate import create_task_workflow, record_workflow_node


NODE_ID = "n1"


def _node(node_id=NODE_ID):
    return DAGNode(
        node_id,
        node_id,
        [],
        [],
        "g1",
        {
            "files": [node_id + ".py"],
            "worker": "worker-" + node_id,
            "worktree": ".worktrees/" + node_id,
            "worker_profile": {
                "worker": "codex",
                "model": "test",
                "reasoning": "normal",
                "fallbacks": [],
                "selection_basis": {
                    "issue_complexity_ref": node_id,
                    "complexity_band": "standard",
                    "risk_tags": [],
                    "availability_evidence": "test",
                },
                "writer": "writer",
                "worktree": ".worktrees/" + node_id,
                "branch": "branch-" + node_id,
                "allowlist": [node_id + ".py"],
            },
            "writer": "writer",
            "branch": "branch-" + node_id,
            "allowlist": [node_id + ".py"],
        },
        "ready",
    )


class BoundBindingRunner(FakeRunner):
    """FakeRunner whose observed binding carries host and cursor evidence."""

    def task_binding(self, contract, worktree, run_id, status):
        return TaskBinding(
            provider="fake",
            mode="visible",
            issue_id=contract["node_id"],
            role=contract["role"],
            task_id="task-" + contract["role"] + "-1",
            host="host-1",
            worktree=str(worktree),
            branch=str(contract.get("branch", "")),
            run_id=run_id,
            status=status,
            cursor="cursor-1",
            hostId="host-1",
            generation=contract["generation"],
        )


class DeliveryGateRereportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        self.paths.vibe.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n',
            encoding="utf-8",
        )
        save_contract(
            self.paths,
            build_contract(self.paths.root, provider="fake", host_id="local"),
        )
        self.capabilities = AgentCapabilities(
            "fake", True, True, True, True, True, "full"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _engine_monitor(self, nodes):
        (self.paths.root / "docs").mkdir(parents=True, exist_ok=True)
        (self.paths.root / "docs" / "prd.md").write_text("prd\n", encoding="utf-8")
        (self.paths.root / "docs" / "spec.md").write_text("spec\n", encoding="utf-8")
        workflow = create_task_workflow("plan-88", TaskContext(5, 5, 5, 5, 5))
        for node_id in REQUIRED_COMPLEX_WORKFLOW:
            record_workflow_node(
                workflow, node_id, {"ref": node_id}, {"ref": node_id},
                {"ref": node_id},
            )
        (self.paths.vibe / "state.json").write_text(
            json.dumps(
                {
                    "workflow_version": 2,
                    "session_gate": "s0_required",
                    "task_workflow": workflow,
                }
            ),
            encoding="utf-8",
        )
        plan = Plan(
            "plan-88",
            1,
            "docs/prd.md",
            [item.id for item in nodes],
            "draft",
            spec_path="docs/spec.md",
            complexity_band="complex",
            nodes=list(nodes),
            integration_contract={
                "iteration_context": {"kind": "iteration", "based_on": "V5"},
                "compatibility_scope": ["V5 API"],
                "agentsmd_acceptance_refs": ["AGENTS.md#8"],
                "integration_acceptance_contract": {"checks": ["all"]},
                "unverified_or_excluded": ["provider"],
            },
        )
        plan = append_integration_review_node(plan)
        nodes = list(plan.nodes)
        attestation = create_engine_attestation(
            plan_id=plan.plan_id,
            plan_revision=plan.version,
            execution_engine="vibeguide_monitor",
            engine_mode="dag",
            provider="fake",
            capability_facts={"fake.worktree": True},
            provenance="live:test",
            now="2026-09-29T00:00:00Z",
        )
        target = self.paths.vibe / "plans" / plan.plan_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "engine-attestation.json").write_text(
            json.dumps(attestation), encoding="utf-8"
        )
        card = build_authorization_card(
            plan,
            nodes,
            self.capabilities,
            execution_engine="vibeguide_monitor",
            engine_mode="dag",
            engine_evidence_ref=attestation["evidence_ref"],
        )
        monitor = Monitor(self.paths, plan, nodes)
        # The topology-projection evidence gate is a separate contract; this
        # fixture isolates the Issue-88 delivery-evidence classification.
        monitor._validate_execution_topology = lambda snapshot: None
        return monitor, authorize(card, "AUTHORIZE")

    def test_format_gap_records_acceptance_rejected_and_allows_same_session_rereport(self):
        monitor, record = self._engine_monitor([_node()])
        runner = BoundBindingRunner(
            events={
                (NODE_ID, "developer"): [
                    ("delivered", {"evidence": "delivery-attempt-1"}),
                    (
                        "delivered",
                        {
                            "evidence": "delivery-attempt-2",
                            "delivery_evidence": {
                                "completion_marker": "DONE",
                                "delivery_path": "/out",
                                "thread_status": "complete",
                            },
                        },
                    ),
                ],
            }
        )
        snapshot = monitor.start(record, runner)
        handle = snapshot.handles[NODE_ID]

        snapshot = monitor.tick(snapshot.run_id, runner)

        current = snapshot.nodes[NODE_ID]
        self.assertNotEqual(current["status"], "blocked_unknown")
        self.assertIsNotNone(current.get("active_task"))
        self.assertEqual(snapshot.handles.get(NODE_ID), handle)
        events = load_events(self.paths, snapshot.run_id)
        names = [event["event"] for event in events]
        self.assertIn("acceptance_rejected", names)
        rejected = events[names.index("acceptance_rejected")]
        self.assertEqual(
            rejected["data"]["disposition"], "acceptance_rejected"
        )
        self.assertNotIn("delivered", names)

        snapshot = monitor.tick(snapshot.run_id, runner)

        current = snapshot.nodes[NODE_ID]
        self.assertEqual(current["status"], "review")
        self.assertTrue(any(
            event["event"] == "delivered"
            for event in load_events(self.paths, snapshot.run_id)
        ))

    def test_missing_task_identity_stays_blocked_unknown(self):
        monitor, record = self._engine_monitor([_node()])
        runner = FakeRunner(
            events={
                (NODE_ID, "developer"): [
                    ("delivered", {"evidence": "delivery-attempt"}),
                ],
            }
        )
        snapshot = monitor.start(record, runner)

        snapshot = monitor.tick(snapshot.run_id, runner)

        current = snapshot.nodes[NODE_ID]
        self.assertEqual(current["status"], "blocked_unknown")
        # FakeRunner bindings carry task identity but no host/cursor
        # evidence; binding-class gaps must remain fail-closed.
        self.assertIn("host identity is missing", current["reason"])
        self.assertIn("current cursor is missing", current["reason"])
        events = load_events(self.paths, snapshot.run_id)
        self.assertFalse(any(
            event["event"] == "acceptance_rejected" for event in events
        ))

    def test_unclassifiable_delivery_evidence_fails_closed(self):
        monitor, record = self._engine_monitor([_node()])
        runner = BoundBindingRunner(
            events={
                (NODE_ID, "developer"): [
                    ("delivered", {"delivery_evidence": "not-a-mapping"}),
                ],
            }
        )
        snapshot = monitor.start(record, runner)

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes[NODE_ID]["status"], "blocked_unknown")


if __name__ == "__main__":
    unittest.main()
