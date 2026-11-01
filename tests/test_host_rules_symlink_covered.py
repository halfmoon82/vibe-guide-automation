"""Host rules G1/G2: covered symlinks, doctor cause split, one-time skip notice."""

import unittest
from pathlib import Path

from vibe_guide import host_rules
from vibe_guide.doctor import doctor
from vibe_guide.paths import ProjectPaths
from vibe_guide.scanner import scan_project

from tests.test_host_rules import HostRulesTestBase, _tree


class SymlinkCoveredTests(HostRulesTestBase):
    def _link_codex(self, target):
        link = self.home / ".codex" / "AGENTS.md"
        link.parent.mkdir()
        link.symlink_to(target)
        return link

    def _files(self):
        doc = doctor(scan_project(ProjectPaths.from_cwd(self.project)))
        return doc.facts["host_rules"]["files"], doc

    def test_link_to_another_candidate_is_covered_and_skipped(self):
        self._link_codex(self.agents)
        result = self.apply()
        self.assertEqual(result.payload["host_rules"]["status"], "applied")
        self.assertEqual(
            sorted(result.payload["host_rules"]["paths"]), ["~/.claude/CLAUDE.md", "~/AGENTS.md"]
        )
        files, doc = self._files()
        self.assertEqual(files["~/.codex/AGENTS.md"], "covered")
        self.assertEqual(files["~/AGENTS.md"], "current")
        self.assertFalse([w for w in doc.warnings if ".codex" in w])

    def test_link_outside_candidates_still_refused_with_reason(self):
        outside = self.home.parent / "dotfiles-AGENTS.md"
        outside.write_text("keep\n", encoding="utf-8")
        self._link_codex(outside)
        before = _tree(self.home)
        blocked = self.init("--host-rules", "apply").payload["host_rules"]
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("~/.codex/AGENTS.md", blocked["reason"])
        self.assertIn(str(outside.resolve()), blocked["reason"])
        self.assertEqual(_tree(self.home), before)
        files, doc = self._files()
        self.assertEqual(files["~/.codex/AGENTS.md"], "symlink_outside")
        self.assertTrue([w for w in doc.warnings if "符号链接" in w])

    def test_doctor_splits_missing_and_permission(self):
        files, _ = self._files()
        self.assertEqual(files["~/.codex/AGENTS.md"], "absent")
        self.assertEqual(host_rules._file_status(self.home, ".codex/AGENTS.md"), "absent")
        self._link_codex(self.home / "nowhere.md")
        files, _ = self._files()
        self.assertEqual(files["~/.codex/AGENTS.md"], "symlink_outside")
        self.agents.chmod(0)
        try:
            files, _ = self._files()
            self.assertEqual(files["~/AGENTS.md"], "permission")
        finally:
            self.agents.chmod(0o600)


class SkipNoticeTests(HostRulesTestBase):
    def test_notice_once_then_silent(self):
        first = self.init("--host-rules", "skip")
        notice = first.payload["host_rules"]["notice"]
        self.assertIn("vibe init --confirm --host-rules apply", notice)
        self.assertIn("对齐门", notice)
        self.assertIn(notice, first.text)
        for again in (self.init("--host-rules", "skip"), self.init()):
            self.assertNotIn("notice", again.payload["host_rules"])
            self.assertNotIn("host-rules apply", again.text)
        doc = doctor(scan_project(ProjectPaths.from_cwd(self.project)))
        self.assertNotIn("host-rules apply", " ".join(doc.warnings))


if __name__ == "__main__":
    unittest.main()
