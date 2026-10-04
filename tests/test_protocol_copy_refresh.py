"""Protocol copies that are a byte-exact earlier release are refreshed.

``vibe init`` materializes prd-guide and vibe-entry into the project and used
to never touch them again, so an upgrade left every project on the protocol
it was first initialized with (the 5.0.3 heartbeat fix never reached them).
A copy whose digest matches a version vibe itself shipped cannot carry a
local edit, so it is replaced; anything else is still left for a human.
"""
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide import protocols
from vibe_guide.cli import run_cli
from vibe_guide.protocols import (
    PRD_GUIDE_PROPOSAL_RELATIVE,
    SHIPPED_PROTOCOL_DIGESTS,
    VIBE_ENTRY_PROPOSAL_RELATIVE,
    load_protocol,
)

ROOT = Path(__file__).resolve().parent.parent
OLD_TEXT = "# prd-guide as an earlier release shipped it\n"
OLD_DIGEST = hashlib.sha256(OLD_TEXT.encode("utf-8")).hexdigest()
COPIES = (
    ("prd-guide", PRD_GUIDE_PROPOSAL_RELATIVE),
    ("vibe-entry", VIBE_ENTRY_PROPOSAL_RELATIVE),
)


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ShippedDigestRegistryTests(unittest.TestCase):
    def test_the_current_protocol_is_registered(self):
        """Changing a protocol without registering it would strand every
        project initialized on that version once the next release ships."""
        for name, _ in COPIES:
            self.assertIn(_digest(load_protocol(name)), SHIPPED_PROTOCOL_DIGESTS[name], name)

    def test_every_committed_version_is_registered(self):
        for name, _ in COPIES:
            relative = "vibe_guide/protocols/{}.md".format(name)
            try:
                log = subprocess.run(
                    ["git", "log", "--format=%H", "--", relative],
                    cwd=ROOT, capture_output=True, text=True, check=True,
                ).stdout.split()
            except (OSError, subprocess.CalledProcessError):
                self.skipTest("git history unavailable")
            if len(log) < 2:
                self.skipTest("shallow checkout")
            for commit in log:
                shown = subprocess.run(
                    ["git", "show", "{}:{}".format(commit, relative)],
                    cwd=ROOT, capture_output=True, check=False,
                )
                if shown.returncode != 0:
                    continue
                self.assertIn(
                    hashlib.sha256(shown.stdout).hexdigest(),
                    SHIPPED_PROTOCOL_DIGESTS[name],
                    "{} @ {}".format(name, commit[:7]),
                )


class _Case(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vibe-protocol-refresh-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        result = run_cli(["init", "--confirm", "--json"], self.root)
        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        patcher = mock.patch.dict(
            protocols.SHIPPED_PROTOCOL_DIGESTS,
            {name: SHIPPED_PROTOCOL_DIGESTS[name] | {OLD_DIGEST} for name, _ in COPIES},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def age(self, relative, text=OLD_TEXT):
        path = self.root / relative
        path.write_text(text, encoding="utf-8")
        return path


class InitRefreshTests(_Case):
    def test_init_refreshes_an_unedited_earlier_release(self):
        for name, relative in COPIES:
            copy = self.age(relative)
            result = run_cli(["init", "--confirm", "--json"], self.root)
            self.assertEqual(copy.read_text(encoding="utf-8"), load_protocol(name), name)
            self.assertIn(relative, result.payload.get("paths", []))
            self.assertFalse(
                any(relative in note for note in result.payload.get("notes", [])),
                result.payload,
            )

    def test_init_still_leaves_a_local_edit_alone(self):
        copy = self.age(PRD_GUIDE_PROPOSAL_RELATIVE, OLD_TEXT + "本地补充\n")
        result = run_cli(["init", "--confirm", "--json"], self.root)
        self.assertEqual(copy.read_text(encoding="utf-8"), OLD_TEXT + "本地补充\n")
        self.assertTrue(
            any(PRD_GUIDE_PROPOSAL_RELATIVE in note and "不一致" in note
                for note in result.payload.get("notes", [])),
            result.payload,
        )


class CommandRefreshTests(_Case):
    def test_working_commands_refresh_and_report(self):
        for command in (["resume"], ["monitor"], ["plan", "--request", "改个文案"]):
            for name, relative in COPIES:
                self.age(relative)
            result = run_cli(command + ["--json"], self.root)
            for name, relative in COPIES:
                self.assertEqual(
                    (self.root / relative).read_text(encoding="utf-8"),
                    load_protocol(name), command,
                )
            self.assertEqual(
                sorted(result.payload.get("protocol_refreshed", [])),
                sorted(relative for _, relative in COPIES), command,
            )
            text = run_cli(command, self.root).text
            self.assertNotIn("协议副本", text, "an already-current copy is not re-reported")

    def test_text_output_names_the_refreshed_copy(self):
        self.age(VIBE_ENTRY_PROPOSAL_RELATIVE)
        text = run_cli(["resume"], self.root).text
        self.assertIn(VIBE_ENTRY_PROPOSAL_RELATIVE, text)

    def test_local_edit_is_not_touched(self):
        edited = OLD_TEXT + "本地补充\n"
        copy = self.age(PRD_GUIDE_PROPOSAL_RELATIVE, edited)
        result = run_cli(["resume", "--json"], self.root)
        self.assertEqual(copy.read_text(encoding="utf-8"), edited)
        self.assertNotIn("protocol_refreshed", result.payload)

    def test_read_only_commands_never_refresh(self):
        copy = self.age(PRD_GUIDE_PROPOSAL_RELATIVE)
        for command in (["scan"], ["status"], ["plan", "--print-protocol"], ["doctor"]):
            run_cli(command, self.root)
            self.assertEqual(copy.read_text(encoding="utf-8"), OLD_TEXT, command)

    def test_symlinked_copy_is_not_followed(self):
        outside = Path(tempfile.mkdtemp(prefix="vibe-protocol-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        target = outside / "SKILL.md"
        target.write_text(OLD_TEXT, encoding="utf-8")
        copy = self.root / PRD_GUIDE_PROPOSAL_RELATIVE
        copy.unlink()
        copy.symlink_to(target)
        run_cli(["resume"], self.root)
        self.assertEqual(target.read_text(encoding="utf-8"), OLD_TEXT)

    def test_symlinked_directory_is_not_followed(self):
        outside = Path(tempfile.mkdtemp(prefix="vibe-protocol-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "SKILL.md").write_text(OLD_TEXT, encoding="utf-8")
        folder = (self.root / PRD_GUIDE_PROPOSAL_RELATIVE).parent
        shutil.rmtree(folder)
        folder.symlink_to(outside, target_is_directory=True)
        run_cli(["resume"], self.root)
        self.assertEqual((outside / "SKILL.md").read_text(encoding="utf-8"), OLD_TEXT)

    def test_uninitialized_project_is_not_written(self):
        bare = Path(tempfile.mkdtemp(prefix="vibe-protocol-bare-"))
        self.addCleanup(shutil.rmtree, bare, True)
        run_cli(["resume"], bare)
        self.assertEqual(list(bare.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
