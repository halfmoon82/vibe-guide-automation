"""Heal pack: what a stuck provider receipt should look like versus what arrived.

The pack is built from the reader's own definitions
(``runners.provider_action`` constants and ``*_differences`` functions), so the
expected shape it reports is the one the reader enforces.  It carries only
program constants, field names and type names -- never receipt values -- which
is also why persistence redaction has nothing to erase from it.
"""

import re
from typing import Any, Dict, List, Optional, Sequence

from .adapters.task_provider import ProviderActionStore
from .paths import ProjectPaths
from .runners.provider_action import (
    RESUME_RECEIPT_FIELD,
    WAIT_CURSOR_MAX_LENGTH,
    resume_receipt_differences,
    wait_cursor_differences,
)

PACK_SCHEMA = 1
UNRECOGNIZED_NOTE = "未识别具体格式"

_CHECKS = {
    "resume": resume_receipt_differences,
    "wait": wait_cursor_differences,
}
_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_EVENT_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,63}$")
_MAX_ACTUAL_FIELDS = 32
_MAX_EVENT_TYPES = 20
_UNNAMEABLE = "<unnameable>"

REPAIR_ENTRIES = (
    "edit only the `payload` object of the result file named in `files`; keep "
    "schema_version, action_id and request_digest unchanged",
    "then call vibe_guide.heal_pack.recheck_heal_pack with this pack; the same "
    "reader validation decides, not the edit",
)


def _expected(operation: str) -> List[Dict[str, str]]:
    if operation == "resume":
        return [{"field": RESUME_RECEIPT_FIELD, "type": "bool", "requirement": "true"}]
    return [
        {
            "field": "cursor",
            "type": "str",
            "requirement": "optional; non-empty, at most %d characters, no NUL"
            % WAIT_CURSOR_MAX_LENGTH,
        },
    ]


def _label(value: Any) -> str:
    return value if isinstance(value, str) and _FIELD_NAME.fullmatch(value) else ""


def _actual(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    fields = []
    for name in list(payload)[:_MAX_ACTUAL_FIELDS]:
        fields.append({"field": _label(name) or _UNNAMEABLE, "type": type(payload[name]).__name__})
    return fields


def _event_types(recent_events: Optional[Sequence[Any]]) -> List[str]:
    kept = [
        item for item in (recent_events or ())
        if isinstance(item, str) and _EVENT_TYPE.fullmatch(item)
    ]
    return kept[-_MAX_EVENT_TYPES:]


def _relative(action_id: str, folder: str) -> str:
    return ".vibe/provider-actions/%s/%s.json" % (folder, action_id)


def _order(action: Dict[str, Any]):
    return tuple(
        value if isinstance(value, int) and not isinstance(value, bool) else 0
        for value in (action.get("generation"), action.get("sequence"))
    )


def _latest_result_bearing_action(
    store: ProviderActionStore, run_id: str, node_id: str, role: str
) -> Optional[Dict[str, Any]]:
    latest = None
    for path in sorted(store._directory("requests").glob("action-*.json")):
        try:
            action = store.action(path.stem)
        except (OSError, ValueError):
            continue
        if (
            action.get("run_id") != run_id
            or action.get("issue_id") != node_id
            or action.get("role") != role
            or action.get("operation") not in _CHECKS
            or not (store._directory("results") / (path.stem + ".json")).is_file()
        ):
            continue
        if latest is None or _order(action) > _order(latest):
            latest = action
    return latest


def _stored_payload(store: ProviderActionStore, action_id: str):
    """(payload, problem): the bound result payload, or why it cannot be read."""
    try:
        payload = store.result(action_id)
    except (OSError, ValueError):
        return None, "result record is not a readable record bound to its request"
    if payload is None:
        return None, "no result file"
    return payload, None


def build_heal_pack(
    paths: ProjectPaths,
    run_id: str,
    node_id: str,
    role: str,
    *,
    node_status: str = "",
    blocker: str = "",
    recent_events: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """Structured diagnosis for a node whose provider receipt the reader refused.

    Falls back to a generic pack (``recognized`` false) when no refused receipt
    is found; the generic pack never invents an expected format.
    """
    store = ProviderActionStore(paths)
    pack: Dict[str, Any] = {
        "schema": PACK_SCHEMA,
        "recognized": False,
        "run_id": run_id,
        "node_id": node_id,
        "role": role,
        "node_status": _label(node_status),
        "blocker_class": _label(blocker),
        "recent_event_types": _event_types(recent_events),
        "files": [],
        "expected": [],
        "actual": [],
        "differences": [],
        "repair_entries": [],
        "note": UNRECOGNIZED_NOTE,
    }
    action = _latest_result_bearing_action(store, run_id, node_id, role)
    if action is None:
        return pack
    action_id = action["action_id"]
    operation = action["operation"]
    payload, problem = _stored_payload(store, action_id)
    pack["files"] = [
        _relative(action_id, "requests"),
        _relative(action_id, "results"),
    ]
    if problem is not None:
        pack["differences"] = [problem]
        pack["repair_entries"] = list(REPAIR_ENTRIES)
        pack["action_id"] = action_id
        pack["operation"] = operation
        pack["recognized"] = True
        pack["note"] = "result file cannot be read as a bound record"
        return pack
    differences = _CHECKS[operation](payload)
    if not differences:
        return pack
    pack.update({
        "recognized": True,
        "action_id": action_id,
        "operation": operation,
        "expected": _expected(operation),
        "actual": _actual(payload),
        "differences": differences,
        "repair_entries": list(REPAIR_ENTRIES),
        "note": "reader refuses this receipt",
    })
    return pack


def recheck_heal_pack(paths: ProjectPaths, pack: Dict[str, Any]) -> Dict[str, Any]:
    """Re-run the reader's validation on the (possibly edited) result file."""
    action_id = pack.get("action_id") if isinstance(pack, dict) else None
    if not isinstance(pack, dict) or not pack.get("recognized") or not isinstance(action_id, str):
        return {"passed": False, "differences": [UNRECOGNIZED_NOTE + ": nothing to re-check"]}
    store = ProviderActionStore(paths)
    try:
        operation = store.action(action_id).get("operation")
    except (OSError, ValueError):
        return {"passed": False, "differences": ["request for the pack's action is unreadable"]}
    if operation not in _CHECKS:
        return {"passed": False, "differences": [UNRECOGNIZED_NOTE + ": nothing to re-check"]}
    payload, problem = _stored_payload(store, action_id)
    if problem is not None:
        return {"passed": False, "differences": [problem]}
    differences = _CHECKS[operation](payload)
    return {"passed": not differences, "differences": differences}
