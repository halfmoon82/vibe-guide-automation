"""Docs contract: the Claude Code background provider is documented, without secrets.

Anchors to the row/section that owns each fact (not whole-file assertIn).
"""
import hashlib
import json
import re
import unittest
from pathlib import Path

from vibe_guide.protocols import SHIPPED_PROTOCOL_DIGESTS

ROOT = Path(__file__).resolve().parent.parent
PRD_GUIDE = ROOT / "vibe_guide" / "protocols" / "prd-guide.md"
SECRET_RE = re.compile(r"sk-[A-Za-z0-9-]{8,}")


def _section_64(text):
    start = text.index("### 6.4 ")
    end = text.index("\n### 6.5", start)
    return text[start:end]


def _bullet(section, marker):
    start = section.index(marker)
    nxt = section.find("\n- **", start + 1)
    return section[start:] if nxt == -1 else section[start:nxt]


def _table_rows(section):
    rows = {}
    header = None
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if set(cells[0]) <= set("-: "):
            continue
        rows[cells[0].strip("`")] = cells
    return header, rows


def _readme_section(path, heading):
    text = path.read_text(encoding="utf-8")
    start = text.index(heading)
    nxt = re.search(r"\n#{2,3} ", text[start + len(heading):])
    return text[start:] if not nxt else text[start:start + len(heading) + nxt.start()]


class PrdGuideBackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = PRD_GUIDE.read_text(encoding="utf-8")
        cls.s64 = _section_64(cls.text)

    def test_table_has_background_column(self):
        header, rows = _table_rows(self.s64)
        self.assertEqual(header[-1].strip("`"), "claude-code-background")
        for op in ("create", "locate", "visibility", "resume", "wait"):
            self.assertIn(op, rows)
            self.assertEqual(len(rows[op]), 5, op)
            self.assertTrue(rows[op][4], op)
        self.assertIn("claude attach", rows["locate"][4])
        self.assertIn("--resume", rows["resume"][4])

    def test_codex_bullet_points_to_background(self):
        codex = _bullet(self.s64, "- **Codex 本地桌面**")
        self.assertIn("无人值守", codex)
        self.assertIn("claude-code-background", codex)

    def test_desktop_bullet_mentions_unconfigured(self):
        desk = _bullet(self.s64, "- **Claude Code 桌面**")
        self.assertIn("未配置后台启动器", desk)

    def test_background_bullet_facts(self):
        bg = _bullet(self.s64, "- **Claude Code 后台")
        for needle in ("claude attach", "bypassPermissions", "--model",
                       "worker-deliver", "完整会话 id"):
            self.assertIn(needle, bg)
        self.assertNotIn("无人值守", bg)

    def test_background_bullet_pitfalls(self):
        bg = _bullet(self.s64, "- **Claude Code 后台")
        for needle in (
            "**不带任何其他参数**，否则会复制出新会话",
            "先 `claude stop`，再用**完整会话 id**",
            "`acceptEdits` 第一条 Bash 就卡在 `blocked`",
            "`dontAsk` 直接拒绝 Bash 却报 `done`",
            "`state=done` 只表示这一轮结束、不代表任务成功",
            "`blocked` 需要处理，不能当进行中",
            "未验证、不得当作可用：`--session-id`",
            "桌面内置 CLI 下的 `--bg`",
        ):
            self.assertIn(needle, bg)

    def test_no_secret_or_real_path(self):
        self.assertIsNone(SECRET_RE.search(self.s64))
        self.assertNotIn("/Users/", self.s64)
        self.assertNotIn("ANTHROPIC_API_KEY=", self.s64)

    def test_digest_registered(self):
        digest = hashlib.sha256(PRD_GUIDE.read_bytes()).hexdigest()
        self.assertIn(digest, SHIPPED_PROTOCOL_DIGESTS["prd-guide"])


class ReadmeBackgroundTests(unittest.TestCase):
    CASES = (
        ("README.md", "### Claude Code 后台派发"),
        ("README.en.md", "### Claude Code background dispatch"),
    )

    def test_sections(self):
        for name, heading in self.CASES:
            with self.subTest(name):
                sec = _readme_section(ROOT / name, heading)
                for needle in ("$VIBE_HOME/config.json", "claude_background",
                               "settings_file", "default_model", "apiKeyHelper",
                               "claude attach"):
                    self.assertIn(needle, sec)
                blocks = re.findall(r"```json\n(.*?)\n```", sec, re.S)
                self.assertGreaterEqual(len(blocks), 2)
                for block in blocks:
                    json.loads(block)

    def test_scan_not_attest_and_unverified_boundary(self):
        for name, heading, unverified in (
            ("README.md", self.CASES[0][1], "未验证、不得当作可用：`--session-id`"),
            ("README.en.md", self.CASES[1][1], "Unverified, do not treat as available: pre-assigning a session id with `--session-id`"),
        ):
            with self.subTest(name):
                sec = _readme_section(ROOT / name, heading)
                self.assertIn("`vibe scan --json`", sec)
                self.assertNotIn("vibe attest", sec)
                self.assertIn(unverified, sec)

    def test_no_secret_or_real_path(self):
        for name, heading in self.CASES:
            with self.subTest(name):
                sec = _readme_section(ROOT / name, heading)
                self.assertIsNone(SECRET_RE.search(sec))
                self.assertNotIn("/Users/", sec)
                self.assertNotIn("ANTHROPIC_API_KEY=", sec)


if __name__ == "__main__":
    unittest.main()
