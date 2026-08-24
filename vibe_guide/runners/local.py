"""Bounded local subprocess runner for explicitly confirmed adapter commands."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import uuid

from ..authorization import validate_runtime_contract
from ..contracts import RunEvent, RunHandle, Runner


_OUTPUT_LIMIT = 64 * 1024
_EVENT_LIMIT = 64
_SUPPORTED_EVENTS = {
    "accepted", "complete", "delivered", "failed", "rework", "stopped",
    "timeout", "unknown",
}


@dataclass
class _ProcessRecord:
    process: Any
    worktree: Path
    adapter_id: str
    provenance: Dict[str, Any]
    buffers: Dict[str, bytearray] = field(
        default_factory=lambda: {"out": bytearray(), "err": bytearray()}
    )
    truncated: bool = False
    readers: List[threading.Thread] = field(default_factory=list)
    emitted: bool = False
    stopped: bool = False


class LocalRunner(Runner):
    """Run only commands that exactly match a caller-supplied adapter contract."""

    def __init__(self, confirmed_commands: Mapping[str, Sequence[str]]):
        if not isinstance(confirmed_commands, Mapping) or not confirmed_commands:
            raise ValueError("at least one confirmed adapter command is required")
        self._confirmed: Dict[str, Tuple[str, ...]] = {}
        for adapter_id, command in confirmed_commands.items():
            self._confirmed[self._adapter_id(adapter_id)] = self._command(command)
        self._records: Dict[str, _ProcessRecord] = {}
        self.start_contracts: List[Dict[str, Any]] = []

    @property
    def start_count(self) -> int:
        return len(self.start_contracts)

    def start(self, contract: dict, worktree: Path) -> RunHandle:
        validate_runtime_contract(contract)
        adapter_id = self._adapter_id(contract.get("adapter_id"))
        command = self._command(contract.get("command"))
        if self._confirmed.get(adapter_id) != command:
            raise PermissionError("command is not the exact confirmed adapter command")
        worktree = Path(worktree).resolve()
        if not worktree.is_dir():
            raise ValueError("runner worktree must be an existing directory")

        handle = RunHandle("local-" + uuid.uuid4().hex)
        environment = os.environ.copy()
        environment.update({
            "VIBE_NODE_ID": str(contract.get("node_id", "")),
            "VIBE_TASK_ROLE": str(contract.get("role", "")),
            "VIBE_TASK_PHASE": str(contract.get("phase", "")),
        })
        process = subprocess.Popen(
            list(command), cwd=str(worktree), env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        record = _ProcessRecord(
            process=process,
            worktree=worktree,
            adapter_id=adapter_id,
            provenance={
                "node_id": contract.get("node_id"),
                "role": contract.get("role"),
                "task_id": contract.get("task_id"),
                "generation": contract.get("generation"),
                "handle_id": handle.run_id,
            },
        )
        self._records[handle.run_id] = record
        for stream_name, stream in (("out", process.stdout), ("err", process.stderr)):
            reader = threading.Thread(
                target=self._drain, args=(record, stream_name, stream), daemon=True
            )
            record.readers.append(reader)
            reader.start()
        self.start_contracts.append({
            "adapter_id": adapter_id,
            "node_id": contract.get("node_id"),
            "role": contract.get("role"),
            "phase": contract.get("phase"),
            "task_id": contract.get("task_id"),
            "generation": contract.get("generation"),
        })
        self._write_metadata(handle.run_id, record, "running", None, 0)
        return handle

    def poll(self, handle: RunHandle) -> List[RunEvent]:
        record = self._record(handle)
        if record.emitted:
            return []
        exit_status = record.process.poll()
        if exit_status is None:
            return []
        for reader in record.readers:
            reader.join(timeout=1)
        events = self._events(handle.run_id, record, int(exit_status))
        record.emitted = True
        self._write_metadata(
            handle.run_id, record, "stopped" if record.stopped else "exited",
            int(exit_status), len(events),
        )
        return events

    def stop(self, handle: RunHandle) -> None:
        record = self._record(handle)
        if record.process.poll() is None:
            record.process.terminate()
            try:
                record.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                record.process.kill()
                record.process.wait(timeout=1)
        record.stopped = True

    @staticmethod
    def _adapter_id(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("adapter_id is required")
        normalized = value.strip()
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
        if any(character not in allowed for character in normalized):
            raise ValueError("adapter_id must be a simple identifier")
        return normalized

    @staticmethod
    def _command(value: Any) -> Tuple[str, ...]:
        if not isinstance(value, (list, tuple)) or not value or len(value) > 64:
            raise ValueError("adapter command must be a non-empty bounded list")
        if any(not isinstance(item, str) or not item or "\x00" in item for item in value):
            raise ValueError("adapter command contains an invalid argument")
        return tuple(value)

    def _record(self, handle: RunHandle) -> _ProcessRecord:
        if not isinstance(handle, RunHandle) or handle.run_id not in self._records:
            raise ValueError("unknown local runner handle")
        return self._records[handle.run_id]

    @staticmethod
    def _drain(record: _ProcessRecord, stream_name: str, stream: Any) -> None:
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                buffer = record.buffers[stream_name]
                remaining = _OUTPUT_LIMIT - len(buffer)
                if remaining > 0:
                    buffer.extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0):
                    record.truncated = True
        finally:
            stream.close()

    def _events(
        self, handle_id: str, record: _ProcessRecord, exit_status: int
    ) -> List[RunEvent]:
        if record.stopped:
            return [self._event("stopped", record, "local process was stopped")]
        if record.truncated:
            return [self._event("unknown", record, "provider output exceeded the bound")]
        if exit_status != 0:
            return [self._event("failed", record, "local process exited unsuccessfully")]
        try:
            output = bytes(record.buffers["out"]).decode("utf-8")
        except UnicodeDecodeError:
            return [self._event("unknown", record, "provider output was not UTF-8")]
        lines = [line for line in output.splitlines() if line.strip()]
        if not lines or len(lines) > _EVENT_LIMIT:
            return [self._event("unknown", record, "provider returned no bounded structured event")]

        result = []
        for index, line in enumerate(lines, start=1):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                return [self._event("unknown", record, "provider event was not valid JSON")]
            if not isinstance(raw, dict) or set(raw) != {"event", "data"}:
                return [self._event("unknown", record, "provider event schema was invalid")]
            event_name, data = raw.get("event"), raw.get("data")
            if event_name not in _SUPPORTED_EVENTS or not isinstance(data, dict):
                return [self._event("unknown", record, "provider event was unsupported")]
            safe = dict(record.provenance)
            if isinstance(data.get("in_contract"), bool):
                safe["in_contract"] = data["in_contract"]
            if "finding" in data:
                safe["finding"] = "local-runner:{}:event-{}".format(handle_id, index)
            elif "evidence" in data:
                safe["evidence"] = "local-runner:{}:event-{}".format(handle_id, index)
            if event_name in {"unknown", "timeout", "failed", "stopped"}:
                safe["reason"] = "provider reported " + event_name
            result.append(RunEvent(event_name, safe))
        return result

    @staticmethod
    def _event(event: str, record: _ProcessRecord, reason: str) -> RunEvent:
        data = dict(record.provenance)
        data["reason"] = reason
        return RunEvent(event, data)

    @staticmethod
    def _metadata_path(record: _ProcessRecord, handle_id: str) -> Path:
        vibe = record.worktree / ".vibe"
        if vibe.is_symlink():
            raise ValueError("runner metadata directory may not be a symlink")
        directory = vibe / "local-runner"
        if directory.is_symlink():
            raise ValueError("runner metadata directory may not be a symlink")
        directory.mkdir(parents=True, exist_ok=True)
        return directory / (handle_id + ".json")

    def _write_metadata(
        self, handle_id: str, record: _ProcessRecord, status: str,
        exit_status: Optional[int], event_count: int,
    ) -> None:
        path = self._metadata_path(record, handle_id)
        payload = {
            "schema_version": 1,
            "handle_id": handle_id,
            "pid": record.process.pid,
            "status": status,
            "exit_status": exit_status,
            "adapter_id": record.adapter_id,
            "command_name": Path(self._confirmed[record.adapter_id][0]).name,
            "provenance_ref": "confirmed-command:" + record.adapter_id,
            "event_count": event_count,
            "output_truncated": record.truncated,
        }
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
