"""Versioned, multi-process-safe registry for provider task bindings."""

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .paths import ProjectPaths
from .state import (
    LEASE_SCHEMA_VERSION,
    _safe_project_path,
    interprocess_lock,
    run_dir,
    supervisor_lease_id,
    validate_run_id,
)
from .models import (
    BindingIntent,
    BindingObservation,
    BindingVerification,
    SupervisorLeaseObservation,
    WaitThreadsCursorObservation,
)


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
_TERMINAL_STATUSES = {
    "archived",
    "blocked_design",
    "failed",
    "stopped",
}
_DIGEST_HEX = set("0123456789abcdef")


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
    allowlist: List[str] = field(default_factory=list)
    route_digest: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None
    capability_contract_digest: Optional[str] = None
    successor_of: Optional[str] = None
    binding_intent: Optional[Dict[str, Any]] = None
    binding_observation: Optional[Dict[str, Any]] = None
    binding_state: str = "blocked_unknown"
    business_write_allowed: bool = False
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

        if not isinstance(self.allowlist, (list, tuple)):
            raise ValueError("task binding allowlist is invalid")
        normalized_allowlist = []
        for item in self.allowlist:
            if not isinstance(item, str) or not item.strip() or "\x00" in item:
                raise ValueError("task binding allowlist is invalid")
            normalized_allowlist.append(item)
        if len(normalized_allowlist) != len(set(normalized_allowlist)):
            raise ValueError("task binding allowlist contains duplicates")
        self.allowlist = normalized_allowlist
        if self.capability_contract_digest is not None:
            if (
                not isinstance(self.capability_contract_digest, str)
                or len(self.capability_contract_digest) != 64
                or any(
                    item not in _DIGEST_HEX
                    for item in self.capability_contract_digest.lower()
                )
            ):
                raise ValueError("task binding capability contract digest is invalid")
            self.capability_contract_digest = self.capability_contract_digest.lower()
        for name in ("route_digest", "model", "reasoning"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("task binding %s is invalid" % name)
        if self.route_digest is not None and (
            len(self.route_digest) != 64
            or any(char not in _DIGEST_HEX for char in self.route_digest.lower())
        ):
            raise ValueError("task binding route digest is invalid")
        if self.route_digest is not None:
            self.route_digest = self.route_digest.lower()
        if self.successor_of is not None and (
            not isinstance(self.successor_of, str) or not self.successor_of
        ):
            raise ValueError("task binding successor predecessor is invalid")
        for name in ("binding_intent", "binding_observation"):
            value = getattr(self, name)
            if value is not None:
                validator = BindingIntent if name == "binding_intent" else BindingObservation
                if isinstance(value, validator):
                    continue
                if not isinstance(value, dict):
                    raise ValueError("%s must be a JSON object" % name)
                try:
                    setattr(self, name, validator.from_dict(value).to_dict())
                except (TypeError, ValueError) as error:
                    raise ValueError("%s is invalid" % name) from error
        if self.binding_state not in {"blocked_unknown", "binding_verified"}:
            raise ValueError("task binding state is invalid")
        if type(self.business_write_allowed) is not bool:
            raise ValueError("task binding business write flag is invalid")
        if self.business_write_allowed and self.binding_state != "binding_verified":
            raise ValueError("business write requires verified binding")
        if self.binding_state == "binding_verified":
            if not isinstance(self.binding_intent, BindingIntent) or not isinstance(
                self.binding_observation, BindingObservation
            ):
                raise ValueError("verified task binding requires binding evidence")
            intent = self.binding_intent
            observation = self.binding_observation
            if not isinstance(observation.lease, SupervisorLeaseObservation):
                raise ValueError("verified task binding lease lacks provenance")
            if not isinstance(observation.cursor_observation, WaitThreadsCursorObservation):
                raise ValueError("verified task binding cursor lacks provenance")
            if not observation.lease.active or observation.lease.status != "active":
                raise ValueError("verified task binding lease is not active")
            if any(
                getattr(intent, field, None) in (None, "")
                for field in ("node_id", "lease_id", "head_sha", "clean", "cursor")
            ):
                raise ValueError("verified task binding intent is incomplete")
            if any(
                getattr(observation, field, None) in (None, "")
                for field in (
                    "project_id", "task_id", "node_id", "host_id", "worktree",
                    "managed_root", "branch", "base_sha", "head_sha", "lease",
                    "cursor", "cursor_source", "cursor_task_id", "cursor_host_id",
                    "cursor_lineage",
                )
            ):
                raise ValueError("verified task binding observation is incomplete")
            verification = validate_binding(intent, observation)
            if not verification.verified:
                raise ValueError("verified task binding evidence failed validation")

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
        result = {
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
            "allowlist": list(self.allowlist),
            "capability_contract_digest": self.capability_contract_digest,
            "successor_of": self.successor_of,
            "route_digest": self.route_digest,
            "model": self.model,
            "reasoning": self.reasoning,
        }
        if self.binding_intent is not None:
            result["binding_intent"] = (
                self.binding_intent.to_dict()
                if isinstance(self.binding_intent, BindingIntent)
                else dict(self.binding_intent)
            )
        if self.binding_observation is not None:
            result["binding_observation"] = (
                self.binding_observation.to_dict()
                if isinstance(self.binding_observation, BindingObservation)
                else dict(self.binding_observation)
            )
        if self.binding_state != "blocked_unknown" or self.business_write_allowed:
            result["binding_state"] = self.binding_state
            result["business_write_allowed"] = self.business_write_allowed
        return result

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
        optional = {"binding_intent", "binding_observation", "binding_state", "business_write_allowed"}
        allowed = expected | {"allowlist", "capability_contract_digest", "successor_of", "route_digest", "model", "reasoning"} | optional
        if not isinstance(data, dict) or not set(data).issubset(allowed) or not expected.issubset(data):
            raise ValueError("task binding record schema is invalid")
        normalized = dict(data)
        normalized.setdefault("allowlist", [])
        normalized.setdefault("capability_contract_digest", None)
        normalized.setdefault("successor_of", None)
        normalized.setdefault("route_digest", None)
        normalized.setdefault("model", None)
        normalized.setdefault("reasoning", None)
        normalized.setdefault("binding_intent", None)
        normalized.setdefault("binding_observation", None)
        normalized.setdefault("binding_state", "blocked_unknown")
        normalized.setdefault("business_write_allowed", False)
        if normalized["binding_state"] == "binding_verified":
            normalized["binding_state"] = "blocked_unknown"
            normalized["business_write_allowed"] = False
        elif normalized["binding_state"] != "binding_verified" and normalized[
            "business_write_allowed"
        ]:
            normalized["business_write_allowed"] = False
        return cls(**normalized)


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
                if (
                    current.composite_identity != persistent.composite_identity
                    and (
                        current.run_id == persistent.run_id
                        or current.status not in _TERMINAL_STATUSES
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


def validate_binding(
    intent: BindingIntent, observation: BindingObservation
) -> BindingVerification:
    """Pure fail-closed gate for supervisor-owned provider task bindings.

    Provider-private lease metadata is intentionally not required.  The lease
    must instead be an active local supervisor observation, while the cursor
    must come from a current ``wait_threads`` observation.
    """

    missing: List[str] = []
    conflicts: List[str] = []
    if not isinstance(intent, BindingIntent) or not isinstance(observation, BindingObservation):
        return BindingVerification("blocked_unknown", False, ["binding_observation"], [])

    for field in ("node_id", "lease_id", "head_sha", "clean", "cursor"):
        if getattr(intent, field, None) in (None, ""):
            missing.append(field)

    for field in (
        "project_id",
        "task_id",
        "node_id",
        "host_id",
        "worktree",
        "managed_root",
        "branch",
        "base_sha",
    ):
        observed = getattr(observation, field)
        expected = getattr(intent, field)
        if observed in (None, ""):
            missing.append(field)
        elif observed != expected:
            conflicts.append(field)

    if observation.head_sha in (None, ""):
        missing.append("head_sha")
    elif intent.head_sha is not None and observation.head_sha != intent.head_sha:
        conflicts.append("head_sha")
    if type(observation.clean) is not bool:
        missing.append("clean")
    elif not observation.clean:
        conflicts.append("clean")
    elif intent.clean is not None and observation.clean != intent.clean:
        conflicts.append("clean")

    lease = observation.lease
    if not isinstance(lease, SupervisorLeaseObservation):
        missing.append("lease")
    else:
        expected_lease_id = supervisor_lease_id(
            lease.node_id, lease.worktree, lease.run_id
        )
        if (
            not lease.active
            or lease.status != "active"
            or lease.schema_version != LEASE_SCHEMA_VERSION
            or lease.node_id != intent.node_id
            or lease.worktree != intent.worktree
            or lease.lease_id != expected_lease_id
        ):
            conflicts.append("lease")
        if intent.lease_id is not None and lease.lease_id != intent.lease_id:
            conflicts.append("lease")
        if intent.lease is not None and lease.to_dict() != intent.lease:
            conflicts.append("lease")

    if observation.cursor in (None, ""):
        missing.append("cursor")
    elif observation.source != "codex_app__wait_threads":
        conflicts.append("cursor")
    elif not isinstance(observation.cursor_observation, WaitThreadsCursorObservation):
        conflicts.append("cursor")
    elif (
        observation.cursor_source != "codex_app__wait_threads"
        or observation.cursor_task_id != observation.task_id
        or observation.cursor_host_id != observation.host_id
        or observation.cursor_lineage != observation.cursor_observation.lineage
        or observation.cursor_observation.task_id != observation.task_id
        or observation.cursor_observation.host_id != observation.host_id
        or observation.cursor_observation.cursor != observation.cursor
    ):
        conflicts.append("cursor")
    elif observation.cursor != intent.cursor:
        conflicts.append("cursor")

    # Preserve stable ordering for JSON evidence and deterministic tests.
    missing = list(dict.fromkeys(missing))
    conflicts = list(dict.fromkeys(conflicts))
    verified = not missing and not conflicts
    return BindingVerification(
        "binding_verified" if verified else "blocked_unknown",
        verified,
        missing,
        conflicts,
    )


def binding_contract_enabled(contract: Any) -> bool:
    """Return whether a contract opted into the V3.9 runtime binding gate.

    Legacy contracts deliberately remain untouched.  V3.9 callers may use the
    explicit version flag or the equivalent boolean used by early revisions.
    The presence of structured evidence also opts in, while a plain mapping is
    never accepted as evidence by :func:`runtime_binding_gate`.
    """
    if not isinstance(contract, dict):
        return False
    version = contract.get("binding_contract_version")
    if isinstance(version, (int, str)) and version in {39, "39", "3.9", "v3.9", "V3.9"}:
        return True
    return bool(
        contract.get("v39_binding") is True
        or contract.get("binding_required") is True
        or "binding_intent" in contract
        or "binding_observation" in contract
    )


def runtime_binding_gate(
    contract: Any, binding: Any = None
) -> BindingVerification:
    """Evaluate the V3.9 gate using only protected provenance objects.

    A caller may pass evidence on the live ``TaskBinding`` or directly on the
    runtime contract.  JSON dictionaries are intentionally rejected: persisted
    records do not carry the private provenance token issued by the lease and
    wait_threads readers.
    """
    if not binding_contract_enabled(contract):
        return BindingVerification("binding_verified", True, [], [])
    binding_intent = getattr(binding, "binding_intent", None)
    binding_observation = getattr(binding, "binding_observation", None)
    contract_intent = contract.get("binding_intent") if isinstance(contract, dict) else None
    contract_observation = contract.get("binding_observation") if isinstance(contract, dict) else None
    # Contract and binding evidence must describe the same live observation;
    # persisted JSON/dict values never count as provenance.
    if contract_intent is not None and not isinstance(contract_intent, BindingIntent):
        return BindingVerification("blocked_unknown", False, ["binding_provenance"], [])
    if contract_observation is not None and not isinstance(contract_observation, BindingObservation):
        return BindingVerification("blocked_unknown", False, ["binding_provenance"], [])
    if binding_intent is not None and contract_intent is not None and binding_intent != contract_intent:
        return BindingVerification("blocked_unknown", False, [], ["binding_intent"])
    if binding_observation is not None and contract_observation is not None and binding_observation != contract_observation:
        return BindingVerification("blocked_unknown", False, [], ["binding_observation"])
    intent = binding_intent if binding_intent is not None else contract_intent
    observation = binding_observation if binding_observation is not None else contract_observation
    if not isinstance(intent, BindingIntent) or not isinstance(observation, BindingObservation):
        return BindingVerification("blocked_unknown", False, ["binding_provenance"], [])
    verification = validate_binding(intent, observation)
    extra_conflicts = []
    if binding is not None:
        for attr, field in (("task_id", "task_id"), ("host", "host_id"), ("worktree", "worktree"), ("branch", "branch")):
            actual = getattr(binding, attr, None)
            expected = getattr(intent, field, None)
            if actual not in (None, "") and actual != expected:
                extra_conflicts.append(field)
    if isinstance(contract, dict):
        # ``task_id`` in a monitor contract is the logical Issue identity
        # before provider creation; the provider task id is checked through
        # the binding itself.  Do not compare those two namespaces.
        for key, field in (("project_id", "project_id"), ("host_id", "host_id"), ("worktree", "worktree"), ("managed_root", "managed_root"), ("branch", "branch"), ("base_sha", "base_sha")):
            expected = contract.get(key)
            if expected not in (None, "") and expected != getattr(intent, field, None):
                extra_conflicts.append(field)
        provider_task_id = contract.get("provider_task_id")
        contract_task_id = contract.get("task_id")
        if provider_task_id in (None, "") and contract_task_id not in (
            None,
            "",
            contract.get("node_id"),
            "{}:{}".format(contract.get("role"), contract.get("node_id")),
        ):
            provider_task_id = contract_task_id
        if provider_task_id not in (None, "") and provider_task_id != intent.task_id:
            extra_conflicts.append("task_id")
        for key, observed_field in (("head_sha", "head_sha"), ("clean", "clean"), ("cursor", "cursor")):
            expected = contract.get(key)
            if expected not in (None, "") and expected != getattr(observation, observed_field, None):
                extra_conflicts.append(key)
        if "lease" in contract and contract.get("lease") != getattr(observation, "lease", None):
            extra_conflicts.append("lease")
    if extra_conflicts:
        verification = BindingVerification(
            "blocked_unknown", False, list(verification.missing),
            list(dict.fromkeys(verification.conflicts + extra_conflicts)),
        )
    if verification.verified and binding is not None:
        # TaskBinding is mutable even though its evidence records are frozen;
        # retain the live proof for the immediate start/write decision.
        try:
            binding.binding_intent = intent
            binding.binding_observation = observation
            binding.binding_state = verification.binding_state
            binding.business_write_allowed = verification.business_write_allowed
        except (AttributeError, TypeError):
            pass
    return verification


verify_binding = validate_binding
validate_task_binding = validate_binding
verify_runtime_binding = runtime_binding_gate
require_runtime_binding = runtime_binding_gate
