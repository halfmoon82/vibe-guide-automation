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
    """A worker that already reported is waiting on the supervisor, not silent."""
    active = node.get("active_task")
    role = active.get("role") if isinstance(active, Mapping) else None
    if role not in ("developer", "reviewer"):
        return False
    directory = paths.vibe / "provider-actions" / "deliveries"
    try:
        return any(directory.glob("%s-%s-%s-g*.json" % (run_id, node_id, role)))
    except OSError:
        return False


def probe(paths: ProjectPaths, run_id: str, node_id: str, node: Mapping[str, Any],
          now: Optional[float] = None) -> Dict[str, Any]:
    """``{"state": "silent"|"active"|"unknown", ...}`` for a running node's worker."""
    if has_delivered(paths, run_id, node_id, node):
        return {"state": "active", "reason": "delivered"}
    path = transcript_path(paths, node)
    if path is None:
        return {"state": "unknown", "reason": "transcript not found"}
    try:
        age = (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return {"state": "unknown", "reason": "transcript unreadable"}
    state = "silent" if age >= SILENCE_SECONDS else "active"
    return {"state": state, "silent_minutes": int(age // 60), "transcript": str(path)}
