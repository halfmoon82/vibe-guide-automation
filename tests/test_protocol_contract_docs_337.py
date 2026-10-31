"""Docs contract (#337 docs half + #339 docs half): locate parameter shapes and
the visible-dispatch missing-worktree machine code are written into the protocols.

Anchors to the row/sentence that owns each fact (not whole-file assertIn).
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRD_GUIDE = ROOT / "vibe_guide" / "protocols" / "prd-guide.md"
SUPERVISOR_CARD = ROOT / "vibe_guide" / "protocols" / "supervisor-card.md"


def _section(text, heading, next_heading):
    start = text.index(heading)
    end = text.index(next_heading, start)
    return text[start:end]


def _locate_row(section):
    for line in section.splitlines():
        if line.startswith("|") and "`locate`" in line:
            return line
    raise AssertionError("section table has no locate row")


class LocateParameterShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = PRD_GUIDE.read_text(encoding="utf-8")
        cls.s64 = _section(cls.text, "### 6.4 ", "\n### 6.5")

    def test_locate_row_names_thread_id_and_no_extra_host_id(self):
        row = _locate_row(self.s64)
        self.assertIn("threadId", row)
        self.assertIn("不要多传 hostId", row)

    def test_codex_locate_shape_is_a_verified_fact(self):
        row = _locate_row(self.s64)
        self.assertIn('{"threadId"', row)
        self.assertIn("#316", row)

    def test_row_covers_all_visible_providers(self):
        row = _locate_row(self.s64)
        self.assertIn("open_session_in", row)
        self.assertIn("未验证", row)  # Claude shape is not guessed
        self.assertIn("job id", row)  # WorkBuddy get(job_id)


class WorktreeMissingCodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = PRD_GUIDE.read_text(encoding="utf-8")
        cls.s63 = _section(cls.text, "### 6.3 ", "\n### 6.4")

    def test_visible_dispatch_missing_worktree_carries_machine_code(self):
        start = self.s63.index("**可见派发仍需 worktree 先存在**")
        sentence = self.s63[start:]
        self.assertIn("worktree_missing", sentence)
        self.assertIn("需要监工建工作树", sentence)


class SupervisorCardLocateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SUPERVISOR_CARD.read_text(encoding="utf-8")

    def test_card_has_locate_section_with_native_args(self):
        self.assertIn("## locate", self.text)
        self.assertIn("native_args", self.text)
        self.assertIn("不要多传", self.text)

    def test_card_codex_locate_only_thread_id(self):
        self.assertIn('{"threadId": <task_id>}', self.text)
