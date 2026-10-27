"""Stall (idle-loop) detection: per-beat node progress fingerprints.

Pure functions over a run snapshot's nodes/tasks and the event stream, plus an
atomic persisted progress record (``progress.json`` in the run directory).  A
node whose fingerprint stays unchanged for ``STALL_BEATS`` consecutive beats is
stalled; the escalation tier then depends on how many ``self_heal_attempt``
events were recorded since the fingerprint last changed.

Unreadable or corrupt prior records are "unknown": counting restarts from zero
and the result is never reported as "nothing to do".
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .paths import ProjectPaths
from .state import _atomic_bytes, run_dir

PROGRESS_FILE = "progress.json"
SCHEMA_VERSION = 1
STALL_BEATS = 3
ESCALATE_ATTEMPTS = 2
TERMINAL_STATUSES = frozenset({"accepted"})

TIER_AUTO_RETRY = "auto_retry"
TIER_INVESTIGATE = "supervisor_investigate"
TIER_ESCALATE = "escalate_to_pm"

# Volatile retryable_action fields (next_retry_at, attempt) are deliberately absent.
_DELIVERY_KEYS = ("handoff_file", "status_file")


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def node_fingerprint(
    node: Mapping[str, Any],
    tasks: Optional[Mapping[str, Any]] = None,
    node_id: Optional[str] = None,
) -> str:
    """Digest of a node's progress-bearing fields only.

    Snapshot ``tasks`` are keyed ``"<node_id>:<role>"``, so the binding of the
    active task can only be found when the caller supplies ``node_id``.
    """
    active = node.get("active_task")
    role = active.get("role") if isinstance(active, Mapping) else None
    evidence = node.get("evidence")
    latest_evidence = evidence[-1] if isinstance(evidence, list) and evidence else None
    action = node.get("retryable_action")
    observation = action.get("last_observation_ref") if isinstance(action, Mapping) else None
    task_state = None
    binding = tasks.get("{}:{}".format(node_id, role)) if tasks and node_id and role else None
    if isinstance(binding, Mapping):
        task_state = [binding.get("status"), binding.get("generation"), binding.get("cursor")]
    return _digest({
        "status": node.get("status"),
        "developer_generation": node.get("developer_generation"),
        "review_generation": node.get("review_generation"),
        "active_task": active,
        "task": task_state,
        "delivery": [node.get(key) for key in _DELIVERY_KEYS],
        "latest_evidence": latest_evidence,
        "observation": observation,
    })


def count_self_heal_attempts(events: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for event in events or ():
        if not isinstance(event, Mapping) or event.get("event") != "self_heal_attempt":
            continue
        data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
        node_id = data.get("node_id") or event.get("node_id") or event.get("node")
        if isinstance(node_id, str) and node_id:
            counts[node_id] = counts.get(node_id, 0) + 1
    return counts


def _valid_record(record: Any) -> bool:
    if not isinstance(record, dict) or not isinstance(record.get("nodes"), dict):
        return False
    for entry in record["nodes"].values():
        if not isinstance(entry, dict) or not isinstance(entry.get("fingerprint"), str):
            return False
        for key in ("unchanged_beats", "attempts_baseline"):
            value = entry.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return False
    return True


def _tier(unchanged: int, attempts_since: int) -> str:
    if unchanged < STALL_BEATS:
        return TIER_AUTO_RETRY
    if attempts_since >= ESCALATE_ATTEMPTS:
        return TIER_ESCALATE
    return TIER_INVESTIGATE


def assess_stalls(
    nodes: Mapping[str, Mapping[str, Any]],
    tasks: Optional[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
    previous: Any,
) -> Dict[str, Any]:
    """Return ``{"record": <new persisted record>, "nodes": {id: result}, "unknown_previous": bool}``.

    ``previous`` is the last persisted record (or anything unreadable, which is
    treated as unknown and counted from zero).
    """
    unknown = not _valid_record(previous)
    prior = {} if unknown else previous["nodes"]
    attempts = count_self_heal_attempts(events)
    record_nodes: Dict[str, Dict[str, Any]] = {}
    results: Dict[str, Dict[str, Any]] = {}
    for node_id, node in (nodes or {}).items():
        if not isinstance(node, Mapping) or node.get("status") in TERMINAL_STATUSES:
            continue
        fingerprint = node_fingerprint(node, tasks, node_id)
        total = attempts.get(node_id, 0)
        old = prior.get(node_id)
        if old and old["fingerprint"] == fingerprint:
            unchanged = old["unchanged_beats"] + 1
            baseline = min(old["attempts_baseline"], total)
        else:
            unchanged, baseline = 1, total
        since = total - baseline
        record_nodes[node_id] = {
            "fingerprint": fingerprint,
            "unchanged_beats": unchanged,
            "attempts_baseline": baseline,
        }
        results[node_id] = {
            "fingerprint": fingerprint,
            "unchanged_beats": unchanged,
            "attempts_since_change": since,
            "stalled": unchanged >= STALL_BEATS,
            "tier": _tier(unchanged, since),
            "unknown_previous": unknown,
        }
    return {
        "record": {"schema_version": SCHEMA_VERSION, "nodes": record_nodes},
        "nodes": results,
        "unknown_previous": unknown,
    }


def progress_path(paths: ProjectPaths, run_id: str) -> Path:
    return run_dir(paths, run_id) / PROGRESS_FILE


def load_progress(paths: ProjectPaths, run_id: str) -> Optional[Dict[str, Any]]:
    """Persisted record, or None when missing/unreadable (both mean unknown)."""
    try:
        record = json.loads(progress_path(paths, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if _valid_record(record) else None


def save_progress(paths: ProjectPaths, run_id: str, record: Mapping[str, Any]) -> None:
    data = json.dumps(record, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8")
    _atomic_bytes(run_dir(paths, run_id, create=True) / PROGRESS_FILE, data + b"\n")


def record_beat(
    paths: ProjectPaths,
    run_id: str,
    nodes: Mapping[str, Mapping[str, Any]],
    tasks: Optional[Mapping[str, Any]],
    events: List[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Assess one beat against the persisted record and persist the new one."""
    outcome = assess_stalls(nodes, tasks, events, load_progress(paths, run_id))
    save_progress(paths, run_id, outcome["record"])
    return outcome
