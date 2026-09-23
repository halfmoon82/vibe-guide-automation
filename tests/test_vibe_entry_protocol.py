"""vibe-entry protocol contract: shipped, self-contained, materialized.

The vibe-entry protocol is what the host agent reads at the start of a new
session to self-triage S0/S1 without invoking vibe, entering the CLI only for
requests scored complex.  These tests pin (1) the protocol shipping as package
data, (2) self-containment -- a fresh host with only vibe installed can run the
whole entry protocol, (3) the planner's five dimensions and thresholds as the
single source of truth the document cannot drift from, and (4) init
materializing it next to prd-guide with the same never-rewrite semantics.
"""
import tempfile
import unittest
from pathlib import Path

from vibe_guide.paths import ProjectPaths

ROOT = Path(__file__).resolve().parent.parent


class VibeEntryProtocolShippingTests(unittest.TestCase):
    def test_protocol_is_shipped_as_package_data(self):
        from vibe_guide import protocols
        self.assertTrue((Path(protocols.__file__).parent / "vibe-entry.md").is_file())

    def test_protocol_loads_by_simple_name(self):
        from vibe_guide.protocols import load_protocol
        text = load_protocol("vibe-entry")
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip())

    def test_protocol_embeds_planner_five_dimensions(self):
        """The quick table must name the planner's dimensions, not a copy.

        `--s1` feeds planner.TaskContext; a fifth dimension with different
        semantics (the local methodology skill uses read-depth) would drift.
        """
        from vibe_guide.protocols import load_protocol
        text = load_protocol("vibe-entry")
        for field in ("steps", "domains", "uncertainty", "failure_cost", "toolchain"):
            self.assertIn(field, text, field)

    def test_protocol_states_thresholds(self):
        from vibe_guide.protocols import load_protocol
        text = load_protocol("vibe-entry")
        for token in ("<=8", "9-15", ">15"):
            self.assertIn(token, text, token)

    def test_protocol_complex_path_names_scan_and_plan_with_s1(self):
        from vibe_guide.protocols import load_protocol
        text = load_protocol("vibe-entry")
        self.assertIn("vibe scan", text)
        self.assertIn("vibe plan --request", text)
        self.assertIn("--s1", text)

    def test_protocol_is_self_contained(self):
        """An external methodology skill may be referenced only as optional.

        External users never installed `complex-task-methodology`; every line
        naming it must carry the optional marker, and no line may require it.
        """
        from vibe_guide.protocols import load_protocol
        text = load_protocol("vibe-entry")
        for line in text.splitlines():
            if "complex-task-methodology" in line:
                self.assertIn("可选", line, line)
                self.assertNotIn("必须", line, line)

    def test_protocol_states_gate_discipline(self):
        from vibe_guide.protocols import load_protocol
        text = load_protocol("vibe-entry")
        self.assertIn("停下报告", text)
        self.assertIn("不得", text)


class VibeEntryMaterializationTests(unittest.TestCase):
    def _init(self, root):
        from vibe_guide.initializer import init_project
        return init_project(ProjectPaths.from_cwd(root), True)

    def test_init_materializes_vibe_entry_skill_next_to_prd_guide(self):
        from vibe_guide.protocols import load_protocol
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self._init(root)
            skill = root / ".vibe" / "proposals" / "skills" / "vibe-entry" / "SKILL.md"
            self.assertTrue(skill.is_file())
            self.assertEqual(skill.read_text(encoding="utf-8"), load_protocol("vibe-entry"))
            self.assertIn(".vibe/proposals/skills/vibe-entry/SKILL.md", result.paths)
            # prd-guide regression: same init still ships it.
            self.assertTrue((root / ".vibe" / "proposals" / "skills" / "prd-guide" / "SKILL.md").is_file())

    def test_reinit_preserves_user_edited_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._init(root)
            skill = root / ".vibe" / "proposals" / "skills" / "vibe-entry" / "SKILL.md"
            skill.write_text("# user edits\n", encoding="utf-8")
            self._init(root)
            self.assertEqual(skill.read_text(encoding="utf-8"), "# user edits\n")


if __name__ == "__main__":
    unittest.main()
