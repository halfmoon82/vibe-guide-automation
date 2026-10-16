"""The rules must land in a file the host actually loads.

`apply-agentsmd` writes the capability rules to exactly one file.  Which file
that is has to match what each supported host reads at session start -- write
them into a file nobody opens and every check stays green while the host runs
without the rules (#126).  Each host is therefore pinned here by the file it
loads, so a host cannot be added to the writer without a test saying where its
rules go, and `vibe doctor` cannot go green while one of them reads nothing.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide.cli import run_cli
from vibe_guide.doctor import doctor
from vibe_guide.initializer import _rules_target
from vibe_guide.paths import ProjectPaths
from vibe_guide.scanner import (
    claude_code_project,
    claude_code_session,
    claude_entry_imports,
    claude_entry_reaches_rules,
    scan_project,
)


#: The file each host loads for project context, by host.  Codex reads
#: AGENTS.md; a WorkBuddy host reads the first of CODEBUDDY.md / AGENTS.md it
#: finds; Claude Code reads only CLAUDE.md and never AGENTS.md at all, which is
#: why it is the one host whose rules file may be a *different* file from the
#: one the rules are written to -- reached through an import line.
_HOST_RULES_FILE = {
    'codex': 'AGENTS.md',
    'workbuddy': 'CODEBUDDY.md',
    'claude-code': 'CLAUDE.md',
}


class HostRulesReachTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Unset would fall back to ~/.workbuddy, so pin an empty host dir.
        # CLAUDECODE is cleared rather than left alone: this suite may itself be
        # run from a Claude Code session, and the writer reads that marker --
        # inheriting it would make "no footprint means no CLAUDE.md" pass or
        # fail depending on who ran pytest.  The tests that want the marker set
        # it themselves.
        patcher = mock.patch.dict(os.environ, {
            "WORKBUDDY_CONFIG_DIR": str(Path(self._tmp.name) / "no-host"),
            "CODEBUDDY_CONFIG_DIR": "",
            "CLAUDECODE": "",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()
        (self.root / ".vibe").mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)

    def _write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _scan(self):
        return scan_project(ProjectPaths(self.root))

    def _apply(self):
        initialized = run_cli(["init", "--confirm", "--json"], self.root)
        self.assertEqual(initialized.exit_code, 0, initialized.text)
        return run_cli(["apply-agentsmd", "--confirm", "--json"], self.root)

    def _issues(self):
        return doctor(self._scan()).issues

    # -- the contract itself -------------------------------------------------

    def test_a_bare_project_writes_a_file_some_host_loads(self):
        """The default target must be a name at least one host reads.

        Every host is pinned in `_HOST_RULES_FILE`, so a writer that picked a
        name outside it would put the rules where no host looks at all.
        """
        self.assertIn(_rules_target(self.root).name, _HOST_RULES_FILE.values())

    def test_codex_reads_the_file_the_rules_are_written_to(self):
        applied = self._apply()
        self.assertEqual(applied.exit_code, 0, applied.text)
        target = _rules_target(self.root)
        self.assertEqual(target.name, _HOST_RULES_FILE['codex'])
        self.assertIn("## ", target.read_text(encoding="utf-8"))

    def test_a_workbuddy_project_writes_where_its_host_reads(self):
        self._write("CODEBUDDY.md", "# codebuddy rules\n")
        applied = self._apply()
        self.assertEqual(applied.exit_code, 0, applied.text)
        target = _rules_target(self.root)
        self.assertEqual(target.name, _HOST_RULES_FILE['workbuddy'])
        self.assertIn("## ", target.read_text(encoding="utf-8"))

    def test_a_claude_code_project_reaches_the_file_the_rules_are_written_to(self):
        self._write("CLAUDE.md", "# claude rules\n")
        applied = self._apply()
        self.assertEqual(applied.exit_code, 0, applied.text)
        target = _rules_target(self.root)
        # Claude Code loads CLAUDE.md, so the rules landing in AGENTS.md is
        # only correct if CLAUDE.md imports it.
        self.assertTrue(
            claude_entry_reaches_rules(self.root, target.name),
            (self.root / "CLAUDE.md").read_text(encoding="utf-8"),
        )
        self.assertNotIn('rules file not loaded by host: Claude Code', self._issues())

    def test_a_project_used_by_both_hosts_imports_the_file_the_rules_went_to(self):
        # WorkBuddy reads CODEBUDDY.md, so that is where the rules go; the
        # import must name that file, not AGENTS.md, or Claude Code follows a
        # line into a file this project never wrote.
        self._write("CODEBUDDY.md", "# codebuddy rules\n")
        self._write("CLAUDE.md", "# claude rules\n")
        self.assertEqual(self._apply().exit_code, 0)
        content = (self.root / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("@CODEBUDDY.md", content)
        self.assertNotIn("@AGENTS.md", content)
        self.assertNotIn('rules file not loaded by host: Claude Code', self._issues())

    # -- the repair is idempotent and never takes over the memory file -------

    def test_the_import_is_added_once_and_only_once(self):
        self._write("CLAUDE.md", "# claude rules\n")
        self.assertEqual(self._apply().exit_code, 0)
        entry = self.root / "CLAUDE.md"
        first = entry.read_text(encoding="utf-8")
        self.assertIn("@AGENTS.md", first)
        self.assertEqual(first.count("@AGENTS.md"), 1, first)
        again = run_cli(["apply-agentsmd", "--confirm", "--json"], self.root)
        self.assertEqual(again.exit_code, 0, again.text)
        self.assertEqual(entry.read_text(encoding="utf-8"), first)

    def test_the_user_memory_file_keeps_its_own_text(self):
        self._write("CLAUDE.md", "# claude rules\n\n- keep me\n")
        self.assertEqual(self._apply().exit_code, 0)
        content = (self.root / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("- keep me", content)
        self.assertTrue(content.startswith("# claude rules\n"), content)

    def test_a_claude_md_that_already_imports_is_left_alone(self):
        self._write("CLAUDE.md", "# claude rules\n\n@AGENTS.md\n")
        before = (self.root / "CLAUDE.md").read_bytes()
        self.assertEqual(self._apply().exit_code, 0)
        self.assertEqual((self.root / "CLAUDE.md").read_bytes(), before)

    def test_a_claude_md_symlinked_to_the_rules_file_needs_no_import(self):
        # The memory file *is* the rules file here; appending an import would
        # edit the rules file through the link.
        self._write("AGENTS.md", "# rules\n")
        (self.root / "CLAUDE.md").symlink_to("AGENTS.md")
        applied = self._apply()
        self.assertEqual(applied.exit_code, 0, applied.text)
        self.assertTrue((self.root / "CLAUDE.md").is_symlink())
        self.assertNotIn("@AGENTS.md", (self.root / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertNotIn('rules file not loaded by host: Claude Code', self._issues())

    def test_a_claude_md_symlinked_elsewhere_is_reported_not_rewritten(self):
        self._write("elsewhere.md", "# not the rules\n")
        (self.root / "CLAUDE.md").symlink_to("elsewhere.md")
        applied = self._apply()
        self.assertEqual(applied.exit_code, 0, applied.text)
        self.assertTrue((self.root / "CLAUDE.md").is_symlink())
        self.assertEqual(
            (self.root / "elsewhere.md").read_text(encoding="utf-8"),
            "# not the rules\n",
        )
        self.assertIn('rules file not loaded by host: Claude Code', self._issues())

    def test_a_dot_claude_directory_alone_is_enough_to_wire_the_import(self):
        # `.claude/` is what a project gets once Claude Code has run in it, so
        # the host is present even before CLAUDE.md exists.
        (self.root / ".claude").mkdir()
        self.assertEqual(self._apply().exit_code, 0)
        self.assertIn("@AGENTS.md", (self.root / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertNotIn('rules file not loaded by host: Claude Code', self._issues())

    def test_a_project_without_claude_code_gets_no_claude_md(self):
        self.assertEqual(self._apply().exit_code, 0)
        self.assertFalse((self.root / "CLAUDE.md").exists())

    def test_a_claude_code_session_may_create_the_memory_file_it_will_read(self):
        # Fresh clone, no footprint yet, vibe invoked from inside Claude Code:
        # the caller *is* the host that reads CLAUDE.md, so creating it is that
        # host setting up its own project rather than a file invented behind
        # the user's back.  Without the marker the same project gets nothing.
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            self.assertIs(claude_code_session(), True)
            self.assertIs(claude_code_project(self.root), False)
            self.assertEqual(self._apply().exit_code, 0)
        self.assertIn("@AGENTS.md", (self.root / "CLAUDE.md").read_text(encoding="utf-8"))
        # And the footprint it just created is what `doctor` now judges, so the
        # check goes green without ever reading the caller's environment.
        self.assertNotIn('rules file not loaded by host: Claude Code', self._issues())

    def test_the_session_marker_is_not_part_of_the_project_verdict(self):
        # A project cannot be reported on differently because of who ran the
        # check, so `scan_project` must ignore the marker entirely.
        self._write("AGENTS.md", "## Capability and Tool Truth\n\n- rule\n")
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            report = self._scan()
        self.assertIs(report.claude_code_host, False)
        self.assertIs(report.claude_rules_loaded, False)

    def test_a_symlinked_dot_claude_directory_is_a_footprint(self):
        shared = Path(self._tmp.name) / "shared-claude"
        shared.mkdir()
        (self.root / ".claude").symlink_to(shared)
        self.assertIs(claude_code_project(self.root), True)
        self.assertEqual(self._apply().exit_code, 0)
        self.assertIn("@AGENTS.md", (self.root / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_a_symlink_loop_is_reported_not_raised(self):
        # `Path.resolve()` raises RuntimeError for a loop on Python 3.9-3.12,
        # where this package still runs.  A loop reaches no rules, so the only
        # acceptable outcome is the ordinary "not loaded" verdict.
        self._write("AGENTS.md", "## Capability and Tool Truth\n\n- rule\n")
        (self.root / "CLAUDE.md").symlink_to("CLAUDE.md")
        self.assertIs(claude_entry_reaches_rules(self.root, "AGENTS.md"), False)
        self.assertIs(self._scan().claude_rules_loaded, False)
        self.assertIn('rules file not loaded by host: Claude Code', self._issues())

    def test_an_existing_memory_file_keeps_its_mode(self):
        entry = self._write("CLAUDE.md", "# claude rules\n")
        os.chmod(entry, 0o644)
        self.assertEqual(self._apply().exit_code, 0)
        self.assertIn("@AGENTS.md", entry.read_text(encoding="utf-8"))
        self.assertEqual(entry.stat().st_mode & 0o777, 0o644)

    def test_the_only_form_that_counts_is_the_bare_import_line(self):
        # Claude Code's syntax has not been calibrated against a live session
        # here, so anything but the exact line the writer produces counts as
        # *not* imported.  Wrong in that direction shows up as a doctor warning
        # and a redundant line; wrong in the other direction is a silent host
        # that reads nothing.
        for text in (
            "@./AGENTS.md\n",
            "see @AGENTS.md\n",
            "  @AGENTS.md\n\n",          # trailing blank lines are fine
        ):
            with self.subTest(text=text):
                self.assertIs(
                    claude_entry_imports(text, "AGENTS.md"),
                    text.strip() == "@AGENTS.md",
                    text,
                )
        self.assertIs(claude_entry_imports("@AGENTS.md\n", "AGENTS.md"), True)

    def test_the_fence_state_machine_covers_both_markers(self):
        for fence in ("```", "~~~", "````"):
            with self.subTest(fence=fence):
                self.assertIs(
                    claude_entry_imports(
                        "# rules\n\n" + fence + "\n@AGENTS.md\n" + fence + "\n",
                        "AGENTS.md",
                    ),
                    False,
                )
                # The same fence closed, then a real import after it.
                self.assertIs(
                    claude_entry_imports(
                        fence + "\n@AGENTS.md\n" + fence + "\n@AGENTS.md\n",
                        "AGENTS.md",
                    ),
                    True,
                )

    def test_an_unclosed_fence_hides_everything_after_it(self):
        # One opening marker and no close: the import is inside the block as
        # far as any reader is concerned, so it must not count.
        for fence in ("```", "~~~"):
            with self.subTest(fence=fence):
                self.assertIs(
                    claude_entry_imports(
                        "# rules\n" + fence + "\n@AGENTS.md\n", "AGENTS.md"
                    ),
                    False,
                )

    def test_the_cli_says_which_file_it_wrote(self):
        # Existing project: the rules are already in AGENTS.md, only the import
        # is new.  The summary must not read as if the rules file was rewritten.
        self.assertEqual(self._apply().exit_code, 0)
        self.assertFalse((self.root / "CLAUDE.md").exists())
        self._write("CLAUDE.md", "# claude rules\n")
        applied = run_cli(["apply-agentsmd", "--confirm"], self.root)
        self.assertEqual(applied.exit_code, 0, applied.text)
        self.assertIn("CLAUDE.md", applied.text)
        self.assertIn("引入行", applied.text)

    # -- what the check must not accept --------------------------------------

    def test_rules_without_an_import_are_reported(self):
        self._write("AGENTS.md", "## Capability and Tool Truth\n\n- rule\n")
        self._write("CLAUDE.md", "# claude rules\n")
        report = self._scan()
        self.assertIs(report.claude_code_host, True)
        self.assertIs(report.claude_rules_loaded, False)
        self.assertIn('rules file not loaded by host: Claude Code', self._issues())

    def test_an_import_inside_a_fence_does_not_count(self):
        # Claude Code does not read an import written inside a code block, so
        # counting it would report the host as covered while it reads nothing.
        self._write("AGENTS.md", "## Capability and Tool Truth\n\n- rule\n")
        self._write("CLAUDE.md", "# claude rules\n\n```\n@AGENTS.md\n```\n")
        self.assertIs(self._scan().claude_rules_loaded, False)
        self.assertIn('rules file not loaded by host: Claude Code', self._issues())

    def test_a_quoted_import_does_not_count(self):
        self._write("AGENTS.md", "## Capability and Tool Truth\n\n- rule\n")
        self._write("CLAUDE.md", "# claude rules\n\n> @AGENTS.md\n")
        self.assertIs(self._scan().claude_rules_loaded, False)

    def test_an_import_with_a_trailing_comment_does_not_count(self):
        self._write("AGENTS.md", "## Capability and Tool Truth\n\n- rule\n")
        self._write("CLAUDE.md", "# claude rules\n\n@AGENTS.md see notes\n")
        self.assertIs(self._scan().claude_rules_loaded, False)

    def test_a_project_without_the_host_is_not_reported(self):
        self._write("AGENTS.md", "## Capability and Tool Truth\n\n- rule\n")
        report = self._scan()
        self.assertIs(report.claude_code_host, False)
        self.assertNotIn('rules file not loaded by host: Claude Code', self._issues())

    def test_a_missing_rules_file_is_reported_once(self):
        # With no rules file at all, "missing AGENTS.md" already says it; a
        # second issue about loading a file that is not there is noise.
        self._write("CLAUDE.md", "# claude rules\n")
        issues = self._issues()
        self.assertIn('missing AGENTS.md', issues)
        self.assertNotIn('rules file not loaded by host: Claude Code', issues)

    def test_the_facts_report_the_host_and_the_reach_together(self):
        self._write("AGENTS.md", "## Capability and Tool Truth\n\n- rule\n")
        self._write("CLAUDE.md", "# claude rules\n\n@AGENTS.md\n")
        self.assertEqual(
            doctor(self._scan()).facts['claude_code'],
            {'host': True, 'rules_loaded': True},
        )


if __name__ == "__main__":
    unittest.main()
