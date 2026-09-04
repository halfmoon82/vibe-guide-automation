import copy
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.authorization import evaluate_v4_closeout
from vibe_guide.change_requests import build_v4_delivery_manifest
from vibe_guide.dag import schedule_ready_nodes, transition_node_status
from vibe_guide.models import DAGNode, V4ExecutionPolicy
from vibe_guide.monitor import heal_v4_node
from vibe_guide.planner import TaskContext, classify_s0, route_task, score_s1
from vibe_guide.sdd_runtime import validate_sdd_write_gate
from vibe_guide.evidence import ReviewResult, ReworkDecision, classify_rework
from vibe_guide.task_registry import claim_v4_writer, release_v4_writer


class V4EndToEndAcceptanceTests(unittest.TestCase):
    def test_six_ready_nodes_cap_at_five_then_refill_after_acceptance(self):
        nodes = [
            {"id": "n%d" % i, "status": "ready", "developer_task_id": "dev-%d" % i,
             "reviewer_task_id": "review-%d" % i, "writer": "writer-%d" % i}
            for i in range(6)
        ]
        nodes[5]["depends_on"] = ["n0"]
        self.assertEqual(sum(node["status"] == "ready" for node in nodes), 6)
        self.assertEqual(schedule_ready_nodes(nodes), ["n0", "n1", "n2", "n3", "n4"])
        self.assertEqual(sum(node["status"] == "running" for node in nodes), 5)
        for status in ("delivered", "review", "accepted"):
            transition_node_status(nodes[0], status)
        nodes[1]["status"] = nodes[2]["status"] = nodes[3]["status"] = nodes[4]["status"] = "archived"
        self.assertEqual(schedule_ready_nodes(nodes, active_pairs=0), ["n5"])

    def test_hard_dependency_blocks_even_when_capacity_is_available(self):
        nodes = [
            {"id": "dependent", "status": "ready", "depends_on": ["dependency"],
             "developer_task_id": "dev-dependent", "reviewer_task_id": "review-dependent"},
            {"id": "independent", "status": "ready", "developer_task_id": "dev-independent",
             "reviewer_task_id": "review-independent"},
            {"id": "dependency", "status": "planned", "developer_task_id": "dev-dependency",
             "reviewer_task_id": "review-dependency"},
        ]
        self.assertEqual(schedule_ready_nodes(nodes, active_pairs=0), ["independent"])
        self.assertEqual(nodes[0]["status"], "ready")
        nodes[2]["status"] = "accepted"
        nodes[1]["status"] = "archived"
        self.assertEqual(schedule_ready_nodes(nodes, active_pairs=0), ["dependent"])

    def test_provider_timeout_isolates_only_one_node_and_keeps_other_ready_work(self):
        snapshot = {"run_id": "acceptance-timeout", "nodes": {
            "timeout": {"id": "timeout", "status": "running", "task_id": "dev-timeout", "cursor": "c-timeout"},
            "other": {"id": "other", "status": "ready", "task_id": "dev-other",
                      "developer_task_id": "dev-other", "reviewer_task_id": "review-other"},
        }, "healing_events": []}
        result = heal_v4_node(snapshot, "timeout", {"kind": "provider_timeout"})
        self.assertTrue(result.isolated)
        self.assertEqual(snapshot["nodes"]["timeout"]["status"], "blocked_unknown")
        self.assertTrue(snapshot["nodes"]["timeout"]["isolated"])
        self.assertEqual(schedule_ready_nodes(list(snapshot["nodes"].values()), active_pairs=0), ["other"])
        self.assertEqual(snapshot["nodes"]["other"]["status"], "running")

    def test_reviewer_p1_rework_reuses_same_task_pair(self):
        node = {"status": "planned", "developer_task_id": "dev-1", "reviewer_task_id": "review-1"}
        for status in ("ready", "running", "review", "rework", "review"):
            transition_node_status(node, status)
        self.assertEqual(classify_rework([ReviewResult("P1", "assertion", "rework")]),
                         ReworkDecision.CONTINUE_SAME_WORKER)
        self.assertEqual((node["developer_task_id"], node["reviewer_task_id"]), ("dev-1", "review-1"))

    def test_allowlist_and_workspace_conflicts_reject_before_writer_and_accepted_manifest_is_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node = DAGNode(
                "n1", "issue", [], [], None,
                {"project_root": str(root), "worktree": str(root / ".vibe" / "worktrees" / "n1"),
                 "allowlist": ["../outside.py"], "writer": "dev-1", "run_id": "acceptance-gate"},
                "ready", writer="dev-1",
            )
            policy = V4ExecutionPolicy(
                capabilities={"installation": True},
                hard_gates={name: False for name in V4ExecutionPolicy.CANONICAL_HARD_GATES},
                node_ids=["n1"],
            )
            result = validate_sdd_write_gate(policy, node, {})
            self.assertFalse(result.valid)
            self.assertIn("allowlist_escape", result.reasons)

            workspace_conflict = DAGNode(
                "n2", "host checkout", [], [], None,
                {"project_root": str(root), "worktree": str(root), "allowlist": ["safe.py"],
                 "writer": "dev-2", "run_id": "acceptance-workspace-gate"},
                "ready", writer="dev-2",
            )
            conflict = validate_sdd_write_gate(policy, workspace_conflict, {})
            self.assertFalse(conflict.valid)
            self.assertIn("host_checkout_write", conflict.reasons)
            # A rejected gate must not reserve or poison the declared writer:
            # the original writer can claim this exact run/node key directly.
            # This avoids using a different writer as an indirect proxy for
            # the no-claim guarantee.
            self.assertTrue(claim_v4_writer("acceptance-workspace-gate", "n2", "dev-2"))
            self.assertTrue(release_v4_writer("acceptance-workspace-gate", "n2", "dev-2"))

        manifest = build_v4_delivery_manifest({"nodes": {"n1": {"status": "accepted"}}})
        self.assertEqual(manifest["status"], "ready")
        closeout = evaluate_v4_closeout({"nodes": {"n1": {"status": "accepted"}}}, {"allowed_actions": []})
        self.assertEqual(closeout.status, "ready")
        self.assertFalse(closeout.can_execute_external)

    def test_s0_s1_route_and_historical_snapshot_are_read_only(self):
        self.assertTrue(classify_s0("修正标题错别字").simple)
        context = TaskContext(4, 4, 4, 4, 4)
        score = score_s1(context)
        self.assertEqual(score.total, 20)
        self.assertEqual(route_task(context).route, "complex")

        historical = {"run_id": "historical", "nodes": {
            "n1": {"status": "running", "task_id": "task-1", "cursor": "cursor-1", "generation": 2},
            "n2": {"status": "planned", "task_id": "task-2"},
        }, "healing_events": []}
        persisted_before = json.dumps(historical, sort_keys=True, separators=(",", ":"))
        replay = copy.deepcopy(historical)
        result = heal_v4_node(replay, "n1", {"kind": "provider_timeout"})
        self.assertFalse(result.repaired)
        self.assertEqual(json.dumps(historical, sort_keys=True, separators=(",", ":")), persisted_before)
        self.assertEqual(historical["nodes"]["n1"]["cursor"], "cursor-1")
        self.assertEqual(replay["nodes"]["n1"]["status"], "blocked_unknown")


if __name__ == "__main__":
    unittest.main()
