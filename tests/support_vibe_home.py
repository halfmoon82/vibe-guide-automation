"""Hermetic VIBE_HOME and host identity for tests (#378).

A test file opts in with two lines::

    from tests import support_vibe_home as hermetic
    setUpModule, tearDownModule = hermetic.module_hooks()

While active, ``VIBE_HOME`` points at a throwaway directory holding a fixed
minimal ``config.json`` (``claude_bg`` present, no ``model_map``), ``CODEX_HOME``
points at an empty directory, and every host-identity variable (Claude Code,
Codex, WorkBuddy) is removed.  A case that needs a different config or a
specific host says so itself with :func:`override_env` / :func:`write_config`.
"""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

#: Exact host-identity names, plus the prefixes below.
HOST_ENV_NAMES = (
    "CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEBUDDY_SESSION_ID", "AI_AGENT",
)
HOST_ENV_PREFIXES = ("CLAUDE_CODE_", "CODEBUDDY_", "CODEX_THREAD", "CLAUDE_SESSION")


def minimal_config(home):
    return {"claude_bg": {"settings_path": str(Path(home) / "claude-bg" / "settings.json")}}


def write_config(config, home=None):
    """Replace the isolated ``$VIBE_HOME/config.json`` (for cases that declare a config)."""
    home = Path(home or os.environ["VIBE_HOME"])
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _is_host_var(name):
    return name in HOST_ENV_NAMES or name.startswith(HOST_ENV_PREFIXES)


@contextmanager
def override_env(**values):
    """Declare host variables a case needs (value ``None`` removes the name)."""
    saved = {name: os.environ.get(name) for name in values}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def isolated_environment():
    saved = dict(os.environ)
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "vibe-home"
        codex = Path(tmp) / "codex-home"
        home.mkdir()
        codex.mkdir()
        (home / "config.json").write_text(json.dumps(minimal_config(home)), encoding="utf-8")
        try:
            for name in [n for n in os.environ if _is_host_var(n)]:
                del os.environ[name]
            os.environ["VIBE_HOME"] = str(home)
            os.environ["CODEX_HOME"] = str(codex)
            yield home
        finally:
            os.environ.clear()
            os.environ.update(saved)


def module_hooks():
    """``(setUpModule, tearDownModule)`` that hold the isolation for one test file."""
    stack = []

    def set_up():
        context = isolated_environment()
        context.__enter__()
        stack.append(context)

    def tear_down():
        while stack:
            stack.pop().__exit__(None, None, None)

    return set_up, tear_down
