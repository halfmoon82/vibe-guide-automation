import subprocess
import sys
import unittest


class EntryPointSmokeTests(unittest.TestCase):
    def test_module_entrypoint_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "vibe_guide", "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: vibe", result.stdout)

