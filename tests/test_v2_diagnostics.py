import tempfile
import unittest
from pathlib import Path

from vibe_guide.models import WorkerProfile
from vibe_guide.paths import ProjectPaths
from vibe_guide.scanner import scan_project
from vibe_guide.diagnostics import (
    diagnose_skill, check_agents_contract, assert_planning_gate,
    require_execution_ready, screen_session, require_session_screened,
    validate_child_session_binding,
)
from vibe_guide.initializer import init_project
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.workflow_gate import require_capability_contract, session_contract_prompt


class V2DiagnosticsTests(unittest.TestCase):
    def test_global_skill_without_project_reference_is_attention(self):
        with tempfile.TemporaryDirectory() as d:
            paths = ProjectPaths.from_cwd(Path(d))
            report = scan_project(paths)
            result = diagnose_skill("architecture-skill-pack", report, {"global_skills": ["architecture-skill-pack"]})
            self.assertEqual(result.status, "attention")

    def test_contract_and_session_and_child_boundaries(self):
        check = check_agents_contract("# rules\n", ["required rule"])
        self.assertFalse(check.ok)
        with tempfile.TemporaryDirectory() as d:
            paths = ProjectPaths.from_cwd(Path(d))
            gate = screen_session(paths, "s1", "small request")
            require_session_screened(gate)
            worker = WorkerProfile("codex", "model", "normal", [], {"issue_complexity_ref": "i1", "complexity_band": "small", "risk_tags": [], "availability_evidence": "probe"}, worktree="worktree", branch="branch", writer="writer", allowlist=["vibe_guide/x.py"])
            validate_child_session_binding("run-1", "r1", "d1", "n1", "developer", worker)

    def test_missing_plan_evidence_requires_planning(self):
        with tempfile.TemporaryDirectory() as d:
            paths = ProjectPaths.from_cwd(Path(d))
            gate = assert_planning_gate(paths, "p1")
            self.assertEqual(gate.status, "planning_required")
            with self.assertRaises(PermissionError):
                require_execution_ready(gate)

    def test_draft_and_empty_nodes_are_not_execution_ready(self):
        with tempfile.TemporaryDirectory() as d:
            p = ProjectPaths.from_cwd(Path(d)); root = p.vibe / "plans" / "p1"
            (root / "specs").mkdir(parents=True); (root / "issues").mkdir()
            for name, value in (("prd.md", "状态：approved"), ("plan.json", '{"status":"draft"}'), ("nodes.json", "[]"), ("authorization-card.json", "{}")):
                (root / name).write_text(value, encoding="utf-8")
            self.assertEqual(assert_planning_gate(p, "p1").status, "planning_required")

    def test_session_reuse_conflict_and_no_raw_request(self):
        with tempfile.TemporaryDirectory() as d:
            p = ProjectPaths.from_cwd(Path(d)); first = screen_session(p, "same", "secret-token", "user_entry")
            with self.assertRaises(PermissionError):
                screen_session(p, "same", "different", "user_entry")
            raw = (p.vibe / "session-gates.json").read_text()
            self.assertNotIn("secret-token", raw); self.assertNotIn("different", raw)

    def test_worker_profile_binding_fields_and_recursive_roles(self):
        worker = WorkerProfile("codex", "model", "normal", [], {}, worktree="wt", branch="b", allowlist=["../escape"])
        with self.assertRaises(ValueError):
            validate_child_session_binding("r", "1", "d", "n", "planner", worker)
        with self.assertRaises(ValueError):
            validate_child_session_binding("r", "1", "d", "n", "developer", worker)

    def test_existing_state_migrates_and_agents_mismatch_proposal_is_created(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "AGENTS.md").write_text("old rule\n", encoding="utf-8")
            (root / ".vibe").mkdir(); (root / ".vibe" / "state.json").write_text("{}\n", encoding="utf-8")
            result = init_project(ProjectPaths.from_cwd(root), True)
            self.assertEqual(__import__("json").loads((root / ".vibe" / "state.json").read_text())["workflow_version"], 2)
            self.assertTrue((root / ".vibe" / "proposals" / "skills").is_dir())

    def test_agents_capability_rules_are_proposed_without_mutating_agents(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            original = "# Existing\nProject guidance is maintained through the Vibe Guide.\n"
            (root / "AGENTS.md").write_text(original, encoding="utf-8")
            result = init_project(ProjectPaths.from_cwd(root), True)
            proposal = root / ".vibe" / "proposals" / "agentsmd" / "proposal.md"
            self.assertTrue(proposal.is_file())
            proposal_text = proposal.read_text(encoding="utf-8")
            self.assertIn("不得根据记忆、README", proposal_text)
            self.assertIn("unknown_timeout", proposal_text)
            self.assertEqual((root / "AGENTS.md").read_text(encoding="utf-8"), original)

    def test_v2_entry_with_vibe_but_missing_state_is_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / ".vibe").mkdir()
            with self.assertRaises(PermissionError):
                from vibe_guide.workflow_gate import require_entry
                require_entry(ProjectPaths.from_cwd(root), "scan:session", "scan")

    def test_required_v2_entry_without_contract_is_unknown_not_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".vibe").mkdir()
            (root / ".vibe" / "state.json").write_text(
                '{"workflow_version": 2, "session_gate": "s0_required", '
                '"capability_contract_required": true}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(PermissionError, "capability_contract_unknown"):
                require_capability_contract(ProjectPaths.from_cwd(root))

    def test_v2_entry_requires_contract_even_when_legacy_flag_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".vibe").mkdir()
            (root / ".vibe" / "state.json").write_text(
                '{"workflow_version": 2, "session_gate": "s0_required"}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(PermissionError, "capability_contract_unknown"):
                from vibe_guide.workflow_gate import require_entry
                require_entry(ProjectPaths.from_cwd(root), "monitor:missing", "monitor")

    def test_session_contract_prompt_is_scoped_and_redacted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            contract = build_contract(
                root,
                provider="codex",
                host_id="host-a",
                facts={
                    "terminal.exec": {
                        "status": "verified_available",
                        "scope": "task",
                        "route": "runtime.exec",
                        "evidence_ref": "probe-1",
                    }
                },
            )
            prompt = session_contract_prompt(contract)
            self.assertIn(contract.contract_digest, prompt)
            self.assertIn("verified_available", prompt)
            self.assertNotIn(str(root), prompt)
            self.assertNotIn("probe-1", prompt)
            self.assertNotIn("host-a", prompt)
            self.assertNotIn("codex", prompt)

    def test_session_contract_prompt_downgrades_expired_fact_to_stale(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            checked = __import__("datetime").datetime(
                2026, 8, 26, 10, 0, tzinfo=__import__("datetime").timezone.utc
            )
            contract = build_contract(
                root,
                facts={
                    "terminal.exec": {
                        "status": "verified_available",
                        "scope": "task",
                        "route": "runtime.exec",
                        "evidence_ref": "probe-expired",
                        "expires_at": "2026-08-26T09:59:00+00:00",
                    }
                },
                now=checked,
            )
            prompt = session_contract_prompt(contract, now=checked)
            self.assertIn('"terminal.exec":"stale"', prompt)
