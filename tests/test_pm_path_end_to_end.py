"""The product-manager path, end to end, through the public CLI only.

Natural-language request → attest session capabilities → publish from a
business-only product spec → authorize → monitor reaches run_started with a
topology observation.  No `.vibe/` file is hand-edited anywhere; every
artifact comes from a `vibe` command.  This is the test the V4.5 audit found
missing: every earlier "end-to-end" started from a hand-written node-spec.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from vibe_guide.cli import run_cli
from vibe_guide.diagnostics import assert_planning_gate
from vibe_guide.node_spec import reject_engineering_fields
from vibe_guide.paths import ProjectPaths

FIXTURE = Path(__file__).parent / "fixtures" / "pm-path" / "product-spec.json"
REQUEST = "设计并实现保单查看页的 PDF 导出，集成日期范围筛选、编写测试并部署"
FACTS = {name: True for name in (
    "claude-code.agent", "claude-code.shell", "claude-code.subprocess", "claude-code.worktree",
    "claude-code.visible_task.create", "claude-code.visible_task.enter",
    "claude-code.visible_task.resume", "claude-code.visible_task.wait",
    "claude-code.in_session_sdd",
)}


class ProductManagerPathTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="v45-pm-e2e-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = ProjectPaths(self.root)

    def cli(self, *argv):
        return run_cli(list(argv) + ["--json"], self.root)

    def test_natural_language_to_run_started_without_hand_editing_vibe_files(self):
        # 0. The fixture the agent would hand over carries no engineering field.
        spec = json.loads(FIXTURE.read_text(encoding="utf-8"))
        reject_engineering_fields(spec)

        # 1. Route: a complex request without a spec is a draft, not a dead end.
        routed = self.cli("plan", "--request", REQUEST)
        self.assertEqual(routed.payload["route"], "complex", routed.payload)
        self.assertEqual(routed.payload["status"], "planned")

        # 2. Init + attest: the session states its capabilities; vibe records them.
        self.assertEqual(self.cli("init", "--confirm").payload["status"], "ok")
        (self.root / "facts.json").write_text(json.dumps(FACTS), encoding="utf-8")
        attested = self.cli(
            "attest", "--adapter", "claude-code", "--facts", "facts.json",
            "--provenance", "e2e: native visible-task tools observed in this session",
            "--project-id", "pmpathprobe",
        )
        self.assertEqual(attested.payload["status"], "ok", attested.payload)
        self.assertEqual(attested.payload["level"], "full")

        # 3. Publish from the product spec: engineering fields are derived.
        shutil.copy(FIXTURE, self.root / "product-spec.json")
        published = self.cli("plan", "--request", REQUEST, "--plan-id", "pm-plan", "--from-prd", "product-spec.json")
        self.assertEqual(published.payload["status"], "ok", published.payload)
        plan_dir = self.root / ".vibe" / "plans" / "pm-plan"
        plan = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["complexity_band"], "complex")
        self.assertIn("integration-review", plan["node_ids"])
        nodes = json.loads((plan_dir / "nodes.json").read_text(encoding="utf-8"))
        for node in nodes:
            self.assertEqual(node["contract"]["adapter_id"], "claude-code", node["id"])
        for artifact in ("prd.md", "planning-brief.md", "authorization-card.json", "engine-attestation.json", "dag-audit.json", "plan-confirmation.json"):
            self.assertTrue((plan_dir / artifact).is_file(), artifact)
        self.assertEqual(assert_planning_gate(self.paths, "pm-plan").status, "execution_ready")

        # 4. Authorize: one token, ten workflow nodes recorded, nothing hand-written.
        authorized = self.cli("authorize", "--plan", "pm-plan", "--authorize", "AUTHORIZE")
        self.assertEqual(authorized.payload["status"], "ok", authorized.payload)
        self.assertTrue(authorized.payload["authorization_granted"])
        self.assertEqual(len(authorized.payload["recorded_nodes"]), 10)

        # 5. Monitor: the run starts and records its topology; the terminal
        #    state is "waiting for the provider mailbox", never a gate error.
        monitored = self.cli("monitor", "--plan", "pm-plan", "--authorize", "AUTHORIZE")
        self.assertIn(monitored.payload["status"], {"retry_pending", "blocked_unknown", "running"}, monitored.payload)
        run_id = monitored.payload["run_id"]
        events = [json.loads(line)["event"] for line in (self.root / ".vibe" / "runs" / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(events[0], "run_started", events)
        self.assertIn("execution_topology_observed", events)
        self.assertNotIn("blocked_design", events)
        for forbidden in ("required_workflow_blocked", "session_gate_blocked", "topology evidence", "engine_attestation_unavailable"):
            self.assertNotIn(forbidden, json.dumps(monitored.payload, ensure_ascii=False), forbidden)

        # 5b. The dispatch requests themselves must isolate the parallel nodes.
        #     Asserting on the pure function is not enough: the value that
        #     reaches a developer session is the one in child_binding, built
        #     further down the chain.  Two sessions sharing a directory on the
        #     trunk is exactly what this used to do.
        requests = sorted((self.root / ".vibe" / "provider-actions" / "requests").glob("*.json"))
        self.assertTrue(requests, "monitor dispatched no provider create request")
        dispatched = []
        for path in requests:
            binding = json.loads(path.read_text(encoding="utf-8"))["request"]["child_binding"]
            dispatched.append((binding["node_id"], binding["worktree"], binding["branch"]))
        pairs = {(tree, branch) for _, tree, branch in dispatched}
        self.assertEqual(len(pairs), len(dispatched), dispatched)
        for node_id, tree, branch in dispatched:
            self.assertNotIn(branch, {"main", "master"}, node_id)
            self.assertNotIn(tree, {".", "", "./"}, node_id)
            self.assertFalse(tree.startswith(".."), (node_id, tree))

        # 6. Status and resume read the same run back without a second writer.
        status = self.cli("status", "--plan", "pm-plan")
        self.assertEqual(status.payload.get("run_id"), run_id, status.payload)
        resumed = self.cli("resume", "--plan", "pm-plan")
        self.assertEqual(resumed.payload.get("run_id"), run_id, resumed.payload)
        self.assertEqual(len(list((self.root / ".vibe" / "runs").iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
