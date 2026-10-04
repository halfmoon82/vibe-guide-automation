"""Boundaries the WorkBuddy host integration must not cross.

Host discovery may only *add* information; it must not stand in for a pinned
required skill, take over a Claude Code project's rules file, reject pinned
records it did not use to reject, or promote WorkBuddy to full visible
automation before a real session has run it.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vibe_guide.adapters.base import Environment
from vibe_guide.adapters.registry import AdapterRegistry
from vibe_guide.doctor import doctor
from vibe_guide.initializer import _rules_target
from vibe_guide.node_spec import derive_integration_contract
from vibe_guide.paths import ProjectPaths
from vibe_guide.scanner import scan_project


_PINNED = {
    "name": "architecture-skill-pack",
    "source": "https://github.com/lov-team/architecture-skill-pack",
    "commit": "a" * 40,
}


class WorkBuddyHostBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Unset would fall back to ~/.workbuddy, so pin an empty host dir.
        patcher = mock.patch.dict(os.environ, {
            "WORKBUDDY_CONFIG_DIR": str(Path(self._tmp.name) / "no-host"),
            "CODEBUDDY_CONFIG_DIR": "",
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

    def _scan(self):
        return scan_project(ProjectPaths(self.root))

    def test_a_host_provided_skill_does_not_satisfy_the_required_pinned_skill(self):
        self._write("AGENTS.md", "rules\n")
        self._write(
            ".workbuddy/skills/architecture-skill-pack/SKILL.md",
            "---\nname: architecture-skill-pack\n---\n",
        )
        report = doctor(self._scan())
        skills = report.facts["skills"]
        self.assertIn("architecture-skill-pack", skills["host_provided"])
        self.assertIs(skills["required_configured"], False, skills)
        self.assertNotIn("architecture-skill-pack", skills["configured"], skills)
        self.assertIn(".vibe/proposals/skills/proposal.md", report.proposals)

    def test_init_still_proposes_the_required_skill_when_only_the_host_has_it(self):
        from vibe_guide.initializer import init_project

        self._write("AGENTS.md", "rules\n")
        self._write(
            ".workbuddy/skills/architecture-skill-pack/SKILL.md",
            "---\nname: architecture-skill-pack\n---\n",
        )
        init_project(ProjectPaths(self.root), confirm=True)
        self.assertTrue(
            (self.root / ".vibe/proposals/skills/proposal.md").is_file(),
            "a host copy is unpinned; init must still propose the pinned source",
        )

    def test_a_claude_md_only_project_keeps_agents_md_as_its_rules_file(self):
        self._write("CLAUDE.md", "# claude rules\n")
        report = self._scan()
        self.assertFalse(report.agentsmd_exists)
        self.assertIn(report.rules_file, (None, "AGENTS.md"))
        self.assertEqual(_rules_target(self.root).name, "AGENTS.md")

    def test_codebuddy_md_is_still_recognized_as_a_rules_file(self):
        self._write("CODEBUDDY.md", "# codebuddy rules\n")
        report = self._scan()
        self.assertTrue(report.agentsmd_exists)
        self.assertEqual(report.rules_file, "CODEBUDDY.md")

    def test_agents_md_wins_when_both_rules_files_exist(self):
        self._write("AGENTS.md", "rules\n")
        self._write("CODEBUDDY.md", "rules\n")
        self.assertEqual(self._scan().rules_file, "AGENTS.md")
        self.assertEqual(_rules_target(self.root).name, "AGENTS.md")

    def _integration_ref(self):
        entry = SimpleNamespace(plan_id="p", request="r")
        contract = derive_integration_contract({"nodes": [{"id": "a"}]}, entry, ProjectPaths(self.root))
        return contract["agentsmd_acceptance_refs"]

    def test_a_codebuddy_only_project_writes_and_cites_codebuddy_md(self):
        self._write("CODEBUDDY.md", "rules\n")
        self.assertEqual(_rules_target(self.root).name, "CODEBUDDY.md")
        self.assertEqual(self._integration_ref(), ["CODEBUDDY.md"])

    def test_a_symlinked_agents_md_is_still_cited_as_on_main(self):
        self._write("real.md", "rules\n")
        (self.root / "AGENTS.md").symlink_to("real.md")
        self.assertEqual(self._integration_ref(), ["AGENTS.md"])

    def test_a_symlinked_agents_md_beside_codebuddy_md_writes_where_it_cites(self):
        # Citing AGENTS.md while appending to CODEBUDDY.md would split the
        # rules from the reference; main refused the symlink, so keep that.
        self._write("real.md", "rules\n")
        (self.root / "AGENTS.md").symlink_to("real.md")
        self._write("CODEBUDDY.md", "rules\n")
        self.assertEqual(self._integration_ref(), ["AGENTS.md"])
        self.assertEqual(_rules_target(self.root).name, "AGENTS.md")

    def test_an_agents_md_that_is_not_a_file_leaves_codebuddy_md_as_the_target(self):
        # A dangling link or a directory is cited as CODEBUDDY.md, so the
        # rules must be written there too -- the pairing main already had.
        self._write("CODEBUDDY.md", "rules\n")
        (self.root / "AGENTS.md").symlink_to("nope.md")
        self.assertEqual(self._integration_ref(), ["CODEBUDDY.md"])
        self.assertEqual(_rules_target(self.root).name, "CODEBUDDY.md")
        (self.root / "AGENTS.md").unlink()
        (self.root / "AGENTS.md").mkdir()
        self.assertEqual(self._integration_ref(), ["CODEBUDDY.md"])
        self.assertEqual(_rules_target(self.root).name, "CODEBUDDY.md")

    def test_an_agents_md_linked_to_codebuddy_md_still_receives_the_rules(self):
        # Writing CODEBUDDY.md is writing the file AGENTS.md cites here.
        from vibe_guide.cli import run_cli

        self._write("CODEBUDDY.md", "# codebuddy rules\n")
        (self.root / "AGENTS.md").symlink_to("CODEBUDDY.md")
        self.assertEqual(_rules_target(self.root).name, "CODEBUDDY.md")
        run_cli(["init", "--confirm", "--json"], self.root)
        applied = run_cli(["apply-agentsmd", "--confirm", "--json"], self.root)
        self.assertEqual(applied.exit_code, 0, applied.text)
        self.assertTrue((self.root / "AGENTS.md").is_symlink())
        self.assertIn("## ", (self.root / "CODEBUDDY.md").read_text(encoding="utf-8"))

    def test_a_link_spelling_codebuddy_md_in_another_case_is_the_same_file(self):
        self._write("codebuddy.md", "rules\n")
        if not (self.root / "CODEBUDDY.md").is_file():
            self.skipTest("case-sensitive filesystem")
        (self.root / "AGENTS.md").symlink_to("codebuddy.md")
        self.assertEqual(_rules_target(self.root).name, "CODEBUDDY.md")

    def test_apply_agentsmd_without_confirm_names_the_rules_file(self):
        from vibe_guide.cli import run_cli

        self._write("CODEBUDDY.md", "# codebuddy rules\n")
        paused = run_cli(["apply-agentsmd"], self.root)
        self.assertIn("CODEBUDDY.md", paused.text)
        self.assertNotIn("AGENTS.md", paused.text)

    def test_apply_agentsmd_names_the_file_it_actually_wrote(self):
        from vibe_guide.cli import run_cli

        self._write("CODEBUDDY.md", "# codebuddy rules\n")
        run_cli(["init", "--confirm", "--json"], self.root)
        applied = run_cli(["apply-agentsmd", "--confirm"], self.root)
        self.assertEqual(applied.exit_code, 0, applied.text)
        self.assertIn("CODEBUDDY.md", applied.text)
        self.assertNotIn("AGENTS.md", applied.text)
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_a_pinned_host_record_outside_every_host_root_is_invalid(self):
        outside = Path(self._tmp.name) / "elsewhere" / "stray-skill"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text("---\nname: stray-skill\n---\n", encoding="utf-8")
        self._write("AGENTS.md", "rules\n")
        self._write(".vibe/config.json", json.dumps({"skills": [
            {"name": "stray-skill", "origin": "workbuddy", "path": str(outside)},
        ]}))
        skills = self._scan().skills
        self.assertEqual([s["valid"] for s in skills if s["name"] == "stray-skill"], [False], skills)

    def test_a_pinned_record_with_a_non_workbuddy_origin_is_validated_as_before(self):
        self._write("AGENTS.md", "rules\n")
        self._write(
            ".vibe/config.json",
            json.dumps({"skills": [dict(_PINNED, origin="github")]}),
        )
        skills = self._scan().skills
        self.assertEqual(len(skills), 1, skills)
        self.assertIs(skills[0]["valid"], True, skills)
        self.assertEqual(skills[0]["source"], _PINNED["source"], skills)

    def test_workbuddy_declaration_alone_does_not_promote_it(self):
        """The manifest declares the control plane; only observed facts promote it.

        The manifest carrying ``native_control_plane: true`` is a statement of
        what the platform offers, not evidence that this session can use it:
        with the create/enter/resume/wait lifecycle only partly observed,
        WorkBuddy stays `guide`.  Host discovery may only *add* information.
        """
        adapter = AdapterRegistry().get("workbuddy")
        self.assertIs(adapter.manifest["native_control_plane"], True)
        partial = {
            "workbuddy.agent": True, "workbuddy.shell": True,
            "workbuddy.subprocess": True, "workbuddy.worktree": True,
            "workbuddy.visible_task.create": True,
        }
        env = Environment(
            commands={"workbuddy.agent": True},
            facts=partial,
            provenance={name: "self-attest" for name in partial},
        )
        capabilities = adapter.detect(env).capabilities
        self.assertIs(capabilities.visible_automation, False)
        self.assertNotEqual(capabilities.level, "full")
        self.assertEqual(capabilities.mode, "guide")


if __name__ == "__main__":
    unittest.main()
