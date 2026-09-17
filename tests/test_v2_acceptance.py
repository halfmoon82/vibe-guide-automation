import hashlib
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class PackagingAcceptanceTests(unittest.TestCase):
    def test_one_version_and_all_artifact_kinds_build(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            result = subprocess.run(
                [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-w", str(out), str(root)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            wheel = list(out.glob("*.whl"))
            self.assertEqual(len(wheel), 1)
            version = re.search(r"-(\d+\.\d+\.\d+)-", wheel[0].name).group(1)
            from vibe_guide import __version__ as current_version
            self.assertEqual(version, current_version)
            digest = hashlib.sha256(wheel[0].read_bytes()).hexdigest()
            self.assertEqual(len(digest), 64)

    def test_sdist_and_wheel_install_in_clean_venv(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "dist"
            out.mkdir()
            build = subprocess.run(
                [sys.executable, "setup.py", "sdist", "--dist-dir", str(out)],
                cwd=str(root), text=True, capture_output=True, check=False,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            from vibe_guide import __version__ as current_version
            from tests.support_packaging import find_sdist
            sdist = find_sdist(out, current_version)
            wheel_build = subprocess.run(
                [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-w", str(out), str(root)],
                cwd=str(root), text=True, capture_output=True, check=False,
            )
            self.assertEqual(wheel_build.returncode, 0, wheel_build.stderr)
            wheel = next(out.glob("*.whl"))
            for artifact in (sdist, wheel):
                venv = Path(directory) / ("venv-" + artifact.suffix.replace(".", ""))
                created = subprocess.run([sys.executable, "-m", "venv", str(venv)], text=True, capture_output=True, check=False)
                self.assertEqual(created.returncode, 0, created.stderr)
                installed = subprocess.run([str(venv / "bin/python"), "-m", "pip", "install", "--no-deps", str(artifact)], text=True, capture_output=True, check=False)
                self.assertEqual(installed.returncode, 0, installed.stderr)
                receiving = Path(directory) / ("receiving-" + artifact.suffix.replace(".", ""))
                receiving.mkdir()
                smoke = subprocess.run([str(venv / "bin/python"), "-m", "vibe_guide", "doctor", "--json"], cwd=str(receiving), text=True, capture_output=True, check=False)
                # An uninitialized directory is legitimately "blocked" for
                # doctor since V4 (missing AGENTS.md / knowledge / Skills);
                # the smoke test proves the installed package runs and its
                # manifests load, not that an empty directory is healthy.
                self.assertIn(smoke.returncode, (0, 3), smoke.stderr)
                import json as _json
                self.assertEqual(_json.loads(smoke.stdout)["command"], "doctor", smoke.stdout)
                console = venv / "bin" / "vibe"
                self.assertTrue(console.exists(), "console entrypoint was not installed for %s" % artifact.name)
