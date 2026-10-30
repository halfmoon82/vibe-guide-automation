"""launchd keep-alive for ``vibe daemon`` (authorization card option, off by default).

The card's ``launchd_keepalive`` choice is the only automatic way a killed daemon
comes back: the supervisor heartbeat reports a dead daemon but never restarts it.
Enabling it writes one per-run LaunchAgent plist and loads it with ``launchctl``.

Every external effect goes through injected pieces, so tests never touch the
machine: ``runner`` replaces ``launchctl`` and ``agents_dir`` replaces
``~/Library/LaunchAgents``.  Only macOS has a launchd; other platforms get
``unsupported`` from the same entry points.  Verified so far: nothing on a real
machine (the code is exercised against a fake ``launchctl`` only).
"""

import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import fsio
from .state import run_dir, validate_run_id

LABEL_PREFIX = "com.vibeguide.daemon."
LAUNCHCTL = "/bin/launchctl"
THROTTLE_SECONDS = 30
LAUNCHCTL_TIMEOUT = 30
#: launchctl exit code for "no such service"
_NOT_LOADED = 113
#: Platforms and whether the keep-alive has been checked on a real machine there.
PLATFORM_VERIFICATION = {"darwin": "未验证", "linux": "未验证", "win32": "未验证"}
UNVERIFIED_NOTE = "开机保活（launchd）尚未在真机验证"
UNSUPPORTED_REASON = "launchd keep-alive is only available on macOS; nothing was done"
_REQUIRED_KEYS = ("Label", "ProgramArguments", "RunAtLoad", "KeepAlive", "WorkingDirectory")


def default_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _run_launchctl(argv: List[str]):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=LAUNCHCTL_TIMEOUT)


def label(run_id: str) -> str:
    return LABEL_PREFIX + validate_run_id(run_id)


def plist_path(agents_dir: Path, run_id: str) -> Path:
    return Path(agents_dir) / (label(run_id) + ".plist")


def build_plist(paths, run_id: str, command: List[str]) -> bytes:
    log = str(run_dir(paths, run_id, create=True) / "daemon.log")
    search = [item for item in os.environ.get("PATH", "").split(":") if os.path.isabs(item)]
    return plistlib.dumps({
        "Label": label(run_id),
        "ProgramArguments": list(command),
        "WorkingDirectory": str(paths.root),
        "RunAtLoad": True,
        # a deliberate exit (run finished, `daemon stop`) is exit 0 and stays down
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_SECONDS,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PATH": ":".join(search) or "/usr/bin:/bin:/usr/sbin:/sbin",
                                 "VIBE_HOME": str(paths.vibe_home)},
        "StandardOutPath": log,
        "StandardErrorPath": log,
    })


def validate_plist(data: bytes, run_id: str) -> List[str]:
    """Problems found in plist ``data``; an empty list means it is acceptable."""
    try:
        value = plistlib.loads(data)
    except Exception:
        return ["plist is not parseable"]
    if not isinstance(value, dict):
        return ["plist root must be a dictionary"]
    problems = ["missing key " + key for key in _REQUIRED_KEYS if key not in value]
    if value.get("Label") != label(run_id):
        problems.append("label does not match the run")
    command = value.get("ProgramArguments")
    if (not isinstance(command, list) or not command
            or not all(isinstance(item, str) and "\0" not in item for item in command)
            or not os.path.isabs(command[0])):
        problems.append("program path must be absolute")
    elif "daemon" not in command or "run" not in command or run_id not in command:
        problems.append("command is not this run's daemon")
    if value.get("KeepAlive") != {"SuccessfulExit": False}:
        problems.append("keep-alive policy must be SuccessfulExit=false")
    if value.get("RunAtLoad") is not True:
        problems.append("RunAtLoad must be true")
    return problems


def _target(uid: int, run_id: str) -> str:
    return "gui/%d/%s" % (uid, label(run_id))


def _launchctl(runner, *args: str):
    try:
        return runner([LAUNCHCTL] + list(args)).returncode, None
    except (OSError, subprocess.SubprocessError) as error:
        return None, type(error).__name__


def _context(platform, agents_dir, uid, runner) -> Dict[str, Any]:
    return {"platform": platform or sys.platform,
            "agents_dir": Path(agents_dir) if agents_dir is not None else default_agents_dir(),
            "uid": os.getuid() if uid is None else uid,
            "runner": runner or _run_launchctl}


def _unsupported(platform: str) -> Dict[str, Any]:
    return {"status": "unsupported", "platform": platform, "reason": UNSUPPORTED_REASON,
            "verification": PLATFORM_VERIFICATION.get(platform, "未验证")}


def install(paths, run_id: str, command: List[str], *, runner: Optional[Callable] = None,
            agents_dir=None, platform: Optional[str] = None, uid: Optional[int] = None) -> Dict[str, Any]:
    """Write the plist, check it, load it.  ``loaded`` or a reason; never raises for launchd faults."""
    ctx = _context(platform, agents_dir, uid, runner)
    if ctx["platform"] != "darwin":
        return _unsupported(ctx["platform"])
    plist = plist_path(ctx["agents_dir"], run_id)
    data = build_plist(paths, run_id, command)
    problems = validate_plist(data, run_id)
    if problems:
        return {"status": "refused", "label": label(run_id), "reason": "; ".join(problems)}
    try:
        fsio.atomic_write_bytes(plist, data)
    except (OSError, ValueError) as error:
        return {"status": "failed", "label": label(run_id),
                "reason": "could not write the plist: %s" % type(error).__name__}
    # a stale job of the same label is unloaded first so bootstrap sees the new file
    _launchctl(ctx["runner"], "bootout", _target(ctx["uid"], run_id))
    code, error = _launchctl(ctx["runner"], "bootstrap", "gui/%d" % ctx["uid"], str(plist))
    if error is not None or code != 0:
        plist.unlink(missing_ok=True)
        return {"status": "failed", "label": label(run_id),
                "reason": "launchctl bootstrap %s" % (error or "exit %s" % code)}
    return {"status": "loaded", "label": label(run_id), "plist": str(plist)}


def uninstall(paths, run_id: str, *, runner: Optional[Callable] = None, agents_dir=None,
              platform: Optional[str] = None, uid: Optional[int] = None) -> Dict[str, Any]:
    """Unload the job and delete the plist; the plist is kept when the unload failed."""
    ctx = _context(platform, agents_dir, uid, runner)
    if ctx["platform"] != "darwin":
        return _unsupported(ctx["platform"])
    plist = plist_path(ctx["agents_dir"], run_id)
    if not plist.exists():
        return {"status": "not_installed", "label": label(run_id)}
    code, error = _launchctl(ctx["runner"], "bootout", _target(ctx["uid"], run_id))
    if error is not None or code not in (0, _NOT_LOADED):
        return {"status": "unload_failed", "label": label(run_id), "plist": str(plist),
                "reason": "launchctl bootout %s; the plist was kept" % (error or "exit %s" % code)}
    plist.unlink(missing_ok=True)
    return {"status": "uninstalled", "label": label(run_id), "plist": str(plist)}
