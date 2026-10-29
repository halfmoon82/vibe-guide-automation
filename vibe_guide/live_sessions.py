"""Confirm that a run has no live worker session before its evidence is archived.

A run parked in ``blocked_unknown`` may carry a quarantined writer lease that
exists to stop a possibly live session from being dispatched twice.  Archiving
moves that lease away, so it is allowed only after every session the run could
own is shown to be retired.  Anything that cannot be shown is "unknown" and
blocks the archive.
"""

import re
from pathlib import Path
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


def _node_worktrees(paths, snapshot) -> List[Path]:
    trees = []
    for node in (getattr(snapshot, "nodes", None) or {}).values():
        worktree = node.get("worktree") if isinstance(node, dict) else None
        if isinstance(worktree, str) and worktree:
            path = Path(worktree)
            trees.append((path if path.is_absolute() else paths.root / path).resolve())
    return trees


def probe_live_sessions(paths, snapshot) -> Dict[str, Any]:
    """``{"live": [session ids], "unknown": reason or None}`` for the run's sessions."""
    if snapshot is None:
        return {"live": [], "unknown": "run snapshot is unreadable, so its sessions cannot be listed"}
    bindings = _bindings(snapshot)
    ids = {_session_id(item) for item in bindings} - {""}
    live: List[str] = []
    if any(item.get("mode") == "background" for item in bindings):
        from .adapters.claude_bg import ClaudeBgUnknown, list_agent_sessions, load_claude_bg_config
        try:
            entries = list_agent_sessions(load_claude_bg_config(paths), paths.root)
        except (ClaudeBgUnknown, ValueError, OSError) as error:
            reason = getattr(error, "reason", None) or str(error)
            return {"live": [], "unknown": "claude agents could not be read: %s" % str(reason)[:160]}
        trees = _node_worktrees(paths, snapshot)
        for entry in entries:
            if entry.get("state") in RETIRED_STATES:
                continue
            session_id = entry.get("sessionId")
            cwd = entry.get("cwd")
            try:
                inside = isinstance(cwd, str) and any(
                    Path(cwd).resolve() == tree or tree in Path(cwd).resolve().parents for tree in trees)
            except OSError:
                inside = False
            if session_id in ids or inside:
                live.append(str(session_id or entry.get("id") or "?")[:8])
        return {"live": sorted(set(live)), "unknown": None}
    # Visible sessions cannot be listed from here: a bound one counts as live
    # until its registry status says it ended.
    for item in bindings:
        if _session_id(item) and item.get("status") not in _RETIRED_BINDINGS:
            live.append(_session_id(item)[:8])
    return {"live": sorted(set(live)), "unknown": None}
