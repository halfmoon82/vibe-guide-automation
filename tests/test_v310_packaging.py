import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# The upgrade target is the version the package currently declares; the
# legacy 2.0.0 side of the scenario is synthesized below and stays fixed.
from vibe_guide import __version__ as TARGET_VERSION  # noqa: E402


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


def _setup_py_version_literal():
    """The `version=` literal setuptools<61 builds from, read without executing."""
    tree = ast.parse((ROOT / "setup.py").read_text(encoding="utf-8"))
    return next(
        keyword.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "setup"
        for keyword in node.keywords
        if keyword.arg == "version"
    )


class PackagingV310Tests(unittest.TestCase):
    def test_metadata_and_source_version_are_consistent(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(pyproject, rf"(?ms)^\[project\].*?^version\s*=\s*['\"]{TARGET_VERSION}['\"]")
        setup_version = _run([sys.executable, "setup.py", "--version"], cwd=ROOT).stdout.strip()
        self.assertEqual(setup_version, TARGET_VERSION)
        # `setup.py --version` answers from pyproject on setuptools>=61, so it
        # stays green while setup.py's own literal rots. On the setuptools 58
        # path the README documents for Python 3.9, that literal is what ships:
        # it sat at 4.5.0 through the 4.8.0 bump. Assert the literal itself.
        self.assertEqual(_setup_py_version_literal(), TARGET_VERSION)
        source = (ROOT / "vibe_guide" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn(f'__version__ = "{TARGET_VERSION}"', source)

    def test_the_version_the_installer_reports_is_the_package_version(self):
        """`vibe install` must not announce a different release than it is.

        This was a second hardcoded literal, three minors behind: a fresh
        install of 4.5.0 reported 4.2.2, and `inspect_compatibility` compared
        that stale value against a project's recorded config version, reading
        the package's own projects as "mixed".
        """
        from vibe_guide.installation import PACKAGE_VERSION, inspect_compatibility
        self.assertEqual(PACKAGE_VERSION, TARGET_VERSION)
        source = (ROOT / "vibe_guide" / "installation.py").read_text(encoding="utf-8")
        self.assertNotRegex(
            source,
            r'PACKAGE_VERSION\s*=\s*[\'"]',
            "PACKAGE_VERSION must derive from __version__, not a literal",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".vibe").mkdir()
            (root / ".vibe" / "config.json").write_text(
                json.dumps({"version": TARGET_VERSION}), encoding="utf-8"
            )
            report = inspect_compatibility(root)
        # The two values this defect put out of step, not the derived verdict:
        # `status` also folds in `installed_package_version`, which comes from
        # the environment's install metadata.  A checkout whose editable
        # install predates the bump reports "mixed" for a reason that has
        # nothing to do with the literal under test.
        self.assertEqual(report["versions"]["package_version"], TARGET_VERSION, report)
        self.assertEqual(report["versions"]["config_version"], TARGET_VERSION, report)

    def test_the_migration_namespace_is_not_tied_to_the_release(self):
        """A namespace is a directory on disk, so its name must stay put.

        Deriving it from the version would make every release write a new
        directory and stop reading the ones earlier releases created.
        """
        from vibe_guide.installation import MIGRATION_NAMESPACE, migration_preview
        with tempfile.TemporaryDirectory() as tmp:
            preview = migration_preview(Path(tmp))
        self.assertEqual(preview["target_namespace"], MIGRATION_NAMESPACE)
        self.assertNotIn(TARGET_VERSION, MIGRATION_NAMESPACE)
        # One definition, because writer and reader must agree: `migrate_state`
        # creates this directory and `rollback_state` looks for it.  While the
        # name was built from the version at four separate sites, deriving it
        # made rollback search a directory migration had never written.
        # Counted as parsed string literals rather than as raw text, so a
        # docstring mentioning the name does not read as a second definition
        # and switching to single quotes does not hide one.
        tree = ast.parse((ROOT / "vibe_guide" / "installation.py").read_text(encoding="utf-8"))
        literals = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value == MIGRATION_NAMESPACE
        ]
        self.assertEqual(len(literals), 1, "namespace name is spelled out more than once")

    def test_migrated_state_can_be_rolled_back(self):
        """Writer and reader of the namespace must resolve the same path.

        A split definition left this green in isolation and only failed here,
        because the two halves are in different functions.
        """
        from vibe_guide.installation import migrate_state, rollback_state
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".vibe").mkdir()
            (root / ".vibe" / "state.json").write_text('{"workflow_version": 2}', encoding="utf-8")
            self.assertEqual(migrate_state(root)["status"], "complete")
            rolled = rollback_state(root)
        self.assertEqual(rolled["status"], "complete", rolled)
        self.assertTrue(rolled["current_namespace_preserved"], rolled)

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

    def test_previous_release_to_current_upgrade_per_install_kind(self):
        """The declared upgrade path is the last tagged release -> this one.

        AGENTS.md's delivery rule wants wheel, sdist and source verified
        separately and the upgrade verified along a named old->new path.
        The 2.0.0 scenario above is synthetic; this one builds the previous
        release from its git tag, so the artifacts on the old side are the
        ones that were actually shipped, not a stand-in.
        """
        previous_version, previous_tag = _previous_release(self)
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            current_dist = temp / "current-dist"
            current_dist.mkdir()
            current_wheel, current_sdist = self._build_artifacts(current_dist)

            previous_src = temp / "previous-src"
            previous_src.mkdir()
            archive = subprocess.run(
                ["git", "archive", previous_tag],
                cwd=ROOT, check=True, stdout=subprocess.PIPE,
            )
            subprocess.run(["tar", "-x", "-C", str(previous_src)], input=archive.stdout, check=True)
            previous_dist = temp / "previous-dist"
            previous_dist.mkdir()
            _run([sys.executable, "setup.py", "bdist_wheel", "--dist-dir", str(previous_dist)], cwd=previous_src)
            _run([sys.executable, "setup.py", "sdist", "--dist-dir", str(previous_dist)], cwd=previous_src)
            previous_wheel = next(previous_dist.glob("*.whl"))
            previous_sdist = next(previous_dist.glob("*.tar.gz"))
            self.assertIn(previous_version, previous_wheel.name)
            self.assertIn(previous_version, previous_sdist.name)

            kinds = (
                ("wheel", previous_wheel, current_wheel),
                ("sdist", previous_sdist, current_sdist),
                ("source", previous_src, ROOT),
            )
            evidence = {}
            for label, old, new in kinds:
                with self.subTest(install=label):
                    python = self._new_venv(temp / label)
                    _run([str(python), "-m", "pip", "install", "--no-deps", str(old)], cwd=temp / label)
                    self._assert_installed(python, previous_version)
                    _run([str(python), "-m", "pip", "install", "--no-deps", "--upgrade", str(new)], cwd=temp / label)
                    self._assert_installed(python, TARGET_VERSION)
                    _run([str(python), "-m", "pip", "install", "--no-deps", "--force-reinstall", str(old)], cwd=temp / label)
                    self._assert_installed(python, previous_version)
                    evidence[label] = {
                        "source_version": previous_version,
                        "target_version": TARGET_VERSION,
                        "upgrade_verified": True,
                        "rollback_verified": True,
                    }
            self.assertEqual(sorted(evidence), ["sdist", "source", "wheel"])


def _previous_release(test):
    """The highest `v*` tag below the version the package declares.

    A checkout without git cannot name its predecessor, so it skips loudly; a
    git checkout that has tags but none below the current version is a
    release-hygiene failure, not a skip.
    """
    if not (ROOT / ".git").exists():
        test.skipTest("not a git checkout: previous release tag unavailable")
    listing = subprocess.run(
        ["git", "tag", "--list", "v*"], cwd=ROOT, check=True, text=True, stdout=subprocess.PIPE,
    ).stdout.split()
    target = tuple(int(part) for part in TARGET_VERSION.split("."))
    candidates = []
    for tag in listing:
        try:
            parts = tuple(int(part) for part in tag[1:].split("."))
        except ValueError:
            continue
        # Tags are a mix of three-part (v4.5.0) and two-part (v4.8); pad so the
        # short ones stay in the running. Requiring three parts silently dropped
        # v4.8, and would have hard-failed here once the padded tags aged out.
        if not 1 <= len(parts) <= 3:
            continue
        parts += (0,) * (3 - len(parts))
        if parts < target:
            candidates.append((parts, tag))
    test.assertTrue(candidates, "no tagged release below %s to upgrade from" % TARGET_VERSION)
    parts, tag = max(candidates)
    return ".".join(str(part) for part in parts), tag


if __name__ == "__main__":
    unittest.main()
