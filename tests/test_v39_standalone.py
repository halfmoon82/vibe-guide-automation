import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StandaloneInstallTests(unittest.TestCase):
    def _run(self, *args, cwd=None, env=None):
        return subprocess.run(
            list(args), cwd=cwd, env=env, text=True, capture_output=True
        )

    def test_wheel_install_exposes_full_cli_in_fresh_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            venv = root / "venv"
            wheelhouse = root / "wheelhouse"
            self.assertEqual(self._run(sys.executable, "-m", "venv", str(venv)).returncode, 0)
            python = venv / "bin" / "python"
            vibe = venv / "bin" / "vibe"
            built = self._run(
                str(python), "-m", "pip", "wheel", "--no-deps",
                "--wheel-dir", str(wheelhouse), str(ROOT),
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            wheel = next(wheelhouse.glob("vibe_guide-*.whl"))
            installed = self._run(str(python), "-m", "pip", "install", "--no-deps", str(wheel))
            self.assertEqual(installed.returncode, 0, installed.stderr)
            help_result = self._run(str(vibe), "--help", cwd=root)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("usage: vibe", help_result.stdout)

    def test_sdist_install_exposes_module_cli_in_blank_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            venv = root / "venv"
            dist = root / "dist"
            self.assertEqual(self._run(sys.executable, "-m", "venv", str(venv)).returncode, 0)
            python = venv / "bin" / "python"
            source = root / "source"
            shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"))
            built = self._run(str(python), "setup.py", "sdist", "--dist-dir", str(dist), cwd=source)
            self.assertEqual(built.returncode, 0, built.stderr)
            archive = next(dist.glob("vibe-guide-*.tar.gz"))
            installed = self._run(str(python), "-m", "pip", "install", "--no-deps", str(archive))
            self.assertEqual(installed.returncode, 0, installed.stderr)
            result = self._run(str(python), "-m", "vibe_guide", "--help", cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage: vibe", result.stdout)

    def test_upgrade_is_explicit_idempotent_and_does_not_deploy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            blocked = self._run(sys.executable, "-m", "vibe_guide", "upgrade", "--json", cwd=root, env=env)
            self.assertEqual(blocked.returncode, 3)
            self.assertFalse((root / ".vibe").exists())
            first = self._run(sys.executable, "-m", "vibe_guide", "upgrade", "--confirm", "--json", cwd=root, env=env)
            self.assertEqual(first.returncode, 0, first.stderr)
            payload = json.loads(first.stdout)
            self.assertEqual(payload["command"], "upgrade")
            self.assertTrue(payload["changed"])
            second = self._run(sys.executable, "-m", "vibe_guide", "upgrade", "--confirm", "--json", cwd=root, env=env)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertFalse(json.loads(second.stdout)["changed"])
            self.assertFalse((root / ".vibe" / "deploy").exists())


if __name__ == "__main__":
    unittest.main()
