import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vibe_guide.installation import InstallStateMachine, run_upgrade
from vibe_guide.models import InstallRequest
from vibe_guide.paths import ProjectPaths


ROOT = Path(__file__).resolve().parents[1]


def _run(command, *, cwd, env=None):
    return subprocess.run(command, cwd=cwd, env=env, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


class V4PackagingCompatibilityTests(unittest.TestCase):
    def _venv_python(self, root):
        _run([sys.executable, "-m", "venv", str(root / "venv")], cwd=root)
        return root / "venv" / "bin" / "python"

    def test_v310_wheel_sdist_and_source_install(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            dist = temp / "dist"
            dist.mkdir()
            _run([sys.executable, "setup.py", "bdist_wheel", "--dist-dir", str(dist)], cwd=ROOT)
            _run([sys.executable, "setup.py", "sdist", "--dist-dir", str(dist)], cwd=ROOT)
            wheel = next(dist.glob("*.whl"))
            sdist = next(dist.glob("*.tar.gz"))
            self.assertIn("4.0.0", wheel.name)
            self.assertIn("4.0.0", sdist.name)
            for label, artifact in (("wheel", wheel), ("sdist", sdist), ("source", ROOT)):
                with self.subTest(install=label):
                    env_root = temp / label
                    env_root.mkdir()
                    python = self._venv_python(env_root)
                    env = os.environ.copy()
                    env.pop("PYTHONPATH", None)
                    _run([str(python), "-m", "pip", "install", "--no-deps", str(artifact)], cwd=env_root, env=env)
                    result = _run([str(python), "-c", "import vibe_guide; print(vibe_guide.__version__)"], cwd=temp, env=env)
                    self.assertEqual(result.stdout.strip(), "4.0.0")

    def test_v200_upgrade_and_rollback_and_both_install_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mode in ("layered", "bundled"):
                with self.subTest(mode=mode):
                    result = InstallStateMachine().run(mode, root / mode)
                    self.assertEqual(result.status, "complete")
                    self.assertEqual(result.mode, mode)

            legacy = root / "legacy"
            package = legacy / "vibe_guide"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text('__version__ = "2.0.0"\n', encoding="utf-8")
            (legacy / "setup.py").write_text(
                "from setuptools import setup; setup(name='vibe-guide', version='2.0.0', packages=['vibe_guide'])\n",
                encoding="utf-8",
            )
            (root / "legacy-dist").mkdir()
            _run([sys.executable, "setup.py", "bdist_wheel", "--dist-dir", str(root / "legacy-dist")], cwd=legacy)
            legacy_wheel = next((root / "legacy-dist").glob("*.whl"))
            current_dist = root / "current-dist"
            current_dist.mkdir()
            _run([sys.executable, "setup.py", "bdist_wheel", "--dist-dir", str(current_dist)], cwd=ROOT)
            current_wheel = next(current_dist.glob("*.whl"))

            # Exercise the real package upgrade and rollback path in an
            # isolated interpreter, independently of the migration helper.
            venv_root = root / "upgrade-venv"
            venv_root.mkdir()
            python = self._venv_python(venv_root)
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            _run([str(python), "-m", "pip", "install", "--no-deps", str(legacy_wheel)], cwd=venv_root, env=env)
            self.assertEqual(_run([str(python), "-c", "import vibe_guide; print(vibe_guide.__version__)"], cwd=root, env=env).stdout.strip(), "2.0.0")
            _run([str(python), "-m", "pip", "install", "--no-deps", "--upgrade", str(current_wheel)], cwd=venv_root, env=env)
            self.assertEqual(_run([str(python), "-c", "import vibe_guide; print(vibe_guide.__version__)"], cwd=root, env=env).stdout.strip(), "4.0.0")
            _run([str(python), "-m", "pip", "install", "--no-deps", "--force-reinstall", str(legacy_wheel)], cwd=venv_root, env=env)
            self.assertEqual(_run([str(python), "-c", "import vibe_guide; print(vibe_guide.__version__)"], cwd=root, env=env).stdout.strip(), "2.0.0")

            upgrade_target = root / "upgrade-target"
            upgrade_target.mkdir()
            (upgrade_target / ".vibe").mkdir()
            (upgrade_target / ".vibe" / "config.json").write_text('{"version":"2.0.0"}', encoding="utf-8")
            migration = run_upgrade(InstallRequest("layered", True, upgrade_target), ProjectPaths(upgrade_target),
                                    lambda *_: {"status": "approved"}, lambda *_: {"status": "verified"})
            self.assertEqual(migration.version_before, "2.0.0")
            self.assertEqual(migration.migration["target_version"], "4.0.0")
            self.assertTrue(legacy_wheel.exists())

    def test_v310_package_upgrades_directly_to_v4_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy-v310"
            package = legacy / "vibe_guide"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text('__version__ = "3.10.0"\n', encoding="utf-8")
            (legacy / "setup.py").write_text(
                "from setuptools import setup; setup(name='vibe-guide', version='3.10.0', packages=['vibe_guide'])\n",
                encoding="utf-8",
            )
            legacy_dist = root / "legacy-dist"
            legacy_dist.mkdir()
            _run([sys.executable, "setup.py", "bdist_wheel", "--dist-dir", str(legacy_dist)], cwd=legacy)
            legacy_wheel = next(legacy_dist.glob("*.whl"))

            current_dist = root / "current-dist"
            current_dist.mkdir()
            _run([sys.executable, "setup.py", "bdist_wheel", "--dist-dir", str(current_dist)], cwd=ROOT)
            current_wheel = next(current_dist.glob("*.whl"))

            env_root = root / "upgrade-venv"
            env_root.mkdir()
            python = self._venv_python(env_root)
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            _run([str(python), "-m", "pip", "install", "--no-deps", str(legacy_wheel)], cwd=env_root, env=env)
            self.assertEqual(_run([str(python), "-c", "import vibe_guide; print(vibe_guide.__version__)"], cwd=root, env=env).stdout.strip(), "3.10.0")
            _run([str(python), "-m", "pip", "install", "--no-deps", "--upgrade", str(current_wheel)], cwd=env_root, env=env)
            self.assertEqual(_run([str(python), "-c", "import vibe_guide; print(vibe_guide.__version__)"], cwd=root, env=env).stdout.strip(), "4.0.0")
            _run([str(python), "-m", "pip", "install", "--no-deps", "--force-reinstall", str(legacy_wheel)], cwd=env_root, env=env)
            self.assertEqual(_run([str(python), "-c", "import vibe_guide; print(vibe_guide.__version__)"], cwd=root, env=env).stdout.strip(), "3.10.0")


if __name__ == "__main__":
    unittest.main()
