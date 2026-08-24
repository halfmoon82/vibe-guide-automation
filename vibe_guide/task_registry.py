"""Versioned, multi-process-safe registry for provider task bindings."""

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .paths import ProjectPaths
from .state import _safe_project_path, interprocess_lock, run_dir, validate_run_id


REGISTRY_SCHEMA_VERSION = 1
BINDING_SCHEMA_VERSION = 1
_ROLES = {"developer", "reviewer"}
_MODES = {"visible", "background"}
_STATUSES = {
    "created",
    "start_pending",
    "running",
    "delivered",
    "review",
    "rework",
    "accepted",
    "archived",
    "blocked_design",
    "blocked_unknown",
    "failed",
    "stopped",
}


def _digest_reference(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class TaskBinding:
    provider: str
    mode: str
    issue_id: str
    role: str
    task_id: Optional[str] = None
    host: Optional[str] = None
    worktree: str = ""
    branch: str = ""
    status_file: str = ""
    handoff_file: str = ""
    cursor: Optional[str] = None
    token: Optional[str] = field(default=None, repr=False)
    threadId: Optional[str] = None
    hostId: Optional[str] = None
    run_id: Optional[str] = None
    platform_task_id: Optional[str] = None
    status: str = "created"
    visible: Optional[bool] = None
    limitations: List[str] = field(default_factory=list)
    thread_id: Optional[str] = None
    host_id: Optional[str] = None
    continuation_digest: Optional[str] = None
    generation: int = 0
    schema_version: int = BINDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BINDING_SCHEMA_VERSION:
            raise ValueError("unsupported task binding schema")
        if not isinstance(self.provider, str) or not self.provider:
            raise ValueError("provider is required")
        if self.mode not in _MODES:
            raise ValueError("mode must be visible or background")
        if self.role not in _ROLES:
            raise ValueError("role must be developer or reviewer")
        if not isinstance(self.issue_id, str) or not self.issue_id:
            raise ValueError("issue_id is required")
        if self.status not in _STATUSES:
            raise ValueError("task binding status is invalid")
        if not isinstance(self.generation, int) or self.generation < 0:
            raise ValueError("task binding generation is invalid")

        ids = [
            item
            for item in (
                self.task_id,
                self.platform_task_id,
                self.threadId,
                self.thread_id,
            )
            if item is not None
        ]
        if any(not isinstance(item, str) or not item for item in ids):
            raise ValueError("platform task identity is invalid")
        if len(set(ids)) > 1:
            raise ValueError("platform task identity aliases disagree")
        canonical_id = ids[0] if ids else None
        self.task_id = canonical_id
        self.platform_task_id = canonical_id
        self.threadId = canonical_id if self.provider == "codex" else self.threadId
        self.thread_id = self.threadId

        hosts = [item for item in (self.host, self.hostId, self.host_id) if item is not None]
        if any(not isinstance(item, str) or not item for item in hosts):
            raise ValueError("platform host identity is invalid")
        if len(set(hosts)) > 1:
            raise ValueError("platform host identity aliases disagree")
        canonical_host = hosts[0] if hosts else None
        self.host = canonical_host
        self.hostId = canonical_host if self.provider == "codex" else self.hostId
        self.host_id = self.hostId

        expected_visible = self.mode == "visible"
        if self.visible is not None and self.visible != expected_visible:
            raise ValueError("task visibility disagrees with provider mode")
        self.visible = expected_visible
        if self.mode == "visible" and not canonical_id:
            raise ValueError("visible task binding requires a platform task id")
        if self.mode == "visible" and not canonical_host:
            raise ValueError("visible task binding requires a host")

        if self.cursor is not None:
            if not isinstance(self.cursor, str) or len(self.cursor) > 4096:
                raise ValueError("continuation cursor is invalid")
        if self.token is not None:
            if not isinstance(self.token, str):
                raise ValueError("continuation token is invalid")
            digest = _digest_reference(self.token)
            if self.continuation_digest and self.continuation_digest != digest:
                raise ValueError("continuation token digest is inconsistent")
            self.continuation_digest = digest
            self.token = None
        if self.continuation_digest is not None:
            if not isinstance(self.continuation_digest, str) or len(self.continuation_digest) != 64:
                raise ValueError("continuation digest is invalid")

    @property
    def identity(self) -> Optional[str]:
        return self.task_id

    @property
    def composite_identity(self) -> Tuple[Any, ...]:
        return (
            self.provider,
            self.mode,
            self.issue_id,
            self.role,
            self.task_id,
            self.host,
            self.worktree,
            self.branch,
        )

    def to_dict(self) -> Dict[str, Any]:
        continuation_digest = self.continuation_digest
        if self.token is not None:
            continuation_digest = _digest_reference(self.token)
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "mode": self.mode,
            "issue_id": self.issue_id,
            "role": self.role,
            "task_id": self.task_id,
            "platform_task_id": self.platform_task_id,
            "host": self.host,
            "worktree": self.worktree,
            "branch": self.branch,
            "status_file": self.status_file,
            "handoff_file": self.handoff_file,
            "cursor": self.cursor,
            "continuation_digest": continuation_digest,
            "threadId": self.threadId,
            "hostId": self.hostId,
            "run_id": self.run_id,
            "status": self.status,
            "visible": self.visible,
            "limitations": list(self.limitations),
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskBinding":
        expected = {
            "schema_version",
            "provider",
            "mode",
            "issue_id",
            "role",
            "task_id",
            "platform_task_id",
            "host",
            "worktree",
            "branch",
            "status_file",
            "handoff_file",
            "cursor",
            "continuation_digest",
            "threadId",
            "hostId",
            "run_id",
            "status",
            "visible",
            "limitations",
            "generation",
        }
        if not isinstance(data, dict) or set(data) != expected:
            raise ValueError("task binding record schema is invalid")
        return cls(**data)


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("registry parent may not be a symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + path.name + ".", dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary_path), str(path))
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _run_tasks_path(paths: ProjectPaths, run_id: Optional[str], create: bool) -> Path:
    if run_id is None:
        raise ValueError("task binding run_id is required")
    validate_run_id(run_id)
    return run_dir(paths, run_id, create=create) / "tasks.json"


def _read_registry(
    path: Path, expected_run_id: Optional[str] = None
) -> Tuple[int, List[TaskBinding]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0, []
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
        raise ValueError("task registry is not valid JSON") from error
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "revision", "bindings"}:
        raise ValueError("task registry schema is invalid")
    if raw["schema_version"] != REGISTRY_SCHEMA_VERSION:
        raise ValueError("unsupported task registry schema")
    if not isinstance(raw["revision"], int) or raw["revision"] < 0:
        raise ValueError("task registry revision is invalid")
    if not isinstance(raw["bindings"], list):
        raise ValueError("task registry bindings must be a list")
    bindings = [TaskBinding.from_dict(item) for item in raw["bindings"]]
    if expected_run_id is not None:
        validate_run_id(expected_run_id)
        if any(binding.run_id != expected_run_id for binding in bindings):
            raise ValueError("task binding run lineage does not match registry path")
    return raw["revision"], bindings


def _registry_paths(paths: ProjectPaths) -> Iterable[Path]:
    probe = run_dir(paths, "registry-probe", create=False)
    run_root = probe.parent
    if not run_root.exists():
        return []
    if run_root.is_symlink():
        raise ValueError("task run registry may not be a symlink")
    result = []
    for child in sorted(run_root.iterdir()):
        if child.is_symlink():
            raise ValueError("task run registry contains a symlink")
        if not child.is_dir():
            continue
        candidate = child / "tasks.json"
        if candidate.exists():
            validate_run_id(child.name)
            result.append(candidate)
    return result


def _registry_lock(paths: ProjectPaths) -> Path:
    return _safe_project_path(paths, ".vibe", ".task-registry.lock")


def _all_bindings(paths: ProjectPaths) -> List[TaskBinding]:
    result: List[TaskBinding] = []
    for path in _registry_paths(paths):
        expected_run_id = validate_run_id(path.parent.name)
        _revision, bindings = _read_registry(path, expected_run_id)
        result.extend(bindings)
    return result


def save_task_binding(paths: ProjectPaths, binding: TaskBinding) -> None:
    # Reconstruct from the persistence form so a token assigned after
    # construction is reduced to a digest before any durable or comparison use.
    persistent = TaskBinding.from_dict(binding.to_dict())
    if not persistent.worktree or not persistent.branch:
        raise ValueError("durable task binding requires worktree and branch")
    destination = _run_tasks_path(paths, persistent.run_id, create=True)
    lock_path = _registry_lock(paths)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with interprocess_lock(lock_path):
        existing = _all_bindings(paths)
        for current in existing:
            if current.issue_id != persistent.issue_id:
                continue
            if current.role == persistent.role:
                terminal = {"archived", "blocked_design", "failed", "stopped"}
                if (
                    current.composite_identity != persistent.composite_identity
                    and (
                        current.run_id == persistent.run_id
                        or current.status not in terminal
                    )
                ):
                    raise ValueError("immutable task identity drift")
            elif current.task_id and current.task_id == persistent.task_id:
                raise ValueError("developer and reviewer tasks must be distinct")

        revision, destination_values = _read_registry(
            destination, persistent.run_id
        )
        replaced = False
        for index, current in enumerate(destination_values):
            if current.issue_id == persistent.issue_id and current.role == persistent.role:
                if current.composite_identity != persistent.composite_identity:
                    raise ValueError("immutable task identity drift")
                destination_values[index] = persistent
                replaced = True
                break
        if not replaced:
            destination_values.append(persistent)
        _atomic_json(
            destination,
            {
                "schema_version": REGISTRY_SCHEMA_VERSION,
                "revision": revision + 1,
                "bindings": [item.to_dict() for item in destination_values],
            },
        )


def load_task_binding(
    paths: ProjectPaths,
    issue_id: str,
    role: str,
    run_id: Optional[str] = None,
) -> TaskBinding:
    if role not in _ROLES:
        raise ValueError("role must be developer or reviewer")
    if run_id is not None:
        validate_run_id(run_id)
    lock_path = _registry_lock(paths)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with interprocess_lock(lock_path):
        if run_id is None:
            candidates = _all_bindings(paths)
        else:
            _revision, candidates = _read_registry(
                _run_tasks_path(paths, run_id, create=False), run_id
            )
        matches = [
            item
            for item in candidates
            if item.issue_id == issue_id and item.role == role
        ]
    if not matches:
        raise FileNotFoundError("no task binding for {} {}".format(issue_id, role))
    matches.sort(key=lambda item: item.generation)
    return matches[-1]
