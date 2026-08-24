from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Dict
from dataclasses import field

from .contracts import RunEvent
from .paths import ProjectPaths


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass
class RunSnapshot:
    run_id: str
    plan_id: str
    plan_version: int
    status: str
    nodes: Dict[str, Dict[str, Any]]
    handles: Dict[str, str]
    tasks: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunSnapshot":
        return cls(**data)


def _run_dir(paths: ProjectPaths, run_id: str) -> Path:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("run id must be a simple identifier")
    return paths.root / ".vibe" / "runs" / run_id


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary_path), str(path))
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def save_snapshot(paths: ProjectPaths, snapshot: RunSnapshot) -> None:
    run_dir = _run_dir(paths, snapshot.run_id)
    state_path = run_dir / "state.json"
    previous_path = run_dir / "state.previous.json"
    if state_path.exists():
        current = state_path.read_bytes()
        try:
            RunSnapshot.from_dict(json.loads(current.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            pass
        else:
            _atomic_bytes(previous_path, current)
    serialized = json.dumps(snapshot.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    _atomic_bytes(state_path, serialized)


def load_snapshot(paths: ProjectPaths, run_id: str) -> RunSnapshot:
    run_dir = _run_dir(paths, run_id)
    failures = []
    for path in (run_dir / "state.json", run_dir / "state.previous.json"):
        try:
            return RunSnapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (FileNotFoundError, json.JSONDecodeError, TypeError, UnicodeDecodeError) as error:
            failures.append(error)
    raise ValueError("no valid snapshot for run " + run_id) from failures[-1]


def append_event(paths: ProjectPaths, event: RunEvent) -> None:
    run_id = event.data.get("run_id")
    if not isinstance(run_id, str):
        raise ValueError("event data must include run_id")
    event_path = _run_dir(paths, run_id) / "events.jsonl"
    event_path.parent.mkdir(parents=True, exist_ok=True)
    sequence = 1
    if event_path.exists():
        with event_path.open("rb") as stream:
            lines = [line for line in stream if line.strip()]
        if lines:
            # Never rewrite an existing event.  A malformed/truncated record
            # is an unknown state and must be surfaced instead of being
            # silently treated as an empty log.
            try:
                prior = [json.loads(line.decode("utf-8")) for line in lines]
                sequences = [int(item["sequence"]) for item in prior]
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise ValueError("event log is not valid JSONL") from error
            if sequences != list(range(1, len(sequences) + 1)):
                raise ValueError("event log sequence is not ordered")
            sequence = sequences[-1] + 1
    record = {"sequence": sequence, "event": event.event, "data": event.data}
    with event_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _lease_path(paths: ProjectPaths, node_id: str, worktree: str) -> Path:
    key = hashlib.sha256((node_id + "\0" + worktree).encode("utf-8")).hexdigest()
    return paths.root / ".vibe" / "leases" / (key + ".json")


def acquire_writer_lease(paths: ProjectPaths, node_id: str, worktree: str, run_id: str) -> bool:
    _run_dir(paths, run_id)
    lease_path = _lease_path(paths, node_id, worktree)
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"node_id": node_id, "worktree": worktree, "run_id": run_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        descriptor = os.open(str(lease_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            return json.loads(lease_path.read_text(encoding="utf-8")).get("run_id") == run_id
        except (OSError, json.JSONDecodeError):
            return False
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return True


def release_writer_lease(paths: ProjectPaths, node_id: str, worktree: str, run_id: str) -> bool:
    lease_path = _lease_path(paths, node_id, worktree)
    try:
        owner = json.loads(lease_path.read_text(encoding="utf-8")).get("run_id")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    if owner != run_id:
        return False
    lease_path.unlink()
    return True
