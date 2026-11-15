"""V6.0 P3 migration & measurement closeout contract tests.

Scope (product spec node v6-p3-migration): README upgrade notes, a
per-combination binding report derived mechanically from the frozen support
matrix, and compatibility/rollback fixtures for v5.2.7 -> V6.0.

This node does NOT perform real installs, model downloads, Ollama setup, real
provider triggering, deploy, or release.  Every real-machine combination is
reported as unverified (PRD B25, design A1-A12, error_behavior): a missing
required combination is reported as unverified, and failed combinations are
never deleted afterwards to fake a green report.
"""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from vibe_guide.installation import (
    inspect_compatibility,
    migration_preview,
    migrate_state,
    rollback_state,
)

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures" / "v6_upgrade"

UNVERIFIED_REASON = (
    "需真机/真实 provider/真实用量证据；本节点为文档与夹具收口，"
    "不执行真实安装、模型下载、Ollama 安装、系统配置或发布（PRD 非目标与约束）。"
)

ALL_B_REFS = ["B15", "B16", "B17", "B18", "B19", "B20",
              "B21", "B22", "B23", "B24", "B25"]
ALL_A_REFS = ["A1", "A2", "A3", "A4", "A5", "A6",
              "A7", "A8", "A9", "A10", "A11", "A12"]


def load_fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def build_binding_report(matrix):
    """Deterministically emit one report entry per required combination.

    No real evidence is available to this worker, so each combination is
    reported as unverified with usage 'unknown' (never 0).  Combinations are
    never dropped: B25 forbids deleting failed combinations to fake a green.
    """
    return [
        {
            "id": combo["id"],
            "status": "未验收",
            "evidence": None,
            "usage": "unknown",
            "unverified_reason": UNVERIFIED_REASON,
        }
        for combo in matrix["required_combinations"]
    ]


def validate_binding_report(report, matrix):
    """Independent contract check for the B25 / error_behavior rules."""
    violations = []
    required = [c["id"] for c in matrix["required_combinations"]]
    combos = report.get("combinations", [])
    got = [c.get("id") for c in combos]
    for rid in required:
        if rid not in got:
            violations.append("MISSING_COMBO:" + rid)
    for gid in got:
        if gid not in required:
            violations.append("EXTRA_COMBO:" + str(gid))
    if len(got) != len(set(got)):
        violations.append("DUPLICATE_COMBO")
    unverified = [c for c in combos if c.get("status") != "已验收"]
    if unverified and report.get("complete_release_claimed"):
        violations.append("FALSE_COMPLETE_RELEASE_CLAIM")
    if unverified and report.get("release_status") == "complete":
        violations.append("FALSE_OVERALL_STATUS")
    if unverified and report.get("failed_combinations_retained") is False:
        violations.append("DELETED_FAILED_COMBO")
    for c in combos:
        if c.get("status") != "已验收" and c.get("usage") in (0, 0.0):
            violations.append("USAGE_ZERO_FOR_UNVERIFIED:" + str(c.get("id")))
        if c.get("status") == "已验收" and not c.get("evidence"):
            violations.append("VERIFIED_WITHOUT_EVIDENCE:" + str(c.get("id")))
    return violations


class FrozenMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matrix = load_fixture("v6_support_matrix.json")

    def test_matrix_is_frozen_and_not_a_complete_release(self):
        self.assertEqual(self.matrix["matrix_id"], "v6-support-matrix/1")
        self.assertEqual(self.matrix["status"], "blocked_pending_scope")
        self.assertEqual(self.matrix["frozen_on"], "2026-11-09")

    def test_required_combinations_are_mechanically_enumerable(self):
        combos = self.matrix["required_combinations"]
        self.assertEqual(len(combos), 27)
        ids = [c["id"] for c in combos]
        self.assertEqual(len(ids), len(set(ids)), "combination ids must be unique")
        install = [c for c in combos if c["category"] == "install"]
        entry = [c for c in combos if c["category"] == "entry"]
        self.assertEqual(len(install), 12)
        self.assertEqual(len(entry), 15)
        self.assertTrue(all(c["requires_real_evidence"] for c in combos))
        self.assertTrue(all(c["dimensions"] for c in combos))

    def test_install_grid_is_four_paths_by_three_edges(self):
        install = [c for c in self.matrix["required_combinations"]
                   if c["category"] == "install"]
        paths = {(c["dimensions"]["install_path"], c["dimensions"]["python"])
                 for c in install}
        edges = {c["dimensions"]["upgrade_edge"] for c in install}
        self.assertEqual(len(paths), 4)
        self.assertEqual(len(edges), 3)
        self.assertEqual(len(install), 4 * 3)

    def test_entry_grid_is_three_providers_by_five_scenarios(self):
        entry = [c for c in self.matrix["required_combinations"]
                 if c["category"] == "entry"]
        providers = {c["dimensions"]["provider"] for c in entry}
        scenarios = {c["dimensions"]["scenario"] for c in entry}
        self.assertEqual(len(providers), 3)
        self.assertEqual(len(scenarios), 5)
        self.assertEqual(len(entry), 3 * 5)

    def test_both_upgrade_edges_are_required(self):
        edges = self.matrix["scope"]["upgrade_edges"]
        self.assertIn("V5.2.7_to_V6.0", edges)
        self.assertIn("V5.2.8_to_V6.0", edges)

    def test_scenario_coverage_lists_b15_through_b25(self):
        self.assertEqual(sorted(self.matrix["scenario_coverage"].keys()),
                         sorted(ALL_B_REFS))
        gates = [g["ref"] for g in self.matrix["general_gates"]]
        self.assertIn("B25", gates)

    def test_design_acceptance_refs_cover_a1_through_a12(self):
        self.assertEqual(self.matrix["design_acceptance_refs"], ALL_A_REFS)


class BindingReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matrix = load_fixture("v6_support_matrix.json")
        cls.report = load_fixture("v6_binding_report.json")

    def test_report_covers_every_combination_without_violations(self):
        self.assertEqual(validate_binding_report(self.report, self.matrix), [])
        self.assertEqual(len(self.report["combinations"]),
                         len(self.matrix["required_combinations"]))

    def test_combinations_are_mechanically_generated_no_manual_drops(self):
        # Editing the shipped JSON to drop a failed combination breaks equality.
        self.assertEqual(self.report["combinations"],
                         build_binding_report(self.matrix))

    def test_no_false_complete_release_claim(self):
        self.assertFalse(self.report["complete_release_claimed"])
        self.assertEqual(self.report["release_status"], "blocked_pending_scope")
        self.assertTrue(self.report["failed_combinations_retained"])
        combos = self.report["combinations"]
        self.assertTrue(all(c["status"] == "未验收" for c in combos))
        self.assertTrue(all(c["evidence"] is None for c in combos))
        self.assertTrue(all(c["usage"] == "unknown" for c in combos))

    def test_missing_combination_is_flagged_unverified_not_dropped(self):
        tampered = copy.deepcopy(self.report)
        removed = tampered["combinations"].pop(3)
        violations = validate_binding_report(tampered, self.matrix)
        self.assertIn("MISSING_COMBO:" + removed["id"], violations)

    def test_deleting_a_failed_combo_cannot_turn_the_report_green(self):
        tampered = copy.deepcopy(self.report)
        tampered["combinations"] = [c for c in tampered["combinations"]
                                    if c["id"] != tampered["combinations"][0]["id"]]
        tampered["complete_release_claimed"] = True
        tampered["release_status"] = "complete"
        tampered["failed_combinations_retained"] = False
        violations = validate_binding_report(tampered, self.matrix)
        self.assertIn("MISSING_COMBO:" + self.report["combinations"][0]["id"],
                      violations)
        self.assertIn("FALSE_COMPLETE_RELEASE_CLAIM", violations)
        self.assertIn("DELETED_FAILED_COMBO", violations)
        self.assertTrue(violations, "deleting failures must never yield green")

    def test_usage_is_unknown_not_zero_when_unmeasured(self):
        tampered = copy.deepcopy(self.report)
        tampered["combinations"][0]["usage"] = 0
        self.assertIn(
            "USAGE_ZERO_FOR_UNVERIFIED:" + self.report["combinations"][0]["id"],
            validate_binding_report(tampered, self.matrix),
        )

    def test_claiming_verified_without_evidence_is_rejected(self):
        tampered = copy.deepcopy(self.report)
        tampered["combinations"][0]["status"] = "已验收"
        self.assertIn(
            "VERIFIED_WITHOUT_EVIDENCE:" + self.report["combinations"][0]["id"],
            validate_binding_report(tampered, self.matrix),
        )

    def test_duplicate_and_injected_combinations_are_rejected(self):
        dup = copy.deepcopy(self.report)
        dup["combinations"].append(copy.deepcopy(dup["combinations"][0]))
        self.assertIn("DUPLICATE_COMBO",
                      validate_binding_report(dup, self.matrix))
        extra = copy.deepcopy(self.report)
        injected = copy.deepcopy(extra["combinations"][0])
        injected["id"] = "entry.injected.not_in_matrix"
        extra["combinations"].append(injected)
        self.assertIn("EXTRA_COMBO:entry.injected.not_in_matrix",
                      validate_binding_report(extra, self.matrix))

    def test_unverified_items_and_out_of_scope_actions_are_honest(self):
        items = " ".join(self.report["unverified_items"])
        for needle in ("安装", "provider", "Ollama", "模型", "用量", "未"):
            self.assertIn(needle, items)
        actions = " ".join(self.report["out_of_scope_actions"])
        for needle in ("不", "安装", "下载", "发布"):
            self.assertIn(needle, actions)
        self.assertIn("no_real_install", self.report["execution_flags"])

    def test_design_acceptance_a1_a12_honestly_unverified(self):
        da = self.report["design_acceptance"]
        self.assertEqual([d["ref"] for d in da], ALL_A_REFS)
        for d in da:
            self.assertIn("未", d["status"])
            self.assertTrue(d["owner_node"])

    def test_acceptance_refs_match_matrix_scenarios(self):
        self.assertEqual(sorted(self.report["acceptance_refs"]),
                         sorted(ALL_B_REFS))

    def test_usage_baseline_is_pre_v6_only_and_unknown_not_zero(self):
        usage = self.report["usage"]
        self.assertFalse(usage["measurement_performed"])
        self.assertEqual(usage["v6_combination_usage"], "unknown")
        self.assertFalse(usage["baseline_is_v6_measurement"])
        self.assertIn("不填 0", usage["unknown_policy"])
        self.assertTrue(usage["baseline_sample_known_partial_tokens"])


class UpgradeRollbackCompatibilityTests(unittest.TestCase):
    """Exercise the real installation.py migrate/rollback against a v5.2.7 fixture."""

    @staticmethod
    def _materialize(tmp):
        vibe = tmp / ".vibe"
        shutil.copytree(FIX / "v527_project", vibe)
        return vibe

    @staticmethod
    def _snapshot(vibe):
        return {p.relative_to(vibe).as_posix(): p.read_bytes()
                for p in sorted(vibe.rglob("*")) if p.is_file()}

    def test_preview_and_compatibility_checks_are_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            vibe = self._materialize(Path(d))
            before = self._snapshot(vibe)
            report = inspect_compatibility(Path(d))
            preview = migration_preview(Path(d))
            self.assertTrue(report["mixed"])
            self.assertTrue(preview["read_only"])
            self.assertEqual(self._snapshot(vibe), before)

    def test_upgrade_preserves_source_active_runs_and_customizations(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            vibe = self._materialize(tmp)
            before = self._snapshot(vibe)
            ns = migration_preview(tmp)["target_namespace"]
            result = migrate_state(tmp)
            self.assertEqual(result["status"], "complete")
            self.assertTrue(result["migrated"])
            for rel, content in before.items():
                self.assertEqual((vibe / rel).read_bytes(), content, rel)
            # Legacy state is copied into a read-only historical namespace,
            # never moved; the active run and local customization stay in place.
            self.assertTrue((vibe / "namespaces" / ns / "history" / "state.json").is_file())
            self.assertTrue((vibe / "runs" / "active-run" / "events.jsonl").is_file())
            self.assertTrue((vibe / "proposals" / "skills" / "custom" / "SKILL.md").is_file())
            self.assertTrue((vibe / "migration_evidence.json").is_file())
            self.assertTrue((vibe / "history_manifest.json").is_file())
            self.assertTrue((vibe / "rollback_evidence.json").is_file())

    def test_rollback_only_touches_this_rounds_isolated_area(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            vibe = self._materialize(tmp)
            before = self._snapshot(vibe)
            orig_top = {p.name for p in vibe.iterdir()}
            migrate_state(tmp)
            result = rollback_state(tmp)
            self.assertEqual(result["status"], "complete")
            self.assertTrue(result["source_unchanged"])
            self.assertTrue(result["current_namespace_preserved"])
            self.assertEqual(result["conflicts"], [])
            # Rollback never modifies or deletes any pre-existing user file.
            for rel, content in before.items():
                self.assertTrue((vibe / rel).is_file(), rel)
                self.assertEqual((vibe / rel).read_bytes(), content, rel)
            after_top = {p.name for p in vibe.iterdir()}
            allowed_new = {"namespaces", "migration_evidence.json",
                           "history_manifest.json", "rollback_evidence.json",
                           "rollback"}
            self.assertTrue((after_top - orig_top) <= allowed_new)
            self.assertTrue(orig_top <= after_top)
            self.assertTrue((vibe / "rollback" / "state.json").is_file())

    def test_rollback_is_fail_closed_on_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            vibe = self._materialize(tmp)
            migrate_state(tmp)
            conflict = vibe / "rollback" / "state.json"
            conflict.parent.mkdir(parents=True, exist_ok=True)
            conflict.write_bytes(b"TAMPERED-DO-NOT-OVERWRITE")
            result = rollback_state(tmp)
            self.assertEqual(result["status"], "blocked_invalid")
            self.assertTrue(result["conflicts"])
            self.assertEqual(conflict.read_bytes(), b"TAMPERED-DO-NOT-OVERWRITE")

    def test_rollback_without_history_is_blocked_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._materialize(tmp)  # no migrate_state -> no history namespace
            result = rollback_state(tmp)
            self.assertEqual(result["status"], "blocked_unknown")
            self.assertIn("history", result["reason"])

    def test_upgrade_is_idempotent_on_retry(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            vibe = self._materialize(tmp)
            before = self._snapshot(vibe)
            first = migrate_state(tmp)
            second = migrate_state(tmp)
            self.assertEqual(first["status"], "complete")
            self.assertEqual(second["status"], "complete")
            for rel, content in before.items():
                self.assertEqual((vibe / rel).read_bytes(), content, rel)


class ReadmeUpgradeNotesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_readme_documents_upgrade_rollback_and_honest_status(self):
        self.assertIn("## V6.0 迁移、升级与实测收口", self.readme)
        for needle in ("V5.2.7", "V5.2.8", "V6.0", "回滚只撤销本轮自有变更",
                       "blocked_pending_scope", "未验收", "绑定报告"):
            self.assertIn(needle, self.readme)
        self.assertNotIn("已完整发布", self.readme)

    def test_readme_has_no_retired_labels_or_stale_backticked_tag(self):
        self.assertNotIn("自动修复中", self.readme)
        self.assertNotIn("自动推进中", self.readme)
        # release-tag-gate: historical tags must be uppercase V without backticks.
        self.assertNotIn("`v5.2.8`", self.readme)


if __name__ == "__main__":
    unittest.main()
