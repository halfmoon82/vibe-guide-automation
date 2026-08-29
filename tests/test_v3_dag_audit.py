import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.dag import audit_dag, render_plan_artifacts, validate_dag
from vibe_guide.models import DAGNode, Plan


class FakePaths:
    def __init__(self, root):
        self.root = Path(root)

    def resolve_vibe_path(self, relative):
        return self.root / ".vibe" / Path(relative)


def node(node_id, *, depends=(), status="planned", writer=None, allowlist=None, adapter_id="codex"):
    writer = writer or "writer-" + node_id
    allowlist = list(allowlist or ["vibe_guide/" + node_id + ".py"])
    contract = {
        "input": ["request"],
        "output": ["result"],
        "error_behavior": "return planning_required",
        "acceptance_examples": ["ready set is observable"],
        "risk_tags": ["scheduling"],
        "adapter_id": adapter_id,
        "provider": "codex-app-visible",
        "writer": writer,
        "reviewer": "reviewer-" + node_id,
        "worktree": ".vibe/worktrees/" + node_id,
        "branch": "codex/" + node_id,
        "allowlist": allowlist,
    }
    return DAGNode(node_id, node_id, list(depends), [], "planning", contract, status,
                   writer=writer, worktree=contract["worktree"], allowlist=allowlist)


class V3DAGAuditTests(unittest.TestCase):
    def _publish_source(self, *, acceptance_key="acceptance_examples"):
        value = {
            "title": "Published plan",
            "objective": "Audit the plan",
            "plan_revision": 4,
            "nodes": [{
                "id": "n",
                "title": "Node",
                "depends_on": [],
                "integration_after": [],
                "parallel_group": "planning",
                "status": "planned",
                "contract": {
                    "input": ["request"],
                    "output": ["result"],
                    "error_behavior": "return planning_required",
                    acceptance_key: ["works"],
                    "risk_tags": ["planning"],
                    "adapter_id": "codex",
                    "provider": "codex-app-visible",
                    "writer": "writer-n",
                    "reviewer": "reviewer-n",
                    "worktree": ".vibe/worktrees/n",
                    "branch": "codex/n",
                    "allowlist": ["vibe_guide/n.py"],
                },
                "writer": "writer-n",
                "worktree": ".vibe/worktrees/n",
                "allowlist": ["vibe_guide/n.py"],
            }],
        }
        return value

    def test_publish_binds_nodes_and_revision_across_plan_nodes_and_audit(self):
        from vibe_guide.cli import _publish_plan

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "node-spec.json"
            source.write_text(json.dumps(self._publish_source()), encoding="utf-8")
            plan, nodes, _ = _publish_plan(FakePaths(root), "plan-4", source)
            plan_dir = root / ".vibe" / "plans" / "plan-4"
            plan_payload = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
            nodes_payload = json.loads((plan_dir / "nodes.json").read_text(encoding="utf-8"))
            audit_payload = json.loads((plan_dir / "dag-audit.json").read_text(encoding="utf-8"))
            prd_text = (plan_dir / "prd.md").read_text(encoding="utf-8")
            self.assertEqual([item["id"] for item in plan_payload["nodes"]], ["n"])
            self.assertEqual([item["id"] for item in nodes_payload], ["n"])
            self.assertEqual(plan_payload["nodes"], nodes_payload)
            self.assertEqual(plan_payload["version"], 4)
            self.assertEqual(audit_payload["plan_revision"], 4)
            self.assertEqual(audit_payload["plan_id"], "plan-4")
            self.assertIn("revision: 4", prd_text)
            self.assertNotIn("None", prd_text)
            self.assertFalse((plan_dir / "authorization-card.json").exists())
            self.assertFalse((plan_dir / "plan-confirmation.json").exists())

    def test_publish_accepts_minimal_contract_without_generic_worker_profile(self):
        from vibe_guide.cli import _publish_plan

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "node-spec.json"
            source.write_text(json.dumps(self._publish_source(acceptance_key="acceptance_example")), encoding="utf-8")
            plan, nodes, _ = _publish_plan(FakePaths(root), "plan-minimal", source)
            self.assertEqual(plan.nodes[0].contract["acceptance_example"], ["works"])
            self.assertNotIn("worker_profile", plan.nodes[0].contract)
            spec_text = (root / ".vibe" / "plans" / "plan-minimal" / "specs" / "n.md").read_text(encoding="utf-8")
            self.assertIn("验收示例：[\'works\']", spec_text)
            self.assertNotIn("None", spec_text)

    def test_ready_set_ignores_integration_after_but_blocks_unknown_hard_dependency(self):
        baseline = node("V3-0", status="accepted")
        ready = node("V3-1", depends=["V3-0"])
        blocked = node("V3-2", depends=["missing"])
        plan = Plan("v3", 4, "prd.md", ["V3-0", "V3-1", "V3-2"], "draft",
                    nodes=[baseline, ready, blocked])
        result = audit_dag(plan)
        self.assertEqual(result.status, "blocked_dag")
        self.assertEqual(result.ready_nodes, ["V3-1"])
        self.assertIn("V3-2", result.blocked_nodes)
        self.assertTrue(any("unknown hard" in reason for reason in result.reasons["V3-2"]))

    def test_audit_rejects_adapter_mismatch_cycle_and_writer_allowlist_conflict(self):
        left = node("a", depends=["b"], writer="same", allowlist=["a.py"])
        right = node("b", depends=["a"], writer="same", allowlist=["b.py"], adapter_id="other")
        result = audit_dag(Plan("v3", 4, "prd.md", ["a", "b"], "draft", nodes=[left, right]))
        self.assertEqual(result.status, "blocked_dag")
        reasons = " ".join(result.reasons["a"] + result.reasons["b"])
        self.assertIn("adapter", reasons)
        self.assertIn("cycle", reasons)
        self.assertIn("writer", reasons)

    def test_contract_digest_changes_when_writer_or_allowlist_changes(self):
        first = node("n")
        plan = Plan("v3", 4, "prd.md", ["n"], "draft", nodes=[first])
        digest_one = audit_dag(plan).digest
        first.contract["writer"] = "changed"
        first.writer = "changed"
        self.assertNotEqual(digest_one, audit_dag(plan).digest)

    def test_render_persists_exact_revision_and_filesystem_artifact(self):
        plan = Plan("v3", 4, "prd.md", ["n"], "draft", nodes=[node("n", status="accepted")])
        with tempfile.TemporaryDirectory() as directory:
            artifacts = render_plan_artifacts(plan, Path(directory))
            payload = json.loads((Path(directory) / "dag-audit.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["plan_revision"], 4)
            self.assertEqual(payload["plan_id"], "v3")
            self.assertEqual(payload["status"], "reviewed")
            self.assertEqual(artifacts.dag_path.name, "dag-audit.json")
            self.assertTrue((Path(directory) / "plan.md").is_file())

    def test_blocked_audit_is_not_published_as_reviewed(self):
        plan = Plan("v3", 4, "prd.md", ["n"], "draft", nodes=[node("n", depends=["missing"])])
        with tempfile.TemporaryDirectory() as directory:
            render_plan_artifacts(plan, Path(directory))
            payload = json.loads((Path(directory) / "dag-audit.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "blocked_dag")
            self.assertEqual(payload["audit_status"], "blocked_dag")

    def test_validate_rejects_absolute_or_parent_allowlist(self):
        for path in ("/tmp/outside.py", "../outside.py"):
            with self.subTest(path=path):
                self.assertFalse(validate_dag([node("n", allowlist=[path])]).valid)

    def test_validate_rejects_overlapping_write_allowlists(self):
        left = node("left", writer="left-w", allowlist=["shared.py"])
        right = node("right", writer="right-w", allowlist=["shared.py"])
        result = validate_dag([left, right])
        self.assertFalse(result.valid)
        self.assertTrue(any("allowlist conflict" in error for error in result.errors))


if __name__ == "__main__":
    unittest.main()
