"""Host rule exception (#152, AC-H1): init decision item and user-level block.

Every test runs under a temporary HOME; the real user directory is
fingerprinted before and after the module to prove it is never touched.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide import host_rules
from vibe_guide.capability_wizard import build_capability_catalog
from vibe_guide.cli import run_cli
from vibe_guide.doctor import doctor
from vibe_guide.paths import ProjectPaths
from vibe_guide.scanner import scan_project


_REAL_HOME = Path(os.path.expanduser("~"))
_REAL_FILES = tuple(_REAL_HOME / rel for rel in host_rules.CANDIDATES)


def _fingerprint():
    result = {}
    for path in _REAL_FILES:
        try:
            result[str(path)] = (
                path.read_bytes(),
                os.path.lexists(str(path) + host_rules.BACKUP_SUFFIX),
            )
        except OSError:
            result[str(path)] = None
    return result


_BEFORE = None


def setUpModule():
    global _BEFORE
    _BEFORE = _fingerprint()


def tearDownModule():
    assert _fingerprint() == _BEFORE, "real user rule files changed"


def _tree(root):
    """Map of every path under *root* to its bytes (None for directories)."""
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            result[os.path.relpath(os.path.join(dirpath, name), root)] = None
        for name in filenames:
            full = os.path.join(dirpath, name)
            result[os.path.relpath(full, root)] = (
                os.readlink(full) if os.path.islink(full) else Path(full).read_bytes()
            )
    return result


class HostRulesTestBase(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup, base)
        self.home = base / "home"
        self.project = base / "project"
        self.home.mkdir()
        self.project.mkdir()
        patcher = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "VIBE_HOME": str(base / "vibe-home")},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(Path.home(), self.home)
        self.claude = self.home / ".claude" / "CLAUDE.md"
        self.agents = self.home / "AGENTS.md"
        self.claude.parent.mkdir()
        self.claude.write_bytes("# 全局规则\n\n- 用中文。\n".encode("utf-8"))
        self.agents.write_bytes("# Agents\n\n对齐门照常执行。".encode("utf-8"))
        self.originals = {
            self.claude: self.claude.read_bytes(),
            self.agents: self.agents.read_bytes(),
        }

    @staticmethod
    def _cleanup(base):
        for dirpath, dirnames, filenames in os.walk(base):
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                if not os.path.islink(full):
                    os.chmod(full, 0o700)
        shutil.rmtree(base, ignore_errors=True)

    def cli(self, *argv):
        return run_cli(list(argv) + ["--json"], self.project)

    def init(self, *extra):
        return self.cli("init", "--confirm", *extra)

    def record(self):
        path = self.project / ".vibe" / "host-rules.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def apply(self):
        preview = self.init("--host-rules", "apply")
        digest = preview.payload["host_rules"]["digest"]
        return self.init("--host-rules", "apply", "--host-rules-digest", digest)


class DecisionItemTests(HostRulesTestBase):
    def test_plain_init_requires_answer_without_touching_home(self):
        before = _tree(self.home)
        result = self.init()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload["status"], "ok")
        hr = result.payload["host_rules"]
        self.assertEqual(hr["status"], "decision_required")
        self.assertIn("是否在用户级规则中加入 vibe 监工会话例外", hr["question"])
        self.assertEqual([o["id"] for o in hr["options"]], ["skip", "apply"])
        self.assertTrue(any("--host-rules skip" in c for c in hr["commands"]))
        self.assertIn("是否在用户级规则中加入 vibe 监工会话例外", result.text)
        self.assertEqual(_tree(self.home), before)
        self.assertIsNone(self.record())
        second = self.init()
        self.assertFalse(second.payload["changed"])
        self.assertEqual(second.payload["host_rules"]["status"], "decision_required")

    def test_skip_records_answer_and_leaves_home_byte_identical(self):
        before = _tree(self.home)
        result = self.init("--host-rules", "skip")
        self.assertEqual(result.payload["status"], "ok")
        self.assertEqual(result.payload["host_rules"]["decision"], "skip")
        self.assertEqual(self.record()["decision"], "skip")
        self.assertEqual(_tree(self.home), before)
        again = self.init()
        self.assertEqual(again.payload["host_rules"]["decision"], "skip")
        self.assertNotEqual(again.payload["host_rules"]["status"], "decision_required")

    def test_catalog_marks_host_rules_sensitive(self):
        items = build_capability_catalog(
            {"capabilities": [{"name": host_rules.HOST_RULES_ID}]}
        )
        self.assertEqual(items[0].layer, "sensitive_external")


class ApplyTests(HostRulesTestBase):
    def test_apply_without_digest_previews_and_writes_nothing(self):
        before = _tree(self.home)
        result = self.init("--host-rules", "apply")
        hr = result.payload["host_rules"]
        self.assertEqual(hr["status"], "preview")
        self.assertIn("~/.claude/CLAUDE.md", hr["diff"])
        self.assertIn("+" + host_rules.END_MARKER, hr["diff"])
        self.assertRegex(hr["digest"], "^[0-9a-f]{64}$")
        self.assertIn(hr["digest"], " ".join(hr["commands"]))
        self.assertEqual(_tree(self.home), before)
        self.assertIsNone(self.record())

    def test_wrong_digest_is_rejected_without_writes(self):
        before = _tree(self.home)
        result = self.init("--host-rules", "apply", "--host-rules-digest", "0" * 64)
        self.assertEqual(result.payload["status"], "ok")
        self.assertEqual(result.payload["host_rules"]["status"], "blocked")
        self.assertEqual(_tree(self.home), before)
        self.assertIsNone(self.record())

    def test_apply_writes_block_backup_and_relative_record(self):
        result = self.apply()
        hr = result.payload["host_rules"]
        self.assertEqual(hr["status"], "applied", hr)
        for path, original in self.originals.items():
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith(original.decode("utf-8")))
            self.assertEqual(text.count(host_rules.END_MARKER), 1)
            self.assertIn("vibe supervisor-beat", text)
            backup = Path(str(path) + host_rules.BACKUP_SUFFIX)
            self.assertEqual(backup.read_bytes(), original)
        self.assertFalse((self.home / ".codex").exists())
        record = self.record()
        self.assertEqual(record["decision"], "apply")
        raw = (self.project / ".vibe" / "host-rules.json").read_text(encoding="utf-8")
        self.assertNotIn(str(self.home), raw)
        self.assertEqual(len(record["applied"]), 2)
        for entry in record["applied"]:
            self.assertTrue(entry["path"].startswith("~/"))
            self.assertTrue(entry["backup"].startswith("~/"))
        doc = doctor(scan_project(ProjectPaths.from_cwd(self.project)))
        files = doc.facts["host_rules"]["files"]
        self.assertEqual(doc.facts["host_rules"]["decision"], "apply")
        self.assertEqual(files["~/.claude/CLAUDE.md"], "current")
        self.assertEqual(files["~/.codex/AGENTS.md"], "absent")
        self.assertFalse(any("监工会话例外" in issue for issue in doc.issues))
        self.assertEqual(doc.warnings, [])
        for entry in record["applied"]:
            self.assertRegex(entry["backup_sha256"], "^[0-9a-f]{64}$")

    def test_reapply_and_reinit_are_idempotent(self):
        self.apply()
        after = _tree(self.home)
        record_bytes = (self.project / ".vibe" / "host-rules.json").read_bytes()
        again = self.apply()
        self.assertEqual(again.payload["host_rules"]["status"], "current")
        plain = self.init()
        self.assertFalse(plain.payload["changed"])
        self.assertEqual(plain.payload["host_rules"]["decision"], "apply")
        self.assertEqual(_tree(self.home), after)
        self.assertEqual(
            (self.project / ".vibe" / "host-rules.json").read_bytes(), record_bytes
        )

    def test_rollback_restores_original_bytes_and_removes_backup(self):
        self.apply()
        result = self.init("--host-rules", "rollback")
        self.assertEqual(
            result.payload["host_rules"]["status"], "rolled_back", result.payload
        )
        for path, original in self.originals.items():
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(os.path.lexists(str(path) + host_rules.BACKUP_SUFFIX))
        self.assertEqual(self.record()["decision"], "rolled_back")
        plain = self.init()
        self.assertNotEqual(plain.payload["host_rules"]["status"], "decision_required")

    def test_rollback_keeps_user_edits_outside_block(self):
        self.apply()
        with self.claude.open("a", encoding="utf-8") as stream:
            stream.write("\n- 用户后加的一行。\n")
        self.init("--host-rules", "rollback")
        text = self.claude.read_text(encoding="utf-8")
        self.assertNotIn(host_rules.END_MARKER, text)
        self.assertIn("- 用户后加的一行。", text)
        self.assertTrue(text.startswith(self.originals[self.claude].decode("utf-8")))
        self.assertFalse(os.path.lexists(str(self.claude) + host_rules.BACKUP_SUFFIX))
        self.assertEqual(self.agents.read_bytes(), self.originals[self.agents])

    def test_rollback_without_apply_is_noop(self):
        before = _tree(self.home)
        result = self.init("--host-rules", "rollback")
        self.assertEqual(result.payload["host_rules"]["status"], "noop")
        self.assertEqual(_tree(self.home), before)
        self.assertIsNone(self.record())

    def test_hand_edited_block_is_reported_and_apply_refuses(self):
        self.apply()
        text = self.claude.read_text(encoding="utf-8")
        edited = text.replace("## vibe 监工会话例外", "## vibe 监工会话例夕", 1)
        self.assertNotEqual(edited, text)
        self.claude.write_text(edited, encoding="utf-8")
        doc = doctor(scan_project(ProjectPaths.from_cwd(self.project)))
        self.assertEqual(
            doc.facts["host_rules"]["files"]["~/.claude/CLAUDE.md"], "modified"
        )
        self.assertTrue(
            any("~/.claude/CLAUDE.md" in w and "人工改过" in w for w in doc.warnings),
            doc.warnings,
        )
        self.assertFalse(any("人工改过" in i for i in doc.issues))
        before = _tree(self.home)
        preview = self.init("--host-rules", "apply")
        self.assertEqual(preview.payload["host_rules"]["status"], "blocked")
        self.assertIn("标记块被人工改过", preview.payload["host_rules"]["reason"])
        self.assertIsNone(host_rules.plan_apply(self.home)["digest"])
        self.assertEqual(_tree(self.home), before)
        rollback = self.init("--host-rules", "rollback")
        self.assertIn(rollback.payload["host_rules"]["status"], {"partial", "blocked"})
        self.assertEqual(self.claude.read_text(encoding="utf-8"), edited)

    def test_older_block_version_is_reported_stale_and_upgraded_in_place(self):
        old_block = host_rules._render_block("## 旧版\n", 0)
        self.claude.write_text("# 全局规则\n\n" + old_block + "\n尾部\n", encoding="utf-8")
        doc = doctor(scan_project(ProjectPaths.from_cwd(self.project)))
        self.assertEqual(doc.facts["host_rules"]["files"]["~/.claude/CLAUDE.md"], "stale")
        self.assertTrue(any("不是当前版本" in w for w in doc.warnings), doc.warnings)
        self.apply()
        text = self.claude.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 全局规则\n\n"))
        self.assertTrue(text.endswith("\n尾部\n"))
        self.assertNotIn("## 旧版", text)
        self.assertEqual(text.count(host_rules.END_MARKER), 1)

    def test_symlinked_target_is_refused_untouched(self):
        outside = self.home.parent / "dotfiles-AGENTS.md"
        outside.write_text("keep\n", encoding="utf-8")
        self.agents.unlink()
        self.agents.symlink_to(outside)
        before = _tree(self.home)
        preview = self.init("--host-rules", "apply")
        self.assertEqual(preview.payload["host_rules"]["status"], "blocked")
        self.assertIn("~/AGENTS.md", preview.payload["host_rules"]["reason"])
        forced = host_rules.apply(self.home, "0" * 64)
        self.assertEqual(forced["status"], "blocked")
        self.assertEqual(_tree(self.home), before)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep\n")

    def test_symlinked_parent_directory_is_refused(self):
        real = self.home.parent / "dotclaude"
        shutil.move(str(self.claude.parent), str(real))
        (self.home / ".claude").symlink_to(real, target_is_directory=True)
        before = (real / "CLAUDE.md").read_bytes()
        preview = self.init("--host-rules", "apply")
        self.assertEqual(preview.payload["host_rules"]["status"], "blocked")
        self.assertEqual((real / "CLAUDE.md").read_bytes(), before)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unwritable_target_blocks_but_init_completes(self):
        plan = host_rules.plan_apply(self.home)
        os.chmod(self.claude.parent, 0o555)
        before = _tree(self.home)
        result = self.init(
            "--host-rules", "apply", "--host-rules-digest", plan["digest"]
        )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload["status"], "ok")
        self.assertTrue((self.project / ".vibe" / "config.json").is_file())
        hr = result.payload["host_rules"]
        self.assertEqual(hr["status"], "blocked")
        self.assertIn("不可写", hr["reason"])
        self.assertEqual(_tree(self.home), before)
        self.assertIsNone(self.record())

    def test_backup_failure_writes_no_target(self):
        Path(str(self.agents) + host_rules.BACKUP_SUFFIX).mkdir()
        plan = host_rules.plan_apply(self.home)
        before = _tree(self.home)
        result = self.init(
            "--host-rules", "apply", "--host-rules-digest", plan["digest"]
        )
        self.assertEqual(result.payload["status"], "ok")
        self.assertEqual(result.payload["host_rules"]["status"], "blocked")
        self.assertIn("备份", result.payload["host_rules"]["reason"])
        self.assertEqual(_tree(self.home), before)

    def test_no_candidate_files_reports_reason_and_creates_nothing(self):
        self.claude.unlink()
        self.agents.unlink()
        before = _tree(self.home)
        result = self.init("--host-rules", "apply")
        hr = result.payload["host_rules"]
        self.assertEqual(hr["status"], "blocked")
        self.assertIn("未找到", hr["reason"])
        self.assertEqual(_tree(self.home), before)

    def test_skip_after_apply_is_refused(self):
        self.apply()
        result = self.init("--host-rules", "skip")
        self.assertEqual(result.payload["host_rules"]["status"], "blocked")
        self.assertEqual(self.record()["decision"], "apply")

    def test_tampered_record_cannot_point_rollback_elsewhere(self):
        self.apply()
        victim = self.home / "other.md"
        # Carries an intact block so only the path allowlist can protect it.
        victim_text = "victim\n\n" + host_rules.CURRENT_BLOCK + "\n"
        victim.write_text(victim_text, encoding="utf-8")
        record = self.record()
        record["applied"][0]["path"] = "~/other.md"
        (self.project / ".vibe" / "host-rules.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
        self.init("--host-rules", "rollback")
        self.assertEqual(victim.read_text(encoding="utf-8"), victim_text)


class ReworkTests(HostRulesTestBase):
    """Review round 1: F1 (backup ownership), F2 (cross-project), F3 (doctor)."""

    def edit_outside_block(self):
        with self.agents.open("a", encoding="utf-8") as stream:
            stream.write("\n用户在块后追加的一行。\n")
        return self.agents.read_text(encoding="utf-8")

    def test_reapply_when_current_then_rollback_keeps_user_edit(self):
        self.apply()
        entries_before = self.record()["applied"]
        self.edit_outside_block()
        again = self.apply()
        self.assertEqual(again.payload["host_rules"]["status"], "current")
        self.assertEqual(self.record()["applied"], entries_before)
        result = self.init("--host-rules", "rollback")
        self.assertEqual(result.payload["host_rules"]["status"], "rolled_back")
        text = self.agents.read_text(encoding="utf-8")
        self.assertNotIn(host_rules.END_MARKER, text)
        self.assertIn("用户在块后追加的一行。", text)
        self.assertTrue(text.startswith(self.originals[self.agents].decode("utf-8")))
        self.assertFalse(os.path.lexists(str(self.agents) + host_rules.BACKUP_SUFFIX))
        self.assertEqual(self.claude.read_bytes(), self.originals[self.claude])

    def _second_project(self):
        other = self.project.parent / "project-d"
        other.mkdir()
        return lambda *extra: run_cli(["init", "--confirm", *extra, "--json"], other)

    def test_two_projects_rollbacks_keep_edit_and_never_restore_foreign_backup(self):
        self.apply()  # project C writes the block and owns the backups
        init_d = self._second_project()
        preview = init_d("--host-rules", "apply")
        applied_d = init_d("--host-rules", "apply", "--host-rules-digest",
                           preview.payload["host_rules"]["digest"])
        self.assertEqual(applied_d.payload["host_rules"]["status"], "current")
        record_d = json.loads(
            (self.project.parent / "project-d" / ".vibe" / "host-rules.json").read_text())
        self.assertTrue(all(e.get("restore") == "block" for e in record_d["applied"]))
        self.assertTrue(all("written_sha256" not in e for e in record_d["applied"]))
        edited = self.edit_outside_block()
        backup = Path(str(self.agents) + host_rules.BACKUP_SUFFIX)
        backup_bytes = backup.read_bytes()

        rolled_d = init_d("--host-rules", "rollback")
        self.assertEqual(rolled_d.payload["host_rules"]["status"], "rolled_back")
        text = self.agents.read_text(encoding="utf-8")
        self.assertNotIn(host_rules.END_MARKER, text)
        self.assertIn("用户在块后追加的一行。", text)
        self.assertEqual(backup.read_bytes(), backup_bytes)  # C's backup left alone
        self.assertNotEqual(text, edited)

        # F2: C's rollback finds no block and must not get stuck in "apply".
        rolled_c = self.init("--host-rules", "rollback")
        hr = rolled_c.payload["host_rules"]
        self.assertEqual(hr["status"], "rolled_back", hr)
        self.assertTrue(any("标记块已不在，视为已回滚" in n for n in hr["notes"]), hr)
        self.assertEqual(self.record()["decision"], "rolled_back")
        self.assertIn("用户在块后追加的一行。", self.agents.read_text(encoding="utf-8"))
        self.assertNotIn(host_rules.END_MARKER, self.agents.read_text(encoding="utf-8"))
        self.assertEqual(self.init("--host-rules", "skip").payload["host_rules"]["decision"], "skip")

    def test_stale_backup_is_replaced_on_write_and_never_restored(self):
        backup = Path(str(self.agents) + host_rules.BACKUP_SUFFIX)
        backup.write_bytes(b"STALE OLDER CONTENT\n")
        self.apply()
        self.assertEqual(backup.read_bytes(), self.originals[self.agents])
        self.init("--host-rules", "rollback")
        self.assertEqual(self.agents.read_bytes(), self.originals[self.agents])

    def test_foreign_backup_with_current_block_is_neither_restored_nor_deleted(self):
        self.agents.write_text(
            self.originals[self.agents].decode("utf-8") + "\n\n" + host_rules.CURRENT_BLOCK + "\n",
            encoding="utf-8")
        backup = Path(str(self.agents) + host_rules.BACKUP_SUFFIX)
        backup.write_bytes(b"FOREIGN\n")
        self.apply()
        entry = next(e for e in self.record()["applied"] if e["path"] == "~/AGENTS.md")
        self.assertEqual(entry, {"path": "~/AGENTS.md", "restore": "block"})
        self.init("--host-rules", "rollback")
        self.assertEqual(self.agents.read_bytes(), self.originals[self.agents] + b"\n")
        self.assertEqual(backup.read_bytes(), b"FOREIGN\n")

    def _as_v2(self):
        body = host_rules.SNIPPET_BODY + "- v2 新增的一条。\n"
        return mock.patch.multiple(
            host_rules, SNIPPET_VERSION=2, SNIPPET_BODY=body,
            CURRENT_BLOCK=host_rules._render_block(body, 2),
        )

    def test_rollback_after_upgrade_removes_every_vibe_block(self):
        self.apply()
        with self._as_v2():
            upgraded = self.apply()
            self.assertEqual(upgraded.payload["host_rules"]["status"], "applied")
            self.assertIn("v2 新增的一条", self.claude.read_text(encoding="utf-8"))
            result = self.init("--host-rules", "rollback")
            self.assertEqual(result.payload["host_rules"]["status"], "rolled_back")
            self.assertEqual(self.claude.read_bytes(), self.originals[self.claude])
            self.assertNotIn(host_rules.END_MARKER, self.agents.read_text(encoding="utf-8"))
            doc = doctor(scan_project(ProjectPaths.from_cwd(self.project)))
            self.assertEqual(doc.facts["host_rules"]["files"]["~/.claude/CLAUDE.md"], "not_applied")
            self.assertEqual(doc.facts["host_rules"]["files"]["~/AGENTS.md"], "not_applied")
            self.assertEqual(self.record()["decision"], "rolled_back")

    def test_rollback_removes_preexisting_older_block(self):
        original = self.originals[self.claude].decode("utf-8")
        self.claude.write_text(
            original + "\n" + host_rules._render_block("## 旧版\n", 0) + "\n", encoding="utf-8")
        self.apply()
        self.init("--host-rules", "rollback")
        text = self.claude.read_text(encoding="utf-8")
        self.assertNotIn(host_rules.END_MARKER, text)
        self.assertEqual(text, original)

    def test_rollback_keeps_hand_edited_block_from_backup_with_note(self):
        original = self.originals[self.claude].decode("utf-8")
        edited_old = host_rules._render_block("## 旧版\n", 0).replace("旧版", "旧板")
        self.claude.write_text(original + "\n" + edited_old + "\n", encoding="utf-8")
        before = self.claude.read_bytes()
        # A hand-edited block blocks apply, so plant the state apply would leave.
        backup = Path(str(self.claude) + host_rules.BACKUP_SUFFIX)
        backup.write_bytes(before)
        written = (original + "\n" + host_rules.CURRENT_BLOCK + "\n").encode("utf-8")
        self.claude.write_bytes(written)
        result = host_rules.rollback(self.home, {"decision": "apply", "applied": [
            {"path": "~/.claude/CLAUDE.md", "backup": "~/.claude/CLAUDE.md" + host_rules.BACKUP_SUFFIX,
             "written_sha256": host_rules._sha(written), "backup_sha256": host_rules._sha(before)}]})
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.claude.read_bytes(), before)
        self.assertTrue(any("人工改过" in n for n in result["notes"]), result)

    def test_doctor_warns_on_modified_block_without_changing_readiness(self):
        self.init()
        paths = ProjectPaths.from_cwd(self.project)
        baseline = doctor(scan_project(paths))
        baseline_cli = self.cli("doctor")
        self.apply()
        text = self.claude.read_text(encoding="utf-8")
        self.claude.write_text(text.replace("light", "lite", 1), encoding="utf-8")
        report = doctor(scan_project(paths))
        self.assertEqual((report.ok, report.status, report.issues),
                         (baseline.ok, baseline.status, baseline.issues))
        self.assertTrue(any("人工改过" in w for w in report.warnings), report.warnings)
        result = self.cli("doctor")
        self.assertEqual(result.exit_code, baseline_cli.exit_code)
        self.assertEqual(result.payload["status"], baseline_cli.payload["status"])
        self.assertEqual(result.payload["issues"], baseline_cli.payload["issues"])
        self.assertTrue(any("人工改过" in w for w in result.payload["warnings"]))
        plain = run_cli(["doctor"], self.project)
        self.assertIn("提醒：", plain.text)


if __name__ == "__main__":
    unittest.main()
