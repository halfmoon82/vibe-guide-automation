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


def _project_roots() -> list:
    """Directories that hold Claude Code's per-project transcript folders."""
    roots = []
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    if config:
        roots.append(Path(os.path.expanduser(config)) / "projects")
    home = Path(os.path.expanduser("~")) / ".claude" / "projects"
    if home not in roots:
        roots.append(home)
    return roots


def locate_transcript(session: str) -> Dict[str, Any]:
    """Look for ``<session>.jsonl`` under every project directory.

    ``{"path": Path|None, "searched": int, "unreadable": [str]}``.  A directory
    the host refuses to list lands in ``unreadable``; ``Path.glob`` would swallow
    that and make a denied directory look like a missing transcript.
    """
    searched, unreadable = 0, []
    for root in _project_roots():
        try:
            entries = list(os.scandir(root))
        except FileNotFoundError:
            continue
        except OSError:
            unreadable.append(str(root))
            continue
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            searched += 1
            candidate = Path(entry.path) / (session + ".jsonl")
            try:
                if candidate.is_file():
                    return {"path": candidate, "searched": searched, "unreadable": unreadable}
            except OSError:
                unreadable.append(entry.path)
    return {"path": None, "searched": searched, "unreadable": unreadable}


def transcript_path(paths: ProjectPaths, node: Mapping[str, Any]) -> Optional[Path]:
    session = _session_id(paths, node)
    if session is None:
        return None
    return locate_transcript(session)["path"]


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
    found = locate_transcript(session)
    path = found["path"]
    if path is None:
        if found["unreadable"]:
            return {"state": "unknown", "reason": "transcript dir unreadable", "category": "unreadable",
                    "session": session, "searched": found["searched"], "unreadable": found["unreadable"]}
        return {"state": "unknown", "reason": "transcript not found", "category": "not_found",
                "session": session, "searched": found["searched"]}
    try:
        age = (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return {"state": "unknown", "reason": "transcript unreadable", "category": "unreadable",
                "session": session, "searched": found["searched"], "unreadable": [str(path)]}
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


def liveness_signal(paths: ProjectPaths, run_id: str, node_id: str, node: Mapping[str, Any]) -> Optional[str]:
    """A token that changes while the node's worker is still producing output.

    It folds the newest delivery file for the node and the session transcript's
    modification time (whole seconds).  A worker that keeps submitting moves the
    token even when the supervisor has not written anything back yet, so the
    stall counter can tell "worker alive, supervisor late" from "nothing moves".
    ``None`` means no signal could be read; callers must not treat it as alive.
    """
    parts = []
    deliveries = paths.vibe / "provider-actions" / "deliveries"
    try:
        # exact node segment: node "dev" must not pick up "dev-api" deliveries
        exact = re.compile(r"{}-{}-(developer|reviewer)-g\d+\.json".format(re.escape(run_id), re.escape(node_id)))
        stamps = [path.stat().st_mtime_ns for path in deliveries.glob("{}-{}-*-g*.json".format(run_id, node_id))
                  if exact.fullmatch(path.name)]
    except OSError:
        stamps = []
    if stamps:
        parts.append("d%d" % max(stamps))
    try:
        path = transcript_path(paths, node)
        if path is not None:
            parts.append("t%d" % int(path.stat().st_mtime))
    except OSError:
        pass
    return "|".join(parts) if parts else None
