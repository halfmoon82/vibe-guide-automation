"""Shared helpers for the packaging acceptance tests.

Fresh ``venv`` environments on Python 3.12+ ship without setuptools, so a
``setup.py`` invocation inside them fails before it tests anything.  The
helpers below make that an explicit, honest skip (offline) or a one-line
bootstrap (online) instead of a spurious failure, and hide the sdist filename
difference between setuptools generations (``vibe-guide-`` vs ``vibe_guide-``).
"""
import subprocess
import importlib.metadata
import json
import os
from pathlib import Path
import sys


def offline_build_environment(python):
    """Expose only the existing build tools to a temporary venv subprocess.

    No distribution is installed or downloaded. Curated links keep the full
    host site-packages (and project checkout) off the child's import path.
    The caller passes the returned environment only to build/install commands.
    """
    python = Path(python).absolute()
    stage = python.parent.parent / 'build-toolchain'
    if not (python.parent.parent / 'pyvenv.cfg').is_file():
        raise ValueError('offline build tools require a temporary venv')
    stage.mkdir(exist_ok=True)
    sources = {}
    for name, packages in (('setuptools', ('setuptools', '_distutils_hack', 'pkg_resources')),
                           ('wheel', ('wheel',))):
        distribution = importlib.metadata.distribution(name)
        metadata = {file.parts[0] for file in distribution.files or ()
                    if file.parts[0].endswith('.dist-info')}
        if len(metadata) != 1:
            raise RuntimeError('local build tool metadata is unavailable: ' + name)
        paths = []
        for relative in (*packages, *sorted(metadata)):
            source = Path(distribution.locate_file(relative)).resolve()
            if not source.is_dir():
                raise RuntimeError('local build tool path is unavailable: ' + str(source))
            target = stage / relative
            if target.is_symlink():
                if target.resolve() != source:
                    raise RuntimeError('temporary build tool identity changed')
            else:
                target.symlink_to(source, target_is_directory=True)
            paths.append(str(source))
        sources[name] = {'version': distribution.version, 'paths': paths}
    environment = os.environ.copy()
    environment.update(PYTHONPATH=str(stage), PIP_CONFIG_FILE=os.devnull,
        PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_DEPS='1',
        PIP_NO_CACHE_DIR='1', PIP_NO_BUILD_ISOLATION='0')
    # pip's --no-build-isolation is store_false: env value 0 sets its
    # build_isolation destination false, including develop's nested pip.
    print('OFFLINE_BUILD_TOOLCHAIN ' + json.dumps({'python': str(python),
        'stage': str(stage), 'sources': sources}, sort_keys=True), file=sys.stderr)
    return environment


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
