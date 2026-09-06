"""Canonical task lifecycle and the single legacy-record migration boundary."""

from typing import Any, Mapping, Dict


class CanonicalTaskStatus:
    CREATED = "CREATED"
    SETUP_PENDING = "SETUP_PENDING"
    RUNNING = "RUNNING"
    DELIVERED = "DELIVERED"
    REVIEW = "REVIEW"
    REWORK = "REWORK"
    ACCEPTED = "ACCEPTED"
    ARCHIVED = "ARCHIVED"
    BLOCKED_UNKNOWN = "BLOCKED_UNKNOWN"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


_ALIASES = {
    "created": CanonicalTaskStatus.CREATED,
    "start_pending": CanonicalTaskStatus.SETUP_PENDING,
    "setup_pending": CanonicalTaskStatus.SETUP_PENDING,
    "running": CanonicalTaskStatus.RUNNING,
    "delivered": CanonicalTaskStatus.DELIVERED,
    "delivery_complete": CanonicalTaskStatus.DELIVERED,
    "review": CanonicalTaskStatus.REVIEW,
    "rework": CanonicalTaskStatus.REWORK,
    "accepted": CanonicalTaskStatus.ACCEPTED,
    "archived": CanonicalTaskStatus.ARCHIVED,
    "blocked_unknown": CanonicalTaskStatus.BLOCKED_UNKNOWN,
    "failed": CanonicalTaskStatus.FAILED,
    "stopped": CanonicalTaskStatus.STOPPED,
}
_CANONICAL_FIELDS = {
    "provider", "mode", "issue_id", "role", "task_id", "platform_task_id",
    "threadId", "thread_id", "host", "hostId", "host_id", "clientThreadId",
    "client_thread_id", "worktree", "branch", "cursor", "generation", "status",
    "visible", "run_id", "limitations", "allowlist", "successor_of",
}


def normalize_task_status(value: str) -> str:
    """Return one stable lifecycle value; unknown input is fail-closed."""
    if not isinstance(value, str):
        return CanonicalTaskStatus.BLOCKED_UNKNOWN
    key = value.strip().casefold()
    if key in {item.casefold() for item in _ALIASES.values()}:
        return value.strip().upper()
    return _ALIASES.get(key, CanonicalTaskStatus.BLOCKED_UNKNOWN)


def migrate_task_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Translate a legacy task payload once, retaining non-canonical evidence."""
    if not isinstance(record, Mapping):
        raise TypeError("task record must be a mapping")
    source = dict(record)
    result = dict(source)
    legacy: Dict[str, Any] = dict(source.get("legacy") or {}) if isinstance(source.get("legacy"), Mapping) else {}

    raw_status = source.get("status")
    client_id = source.get("clientThreadId") or source.get("client_thread_id")
    thread_id = source.get("threadId") or source.get("thread_id") or source.get("task_id")
    if client_id and not thread_id:
        result["status"] = CanonicalTaskStatus.SETUP_PENDING
        result["threadId"] = None
    else:
        result["status"] = normalize_task_status(raw_status or CanonicalTaskStatus.CREATED)
        result["threadId"] = thread_id
    result["clientThreadId"] = client_id
    result["task_id"] = thread_id
    result["platform_task_id"] = thread_id
    result["generation"] = source.get("generation", 0)
    result["legacy"] = legacy
    for key, value in source.items():
        if key not in _CANONICAL_FIELDS and key != "legacy":
            legacy.setdefault(key, value)
    if raw_status is not None and normalize_task_status(raw_status) == CanonicalTaskStatus.BLOCKED_UNKNOWN:
        legacy.setdefault("status", raw_status)
    return result
