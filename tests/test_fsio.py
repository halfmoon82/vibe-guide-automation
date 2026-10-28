import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide import fsio


class FsioTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_roundtrip(self):
        p = self.dir / "a" / "x.json"
        fsio.atomic_write_json(p, {"k": "中"})
        self.assertEqual(fsio.read_json(p), {"k": "中"})

    def test_symlink_target_refused(self):
        real = self.dir / "real"
        real.write_text("keep")
        link = self.dir / "link"
        os.symlink(real, link)
        with self.assertRaises(ValueError):
            fsio.atomic_write_bytes(link, b"x")
        self.assertEqual(real.read_text(), "keep")

    def test_failure_mid_write_keeps_original(self):
        p = self.dir / "f"
        p.write_bytes(b"old")
        with mock.patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                fsio.atomic_write_bytes(p, b"new")
        self.assertEqual(p.read_bytes(), b"old")
        self.assertEqual([x.name for x in self.dir.iterdir()], ["f"])

    def test_missing_vs_corrupt(self):
        with self.assertRaises(fsio.JsonMissingError):
            fsio.read_json(self.dir / "none.json")
        bad = self.dir / "bad.json"
        bad.write_text("{oops")
        with self.assertRaises(fsio.JsonCorruptError):
            fsio.read_json(bad)
        empty = self.dir / "empty.json"
        empty.write_bytes(b"")
        with self.assertRaises(fsio.JsonCorruptError):
            fsio.read_json(empty)


if __name__ == "__main__":
    unittest.main()
