import tempfile
import unittest
from pathlib import Path

from vibe_guide.contracts import IssueContract, check_contract_closure


class V38ContractClosureTests(unittest.TestCase):
    def test_missing_entrypoint_is_not_closed(self):
        issue = IssueContract.from_mapping({
            "issue_id": "V38-1", "goal": "goal", "non_goals": [],
            "owned_paths": ["vibe_guide/missing.py"], "read_paths": [],
            "call_chain": ["vibe_guide/missing.py:entry"],
            "invariants": [{"id": "I1", "entrypoint": "vibe_guide/missing.py:entry",
                            "positive_case": "ok", "negative_case": "bad",
                            "test_command": "python -m unittest"}],
            "expected_red": "fails", "risk_notes": [], "base_sha": "a" * 40,
            "plan_revision": 1, "execution_epoch": 0, "evidence_ref": "ref",
        })
        result = check_contract_closure(issue, Path(tempfile.mkdtemp()))
        self.assertFalse(result.closed)
        self.assertIn("entrypoint", " ".join(result.missing))

    def test_allowlist_and_ownership_gaps_are_distinct_from_missing_entrypoint(self):
        issue = IssueContract.from_mapping({
            "issue_id": "V38-2", "goal": "goal", "non_goals": [],
            "owned_paths": ["vibe_guide/preflight.py"],
            "read_paths": ["vibe_guide/authorization.py"],
            "call_chain": ["vibe_guide/preflight.py:run_preflight", "vibe_guide/bridge.py:observe"],
            "invariants": [{"id": "I1", "entrypoint": "vibe_guide/preflight.py:run_preflight",
                            "positive_case": "ok", "negative_case": "bad", "test_command": "python -m unittest"}],
            "expected_red": "fails", "risk_notes": [], "base_sha": "a" * 40,
            "plan_revision": 3, "execution_epoch": 0, "evidence_ref": "ref",
            "allowlist": ["vibe_guide/preflight.py"],
            "ownership": {"owner": "V38-2", "paths": ["vibe_guide/preflight.py"]},
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "vibe_guide").mkdir()
            (root / "vibe_guide" / "preflight.py").write_text("def run_preflight(): pass\n", encoding="utf-8")
            result = check_contract_closure(issue, root)
        self.assertFalse(result.closed)
        self.assertTrue(any("allowlist" in item for item in result.missing))
        self.assertTrue(any("ownership" in item or "call_chain" in item for item in result.missing))
        self.assertFalse(any("entrypoint missing" in item for item in result.missing))

    def test_closed_contract_requires_real_entrypoint_and_complete_invariant(self):
        issue = IssueContract.from_mapping({
            "issue_id": "V38-2", "goal": "goal", "non_goals": [],
            "owned_paths": ["vibe_guide/preflight.py"], "read_paths": [],
            "call_chain": ["vibe_guide/preflight.py:run_preflight"],
            "invariants": [{"id": "I1", "entrypoint": "vibe_guide/preflight.py:run_preflight",
                            "positive_case": "ok", "negative_case": "bad", "test_command": "python -m unittest"}],
            "expected_red": "fails", "risk_notes": [], "base_sha": "a" * 40,
            "plan_revision": 3, "execution_epoch": 0, "evidence_ref": "ref",
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "vibe_guide").mkdir()
            (root / "vibe_guide" / "preflight.py").write_text("def run_preflight(): pass\n", encoding="utf-8")
            result = check_contract_closure(issue, root)
        self.assertTrue(result.closed)

    def test_paths_outside_project_are_rejected_before_entrypoint_check(self):
        outside = Path(tempfile.mkdtemp()) / "outside.py"
        outside.write_text("def run_preflight(): pass\n", encoding="utf-8")
        issue = IssueContract.from_mapping({
            "issue_id": "V38-2", "goal": "goal", "non_goals": [],
            "owned_paths": ["../outside.py"], "read_paths": [],
            "call_chain": ["../outside.py:run_preflight"],
            "invariants": [{"id": "I1", "entrypoint": "../outside.py:run_preflight", "positive_case": "ok", "negative_case": "bad", "test_command": "python -m unittest"}],
            "expected_red": "fails", "risk_notes": [], "base_sha": "a" * 40,
            "plan_revision": 3, "execution_epoch": 0, "evidence_ref": "ref",
            "allowlist": ["../outside.py"],
        })
        with tempfile.TemporaryDirectory() as directory:
            result = check_contract_closure(issue, Path(directory))
        self.assertFalse(result.closed)
        self.assertTrue(any("outside" in item or "invalid" in item for item in result.missing))

    def test_absolute_allowlist_and_owned_path_are_rejected(self):
        issue = IssueContract.from_mapping({
            "issue_id": "V38-2", "goal": "goal", "non_goals": [],
            "owned_paths": ["/tmp/outside.py"], "read_paths": [],
            "call_chain": ["vibe_guide/preflight.py:run_preflight"],
            "invariants": [{"id": "I1", "entrypoint": "vibe_guide/preflight.py:run_preflight", "positive_case": "ok", "negative_case": "bad", "test_command": "python -m unittest"}],
            "expected_red": "fails", "risk_notes": [], "base_sha": "a" * 40,
            "plan_revision": 3, "execution_epoch": 0, "evidence_ref": "ref",
            "allowlist": ["/tmp/outside.py"],
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "vibe_guide").mkdir()
            (root / "vibe_guide" / "preflight.py").write_text("def run_preflight(): pass\n", encoding="utf-8")
            result = check_contract_closure(issue, root)
        self.assertFalse(result.closed)
        self.assertTrue(any("outside project" in item for item in result.missing))


if __name__ == "__main__":
    unittest.main()
