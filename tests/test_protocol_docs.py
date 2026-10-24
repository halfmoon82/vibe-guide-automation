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
            "**除完整会话 id 和返工消息外不带任何其他参数**（如 `-n`、`--model`），否则会复制出新会话",
            "`vibe attest` 登记了 `claude-code.background_task.launch` 为真",
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


class PrdGuideWriteScopeRuleTests(unittest.TestCase):
    """#265: §4 must tell the planner that overlapping write scopes need
    depends_on, and must not offer integration_after as a fix."""

    @classmethod
    def setUpClass(cls):
        cls.text = PRD_GUIDE.read_text(encoding="utf-8")
        start = cls.text.index("## 4. ")
        cls.section = cls.text[start:cls.text.index("\n## 5.", start)]

    def test_rule_states_depends_on_serializes_overlaps(self):
        bullet_start = self.section.index("写范围重叠的节点")
        bullet = self.section[bullet_start:self.section.index("\n", bullet_start)]
        self.assertIn("depends_on", bullet)
        self.assertIn("串行", bullet)

    def test_rule_does_not_offer_integration_after_as_fix(self):
        bullet_start = self.section.index("写范围重叠的节点")
        bullet = self.section[bullet_start:self.section.index("\n", bullet_start)]
        self.assertIn("不能解除", bullet)
        # "integration_after …不能解除冲突" names it only to forbid it.
        self.assertNotIn("relabel", bullet)

    def test_rule_covers_same_group_overlap(self):
        bullet_start = self.section.index("写范围重叠的节点")
        bullet = self.section[bullet_start:self.section.index("\n", bullet_start)]
        self.assertIn("parallel_group", bullet)
        self.assertIn("也救不了", bullet)


class ReadingMapTests(unittest.TestCase):
    """#216④: the map at the top steers each role to its sections.

    Every section reference the map prints must resolve to a real heading, so
    a renumber or a dropped section cannot leave the map pointing at nothing.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = PRD_GUIDE.read_text(encoding="utf-8")
        start = cls.text.index("## 阅读地图")
        cls.map_ = cls.text[start:cls.text.index("\n## 0. ", start)]

    def test_map_sits_before_the_numbered_sections(self):
        self.assertLess(self.text.index("## 阅读地图"), self.text.index("## 0. "))

    def test_map_names_each_audience(self):
        for needle in ("PRD 引导", "监工", "§6.4", "整合审查", "§7"):
            self.assertIn(needle, self.map_, needle)

    def test_every_section_reference_resolves_to_a_heading(self):
        headings = set(
            re.match(r"^(#{2,3})\s*(\S+)", line).group(2).rstrip(".")
            for line in self.text.splitlines()
            if re.match(r"^#{2,3} ", line)
        )
        for ref in re.findall(r"§([\d.]+)", self.map_):
            self.assertIn(ref.rstrip("."), headings,
                          "map references missing section {}".format(ref))


class ReadmeBackgroundTests(unittest.TestCase):
    CASES = (
        ("README.md", "### Claude Code 后台派发"),
        ("README.en.md", "### Claude Code background dispatch"),
    )

    def test_sections(self):
        for name, heading in self.CASES:
            with self.subTest(name):
                sec = _readme_section(ROOT / name, heading)
                for needle in ("$VIBE_HOME/config.json", "claude_bg", "settings_path",
                               "apiKeyHelper", "claude attach",
                               "claude-code.background_task.launch"):
                    self.assertIn(needle, sec)
                blocks = re.findall(r"```json\n(.*?)\n```", sec, re.S)
                self.assertGreaterEqual(len(blocks), 2)
                for block in blocks:
                    json.loads(block)

    def test_config_example_is_what_the_launcher_reads(self):
        # "A writes == B reads": feed the README example to the real loader.
        try:
            from vibe_guide.adapters.claude_bg import load_claude_bg_config
        except ImportError:
            self.skipTest("background launcher not in this tree")
        import os
        import tempfile
        from unittest import mock
        from vibe_guide.paths import ProjectPaths
        for name, heading in self.CASES:
            with self.subTest(name):
                sec = _readme_section(ROOT / name, heading)
                example = next(
                    json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", sec, re.S)
                    if "claude_bg" in json.loads(b)
                )
                example["claude_bg"]["settings_path"] = "/abs/settings.json"
                with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as proj:
                    Path(home, "config.json").write_text(json.dumps(example), encoding="utf-8")
                    Path(proj, ".vibe").mkdir()
                    with mock.patch.dict(os.environ, {"VIBE_HOME": home}):
                        config = load_claude_bg_config(ProjectPaths(Path(proj)))
                self.assertEqual(config.settings_path, "/abs/settings.json")
                self.assertEqual(config.model, example["claude_bg"]["model"])

    def test_enable_steps_and_unverified_boundary(self):
        for name, heading, unverified in (
            ("README.md", self.CASES[0][1], "未验证、不得当作可用：`--session-id`"),
            ("README.en.md", self.CASES[1][1], "Unverified, do not treat as available: pre-assigning a session id with `--session-id`"),
        ):
            with self.subTest(name):
                sec = _readme_section(ROOT / name, heading)
                # scan reports no capability mode; the session's attest does.
                self.assertIn("`vibe attest", sec)
                self.assertNotIn("mode=background", sec.split("`vibe attest", 1)[0])
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
