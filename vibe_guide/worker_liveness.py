"""Silence of a running worker session, read from the session's transcript file.

vibe stores no per-session "last activity" time; the transcript's modification
time is the one uniform signal across Claude Code topologies.  This module only
measures; it never writes, never changes a node fingerprint, and reports an
unreadable or missing transcript as unknown -- never as healthy.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .paths import ProjectPaths

SILENCE_SECONDS = 15 * 60
#: Escalating to a person needs twice the silence threshold: a single long
#: command (a test run, a build) legitimately leaves the transcript untouched.
ESCALATE_SILENCE_SECONDS = 2 * SILENCE_SECONDS
#: Statuses in which a worker session is doing the node's work.
SESSION_STATUSES = frozenset({"running", "review", "rework"})

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HANDLE_SESSION_DIR = ("provider-actions", "claude-bg-sessions")


def _session_id(paths: ProjectPaths, node: Mapping[str, Any]) -> Optional[str]:
    """The Claude session id behind a node's active task, or None when unknown."""
    active = node.get("active_task")
    if not isinstance(active, Mapping):
        return None
    task_id = active.get("task_id")
    if isinstance(task_id, str) and _UUID.fullmatch(task_id):
        return task_id
    handle = active.get("handle_id")
    if not isinstance(handle, str) or not handle.startswith("claude-bg-"):
        return None
    directory = paths.vibe.joinpath(*_HANDLE_SESSION_DIR)
    try:
        for path in directory.glob("%s*.json" % handle):
            claims = json.loads(path.read_text(encoding="utf-8")).get("claims") or {}
            session = claims.get("session_id")
            if claims.get("handle_id") == handle and isinstance(session, str) and _UUID.fullmatch(session):
                return session
    except (OSError, ValueError, AttributeError):
        return None
    return None


def transcript_path(paths: ProjectPaths, node: Mapping[str, Any]) -> Optional[Path]:
    session = _session_id(paths, node)
    root = Path(os.path.expanduser("~")) / ".claude" / "projects"
    if session is None or not root.is_dir():
        return None
    for candidate in root.glob("*/%s.jsonl" % session):
        return candidate
    return None


def has_delivered(paths: ProjectPaths, run_id: str, node_id: str, node: Mapping[str, Any]) -> bool:
    """This generation's worker already reported: it waits on the supervisor, not silent."""
    active = node.get("active_task")
    if not isinstance(active, Mapping):
        return False
    role, generation = active.get("role"), active.get("generation")
    if role not in ("developer", "reviewer") or isinstance(generation, bool) or not isinstance(generation, int):
        return False
    path = paths.vibe / "provider-actions" / "deliveries" / "{}-{}-{}-g{}.json".format(
        run_id, node_id, role, generation)
    try:
        return path.is_file()
    except OSError:
        return False


def probe(paths: ProjectPaths, run_id: str, node_id: str, node: Mapping[str, Any],
          now: Optional[float] = None) -> Dict[str, Any]:
    """``{"state": "silent"|"active"|"unknown"|"unresolved", ...}`` for a node's worker.

    ``unknown``: the session is identified but its transcript is missing or
    unreadable -- said aloud, never healthy.  ``unresolved``: no session id can
    be derived (other providers, legacy ids), so nothing can be claimed either way.
    """
    if has_delivered(paths, run_id, node_id, node):
        return {"state": "active", "reason": "delivered"}
    session = _session_id(paths, node)
    if session is None:
        return {"state": "unresolved", "reason": "no session id"}
    path = transcript_path(paths, node)
    if path is None:
        return {"state": "unknown", "reason": "transcript not found", "session": session}
    try:
        age = (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return {"state": "unknown", "reason": "transcript unreadable", "session": session}
    state = "silent" if age >= SILENCE_SECONDS else "active"
    return {"state": state, "silent_minutes": int(age // 60), "transcript": str(path), "session": session}


def session_ended(paths: ProjectPaths, run_id: str, node_id: str, node: Mapping[str, Any],
                  now: Optional[float] = None) -> bool:
    """True only when the node's session has delivered and then gone silent.

    The registry keeps reading ``review`` after a reviewer session exits, so a
    stopped binding is not available as proof.  Fail closed: a missing or
    unreadable transcript, an undelivered report, or recent activity all mean
    the session may still be live.
    """
    if not has_delivered(paths, run_id, node_id, node):
        return False
    session = _session_id(paths, node)
    if session is None:
        return False
    path = transcript_path(paths, node)
    if path is None:
        return False
    try:
        age = (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return False
    return age >= SILENCE_SECONDS
