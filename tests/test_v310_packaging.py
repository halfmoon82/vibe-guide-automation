import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET_VERSION = "4.0.0"


def _run(command, *, cwd, env=None):
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


class PackagingV310Tests(unittest.TestCase):
    def test_metadata_and_source_version_are_consistent(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(pyproject, rf"(?ms)^\[project\].*?^version\s*=\s*['\"]{TARGET_VERSION}['\"]")
        setup_version = _run([sys.executable, "setup.py", "--version"], cwd=ROOT).stdout.strip()
        self.assertEqual(setup_version, TARGET_VERSION)
        source = (ROOT / "vibe_guide" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn(f'__version__ = "{TARGET_VERSION}"', source)

    def _build_artifacts(self, out_dir):
        _run([sys.executable, "setup.py", "bdist_wheel", "--dist-dir", str(out_dir)], cwd=ROOT)
        _run([sys.executable, "setup.py", "sdist", "--dist-dir", str(out_dir)], cwd=ROOT)
        wheels = sorted(out_dir.glob("*.whl"))
        sdists = sorted(out_dir.glob("*.tar.gz"))
        self.assertEqual(len(wheels), 1)
        self.assertEqual(len(sdists), 1)
        self.assertIn(TARGET_VERSION, wheels[0].name)
        self.assertIn(TARGET_VERSION, sdists[0].name)
        return wheels[0], sdists[0]

    def _new_venv(self, root):
        root.mkdir(parents=True, exist_ok=True)
        venv = root / "venv"
        _run([sys.executable, "-m", "venv", str(venv)], cwd=root)
        return venv / "bin" / "python"

    def _assert_installed(self, python, expected_version=TARGET_VERSION):
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = _run(
            [str(python), "-c", "import vibe_guide; print(vibe_guide.__version__)"],
            cwd=Path(tempfile.gettempdir()),
            env=env,
        )
        self.assertEqual(result.stdout.strip(), expected_version)

    def test_wheel_sdist_and_source_install_in_clean_environments(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            dist = temp / "dist"
            dist.mkdir()
            wheel, sdist = self._build_artifacts(dist)
            for label, artifact in (("wheel", wheel), ("sdist", sdist), ("source", ROOT)):
                with self.subTest(install=label):
                    python = self._new_venv(temp / label)
                    _run([str(python), "-m", "pip", "install", "--no-deps", str(artifact)], cwd=temp / label)
                    self._assert_installed(python)

    def test_v200_to_v310_upgrade_and_rollback_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            dist = temp / "dist"
            dist.mkdir()
            wheel, _ = self._build_artifacts(dist)

            legacy = temp / "legacy"
            package = legacy / "vibe_guide"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text('__version__ = "2.0.0"\n', encoding="utf-8")
            (legacy / "setup.py").write_text(
                "from setuptools import setup; setup(name='vibe-guide', version='2.0.0', packages=['vibe_guide'])\n",
                encoding="utf-8",
            )
            legacy_dist = temp / "legacy-dist"
            legacy_dist.mkdir()
            _run([sys.executable, "setup.py", "bdist_wheel", "--dist-dir", str(legacy_dist)], cwd=legacy)
            legacy_wheel = next(legacy_dist.glob("*.whl"))

            python = self._new_venv(temp / "upgrade")
            _run([str(python), "-m", "pip", "install", "--no-deps", str(legacy_wheel)], cwd=temp / "upgrade")
            self._assert_installed(python, "2.0.0")
            _run([str(python), "-m", "pip", "install", "--no-deps", "--upgrade", str(wheel)], cwd=temp / "upgrade")
            self._assert_installed(python, TARGET_VERSION)
            _run([str(python), "-m", "pip", "install", "--no-deps", "--force-reinstall", str(legacy_wheel)], cwd=temp / "upgrade")
            self._assert_installed(python, "2.0.0")

            evidence = {
                "source_version": "2.0.0",
                "target_version": TARGET_VERSION,
                "upgrade_verified": True,
                "rollback_verified": True,
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            }
            self.assertEqual(json.loads(json.dumps(evidence))["target_version"], TARGET_VERSION)


if __name__ == "__main__":
    unittest.main()
