"""Model directory disclosure must stay advisory and role-specific."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide import model_catalog
from vibe_guide.authorization import build_authorization_card
from vibe_guide.doctor import doctor
from vibe_guide.models import AgentCapabilities, Plan
from vibe_guide.paths import ProjectPaths
from vibe_guide.scanner import scan_project
from tests.test_integrated_prd_acceptance import _isolate_user_dirs
from tests.test_model_tiers import _node


class ModelDisclosureTests(unittest.TestCase):
    def setUp(self):
        _isolate_user_dirs(self)
        temporary = tempfile.TemporaryDirectory(prefix="r4-model-disclosure-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = Path(os.environ["VIBE_HOME"])

    def test_doctor_uses_global_mapping_and_only_marks_affected_tiers_default(self):
        (self.home / "config.json").write_text(json.dumps({
            "model_map": {"codex": {"deep": ["manual-deep"]}}
        }))
        report = scan_project(ProjectPaths(self.root))
        def timeout():
            raise TimeoutError()
        with patch.dict(model_catalog._PROBES, {"codex": timeout}):
            result = doctor(report)
        self.assertIn("models", result.facts, "doctor must disclose model resolution")
        rows = {(r["platform"], r["tier"]): r for r in result.facts["models"]["resolutions"]}
        self.assertEqual(rows["codex", "deep"]["model"], "manual-deep")
        self.assertEqual(rows["codex", "deep"]["status"], "ok")
        self.assertEqual(rows["codex", "light"]["status"], "unknown")
        self.assertTrue(any("codex" in w and "light" in w and "standard" in w for w in result.warnings))
        self.assertFalse(any("所有 worker" in w for w in result.warnings))
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["config.json"])
        self.assertEqual(list(self.root.iterdir()), [])
        with patch.dict(model_catalog._PROBES, {"codex": lambda: [{"slug": "available"}]}):
            known = doctor(report)
        self.assertEqual((result.ok, result.status, result.issues), (known.ok, known.status, known.issues))

    def test_authorization_card_resolves_reviewer_independently(self):
        (self.home / "config.json").write_text(json.dumps({"model_map": {"codex": {
            "light": ["writer-small"], "standard": ["reviewer-mid", "reviewer-backup"]
        }}}))
        node = _node("docs", files=["docs/a.md"], adapter_id="codex")
        plan = Plan("p", 1, "docs/prd.md", [node.id], "draft")
        capabilities = AgentCapabilities("codex", True, True, True, True, True, "full")
        card = build_authorization_card(plan, [node], capabilities)
        row = card.model_tiers[0]
        self.assertEqual(row["resolved_model"], "writer-small")
        self.assertEqual(row.get("reviewer_resolved_model"), "reviewer-mid")
        self.assertEqual(row["reviewer_fallbacks"], ["reviewer-backup"])
        self.assertEqual(row["reviewer_effort"], "medium")
        self.assertEqual(row["reviewer_resolution"], "ok")

    def test_card_uses_the_dispatch_platform_from_each_node(self):
        node = _node("docs", files=["docs/a.md"], adapter_id="claude-code")
        plan = Plan("p", 1, "docs/prd.md", [node.id], "draft")
        capabilities = AgentCapabilities("codex", True, True, True, True, True, "full")
        card = build_authorization_card(plan, [node], capabilities)
        self.assertEqual(card.model_tiers[0]["resolved_model"], "haiku")
        self.assertEqual(card.model_tiers[0].get("reviewer_resolved_model"), "sonnet")

    def test_card_preserves_explicit_routed_model_instead_of_advertising_directory(self):
        node = _node("explicit", adapter_id="claude-code")
        node.contract["worker_profile"].update(model="already-routed", reasoning="deep")
        plan = Plan("p", 1, "docs/prd.md", [node.id], "draft")
        capabilities = AgentCapabilities("claude-code", True, True, True, True, True, "full")
        row = build_authorization_card(plan, [node], capabilities).model_tiers[0]
        self.assertEqual(row["resolved_model"], "already-routed")
        self.assertEqual(row["reviewer_resolved_model"], "already-routed")
        self.assertEqual(row["effort"], "high")
        self.assertEqual(row["source"], "contract")

    def test_card_preserves_legacy_explicit_model_without_inventing_effort(self):
        node = _node("legacy", adapter_id="claude-code")
        legacy = {
            "worker": "reviewer", "writer": "reviewer", "model": "existing-model",
            "worktree": "wt-ir", "branch": "node/ir", "allowlist": ["."],
        }
        node.contract["worker_profile"] = dict(legacy)
        plan = Plan("p", 1, "docs/prd.md", [node.id], "draft")
        capabilities = AgentCapabilities("claude-code", True, True, True, True, True, "full")
        row = build_authorization_card(plan, [node], capabilities).model_tiers[0]
        self.assertEqual(row["resolved_model"], "existing-model")
        self.assertEqual(row["reviewer_resolved_model"], "existing-model")
        self.assertIsNone(row["effort"])
        self.assertIsNone(row["reviewer_effort"])
        self.assertEqual(row["resolution"], "unknown")
        self.assertEqual(row["source"], "contract")
        self.assertEqual(node.contract["worker_profile"], legacy)

    def test_doctor_reports_illegal_mapping_as_invalid_instead_of_default_unknown(self):
        (self.home / "config.json").write_text('{"model_map":{"codex":{"normal":["bad"]}}}')
        result = doctor(scan_project(ProjectPaths(self.root)))
        self.assertEqual(result.status, 'blocked')
        self.assertFalse(result.ok)
        self.assertTrue(any('model_map.codex.normal' in issue for issue in result.issues))
        self.assertEqual(result.facts['models']['status'], 'invalid')
        self.assertEqual(result.facts['models']['resolutions'], [])

    def test_available_other_platform_is_disclosed_as_default_without_inventing_probe(self):
        report = scan_project(ProjectPaths(self.root))
        report.agent_commands = {'workbuddy': True}
        result = doctor(report)
        rows = [r for r in result.facts['models']['resolutions'] if r['platform'] == 'workbuddy']
        self.assertEqual([(r['tier'], r['status'], r['model']) for r in rows], [
            ('light', 'unknown', 'default'), ('standard', 'unknown', 'default'), ('deep', 'unknown', 'default'),
        ])
