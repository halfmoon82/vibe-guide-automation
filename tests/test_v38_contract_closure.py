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


def _closure_payload(**overrides):
    payload = {
        "issue_id": "V38-9", "goal": "改造设置页交互", "non_goals": [],
        "owned_paths": ["src/settings.vue"], "read_paths": [],
        "call_chain": ["src/settings.vue:render"],
        "invariants": [{"id": "I1", "entrypoint": "src/settings.vue:render",
                        "positive_case": "ok", "negative_case": "bad",
                        "test_command": "python -m unittest"}],
        "expected_red": "fails", "risk_notes": [], "base_sha": "a" * 40,
        "plan_revision": 1, "execution_epoch": 0, "evidence_ref": "ref",
    }
    payload.update(overrides)
    return payload


def _closure_root(directory):
    root = Path(directory)
    (root / "src").mkdir()
    (root / "src" / "settings.vue").write_text("<template/>\n", encoding="utf-8")
    (root / "package.json").write_text('{"dependencies": {"element-plus": "2.3.7"}}\n', encoding="utf-8")
    return root


class EnvironmentFactsClosureTests(unittest.TestCase):
    def test_third_party_ui_change_without_environment_facts_is_not_closed(self):
        issue = IssueContract.from_mapping(_closure_payload(
            goal="把设置页表格换成 element-plus 的 el-table 用法",
        ))
        with tempfile.TemporaryDirectory() as directory:
            result = check_contract_closure(issue, _closure_root(directory))
        self.assertFalse(result.closed)
        self.assertTrue(any("environment_facts" in item for item in result.missing))

    def test_third_party_ui_marker_matching_is_case_insensitive(self):
        issue = IssueContract.from_mapping(_closure_payload(
            goal="升级 Element-Plus 组件用法", environment_facts=[],
        ))
        with tempfile.TemporaryDirectory() as directory:
            result = check_contract_closure(issue, _closure_root(directory))
        self.assertFalse(result.closed)
        self.assertTrue(any("environment_facts" in item for item in result.missing))

    def test_third_party_ui_change_with_verified_fact_is_closed(self):
        issue = IssueContract.from_mapping(_closure_payload(
            goal="把设置页表格换成 element-plus 的 el-table 用法",
            environment_facts=[{
                "fact": "element-plus 实装 2.3.7 无 value prop",
                "source": "package.json",
                "verified_at": "2026-10-04",
            }],
        ))
        with tempfile.TemporaryDirectory() as directory:
            result = check_contract_closure(issue, _closure_root(directory))
        self.assertTrue(result.closed, result.missing)

    def test_environment_fact_missing_fact_or_source_is_not_closed(self):
        for facts in (
            [{"fact": "", "source": "package.json", "verified_at": "2026-10-04"}],
            [{"fact": "element-plus 实装 2.3.7 无 value prop", "source": "", "verified_at": "2026-10-04"}],
            [{"source": "package.json", "verified_at": "2026-10-04"}],
            [{"fact": "element-plus 实装 2.3.7 无 value prop", "verified_at": "2026-10-04"}],
        ):
            issue = IssueContract.from_mapping(_closure_payload(environment_facts=facts))
            with tempfile.TemporaryDirectory() as directory:
                result = check_contract_closure(issue, _closure_root(directory))
            self.assertFalse(result.closed, facts)
            self.assertTrue(any("environment_facts[0]." in item for item in result.missing), result.missing)

    def test_environment_fact_source_repo_path_must_exist(self):
        issue = IssueContract.from_mapping(_closure_payload(
            environment_facts=[{
                "fact": "element-plus 实装 2.3.7 无 value prop",
                "source": "frontend/package-lock.json",
                "verified_at": "2026-10-04",
            }],
        ))
        with tempfile.TemporaryDirectory() as directory:
            result = check_contract_closure(issue, _closure_root(directory))
        self.assertFalse(result.closed)
        self.assertTrue(any("environment_facts[0].source" in item for item in result.missing))

    def test_environment_fact_source_url_or_prose_needs_no_file(self):
        issue = IssueContract.from_mapping(_closure_payload(
            goal="把设置页表格换成 element-plus 的 el-table 用法",
            environment_facts=[
                {"fact": "el-table 无 value prop", "source": "https://element-plus.org/zh-CN/component/table.html", "verified_at": "2026-10-04"},
                {"fact": "官方文档未列 value", "source": "官方文档", "verified_at": "2026-10-04"},
            ],
        ))
        with tempfile.TemporaryDirectory() as directory:
            result = check_contract_closure(issue, _closure_root(directory))
        self.assertTrue(result.closed, result.missing)

    def test_legacy_contract_without_environment_facts_still_closes(self):
        issue = IssueContract.from_mapping(_closure_payload())
        with tempfile.TemporaryDirectory() as directory:
            result = check_contract_closure(issue, _closure_root(directory))
        self.assertTrue(result.closed, result.missing)
