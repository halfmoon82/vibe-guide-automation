"""Integration review F5: a failed project record never leaves an unrecorded block.

apply() writes user files before the project record; if the record cannot be
written, rollback would later find nothing and the block would stay active.
All tests run under a temporary HOME.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide import host_rules
from vibe_guide.cli import run_cli


_REAL_FILES = tuple(Path(os.path.expanduser("~")) / rel for rel in host_rules.CANDIDATES)


def _fingerprint():
    result = {}
    for path in _REAL_FILES:
        try:
            result[str(path)] = path.read_bytes()
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
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            result[os.path.relpath(os.path.join(dirpath, name), root)] = None
        for name in filenames:
            full = os.path.join(dirpath, name)
            result[os.path.relpath(full, root)] = Path(full).read_bytes()
    return result


_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


class RecordFailureTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup, base)
        self.home = base / "home"
        self.project = base / "project"
        self.home.mkdir()
        self.project.mkdir()
        patcher = mock.patch.dict(
            os.environ, {"HOME": str(self.home), "VIBE_HOME": str(base / "vibe-home")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(Path.home(), self.home)
        self.claude = self.home / ".claude" / "CLAUDE.md"
        self.agents = self.home / "AGENTS.md"
        self.claude.parent.mkdir()
        self.claude.write_bytes("# 全局规则\n\n- 用中文。\n".encode("utf-8"))
        self.agents.write_bytes("# Agents\n".encode("utf-8"))
        self.originals = {p: p.read_bytes() for p in (self.claude, self.agents)}
        self.assertEqual(self.init().payload["status"], "ok")

    @staticmethod
    def _cleanup(base):
        for dirpath, dirnames, filenames in os.walk(base):
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                if not os.path.islink(full):
                    os.chmod(full, 0o700)
        shutil.rmtree(base, ignore_errors=True)

    def init(self, *extra, as_json=True):
        argv = ["init", "--confirm", *extra] + (["--json"] if as_json else [])
        return run_cli(argv, self.project)

    def digest(self):
        return self.init("--host-rules", "apply").payload["host_rules"]["digest"]

    def apply(self, as_json=True):
        return self.init("--host-rules", "apply", "--host-rules-digest", self.digest(), as_json=as_json)

    def record(self):
        path = self.project / ".vibe" / "host-rules.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def backup(self, path):
        return Path(str(path) + host_rules.BACKUP_SUFFIX)

    @unittest.skipIf(_ROOT, "root ignores permissions")
    def test_a_unwritable_record_dir_blocks_before_any_user_write(self):
        digest = self.digest()
        before = _tree(self.home)
        os.chmod(self.project / ".vibe", 0o555)
        try:
            result = self.init("--host-rules", "apply", "--host-rules-digest", digest)
        finally:
            os.chmod(self.project / ".vibe", 0o755)
        self.assertEqual(result.payload["status"], "ok")
        hr = result.payload["host_rules"]
        self.assertEqual(hr["status"], "blocked")
        self.assertIn("项目记录 .vibe/host-rules.json 不可写，未写入任何用户级文件", hr["reason"])
        self.assertEqual(_tree(self.home), before)
        for path in self.originals:
            self.assertFalse(os.path.lexists(self.backup(path)))

    def _upgrade_scenario(self):
        """v1 applied and recorded; the next apply is an upgrade to v2."""
        self.assertEqual(self.apply().payload["host_rules"]["status"], "applied")
        body = host_rules.SNIPPET_BODY + "- v2 新增的一条。\n"
        patcher = mock.patch.multiple(
            host_rules, SNIPPET_VERSION=2, SNIPPET_BODY=body,
            CURRENT_BLOCK=host_rules._render_block(body, 2),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_b_record_failure_after_write_undoes_exactly_this_apply(self):
        self._upgrade_scenario()
        digest = self.digest()
        before = _tree(self.home)
        record_before = self.record()
        with mock.patch.object(host_rules, "write_record", side_effect=OSError("disk full")):
            result = self.init("--host-rules", "apply", "--host-rules-digest", digest, as_json=False)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload["status"], "ok")
        hr = result.payload["host_rules"]
        self.assertEqual(hr["status"], "blocked")
        self.assertIn("记录写入失败，已撤回本次写入的用户级文件：OSError", hr["reason"])
        # Byte-exact: user files still hold v1 blocks, earlier backups hold the originals.
        self.assertEqual(_tree(self.home), before)
        for path, original in self.originals.items():
            self.assertEqual(self.backup(path).read_bytes(), original)
        self.assertEqual(self.record(), record_before)
        self.assertNotIn("已写入", result.text)

    def test_b_first_apply_record_failure_leaves_no_backup(self):
        digest = self.digest()
        before = _tree(self.home)
        with mock.patch.object(host_rules, "write_record", side_effect=OSError("disk full")):
            result = self.init("--host-rules", "apply", "--host-rules-digest", digest, as_json=False)
        self.assertEqual(result.payload["host_rules"]["status"], "blocked")
        self.assertEqual(_tree(self.home), before)
        for path in self.originals:
            self.assertFalse(os.path.lexists(self.backup(path)))
        self.assertIsNone(self.record())
        self.assertNotIn("已写入", result.text)

    @unittest.skipIf(_ROOT, "root ignores permissions")
    def test_c_undo_failure_reports_written_unrecorded(self):
        digest = self.digest()

        def fail_and_lock(*_args, **_kwargs):
            # The undo needs a temp file next to each target; lock both dirs.
            os.chmod(self.claude.parent, 0o555)
            os.chmod(self.home, 0o555)
            raise OSError("disk full")

        try:
            with mock.patch.object(host_rules, "write_record", side_effect=fail_and_lock):
                result = self.init("--host-rules", "apply", "--host-rules-digest", digest, as_json=False)
        finally:
            os.chmod(self.home, 0o755)
            os.chmod(self.claude.parent, 0o755)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload["status"], "ok")
        hr = result.payload["host_rules"]
        self.assertEqual(hr["status"], "written_unrecorded", hr)
        self.assertEqual(sorted(hr["paths"]), ["~/.claude/CLAUDE.md", "~/AGENTS.md"])
        self.assertIn("~/AGENTS.md" + host_rules.BACKUP_SUFFIX, hr["reason"])
        self.assertNotIn(str(self.home), hr["reason"])
        notes = result.payload["notes"]
        self.assertTrue(any(n.startswith("用户级规则已写入但未登记：") for n in notes), notes)
        self.assertFalse(any("未写入" in n for n in notes), notes)
        self.assertIn("已写入但未登记", result.text)
        self.assertNotIn("未写入", result.text)
        self.assertIn(host_rules.END_MARKER, self.claude.read_text(encoding="utf-8"))

    def test_d_normal_apply_and_rollback_work_after_a_failed_record(self):
        digest = self.digest()
        with mock.patch.object(host_rules, "write_record", side_effect=OSError("disk full")):
            self.init("--host-rules", "apply", "--host-rules-digest", digest)
        self.assertEqual(self.apply().payload["host_rules"]["status"], "applied")
        self.assertEqual(self.record()["decision"], "apply")
        rolled = self.init("--host-rules", "rollback")
        self.assertEqual(rolled.payload["host_rules"]["status"], "rolled_back")
        for path, original in self.originals.items():
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(os.path.lexists(self.backup(path)))


if __name__ == "__main__":
    unittest.main()
