import sys
import tempfile
import unittest
from pathlib import Path

from vibe_guide.baseline_health import (
    build_baseline_health, load_baseline_health, save_baseline_health,
)
from vibe_guide.paths import ProjectPaths


class V38BaselineHealthTests(unittest.TestCase):
    def test_one_run_manifest_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ProjectPaths(root)
            health = build_baseline_health(root, "a" * 40, [[sys.executable, "-c", "print('ok')"]])
            save_baseline_health(paths, "run", health)
            loaded = load_baseline_health(paths, "run")
            self.assertEqual(loaded.digest(), health.digest())
            self.assertEqual(loaded.commands[0]["exit_code"], 0)

    def test_failed_command_is_classified_as_baseline_defect(self):
        with tempfile.TemporaryDirectory() as directory:
            health = build_baseline_health(Path(directory), "a" * 40,
                                           [[sys.executable, "-c", "raise SystemExit(2)"]])
            self.assertEqual(health.scope, "out_of_scope")
            self.assertEqual(health.commands[0]["exit_code"], 2)


if __name__ == "__main__":
    unittest.main()
