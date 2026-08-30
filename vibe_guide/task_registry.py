"""Atomic per-run registry for developer and reviewer task identities."""

from dataclasses import dataclass, field
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Optional


REGISTRY_SCHEMA_VERSION = 1
_ROLES = {"developer", "reviewer"}
_MODES = {"visible", "background"}
_STATUSES = {
    "created", "start_pending", "running", "delivered", "review", "rework",
    "accepted", "archived", "blocked_design", "blocked_unknown", "failed", "stopped",
}
_EXPECTED_ROUTES = {}


def _digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def register_expected_binding_route(run_id, issue_id, role, host, worktree, branch):
    values = (run_id, issue_id, role, host, worktree, branch)
    if any(not isinstance(item, str) or not item for item in values):
        raise ValueError("expected task route is incomplete")
    _EXPECTED_ROUTES[(run_id, issue_id, role)] = {
        "host": host, "worktree": worktree, "branch": branch,
    }


def _reject_symlink_chain(path, boundary=None):
    """Reject any existing symlink in a path before mkdir/open/replace."""
    candidate = Path(os.path.abspath(str(path)))
    stop = Path(os.path.abspath(str(boundary))) if boundary is not None else None
    items = (candidate,) if stop is None else (candidate, *candidate.parents)
    for item in items:
        if item.is_symlink():
            raise ValueError("task registry path may not contain symlinks")
        if stop is not None and item == stop:
            break


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
    client_thread_id: Optional[str] = None
    token: Optional[str] = field(default=None, repr=False)
    threadId: Optional[str] = None
    hostId: Optional[str] = None
    run_id: Optional[str] = None
    platform_task_id: Optional[str] = None
    status: str = "created"
    visible: Optional[bool] = None
    limitations: List[str] = field(default_factory=list)
    generation: int = 0
    schema_version: int = REGISTRY_SCHEMA_VERSION

    def __post_init__(self):
        if self.schema_version != REGISTRY_SCHEMA_VERSION:
            raise ValueError("task binding schema version is unsupported")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider is required")
        if self.mode not in _MODES:
            raise ValueError("mode must be visible or background")
        if self.role not in _ROLES:
            raise ValueError("role must be developer or reviewer")
        if not isinstance(self.issue_id, str) or not self.issue_id.strip():
            raise ValueError("issue_id is required")
        if self.status not in _STATUSES:
            raise ValueError("task binding status is invalid")
        if not isinstance(self.generation, int) or self.generation < 0:
            raise ValueError("task binding generation is invalid")
        if not isinstance(self.run_id, str) or not self.run_id or not all(
            ch.isalnum() or ch in "_.-" for ch in self.run_id
        ) or not self.run_id[0].isalnum():
            raise ValueError("task binding run_id is invalid")
        ids = [item for item in (self.task_id, self.platform_task_id, self.threadId) if item]
        if any(not isinstance(item, str) for item in ids) or len(set(ids)) > 1:
            raise ValueError("platform task identity aliases disagree")
        canonical_id = ids[0] if ids else None
        self.task_id = self.platform_task_id = canonical_id
        self.threadId = canonical_id if self.provider in {"codex", "codex-app-visible"} else self.threadId
        hosts = [item for item in (self.host, self.hostId) if item]
        if any(not isinstance(item, str) for item in hosts) or len(set(hosts)) > 1:
            raise ValueError("platform host identity aliases disagree")
        canonical_host = hosts[0] if hosts else None
        self.host = canonical_host
        self.hostId = canonical_host if self.provider in {"codex", "codex-app-visible"} else self.hostId
        if self.visible is None:
            self.visible = self.mode == "visible"
        if bool(self.visible) != (self.mode == "visible"):
            raise ValueError("task visibility disagrees with provider mode")
        if self.mode == "visible" and ((not self.task_id or not self.host) and not self.client_thread_id):
            raise ValueError("visible task binding requires task and host identity")
        if self.provider == "codex-app-visible" and self.task_id and not self.client_thread_id:
            if any(not isinstance(value, str) or not value.strip()
                   for value in (self.host, self.worktree, self.branch)):
                raise ValueError("visible Codex task binding route is incomplete")
        if self.provider == "codex-app-visible" and self.task_id:
            try:
                from .adapters.task_provider import observed_provider_route
                observed = observed_provider_route(self.task_id)
            except ImportError:
                observed = None
            if observed is not None and any(
                not observed.get(name) or getattr(self, name) != observed.get(name)
                for name in ("host", "worktree", "branch")
            ):
                raise ValueError("provider route does not match observed binding")
            expected = _EXPECTED_ROUTES.get((self.run_id, self.issue_id, self.role))
            if expected is not None and any(
                getattr(self, name) != expected[name]
                for name in ("host", "worktree", "branch")
            ):
                raise ValueError("provider route does not match expected contract")
        if self.cursor is not None and (not isinstance(self.cursor, str) or len(self.cursor) > 4096):
            raise ValueError("continuation cursor is invalid")
        if self.token is not None:
            self.token = None

    @property
    def identity(self):
        return self.task_id

    @property
    def composite_identity(self):
        return (self.provider, self.mode, self.issue_id, self.role, self.task_id,
                self.host, self.worktree, self.branch)

    def to_dict(self):
        return {
            "schema_version": self.schema_version, "provider": self.provider,
            "mode": self.mode, "issue_id": self.issue_id, "role": self.role,
            "task_id": self.task_id, "platform_task_id": self.platform_task_id,
            "host": self.host, "worktree": self.worktree, "branch": self.branch,
            "status_file": self.status_file, "handoff_file": self.handoff_file,
            "cursor": self.cursor, "client_thread_id": self.client_thread_id,
            "threadId": self.threadId, "hostId": self.hostId,
            "run_id": self.run_id, "status": self.status, "visible": self.visible,
            "limitations": list(self.limitations), "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("task binding record must be an object")
        return cls(**data)


def _run_file(paths, run_id, create=False):
    if not isinstance(run_id, str) or not run_id or not all(
        ch.isalnum() or ch in "_.-" for ch in run_id
    ) or not run_id[0].isalnum():
        raise ValueError("valid run_id is required")
    vibe = Path(paths.vibe)
    runs = vibe / "runs"
    boundary = getattr(paths, "root", None)
    _reject_symlink_chain(vibe, boundary)
    _reject_symlink_chain(runs, boundary)
    if vibe.is_symlink() or runs.is_symlink():
        raise ValueError("task registry root may not be a symlink")
    if create:
        runs.mkdir(parents=True, exist_ok=True)
    root = runs / run_id
    _reject_symlink_chain(root, boundary)
    if root.exists() and root.is_symlink():
        raise ValueError("task run directory may not be a symlink")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("task run directory is invalid")
    return root / "tasks.json"


def _read(path, expected_run_id=None):
    if path.is_symlink():
        raise ValueError("task registry file may not be a symlink")
    if not path.is_file():
        return 0, []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("task registry is not valid JSON") from error
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "revision", "bindings"}:
        raise ValueError("task registry schema is invalid")
    if (raw["schema_version"] != REGISTRY_SCHEMA_VERSION
            or type(raw["revision"]) is not int or raw["revision"] < 0
            or not isinstance(raw["bindings"], list)):
        raise ValueError("task registry schema is invalid")
    bindings = [TaskBinding.from_dict(item) for item in raw["bindings"]]
    if expected_run_id is not None and any(item.run_id != expected_run_id for item in bindings):
        raise ValueError("task binding run provenance does not match registry run")
    return int(raw["revision"]), bindings


def _require_route_provenance(binding):
    """Require durable visible bindings to have current provider route evidence."""
    if binding.provider != "codex-app-visible" or not binding.task_id or binding.client_thread_id:
        return
    try:
        from .adapters.task_provider import observed_provider_route
    except ImportError:
        observed_provider_route = None
    observed = observed_provider_route(binding.task_id) if callable(observed_provider_route) else None
    expected = _EXPECTED_ROUTES.get((binding.run_id, binding.issue_id, binding.role))
    if observed is None and expected is None:
        raise ValueError("visible task binding lacks observed route provenance")
    for route in (observed, expected):
        if route is not None and any(
            not isinstance(route.get(name), str) or not route.get(name).strip()
            or getattr(binding, name) != route.get(name)
            for name in ("host", "worktree", "branch")
        ):
            raise ValueError("visible task binding route provenance does not match")


def _atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("task registry path may not be a symlink")
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _registry_lock(paths, run_id=None):
    vibe = Path(paths.vibe)
    lock = vibe / ".task-registry.lock"
    runs = vibe / "runs"
    boundary = getattr(paths, "root", None)
    _reject_symlink_chain(vibe, boundary)
    _reject_symlink_chain(lock, boundary)
    _reject_symlink_chain(runs, boundary)
    if run_id is not None:
        _reject_symlink_chain(runs / str(run_id), boundary)
    if vibe.is_symlink() or lock.is_symlink():
        raise ValueError("task registry lock path may not be a symlink")
    if runs.is_symlink():
        raise ValueError("task registry runs path may not be a symlink")
    if run_id is not None and (runs / str(run_id)).is_symlink():
        raise ValueError("task registry run directory may not be a symlink")
    vibe.mkdir(parents=True, exist_ok=True)
    if vibe.is_symlink() or lock.is_symlink() or not vibe.is_dir():
        raise ValueError("task registry lock root is invalid")
    try:
        lock.resolve().relative_to(vibe.resolve())
    except ValueError as exc:
        raise ValueError("task registry lock escapes project root") from exc
    with lock.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def save_task_binding(paths, binding):
    persistent = TaskBinding.from_dict(binding.to_dict())
    if not persistent.run_id:
        raise ValueError("task binding run_id is required")
    if not persistent.worktree or not persistent.branch:
        raise ValueError("durable task binding requires worktree and branch")
    with _registry_lock(paths, persistent.run_id):
        destination = _run_file(paths, persistent.run_id, create=True)
        revision, bindings = _read(destination, expected_run_id=persistent.run_id)
        for current in bindings:
            if current.issue_id != persistent.issue_id:
                continue
            if current.role == persistent.role and current.composite_identity != persistent.composite_identity:
                if current.run_id == persistent.run_id or current.status not in {"archived", "stopped", "failed", "blocked_design"}:
                    raise ValueError("immutable task identity drift")
            if current.role != persistent.role and current.task_id and current.task_id == persistent.task_id:
                raise ValueError("developer and reviewer tasks must be distinct")
        replaced = False
        for index, current in enumerate(bindings):
            if current.issue_id == persistent.issue_id and current.role == persistent.role:
                bindings[index] = persistent
                replaced = True
                break
        if not replaced:
            bindings.append(persistent)
        _atomic(destination, {"schema_version": REGISTRY_SCHEMA_VERSION, "revision": revision + 1,
                              "bindings": [item.to_dict() for item in bindings]})


def load_task_binding(paths, issue_id, role, run_id=None):
    if role not in _ROLES:
        raise ValueError("role must be developer or reviewer")
    candidates = []
    with _registry_lock(paths, run_id):
        if run_id is not None:
            _revision, candidates = _read(_run_file(paths, run_id), expected_run_id=run_id)
            for binding in candidates:
                _require_route_provenance(binding)
        else:
            root = Path(paths.vibe) / "runs"
            if root.is_dir():
                for child in root.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        _revision, values = _read(child / "tasks.json", expected_run_id=child.name)
                        for binding in values:
                            _require_route_provenance(binding)
                        candidates.extend(values)
    matches = [item for item in candidates if item.issue_id == issue_id and item.role == role]
    if not matches:
        raise FileNotFoundError("no task binding for {} {}".format(issue_id, role))
    return sorted(matches, key=lambda item: item.generation)[-1]


__all__ = ["TaskBinding", "save_task_binding", "load_task_binding"]
