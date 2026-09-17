"""Shared helpers for the packaging acceptance tests.

Fresh ``venv`` environments on Python 3.12+ ship without setuptools, so a
``setup.py`` invocation inside them fails before it tests anything.  The
helpers below make that an explicit, honest skip (offline) or a one-line
bootstrap (online) instead of a spurious failure, and hide the sdist filename
difference between setuptools generations (``vibe-guide-`` vs ``vibe_guide-``).
"""
import subprocess
from pathlib import Path


def ensure_setuptools(case, python):
    """Bootstrap setuptools+wheel into a fresh venv, or skip the test if offline."""
    probe = subprocess.run([str(python), "-c", "import setuptools"], text=True, capture_output=True)
    if probe.returncode == 0:
        return
    installed = subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "setuptools", "wheel"],
        text=True, capture_output=True,
    )
    if installed.returncode != 0:
        case.skipTest("fresh venv has no setuptools and it could not be installed: " + installed.stderr.strip()[-200:])


def find_sdist(directory, version):
    """Return the sdist for ``version`` under either setuptools naming scheme."""
    candidates = [
        Path(directory) / ("vibe-guide-%s.tar.gz" % version),
        Path(directory) / ("vibe_guide-%s.tar.gz" % version),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise AssertionError("no sdist for version %s in %s" % (version, directory))
