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
from unittest import mock

from vibe_guide.adapters.base import Environment
from vibe_guide.adapters.registry import AdapterRegistry
from vibe_guide.doctor import doctor
from vibe_guide.initializer import _rules_target
from vibe_guide.paths import ProjectPaths
from vibe_guide.scanner import scan_project


_PINNED = {
    "name": "architecture-skill-pack",
    "source": "https://github.com/lov-team/architecture-skill-pack",
    "commit": "a" * 40,
}


class WorkBuddyHostBoundaryTests(unittest.TestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "WORKBUDDY_CONFIG_DIR"}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
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
        skills = doctor(self._scan()).facts["skills"]
        self.assertIn("architecture-skill-pack", skills["host_provided"])
        self.assertIs(skills["required_configured"], False, skills)

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

    def test_workbuddy_is_not_full_visible_until_a_real_session_ran_it(self):
        adapter = AdapterRegistry().get("workbuddy")
        facts = {p["name"]: True for p in adapter.manifest["probes"] if p["kind"] == "fact"}
        env = Environment(
            commands={"workbuddy.agent": True},
            facts=facts,
            provenance={name: "self-attest" for name in facts},
        )
        capabilities = adapter.detect(env).capabilities
        self.assertIs(adapter.manifest["native_control_plane"], False)
        self.assertIs(capabilities.visible_automation, False)
        self.assertNotEqual(capabilities.level, "full")


if __name__ == "__main__":
    unittest.main()
