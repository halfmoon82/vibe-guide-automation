"""Versioned, recoverable state, event, lock, and writer-lease persistence."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Dict, Iterator, List, Optional
import uuid

from .authorization import AuthorizationRecord, is_authorization_integrity_valid
from .contracts import RunEvent
from .paths import ProjectPaths


STATE_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1
LEASE_SCHEMA_VERSION = 1
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RUN_STATUSES = {
    "initialized",
    "running",
    "complete",
    "blocked_unknown",
    "blocked_design",
    "failed",
}
_NODE_STATUSES = {
    "pending",
    "start_pending",
    "running",
    "delivered",
    "review",
    "rework",
    "accepted",
    "blocked_unknown",
    "blocked_design",
    "failed",
    "stopped",
}
_PROVENANCE_KEYS = {
    "role",
    "task_id",
    "handle_id",
    "generation",
    "authorization_digest",
    "node_contract_digest",
}


@dataclass
class RunSnapshot:
    run_id: str
    plan_id: str
    plan_version: int
    status: str
    nodes: Dict[str, Dict[str, Any]]
    handles: Dict[str, str]
    tasks: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    authorization: Dict[str, Any] = field(default_factory=dict)
    authorization_digest: str = ""
    node_contract_digest: str = ""
    event_sequence: int = 0
    schema_version: int = STATE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunSnapshot":
        expected = {
            "schema_version",
            "run_id",
            "plan_id",
            "plan_version",
            "status",
            "nodes",
            "handles",
            "tasks",
            "authorization",
            "authorization_digest",
            "node_contract_digest",
            "event_sequence",
        }
        if not isinstance(data, dict) or set(data) != expected:
            raise ValueError("snapshot schema is invalid")
        normalized = dict(data)
        normalized["authorization"] = AuthorizationRecord.from_dict(
            normalized["authorization"]
        ).to_dict()
        return cls(**normalized)


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("run id must be a simple identifier")
    return run_id


def _safe_project_path(paths: ProjectPaths, *parts: str) -> Path:
    root = paths.root.resolve()
    candidate = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("persistence path may not traverse a symlink")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("persistence path escapes the project root") from error
    return candidate


def run_dir(paths: ProjectPaths, run_id: str, create: bool = False) -> Path:
    validate_run_id(run_id)
    directory = _safe_project_path(paths, ".vibe", "runs", run_id)
    if create:
        directory.mkdir(parents=True, exist_ok=True)
        # Revalidate after mkdir to catch a concurrently inserted symlink.
        directory = _safe_project_path(paths, ".vibe", "runs", run_id)
    return directory


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("atomic write parent may not be a symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + path.name + ".", dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary_path), str(path))
        try:
            parent_descriptor = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def interprocess_lock(lock_path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Exclusive local-process lock with dead-PID recovery.

    This is intentionally a filesystem-local mutex, not a distributed lock.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.parent.is_symlink() or lock_path.is_symlink():
        raise ValueError("lock path may not be a symlink")
    owner = uuid.uuid4().hex
    payload = json.dumps(
        {"pid": os.getpid(), "owner": owner},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    deadline = time.monotonic() + timeout
    while True:
        try:
            descriptor = os.open(
                str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError:
            if lock_path.is_symlink():
                raise ValueError("lock path may not be a symlink")
            try:
                observed = lock_path.read_bytes()
                holder = json.loads(observed.decode("utf-8"))
                holder_pid = holder["pid"]
                holder_owner = holder["owner"]
                valid = (
                    isinstance(holder_pid, int)
                    and isinstance(holder_owner, str)
                    and bool(holder_owner)
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
                valid = False
                holder_pid = -1
            if valid and not _pid_alive(holder_pid):
                try:
                    if lock_path.read_bytes() == observed:
                        lock_path.unlink()
                        continue
                except (FileNotFoundError, OSError):
                    continue
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for persistence lock")
            time.sleep(0.01)
            continue
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        break
    try:
        yield
    finally:
        try:
            if lock_path.read_bytes() == payload:
                lock_path.unlink()
        except FileNotFoundError:
            pass


def _event_lock(paths: ProjectPaths, run_id: str) -> Path:
    return run_dir(paths, run_id, create=True) / ".events.lock"


def _normalize_provenance(
    event: RunEvent, provenance: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    supplied = dict(provenance or {})
    normalized = {
        "role": supplied.get("role", "system"),
        "task_id": supplied.get("task_id"),
        "handle_id": supplied.get("handle_id"),
        "generation": supplied.get("generation", 0),
        "authorization_digest": supplied.get(
            "authorization_digest", event.data.get("authorization_digest", "")
        ),
        "node_contract_digest": supplied.get(
            "node_contract_digest", event.data.get("node_contract_digest", "")
        ),
    }
    if set(normalized) != _PROVENANCE_KEYS:
        raise ValueError("event provenance schema is invalid")
    if normalized["role"] not in {"system", "developer", "reviewer"}:
        raise ValueError("event provenance role is invalid")
    if not isinstance(normalized["generation"], int) or normalized["generation"] < 0:
        raise ValueError("event provenance generation is invalid")
    for key in ("task_id", "handle_id"):
        if normalized[key] is not None and not isinstance(normalized[key], str):
            raise ValueError("event provenance task/handle identity is invalid")
    for key in ("authorization_digest", "node_contract_digest"):
        if not isinstance(normalized[key], str):
            raise ValueError("event provenance digest is invalid")
    return normalized


def _read_event_records(path: Path) -> List[Dict[str, Any]]:
    try:
        raw_lines = [line for line in path.read_bytes().splitlines() if line.strip()]
    except FileNotFoundError:
        return []
    records: List[Dict[str, Any]] = []
    expected_keys = {
        "schema_version",
        "sequence",
        "event_id",
        "run_id",
        "event",
        "provenance",
        "data",
        "previous_event_digest",
        "event_digest",
    }
    previous_digest: Optional[str] = None
    for raw_line in raw_lines:
        try:
            record = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("event log is not valid JSONL") from error
        if not isinstance(record, dict) or set(record) != expected_keys:
            raise ValueError("event record schema is invalid")
        if record["schema_version"] != EVENT_SCHEMA_VERSION:
            raise ValueError("unsupported event record schema")
        if not isinstance(record["sequence"], int) or record["sequence"] < 1:
            raise ValueError("event sequence is invalid")
        if not isinstance(record["event_id"], str) or not record["event_id"]:
            raise ValueError("event id is invalid")
        if not isinstance(record["run_id"], str) or not isinstance(record["event"], str):
            raise ValueError("event identity is invalid")
        if not isinstance(record["data"], dict):
            raise ValueError("event data is invalid")
        provenance = record["provenance"]
        if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE_KEYS:
            raise ValueError("event provenance schema is invalid")
        if provenance["role"] not in {"system", "developer", "reviewer"}:
            raise ValueError("event provenance role is invalid")
        if not isinstance(provenance["generation"], int) or provenance["generation"] < 0:
            raise ValueError("event provenance generation is invalid")
        if record["previous_event_digest"] != previous_digest:
            raise ValueError("event hash chain is inconsistent")
        if not isinstance(record["event_digest"], str) or not _DIGEST.fullmatch(
            record["event_digest"]
        ):
            raise ValueError("event digest is invalid")
        digest_payload = dict(record)
        digest_payload.pop("event_digest")
        expected_digest = hashlib.sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if record["event_digest"] != expected_digest:
            raise ValueError("event record digest is inconsistent")
        records.append(record)
        previous_digest = record["event_digest"]
    if [item["sequence"] for item in records] != list(range(1, len(records) + 1)):
        raise ValueError("event log sequence is not ordered")
    if len({item["event_id"] for item in records}) != len(records):
        raise ValueError("event ids must be unique")
    return records


def load_events(paths: ProjectPaths, run_id: str) -> List[Dict[str, Any]]:
    directory = run_dir(paths, run_id, create=False)
    records = _read_event_records(directory / "events.jsonl")
    if any(record["run_id"] != run_id for record in records):
        raise ValueError("event run lineage is inconsistent")
    return records


def append_event(
    paths: ProjectPaths,
    event: RunEvent,
    provenance: Optional[Dict[str, Any]] = None,
) -> int:
    run_id = event.data.get("run_id")
    validate_run_id(run_id)
    directory = run_dir(paths, run_id, create=True)
    event_path = directory / "events.jsonl"
    normalized_provenance = _normalize_provenance(event, provenance)
    with interprocess_lock(_event_lock(paths, run_id)):
        records = _read_event_records(event_path)
        sequence = len(records) + 1
        record = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "sequence": sequence,
            "event_id": uuid.uuid4().hex,
            "run_id": run_id,
            "event": event.event,
            "provenance": normalized_provenance,
            "data": dict(event.data),
            "previous_event_digest": records[-1]["event_digest"] if records else None,
        }
        record["event_digest"] = hashlib.sha256(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with event_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        return sequence


def _validate_snapshot(snapshot: RunSnapshot, records: List[Dict[str, Any]]) -> None:
    if snapshot.schema_version != STATE_SCHEMA_VERSION:
        raise ValueError("unsupported snapshot schema")
    validate_run_id(snapshot.run_id)
    if not isinstance(snapshot.plan_id, str) or not snapshot.plan_id:
        raise ValueError("snapshot plan id is invalid")
    if not isinstance(snapshot.plan_version, int) or snapshot.plan_version < 1:
        raise ValueError("snapshot plan version is invalid")
    if snapshot.status not in _RUN_STATUSES:
        raise ValueError("snapshot run status is invalid")
    if not isinstance(snapshot.nodes, dict) or not snapshot.nodes:
        raise ValueError("snapshot nodes must be a non-empty object")
    if not isinstance(snapshot.handles, dict) or not isinstance(snapshot.tasks, dict):
        raise ValueError("snapshot handle/task registries are invalid")
    if not isinstance(snapshot.event_sequence, int) or snapshot.event_sequence < 1:
        raise ValueError("snapshot event sequence is invalid")
    if snapshot.event_sequence > len(records):
        raise ValueError("snapshot references missing events")
    if not _DIGEST.fullmatch(snapshot.authorization_digest):
        raise ValueError("snapshot authorization digest is invalid")
    if not _DIGEST.fullmatch(snapshot.node_contract_digest):
        raise ValueError("snapshot node contract digest is invalid")

    authorization = AuthorizationRecord.from_dict(snapshot.authorization)
    if not is_authorization_integrity_valid(authorization):
        raise ValueError("snapshot authorization record digest is invalid")
    if authorization.digest != snapshot.authorization_digest:
        raise ValueError("snapshot authorization lineage is inconsistent")
    if authorization.node_contract_digest != snapshot.node_contract_digest:
        raise ValueError("snapshot contract lineage is inconsistent")
    if authorization.plan_id != snapshot.plan_id or authorization.plan_version != snapshot.plan_version:
        raise ValueError("snapshot plan lineage is inconsistent")
    if set(snapshot.nodes) != set(authorization.node_ids):
        raise ValueError("snapshot node set is inconsistent")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in snapshot.handles.items()):
        raise ValueError("snapshot handles are invalid")
    if not set(snapshot.handles).issubset(snapshot.nodes):
        raise ValueError("snapshot handle node is unknown")
    for node_id, node in snapshot.nodes.items():
        if not isinstance(node, dict) or node.get("status") not in _NODE_STATUSES:
            raise ValueError("snapshot node state is invalid for " + node_id)
    if snapshot.status == "complete" and not all(
        node.get("status") == "accepted" for node in snapshot.nodes.values()
    ):
        raise ValueError("complete run contains unaccepted nodes")

    first = records[0] if records else None
    if first is None or first["event"] != "run_started":
        raise ValueError("snapshot has no run-start event lineage")
    if first["data"].get("authorization_digest") != snapshot.authorization_digest:
        raise ValueError("run-start authorization lineage is inconsistent")
    if first["data"].get("node_contract_digest") != snapshot.node_contract_digest:
        raise ValueError("run-start contract lineage is inconsistent")
    if sorted(first["data"].get("node_ids", [])) != sorted(snapshot.nodes):
        raise ValueError("run-start node lineage is inconsistent")
    for record in records[: snapshot.event_sequence]:
        provenance = record["provenance"]
        if provenance["authorization_digest"] != snapshot.authorization_digest:
            raise ValueError("event authorization provenance is inconsistent")
        if provenance["node_contract_digest"] != snapshot.node_contract_digest:
            raise ValueError("event contract provenance is inconsistent")

    for node_id, node in snapshot.nodes.items():
        if node.get("status") != "accepted":
            continue
        matching = [
            record
            for record in records[: snapshot.event_sequence]
            if record["event"] == "accepted"
            and record["data"].get("node_id") == node_id
        ]
        if not matching:
            raise ValueError("accepted node lacks an acceptance event")
        provenance = matching[-1]["provenance"]
        if provenance["role"] != "reviewer":
            raise ValueError("accepted node lacks reviewer provenance")
        if provenance["task_id"] != node.get("reviewer_identity"):
            raise ValueError("accepted node reviewer identity is inconsistent")
        if provenance["generation"] != node.get("review_generation"):
            raise ValueError("accepted node review generation is inconsistent")


def _decode_snapshot(data: bytes, records: List[Dict[str, Any]]) -> RunSnapshot:
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("snapshot is not valid JSON") from error
    snapshot = RunSnapshot.from_dict(raw)
    _validate_snapshot(snapshot, records)
    return snapshot


def save_snapshot(paths: ProjectPaths, snapshot: RunSnapshot) -> None:
    directory = run_dir(paths, snapshot.run_id, create=True)
    records = load_events(paths, snapshot.run_id)
    _validate_snapshot(snapshot, records)
    state_path = directory / "state.json"
    previous_path = directory / "state.previous.json"
    lock_path = directory / ".state.lock"
    serialized = json.dumps(
        snapshot.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with interprocess_lock(lock_path):
        if state_path.exists():
            current = state_path.read_bytes()
            try:
                _decode_snapshot(current, records)
            except (TypeError, ValueError):
                pass
            else:
                _atomic_bytes(previous_path, current)
        _atomic_bytes(state_path, serialized)


def load_snapshot(paths: ProjectPaths, run_id: str) -> RunSnapshot:
    directory = run_dir(paths, run_id, create=False)
    records = load_events(paths, run_id)
    failures: List[Exception] = []
    for path in (directory / "state.json", directory / "state.previous.json"):
        try:
            snapshot = _decode_snapshot(path.read_bytes(), records)
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            failures.append(error)
            continue
        if snapshot.run_id != run_id:
            failures.append(ValueError("snapshot run id does not match its path"))
            continue
        return snapshot
    if failures:
        raise ValueError("no valid snapshot for run " + run_id) from failures[-1]
    raise ValueError("no valid snapshot for run " + run_id)


def _lease_path(paths: ProjectPaths, node_id: str, worktree: str) -> Path:
    key = hashlib.sha256((node_id + "\0" + worktree).encode("utf-8")).hexdigest()
    return _safe_project_path(paths, ".vibe", "leases", key + ".json")


def _lease_lock(paths: ProjectPaths) -> Path:
    return _safe_project_path(paths, ".vibe", ".leases.lock")


def acquire_writer_lease(
    paths: ProjectPaths, node_id: str, worktree: str, run_id: str
) -> bool:
    validate_run_id(run_id)
    lease_path = _lease_path(paths, node_id, worktree)
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": LEASE_SCHEMA_VERSION,
        "node_id": node_id,
        "worktree": worktree,
        "run_id": run_id,
        "status": "active",
    }
    with interprocess_lock(_lease_lock(paths)):
        if lease_path.exists():
            try:
                existing = json.loads(lease_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            return (
                existing.get("schema_version") == LEASE_SCHEMA_VERSION
                and existing.get("run_id") == run_id
                and existing.get("node_id") == node_id
                and existing.get("worktree") == worktree
            )
        _atomic_bytes(
            lease_path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        return True


def quarantine_writer_lease(
    paths: ProjectPaths,
    node_id: str,
    worktree: str,
    run_id: str,
    reason: str,
) -> bool:
    lease_path = _lease_path(paths, node_id, worktree)
    with interprocess_lock(_lease_lock(paths)):
        try:
            payload = json.loads(lease_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        if payload.get("run_id") != run_id:
            return False
        payload["status"] = "quarantined"
        payload["reason"] = reason
        _atomic_bytes(
            lease_path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        return True


def release_writer_lease(
    paths: ProjectPaths, node_id: str, worktree: str, run_id: str
) -> bool:
    lease_path = _lease_path(paths, node_id, worktree)
    with interprocess_lock(_lease_lock(paths)):
        try:
            owner = json.loads(lease_path.read_text(encoding="utf-8")).get("run_id")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False
        if owner != run_id:
            return False
        lease_path.unlink()
        return True
