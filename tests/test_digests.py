import tempfile
import unittest
from pathlib import Path

from vibe_guide import authorization, authorize_entry, decisions, digests


class DigestGoldenTests(unittest.TestCase):
    def test_canonical_matches_existing(self):
        for value in ({"b": 1, "a": "中文", "n": [1, {"z": None}]}, {}, {"x": "é\n"}):
            expected = authorization._canonical_digest(value)
            self.assertEqual(digests.canonical_json_digest(value), expected)
            self.assertEqual(decisions.canonical_digest(value), expected)

    def test_file_matches_existing(self):
        p = Path(tempfile.mkdtemp()) / "f.bin"
        p.write_bytes(b"abc" * 1000003)
        self.assertEqual(digests.sha256_file(p), authorize_entry._sha(p))

    def test_bytes_known_vector(self):
        self.assertEqual(
            digests.sha256_bytes(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )


if __name__ == "__main__":
    unittest.main()
