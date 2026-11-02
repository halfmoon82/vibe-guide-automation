"""Confirm that a run has no live worker session before its evidence is archived.

A run parked in ``blocked_unknown`` may carry a quarantined writer lease that
exists to stop a possibly live session from being dispatched twice.  Archiving
moves that lease away, so it is allowed only after every session the run could
own is shown to be retired.  Anything that cannot be shown is "unknown" and
blocks the archive.
"""

import json
import re
from typing import Any, Dict, List

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: ``claude agents`` states of a session that will not write again
RETIRED_STATES = frozenset({"stopped", "done", "failed"})
#: task-registry statuses of a bound task that will not write again; blocked_unknown is not one of them
_RETIRED_BINDINGS = frozenset({"accepted", "archived", "failed", "stopped", "blocked_design"})


class RunArchiveDeferred(ValueError):
    """The archive was not attempted: a live or unverifiable session may still write."""


def _bindings(snapshot) -> List[Dict[str, Any]]:
    tasks = getattr(snapshot, "tasks", None)
    if not isinstance(tasks, dict):
        return []
    return [item for key, item in tasks.items() if isinstance(item, dict) and not str(key).startswith("_")]


def _session_id(binding: Dict[str, Any]) -> str:
    for key in ("task_id", "threadId"):
        value = binding.get(key)
        if isinstance(value, str) and (_UUID.fullmatch(value) or key == "threadId"):
            return value
    return ""


def _launched_sessions(paths, run_id):
    """``(session ids, unknown reason)`` of sessions this run's own dispatch started.

    A background dispatch writes ``claude-bg-sessions/<handle>.json`` at launch,
    before the task registry binds the session.  A record belongs to this run
    when its claims carry this run id; the node and generation it names are
    what identify the session.  An unreadable record is "unknown", never
    "no session".
    """
    try:
        directory = paths.resolve_vibe_path("provider-actions/claude-bg-sessions")
    except (OSError, ValueError):
        return set(), "session launch records could not be located"
    if not directory.is_dir():
        return set(), None
    found = set()
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            claims = record["claims"]
            owner = claims.get("run_id")
            session_id = claims.get("session_id")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return set(), "session launch record %s is unreadable" % path.name[:60]
        if owner != run_id:
            continue
        if (not isinstance(session_id, str) or not _UUID.fullmatch(session_id)
                or not isinstance(claims.get("node_id"), str)
                or type(claims.get("generation")) is not int):
            return set(), "session launch record %s has no session identity" % path.name[:60]
        found.add(session_id)
    return found, None


def probe_live_sessions(paths, snapshot) -> Dict[str, Any]:
    """``{"live": [session ids], "unknown": reason or None}`` for the run's sessions."""
    if snapshot is None:
        return {"live": [], "unknown": "run snapshot is unreadable, so its sessions cannot be listed"}
    bindings = _bindings(snapshot)
    ids = {_session_id(item) for item in bindings} - {""}
    launched, unreadable = _launched_sessions(paths, getattr(snapshot, "run_id", None))
    if unreadable:
        return {"live": [], "unknown": unreadable}
    ids |= launched
    live: List[str] = []
    if launched or any(item.get("mode") == "background" for item in bindings):
        from .adapters.claude_bg import ClaudeBgUnknown, list_agent_sessions, load_claude_bg_config
        try:
            entries = list_agent_sessions(load_claude_bg_config(paths), paths.root)
        except (ClaudeBgUnknown, ValueError, OSError) as error:
            reason = getattr(error, "reason", None) or str(error)
            return {"live": [], "unknown": "claude agents could not be read: %s" % str(reason)[:160]}
        for entry in entries:
            if entry.get("state") in RETIRED_STATES:
                continue
            session_id = entry.get("sessionId")
            if session_id in ids:
                live.append(str(session_id or entry.get("id") or "?")[:8])
        return {"live": sorted(set(live)), "unknown": None}
    # Visible sessions cannot be listed from here: a bound one counts as live
    # until its registry status says it ended.
    for item in bindings:
        if _session_id(item) and item.get("status") not in _RETIRED_BINDINGS:
            live.append(_session_id(item)[:8])
    return {"live": sorted(set(live)), "unknown": None}
