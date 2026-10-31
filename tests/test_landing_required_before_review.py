"""An empty landing never reaches review (#336).

Recorded failure: ``scope_landing.landing_report`` returns an empty file list
for an empty diff and the monitor's delivered→reviewer scheduling never checks
whether the delivery actually changed anything.  A developer who reports
without committing real work must be refused with the ``empty_landing``
structural code and can re-report from the same session after a real commit; a
contract that opts in with ``allow_empty_landing: true`` passes and records an
``empty_landing_allowed`` event.
"""
import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.authorization import _canonical_digest, authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.dag import append_integration_review_node
from vibe_guide.engine_attestation import create_engine_attestation
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.planner import REQUIRED_COMPLEX_WORKFLOW, TaskContext
from vibe_guide.runners.claude_bg import ClaudeBgRunner
from vibe_guide.state import load_events
from vibe_guide.task_registry import load_task_binding
from vibe_guide.workflow_gate import create_task_workflow, record_workflow_node

from tests.test_claude_bg_reconnect import GOOD, FakeLauncher


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def landing_node(node_id, allow_empty_landing=False):
    contract = {
        "files": [node_id + ".py"],
        "worker": "worker-" + node_id,
        "worktree": ".worktrees/" + node_id,
        "dispatch_topology": "background",
        "worker_profile": {"worker": "codex", "model": "test", "reasoning": "normal",
                           "fallbacks": [], "selection_basis": {"issue_complexity_ref": node_id,
                           "complexity_band": "standard", "risk_tags": [],
                           "availability_evidence": "test"}, "writer": "writer",
                           "worktree": ".worktrees/" + node_id, "branch": "branch-" + node_id,
                           "allowlist": [node_id + ".py"]},
    }
    if allow_empty_landing:
        contract["allow_empty_landing"] = True
    return DAGNode(node_id, node_id, [], [], "g1", contract, "ready")


class EmptyLandingGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = ProjectPaths(Path(self.temporary.name))
        self.paths.vibe.mkdir(parents=True, exist_ok=True)
        save_contract(self.paths, build_contract(self.paths.root, provider="fake", host_id="local"))
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")
        root = self.paths.root
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "dev@example.com")
        git(root, "config", "user.name", "Dev")
        (root / ".gitignore").write_text(".vibe/\n.worktrees/\n", encoding="utf-8")
        (root / "bg.py").write_text("base\n", encoding="utf-8")
        git(root, "add", ".gitignore", "bg.py")
        git(root, "commit", "-qm", "baseline")
        self.plan, self.nodes, self.record = self._engine_plan()
        self.snapshot = self.monitor().start(self.record, self.runner())
        for node_id in ("bg", "bg2"):
            git(root, "worktree", "add", "-q", self.snapshot.nodes[node_id]["worktree"],
                "-b", self.snapshot.nodes[node_id]["branch"])

    def _engine_plan(self):
        (self.paths.root / "docs").mkdir(parents=True, exist_ok=True)
        (self.paths.root / "docs" / "prd.md").write_text("prd\n", encoding="utf-8")
        (self.paths.root / "docs" / "spec.md").write_text("spec\n", encoding="utf-8")
        workflow = create_task_workflow("plan-landing-gate", TaskContext(5, 5, 5, 5, 5))
        plan_dir = self.paths.vibe / "plans" / "plan-landing-gate"
        plan_dir.mkdir(parents=True, exist_ok=True)
        evidence_file = plan_dir / "workflow-evidence.md"
        evidence_file.write_text("evidence\n", encoding="utf-8")
        artifact = {"ref": evidence_file.name,
                    "sha256": hashlib.sha256(evidence_file.read_bytes()).hexdigest()}
        (plan_dir / "plan.json").write_text(json.dumps({"decisions": []}), encoding="utf-8")
        (plan_dir / "plan-confirmation.json").write_text(
            json.dumps({"authorization_digest": "card", "status": "confirmed"}), encoding="utf-8")
        for node_id in REQUIRED_COMPLEX_WORKFLOW:
            output = {"ref": node_id}
            evidence = {"ref": node_id, "artifact": artifact}
            if node_id == "plan_confirmation":
                output["authorization_digest"] = "card"
            if node_id == "product_decision":
                evidence["decision_digest"] = _canonical_digest(
                    {"decisions": [], "evidence_priority": []})
            record_workflow_node(workflow, node_id, {"ref": node_id}, output, evidence)
        (self.paths.vibe / "state.json").write_text(
            json.dumps({"workflow_version": 2, "session_gate": "s0_required",
                        "task_workflow": workflow}), encoding="utf-8")
        plan = append_integration_review_node(Plan(
            "plan-landing-gate", 1, "docs/prd.md", ["bg", "bg2"], "draft",
            spec_path="docs/spec.md",
            complexity_band="complex",
            nodes=[landing_node("bg", allow_empty_landing=True), landing_node("bg2")],
            integration_contract={
                "iteration_context": {"kind": "iteration", "based_on": "V5"},
                "compatibility_scope": ["V5 API"],
                "agentsmd_acceptance_refs": ["AGENTS.md#8"],
                "integration_acceptance_contract": {"checks": ["all"]},
                "unverified_or_excluded": ["provider"],
            },
        ))
        nodes = list(plan.nodes)
        attestation = create_engine_attestation(
            plan_id=plan.plan_id, plan_revision=plan.version,
            execution_engine="vibeguide_monitor", engine_mode="dag",
            provider="fake", capability_facts={"fake.worktree": True},
            provenance="live:test",
            now=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        (plan_dir / "engine-attestation.json").write_text(json.dumps(attestation), encoding="utf-8")
        card = build_authorization_card(
            plan, nodes, self.capabilities,
            execution_engine="vibeguide_monitor", engine_mode="dag",
            engine_evidence_ref=attestation["evidence_ref"],
        )
        return plan, nodes, authorize(card, "AUTHORIZE")

    def monitor(self):
        monitor = Monitor(self.paths, self.plan, self.nodes)
        monitor._validate_execution_topology = lambda snapshot: None
        return monitor

    def runner(self):
        return ClaudeBgRunner(lambda c, w: FakeLauncher("working"), paths=self.paths)

    def generation(self, snapshot, node_id):
        binding = load_task_binding(self.paths, node_id, "developer", run_id=snapshot.run_id)
        return binding.generation

    def land(self, node_id, files):
        worktree = self.paths.root / self.snapshot.nodes[node_id]["worktree"]
        for name, text in files.items():
            (worktree / name).parent.mkdir(parents=True, exist_ok=True)
            (worktree / name).write_text(text, encoding="utf-8")
            git(worktree, "add", name)
        git(worktree, "commit", "-qm", "work")

    def report(self, snapshot, node_id, payload=None):
        store = ProviderActionStore(self.paths)
        store.record_worker_delivery(
            snapshot.run_id, node_id, "developer",
            payload if payload is not None else dict(GOOD),
            self.generation(snapshot, node_id))
        return self.monitor().resume(snapshot.run_id, self.runner())

    def events(self, run_id, name):
        return [record for record in load_events(self.paths, run_id)
                if record["event"] == name]

    def test_empty_landing_is_rejected_with_empty_landing_code_and_recovers(self):
        # Report without committing anything: the worktree diff is empty.
        snapshot = self.report(self.snapshot, "bg2")
        node = snapshot.nodes["bg2"]
        self.assertNotIn(node["status"], {"review", "delivered", "accepted"})
        [rejection] = self.events(snapshot.run_id, "acceptance_rejected")
        self.assertEqual(rejection["data"].get("rejection_code"), "empty_landing")

        # The same session commits real work and re-reports: now it ships.
        self.land("bg2", {"bg2.py": "work\n"})
        corrected = {"delivery_evidence": dict(GOOD["delivery_evidence"],
                                               delivery_path="fixed.md")}
        snapshot = self.report(snapshot, "bg2", corrected)
        node = snapshot.nodes["bg2"]
        self.assertEqual(node["status"], "review", node.get("reason"))

    def test_allow_empty_landing_contract_passes_and_records_allowed(self):
        snapshot = self.report(self.snapshot, "bg")
        node = snapshot.nodes["bg"]
        self.assertEqual(node["status"], "review", node.get("reason"))
        self.assertEqual(self.events(snapshot.run_id, "acceptance_rejected"), [])
        [allowed] = self.events(snapshot.run_id, "empty_landing_allowed")
        self.assertEqual(allowed["data"].get("node_id"), "bg")

    def test_nonempty_landing_has_no_empty_landing_trace(self):
        self.land("bg2", {"bg2.py": "work\n"})
        snapshot = self.report(self.snapshot, "bg2")
        self.assertEqual(snapshot.nodes["bg2"]["status"], "review")
        self.assertEqual(self.events(snapshot.run_id, "acceptance_rejected"), [])
        self.assertEqual(self.events(snapshot.run_id, "empty_landing_allowed"), [])
