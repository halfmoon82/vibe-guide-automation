"""Reader for ``pm-inbox.jsonl``, the file the daemon's ``PmNotifier`` appends to.

The message format is the writer's and is not changed here.  Reading keeps a
byte-offset cursor (``pm-inbox.cursor.json``) so a message is handed over once.

Failure behaviour, all of it toward "never lose a message":

* a cursor that is unreadable, past the end of the file, or not on a line
  boundary (the inbox was replaced or rewritten) restarts from byte 0;
* a cursor that cannot be written leaves the messages unread and the result
  ``cursor_status == "unknown"``; the caller must not treat them as delivered;
* a line that does not parse is skipped and counted;
* bytes after the last newline are a half-written line and are not read.

``pm-push-receipts.jsonl`` is written by the notifier, one line per push
attempt, and says what became of each message (sent, queued, failed).
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from . import fsio
from .state import interprocess_lock, run_dir

INBOX_FILE = "pm-inbox.jsonl"
CURSOR_FILE = "pm-inbox.cursor.json"
RECEIPT_FILE = "pm-push-receipts.jsonl"
_LOCK_FILE = ".pm-inbox.lock"

#: Kinds the product manager does not need to act on; every other kind, a new
#: one included, is surfaced.
INFORMATIONAL_KINDS = frozenset({"run_complete"})


def needs_pm(message: Dict[str, Any]) -> bool:
    return message.get("kind") not in INFORMATIONAL_KINDS


def _read_cursor(path: Path) -> int:
    try:
        value = fsio.read_json(path)
    except (fsio.JsonMissingError, fsio.JsonCorruptError, OSError, ValueError):
        return 0
    offset = value.get("offset") if isinstance(value, dict) else None
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return 0
    return offset


def read_unread(paths, run_id: str, *, peek: bool = False) -> Dict[str, Any]:
    """Unread messages, and the cursor moved past them unless ``peek``.

    ``cursor_status`` is ``ok`` (advanced, or peeking), ``unknown`` (the cursor
    could not be written: the messages stay unread) or ``reset`` (the old cursor
    was invalid and the file was read from the start; also advanced).
    """
    directory = run_dir(paths, run_id)
    inbox = directory / INBOX_FILE
    cursor_path = directory / CURSOR_FILE
    result: Dict[str, Any] = {"run_id": run_id, "messages": [], "skipped": 0,
                              "cursor_status": "ok", "peek": peek}
    try:
        with interprocess_lock(directory / _LOCK_FILE):
            return _read_locked(inbox, cursor_path, result, peek)
    except (OSError, ValueError, TimeoutError) as error:
        return {**result, "messages": [], "cursor_status": "unknown",
                "reason": "inbox unreadable: %s" % type(error).__name__}


def _read_locked(inbox: Path, cursor_path: Path, result: Dict[str, Any], peek: bool) -> Dict[str, Any]:
    try:
        data = inbox.read_bytes()
    except FileNotFoundError:
        return result
    offset = _read_cursor(cursor_path)
    if offset > len(data) or (offset > 0 and data[offset - 1:offset] != b"\n"):
        offset = 0
        result["cursor_status"] = "reset"
    end = data.rfind(b"\n") + 1  # past the last complete line; 0 when there is none
    messages: List[Dict[str, Any]] = []
    position = offset
    while position < end:
        newline = data.index(b"\n", position)
        line = data[position:newline]
        start, position = position, newline + 1
        if not line.strip():
            continue
        try:
            entry = json.loads(line.decode("utf-8"))
        except ValueError:
            entry = None
        if not isinstance(entry, dict):
            result["skipped"] += 1
            continue
        messages.append({**entry, "offset": start})
    result["messages"] = messages
    if peek or (end <= offset and result["cursor_status"] != "reset"):
        return result
    if _save_cursor(cursor_path, end, result) == "unknown":
        result["cursor_status"] = "unknown"
    return result


def _save_cursor(path: Path, offset: int, result: Dict[str, Any]) -> str:
    try:
        fsio.atomic_write_json(path, {"offset": offset})
    except (OSError, ValueError) as error:
        result["reason"] = "cursor write failed: %s" % type(error).__name__
        return "unknown"
    return "ok"


def delivered(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The messages a caller may treat as handed over: none when the cursor is unknown."""
    if result.get("peek") or result.get("cursor_status") == "unknown":
        return []
    return list(result.get("messages") or [])


def write_receipt(paths, run_id: str, record: Dict[str, Any]) -> bool:
    """Append one push receipt; a failure is returned, never raised."""
    try:
        path = run_dir(paths, run_id, create=True) / RECEIPT_FILE
        with open(str(path), "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except (OSError, ValueError):
        return False
    return True
