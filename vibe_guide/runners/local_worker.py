"""Private bounded worker used by LocalRunner; invoked as a module."""

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from typing import Any, Dict, List


_OUTPUT_LIMIT = 64 * 1024
_EVENT_LIMIT = 64
_SUPPORTED_EVENTS = {
    "accepted",
    "complete",
    "delivered",
    "failed",
    "review_finding",
    "rework",
    "stopped",
    "timeout",
    "unknown",
}


def _decode(name: str):
    value = os.environ.get(name)
    if not value:
        raise ValueError("missing local worker input")
    return json.loads(base64.b64decode(value.encode("ascii")).decode("utf-8"))


def _atomic(path: Path, payload: Dict[str, Any]) -> None:
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("local worker result path may not be a symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + path.name + ".", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _capture(stream: Any, buffer: bytearray, state: Dict[str, bool]) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            remaining = _OUTPUT_LIMIT - len(buffer)
            if remaining > 0:
                buffer.extend(chunk[:remaining])
            if len(chunk) > max(remaining, 0):
                state["truncated"] = True
    finally:
        stream.close()


def _safe_event(
    event_name: str,
    provenance: Dict[str, Any],
    handle_id: str,
    index: int,
    data: Dict[str, Any],
) -> Dict[str, Any]:
    safe = dict(provenance)
    if isinstance(data.get("in_contract"), bool):
        safe["in_contract"] = data["in_contract"]
    if "finding" in data:
        safe["finding"] = "local-runner:{}:event-{}".format(handle_id, index)
    elif "evidence" in data:
        safe["evidence"] = "local-runner:{}:event-{}".format(handle_id, index)
    if isinstance(data.get("consistency"), dict):
        safe["consistency"] = data["consistency"]
    if event_name in {"unknown", "timeout", "failed", "stopped"}:
        safe["reason"] = "provider reported " + event_name
    return {"event": event_name, "data": safe}


def _events(
    output: bytes,
    exit_status: int,
    truncated: bool,
    provenance: Dict[str, Any],
) -> List[Dict[str, Any]]:
    handle_id = str(provenance["handle_id"])
    if truncated:
        return [
            _safe_event(
                "unknown", provenance, handle_id, 1, {"reason": "truncated"}
            )
        ]
    if exit_status != 0:
        return [
            _safe_event("failed", provenance, handle_id, 1, {"reason": "exit"})
        ]
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError:
        return [
            _safe_event("unknown", provenance, handle_id, 1, {"reason": "utf8"})
        ]
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or len(lines) > _EVENT_LIMIT:
        return [
            _safe_event("unknown", provenance, handle_id, 1, {"reason": "count"})
        ]
    result = []
    for index, line in enumerate(lines, start=1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return [
                _safe_event(
                    "unknown", provenance, handle_id, index, {"reason": "json"}
                )
            ]
        if (
            not isinstance(raw, dict)
            or set(raw) != {"event", "data"}
            or raw.get("event") not in _SUPPORTED_EVENTS
            or not isinstance(raw.get("data"), dict)
        ):
            return [
                _safe_event(
                    "unknown", provenance, handle_id, index, {"reason": "schema"}
                )
            ]
        result.append(
            _safe_event(
                raw["event"], provenance, handle_id, index, raw["data"]
            )
        )
    return result


def main() -> int:
    command = _decode("VIBE_LOCAL_COMMAND")
    provenance = _decode("VIBE_LOCAL_PROVENANCE")
    result_path = Path(os.environ["VIBE_LOCAL_RESULT"])
    owner_digest = os.environ["VIBE_LOCAL_OWNER_DIGEST"]
    worktree = Path(os.environ["VIBE_LOCAL_WORKTREE"])
    process = subprocess.Popen(
        command,
        cwd=str(worktree),
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    buffers = {"out": bytearray(), "err": bytearray()}
    state = {"truncated": False}
    threads = []
    for name, stream in (("out", process.stdout), ("err", process.stderr)):
        thread = threading.Thread(
            target=_capture,
            args=(stream, buffers[name], state),
            daemon=True,
        )
        threads.append(thread)
        thread.start()
    exit_status = process.wait()
    for thread in threads:
        thread.join(timeout=2)
    events = _events(
        bytes(buffers["out"]),
        int(exit_status),
        state["truncated"],
        provenance,
    )
    _atomic(
        result_path,
        {
            "schema_version": 1,
            "handle_id": provenance["handle_id"],
            "owner_digest": owner_digest,
            "exit_status": int(exit_status),
            "output_truncated": state["truncated"],
            "events": events,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
