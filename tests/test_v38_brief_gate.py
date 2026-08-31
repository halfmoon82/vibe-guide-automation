import unittest
from pathlib import Path
import tempfile

from vibe_guide.brief import (
    ImplementationBrief, require_brief_before_write, validate_implementation_brief,
)
from vibe_guide.manifest import RunManifest
from vibe_guide.models import DAGNode


class V38BriefGateTests(unittest.TestCase):
    def setUp(self):
        self.manifest = RunManifest.from_mapping({
            "plan_id": "plan", "plan_revision": 1, "run_id": "run",
            "base_sha": "a" * 40, "target_branch": "codex/v38",
            "execution_epoch": 0, "authorization_digest": "",
            "evidence_ref": "events.jsonl",
        })
        self.node = DAGNode("n", "n", [], [], "g", {"input": "x"}, "planned",
                            writer="w", worktree=".vibe/worktrees/n",
                            allowlist=["vibe_guide/a.py"], owned_paths=["vibe_guide/a.py"])

    def test_missing_negative_case_stays_pending(self):
        brief = ImplementationBrief.from_mapping({
            "issue_id": "n", "goal": "goal", "non_goals": [],
            "owned_paths": ["vibe_guide/a.py"], "read_paths": [],
            "call_chain": ["vibe_guide/a.py:entry"],
            "invariants": [{"id": "I1", "entrypoint": "vibe_guide/a.py:entry",
                            "positive_case": "ok", "negative_case": "",
                            "test_command": "python -m unittest"}],
            "base_sha": "a" * 40, "plan_revision": 1, "execution_epoch": 0,
            "evidence_ref": "brief",
        })
        validation = validate_implementation_brief(brief, self.manifest, self.node)
        self.assertFalse(validation.valid)
        with self.assertRaises(ValueError):
            require_brief_before_write(self.node, self.manifest, brief)

    def _valid_brief(self, **overrides):
        data = {
            "issue_id": "n", "plan_id": "plan", "goal": "goal", "non_goals": [],
            "owned_paths": ["vibe_guide/a.py"], "read_paths": [],
            "call_chain": ["vibe_guide/a.py:entry"],
            "invariants": [{"id": "I1", "entrypoint": "vibe_guide/a.py:entry",
                            "positive_case": "ok", "negative_case": "bad",
                            "test_command": "python -m unittest",
                            "evidence_ref": "evidence/I1.json"}],
            "expected_red": "red", "risk_notes": [],
            "base_sha": "a" * 40, "plan_revision": 1, "execution_epoch": 0,
            "authorization_digest": "", "writer": "w", "task_id": "task-n",
            "worktree": ".vibe/worktrees/n", "branch": "branch-n",
            "allowlist": ["vibe_guide/a.py"], "evidence_ref": "evidence/brief.json",
        }
        data.update(overrides)
        return ImplementationBrief.from_mapping(data)

    def test_binding_drift_reports_concrete_checks(self):
        self.node.contract.update({"plan_id": "plan", "task_id": "task-n",
                                   "branch": "branch-n"})
        with tempfile.TemporaryDirectory() as root:
            entry = Path(root) / "vibe_guide" / "a.py"
            entry.parent.mkdir(parents=True)
            entry.write_text("def entry(): pass\n", encoding="utf-8")
            validation = validate_implementation_brief(
                self._valid_brief(branch="other-branch"), self.manifest, self.node,
                project_root=Path(root),
            )
        self.assertFalse(validation.valid)
        self.assertIn("branch", validation.missing)
        self.assertIn("binding.branch", validation.evidence["checks"])

    def test_missing_real_entrypoint_is_fail_closed(self):
        validation = validate_implementation_brief(
            self._valid_brief(), self.manifest, self.node,
            project_root=Path(tempfile.mkdtemp()),
        )
        self.assertFalse(validation.valid)
        self.assertTrue(any("entrypoint" in item for item in validation.missing))

    def test_valid_brief_contains_structured_evidence(self):
        self.node.contract.update({"plan_id": "plan", "task_id": "task-n",
                                   "branch": "branch-n"})
        with tempfile.TemporaryDirectory() as root:
            entry = Path(root) / "vibe_guide" / "a.py"
            entry.parent.mkdir(parents=True)
            entry.write_text("def entry(): pass\n", encoding="utf-8")
            validation = validate_implementation_brief(
                self._valid_brief(), self.manifest, self.node,
                project_root=Path(root),
            )
        self.assertTrue(validation.valid, validation.missing)
        self.assertEqual(validation.evidence["status"], "implementing")
        self.assertEqual(validation.evidence["issue_id"], "n")
        self.assertEqual(validation.evidence["invariants"][0]["id"], "I1")


if __name__ == "__main__":
    unittest.main()
