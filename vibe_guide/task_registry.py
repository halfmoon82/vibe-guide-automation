"""Durable, provider-neutral registration for visible/background tasks.

The registry is deliberately small.  It records task identity and continuation
metadata; it does not attempt to discover platform tasks or infer their state.
"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Iterable, List, Optional

from .paths import ProjectPaths


_ROLES = {"developer", "reviewer"}
_MODES = {"visible", "background"}


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
    token: Optional[str] = None
    threadId: Optional[str] = None
    hostId: Optional[str] = None
    run_id: Optional[str] = None
    platform_task_id: Optional[str] = None
    status: str = "created"
    visible: Optional[bool] = None
    limitations: List[str] = field(default_factory=list)
    # Snake-case aliases make the generic registry convenient for Python
    # providers while preserving the Codex contract's exact camel-case keys.
    thread_id: Optional[str] = None
    host_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.provider:
            raise ValueError("provider is required")
        if self.mode not in _MODES:
            raise ValueError("mode must be visible or background")
        if self.role not in _ROLES:
            raise ValueError("role must be developer or reviewer")
        if not self.issue_id:
            raise ValueError("issue_id is required")

        if self.task_id is None:
            self.task_id = self.platform_task_id
        if self.platform_task_id is None:
            self.platform_task_id = self.task_id
        if self.threadId is None:
            self.threadId = self.thread_id
        if self.thread_id is None:
            self.thread_id = self.threadId
        if self.host is None:
            self.host = self.hostId or self.host_id
        if self.hostId is None:
            self.hostId = self.host_id or self.host
        if self.host_id is None:
            self.host_id = self.hostId
        if self.visible is None:
            self.visible = self.mode == "visible"

        if self.mode == "visible" and not (self.task_id or self.threadId):
            raise ValueError("visible task binding requires a platform task id")
        if self.mode == "visible" and not self.host:
            raise ValueError("visible task binding requires a host")

    @property
    def identity(self) -> Optional[str]:
        """Return the stable platform identity, excluding execution handles."""

        return self.task_id or self.threadId or self.platform_task_id

    def to_dict(self) -> Dict[str, Any]:
        # Keep both generic and Codex-specific fields in the durable record.
        return {
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
            "token": self.token,
            "threadId": self.threadId,
            "hostId": self.hostId,
            "run_id": self.run_id,
            "status": self.status,
            "visible": self.visible,
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskBinding":
        known = {
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
            "token",
            "threadId",
            "hostId",
            "run_id",
            "status",
            "visible",
            "limitations",
            "thread_id",
            "host_id",
        }
        return cls(**{key: value for key, value in data.items() if key in known})


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + path.name + ".", dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary_path), str(path))
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _run_tasks_path(paths: ProjectPaths, run_id: Optional[str]) -> Path:
    if run_id:
        return paths.root / ".vibe" / "runs" / run_id / "tasks.json"
    return paths.root / ".vibe" / "tasks.json"


def _read_bindings(path: Path) -> List[TaskBinding]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
        raise ValueError("task registry is not valid JSON") from error
    if isinstance(raw, dict):
        values = raw.get("bindings", [])
    elif isinstance(raw, list):
        values = raw
    else:
        return []
    result: List[TaskBinding] = []
    for value in values:
        if isinstance(value, dict):
            result.append(TaskBinding.from_dict(value))
    return result


def _registry_paths(paths: ProjectPaths) -> Iterable[Path]:
    root = paths.root / ".vibe"
    legacy = root / "tasks.json"
    if legacy.exists():
        yield legacy
    runs = root / "runs"
    if runs.exists():
        for run_dir in sorted(runs.iterdir()):
            candidate = run_dir / "tasks.json"
            if candidate.exists():
                yield candidate


def save_task_binding(paths: ProjectPaths, binding: TaskBinding) -> None:
    """Atomically save a binding while rejecting a second task writer.

    A continuation may update the same issue/role only when it retains the
    original stable platform identity.  Developer and reviewer identities must
    also remain distinct for one issue.
    """

    destination = _run_tasks_path(paths, binding.run_id)
    existing: List[TaskBinding] = []
    for path in _registry_paths(paths):
        if path != destination:
            existing.extend(_read_bindings(path))
    existing.extend(_read_bindings(destination))

    for current in existing:
        if current.issue_id != binding.issue_id:
            continue
        if current.role == binding.role:
            if not current.identity or not binding.identity or current.identity != binding.identity:
                raise ValueError("duplicate task writer for issue and role")
            # Existing records from another run are retained only when this is
            # the same identity; the new record is the latest continuation
            # state in its destination file.
        elif current.identity and binding.identity and current.identity == binding.identity:
            raise ValueError("developer and reviewer tasks must be distinct")

    destination_values = _read_bindings(destination)
    found_local = False
    for index, current in enumerate(destination_values):
        if current.issue_id == binding.issue_id and current.role == binding.role:
            destination_values[index] = binding
            found_local = True
            break
    if not found_local:
        destination_values.append(binding)
    _atomic_json(
        destination,
        {"version": 1, "bindings": [item.to_dict() for item in destination_values]},
    )


def load_task_binding(paths: ProjectPaths, issue_id: str, role: str) -> TaskBinding:
    if role not in _ROLES:
        raise ValueError("role must be developer or reviewer")
    matches: List[TaskBinding] = []
    for path in _registry_paths(paths):
        matches.extend(
            item
            for item in _read_bindings(path)
            if item.issue_id == issue_id and item.role == role
        )
    if not matches:
        raise FileNotFoundError("no task binding for {} {}".format(issue_id, role))
    return matches[-1]
