"""Platform-neutral task/session bridges.

The public Codex App calls are injected into :class:`CodexAppBridge`; this
package never imports or fabricates the desktop tool implementation.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

try:  # The V3 topology module is also usable in a minimal source checkout.
    from ..paths import ProjectPaths
except ImportError:  # pragma: no cover - full package installs provide paths
    ProjectPaths = Any
try:
    from ..workflow_gate import require_capability_contract, require_entry, require_child_origin
except ImportError:  # pragma: no cover
    def require_capability_contract(paths):
        raise ValueError("capability contract is unavailable")
    def require_entry(*args, **kwargs):
        return None
    def require_child_origin(origin):
        if origin != "worker_dispatch":
            raise PermissionError("invalid child origin")
try:
    from ..diagnostics import validate_child_session_binding
except ImportError:  # pragma: no cover - optional V2 diagnostics
    def validate_child_session_binding(*args, **kwargs):
        return None
from ..models import WorkerProfile


class ProviderUnavailable(RuntimeError):
    """A provider cannot safely create or continue a task."""


class ProviderPending(RuntimeError):
    """A provider returned a pending task handle without a real task id."""


_PROVIDER_ACTIONS = {"create", "locate", "visibility", "resume", "wait"}
_OBSERVED_PROVIDER_ROUTES = {}


def observed_provider_route(task_id):
    """Return the route observed in the latest provider create result."""
    return _OBSERVED_PROVIDER_ROUTES.get(task_id)
PROVIDER_ADAPTER_IDS = {
    "codex-app-visible": "codex", "claude-code-visible": "claude-code",
    "cursor-visible": "cursor", "grok-visible": "grok",
    "workbuddy-visible": "workbuddy", "kimi-code-visible": "kimi-code",
    "deepseek-harness-visible": "deepseek-harness",
}
CAPABILITY_OPERATIONS = ("create", "enter", "resume", "wait", "terminal", "mailbox")
_OBSERVATION_STATUSES = {
    "verified_available", "not_exposed", "permission_denied", "probe_failed",
    "unknown_timeout", "unknown", "stale",
}


def _utc(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _parse_time(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s is required" % field)
    try:
        return _utc(datetime.fromisoformat(value))
    except ValueError as exc:
        raise ValueError("%s is not a valid timestamp" % field) from exc


@dataclass(frozen=True)
class CapabilityObservation:
    """Structured observation returned by a real provider probe."""
    name: str
    status: str
    evidence_ref: str
    observed_at: str
    expires_at: str
    source: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.name not in CAPABILITY_OPERATIONS:
            raise ValueError("unsupported capability observation")
        if self.status not in _OBSERVATION_STATUSES:
            raise ValueError("unsupported capability observation status")
        for field_name in ("evidence_ref", "source"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError("%s is required" % field_name)
        if self.evidence_ref.lower().startswith(("agent:", "self:", "readme:")) or self.source.lower().startswith(("agent:", "self:", "readme:")):
            raise ValueError("agent self-report is not evidence")
        observed = _parse_time(self.observed_at, "observed_at")
        expires = _parse_time(self.expires_at, "expires_at")
        if expires <= observed:
            raise ValueError("observation expires_at must be after observed_at")
        if not isinstance(self.payload, Mapping):
            raise ValueError("observation payload must be an object")

    def to_dict(self):
        return {
            "name": self.name,
            "status": self.status,
            "evidence_ref": self.evidence_ref,
            "observed_at": _parse_time(self.observed_at, "observed_at").isoformat(),
            "expires_at": _parse_time(self.expires_at, "expires_at").isoformat(),
            "source": self.source,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class CapabilityEvaluation:
    provider: str
    host_id: str
    status: str
    capabilities: Mapping[str, CapabilityObservation]
    checked_at: str
    expires_at: str
    evidence_refs: Tuple[str, ...] = ()
    remediation: Tuple[str, ...] = ()
    attempts: int = 0
    failure_observation: Optional[Mapping[str, Any]] = None
    last_operation: Optional[str] = None
    governance_action: Optional[str] = None
    next_action: Optional[str] = None
    recovery_entry: Optional[str] = None

    def to_dict(self):
        return {
            "schema_version": 2,
            "provider": self.provider,
            "host_id": self.host_id,
            "status": self.status,
            "checked_at": self.checked_at,
            "expires_at": self.expires_at,
            "capabilities": {name: value.to_dict() for name, value in sorted(self.capabilities.items())},
            "evidence_refs": list(self.evidence_refs),
            "remediation": list(self.remediation),
            "attempts": self.attempts,
            "failure_observation": dict(self.failure_observation or {}),
            "last_operation": self.last_operation,
            "governance_action": self.governance_action,
            "next_action": self.next_action,
            "recovery_entry": self.recovery_entry,
        }


def validate_mailbox_evidence(observations: Mapping[str, Any], *, now=None, required=CAPABILITY_OPERATIONS):
    if not isinstance(observations, Mapping):
        raise ValueError("mailbox evidence must be an object")
    current = _utc(now)
    normalized = {}
    for name in required:
        if name not in observations:
            raise ValueError("mailbox evidence missing capability: %s" % name)
        raw = observations[name]
        if isinstance(raw, CapabilityObservation):
            item = raw
        elif isinstance(raw, Mapping):
            values = dict(raw)
            supplied_name = values.pop("name", name)
            if supplied_name != name:
                raise ValueError("mailbox evidence capability name mismatch")
            try:
                item = CapabilityObservation(name=name, **values)
            except TypeError as exc:
                raise ValueError("mailbox evidence is incomplete for %s" % name) from exc
        else:
            raise ValueError("mailbox evidence must be structured")
        if _parse_time(item.expires_at, "expires_at") <= current:
            item = CapabilityObservation(item.name, "stale", item.evidence_ref, item.observed_at, item.expires_at, item.source, item.payload)
        normalized[name] = item
    return normalized


def evaluate_provider_capabilities(provider: str, host_id: str, observations: Mapping[str, Any], *, now=None, attempts=0, max_attempts=2, required=CAPABILITY_OPERATIONS):
    if not isinstance(provider, str) or not provider.strip() or not isinstance(host_id, str) or not host_id.strip():
        raise ValueError("provider and host_id are required")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ValueError("attempts must be a non-negative integer")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    normalized = validate_mailbox_evidence(observations, now=now, required=required)
    current = _utc(now)
    bad = {name: item for name, item in normalized.items() if item.status != "verified_available"}
    status = "verified_available" if not bad else ("blocked_unknown" if attempts >= max_attempts else "retry_pending")
    remediation = () if not bad else tuple("refresh real provider evidence for %s" % name for name in sorted(bad))
    expiry = min(_parse_time(item.expires_at, "expires_at") for item in normalized.values())
    first_bad = normalized[sorted(bad)[0]] if bad else None
    return CapabilityEvaluation(
        provider, host_id, status, normalized, current.isoformat(), expiry.isoformat(),
        tuple(item.evidence_ref for item in normalized.values()), remediation, attempts,
        failure_observation=first_bad.to_dict() if first_bad else None,
        last_operation=first_bad.name if first_bad else None,
        governance_action="bounded provider capability refresh" if bad else None,
        next_action=("retry provider capability probe" if status == "retry_pending" else "repair provider evidence and resume from recovery entry") if bad else None,
        recovery_entry="provider-capability-refresh" if bad else None,
    )


ProviderCapabilityObservation = CapabilityObservation
MailboxEvidence = CapabilityObservation
build_capability_evidence = evaluate_provider_capabilities
classify_provider_capabilities = evaluate_provider_capabilities


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


class ProviderActionStore:
    """Bounded request/result mailbox between the CLI and a desktop session."""

    schema_version = 1

    def __init__(self, paths: ProjectPaths):
        self.paths = paths
        self.root = paths.resolve_vibe_path("provider-actions")

    def _directory(self, name: str) -> Path:
        directory = self.root / name
        if self.root.is_symlink() or directory.is_symlink():
            raise ValueError("provider action path may not be a symlink")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def _atomic(path: Path, payload: Dict[str, Any]) -> None:
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("provider action path may not be a symlink")
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > 64 * 1024:
            raise ValueError("provider action record exceeds the size bound")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="." + path.name + ".", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(str(temporary), str(path))
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _read(path: Path) -> Dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise ValueError("provider action record must be a regular file")
        raw = path.read_bytes()
        if len(raw) > 64 * 1024:
            raise ValueError("provider action record exceeds the size bound")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("provider action record is invalid") from error
        if not isinstance(value, dict):
            raise ValueError("provider action record must be an object")
        return value

    def publish_capabilities(
        self,
        adapter_id: str,
        facts: Mapping[str, Any],
        provenance: str,
    ) -> None:
        if not isinstance(adapter_id, str) or not adapter_id:
            raise ValueError("adapter id is required")
        if (
            not isinstance(facts, Mapping)
            or not facts
            or any(
                not isinstance(key, str)
                or not key.startswith(adapter_id + ".")
                or type(value) is not bool
                for key, value in facts.items()
            )
        ):
            raise ValueError("provider capability facts are invalid")
        if not isinstance(provenance, str) or not provenance:
            raise ValueError("provider capability provenance is required")
        self.root.mkdir(parents=True, exist_ok=True)
        self._atomic(
            self.root / "capabilities.json",
            {
                "schema_version": self.schema_version,
                "adapter_id": adapter_id,
                "facts": dict(facts),
                "provenance": provenance,
            },
        )

    def capabilities(self) -> Dict[str, Any]:
        legacy_path = self.root / "capabilities.json"
        if legacy_path.exists():
            value = self._read(legacy_path)
        else:
            v3_path = self.root / "capabilities-v2.json"
            if not v3_path.is_file():
                raise ValueError("provider capability record is missing")
            v3 = self._read(v3_path)
            capabilities = v3.get("capabilities", {})
            if not isinstance(capabilities, dict):
                raise ValueError("provider capability evidence is invalid")
            adapter = PROVIDER_ADAPTER_IDS.get(str(v3.get("provider", "")))
            if not adapter:
                raise ValueError("provider has no explicit adapter mapping")
            value = {
                "schema_version": self.schema_version,
                "adapter_id": adapter,
                "facts": {
                    "%s.%s" % (adapter, name): item.get("status") == "verified_available"
                    for name, item in capabilities.items() if isinstance(item, dict)
                },
                "provenance": ";".join(
                    str(item.get("evidence_ref", "")) for item in capabilities.values()
                    if isinstance(item, dict)
                ),
            }
        if set(value) != {"schema_version", "adapter_id", "facts", "provenance"}:
            raise ValueError("provider capability schema is invalid")
        if value["schema_version"] != self.schema_version:
            raise ValueError("provider capability schema is unsupported")
        return value

    def publish_capability_observations(
        self, provider: str, host_id: str, observations: Mapping[str, Any],
        *, attempts: int = 0, max_attempts: int = 2, now=None,
    ) -> CapabilityEvaluation:
        evaluation = evaluate_provider_capabilities(
            provider, host_id, observations, now=now,
            attempts=attempts, max_attempts=max_attempts,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self._atomic(self.root / "capabilities-v2.json", evaluation.to_dict())
        return evaluation

    def capability_evaluation(self, *, now=None) -> CapabilityEvaluation:
        value = self._read(self.root / "capabilities-v2.json")
        if value.get("schema_version") != 2:
            raise ValueError("provider capability evidence is not a V3 structured record")
        raw = value.get("capabilities", {})
        if not isinstance(raw, dict):
            raise ValueError("provider capability evidence is invalid")
        if raw:
            capabilities = validate_mailbox_evidence(raw, now=now)
        else:
            capabilities = {}
        status = value.get("status", "blocked_unknown")
        if capabilities and any(item.status != "verified_available" for item in capabilities.values()) and status != "blocked_unknown":
            status = "retry_pending"
        return CapabilityEvaluation(
            provider=value.get("provider", ""), host_id=value.get("host_id", ""),
            status=status, capabilities=capabilities,
            checked_at=value.get("checked_at", ""), expires_at=value.get("expires_at", ""),
            evidence_refs=tuple(value.get("evidence_refs", ())),
            remediation=tuple(value.get("remediation", ())), attempts=int(value.get("attempts", 0)),
            failure_observation=value.get("failure_observation"),
            last_operation=value.get("last_operation"), governance_action=value.get("governance_action"),
            next_action=value.get("next_action"), recovery_entry=value.get("recovery_entry"),
        )

    def request(
        self,
        *,
        operation: str,
        provider: str,
        run_id: str,
        issue_id: str,
        role: str,
        generation: int,
        native_tool: str,
        request: Dict[str, Any],
        sequence: int = 0,
    ) -> Dict[str, Any]:
        state = self.paths.vibe / "state.json"
        if state.is_file():
            try:
                origin = request.get("origin", "user_entry") if isinstance(request, dict) else "user_entry"
                if origin == "worker_dispatch":
                    require_child_origin(origin)
                    capability_contract = require_capability_contract(self.paths)
                    binding = request.get("child_binding")
                    required = {"parent_run_id", "plan_revision", "authorization_digest", "node_id", "role", "writer", "worktree", "branch", "allowlist", "worker_profile", "capability_contract_digest"}
                    if not isinstance(binding, dict) or not required.issubset(binding):
                        raise PermissionError("child binding is incomplete")
                    if binding.get("capability_contract_digest") != capability_contract.contract_digest:
                        raise PermissionError("child capability contract digest mismatch")
                    if binding.get("parent_run_id") != run_id or binding.get("node_id") != issue_id or binding.get("role") != role:
                        raise PermissionError("child binding identity mismatch")
                    if not (isinstance(binding.get("plan_revision"), str) and binding["plan_revision"].isdigit() and int(binding["plan_revision"]) > 0 and isinstance(binding.get("authorization_digest"), str) and len(binding["authorization_digest"]) == 64 and all(ch in "0123456789abcdef" for ch in binding["authorization_digest"].lower())):
                        raise PermissionError("child binding parent context is unverifiable")
                    profile = WorkerProfile(**binding["worker_profile"])
                    validate_child_session_binding(binding["parent_run_id"], binding["plan_revision"], binding["authorization_digest"], binding["node_id"], binding["role"], profile)
                else:
                    require_entry(self.paths, "provider:" + run_id + ":" + operation, operation)
            except (OSError, ValueError, TypeError, PermissionError) as error:
                raise ProviderUnavailable("session_gate_blocked") from error
        elif self.paths.vibe.exists():
            raise ProviderUnavailable("session_gate_blocked")
        if operation not in _PROVIDER_ACTIONS:
            raise ValueError("provider action is unsupported")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("provider action sequence is invalid")
        basis = {
            "operation": operation,
            "provider": provider,
            "run_id": run_id,
            "issue_id": issue_id,
            "role": role,
            "generation": generation,
            "sequence": sequence,
        }
        action_id = "action-" + _canonical_digest(basis)[:32]
        payload = {
            "schema_version": self.schema_version,
            "action_id": action_id,
            **basis,
            "native_tool": native_tool,
            "request": request,
        }
        payload["request_digest"] = _canonical_digest(payload)
        directory = self._directory("requests")
        path = directory / (action_id + ".json")
        if path.exists():
            existing = self._read(path)
            if existing != payload:
                raise ValueError("provider action request identity drift")
        else:
            self._atomic(path, payload)
        return payload

    def result(self, action_id: str) -> Optional[Dict[str, Any]]:
        path = self._directory("results") / (action_id + ".json")
        if not path.exists():
            return None
        result = self._read(path)
        request = self._read(
            self._directory("requests") / (action_id + ".json")
        )
        if (
            set(result)
            != {"schema_version", "action_id", "request_digest", "payload"}
            or result["schema_version"] != self.schema_version
            or result["action_id"] != action_id
            or result["request_digest"] != request["request_digest"]
            or not isinstance(result["payload"], dict)
        ):
            raise ValueError("provider action result is not bound to its request")
        payload = result["payload"]
        binding = payload.get("binding") if isinstance(payload, dict) else None
        if isinstance(binding, dict):
            aliases = [binding.get("threadId"), binding.get("task_id")]
            aliases = [value for value in aliases if value not in (None, "")]
            if len(set(aliases)) > 1:
                raise ValueError("provider task identity aliases disagree")
            host_aliases = [binding.get("hostId"), binding.get("host")]
            host_aliases = [value for value in host_aliases if value not in (None, "")]
            if len(set(host_aliases)) > 1:
                raise ValueError("provider host identity aliases disagree")
            task_id = aliases[0] if aliases else None
            route = {
                "host": host_aliases[0] if host_aliases else None,
                "worktree": binding.get("worktree"),
                "branch": binding.get("branch"),
            }
            if isinstance(task_id, str):
                if any(not isinstance(value, str) or not value.strip() for value in route.values()):
                    raise ValueError("provider route binding is incomplete")
                _OBSERVED_PROVIDER_ROUTES[task_id] = route
        return payload

    def complete(self, action_id: str, payload: Dict[str, Any]) -> None:
        request = self._read(
            self._directory("requests") / (action_id + ".json")
        )
        if not isinstance(payload, dict):
            raise ValueError("provider action result payload must be an object")
        self._atomic(
            self._directory("results") / (action_id + ".json"),
            {
                "schema_version": self.schema_version,
                "action_id": action_id,
                "request_digest": request["request_digest"],
                "payload": payload,
            },
        )

    def pending(self) -> List[Dict[str, Any]]:
        request_dir = self._directory("requests")
        result_dir = self._directory("results")
        result = []
        for path in sorted(request_dir.glob("action-*.json")):
            if not (result_dir / path.name).exists():
                result.append(self._read(path))
        return result


@dataclass(frozen=True)
class RepositoryTaskRouting:
    """Confirmed repository and host context for public task creation."""

    project_id: str
    host_id: str
    environment: str
    worktree: str
    branch: str

    def __post_init__(self):
        for name in ("project_id", "host_id", "worktree", "branch"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError("repository routing %s is required" % name)
        if self.environment not in {"worktree", "local"}:
            raise ValueError("repository routing environment must be worktree or local")

    def target(self):
        environment = {"type": self.environment}
        if self.environment == "worktree":
            environment["startingState"] = {
                "type": "branch",
                "branchName": self.branch,
            }
        return {
            "type": "project",
            "projectId": self.project_id,
            "environment": environment,
        }


@dataclass(frozen=True)
class BackgroundTaskRouting:
    """Immutable authorized routing expected from a background launcher."""

    host: str
    worktree: str
    branch: str
    status_file: str
    handoff_file: str

    def __post_init__(self):
        for name in ("host", "worktree", "branch", "status_file", "handoff_file"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("background routing %s is required" % name)

    def matches(self, binding: "TaskBinding") -> bool:
        return all(
            getattr(binding, name) == getattr(self, name)
            for name in ("host", "worktree", "branch", "status_file", "handoff_file")
        )


@dataclass(frozen=True)
class TaskBinding:
    provider: str
    mode: str
    role: str
    issue_id: str
    task_id: Optional[str] = None
    host: Optional[str] = None
    worktree: Optional[str] = None
    branch: Optional[str] = None
    status_file: Optional[str] = None
    handoff_file: Optional[str] = None
    cursor: Optional[str] = None
    client_thread_id: Optional[str] = None
    visible: Optional[bool] = None
    limitations: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider is required")
        if self.mode not in {"visible", "background"}:
            raise ValueError("task mode must be visible or background")
        if self.role not in {"developer", "reviewer"}:
            raise ValueError("task role must be developer or reviewer")
        if not isinstance(self.issue_id, str) or not self.issue_id.strip():
            raise ValueError("issue_id is required")
        if self.mode == "visible" and not self.task_id and not self.client_thread_id:
            raise ValueError("visible task binding requires threadId or clientThreadId")
        expected_visible = self.mode == "visible" and bool(self.task_id)
        if self.visible is not None and self.visible != expected_visible:
            raise ValueError("task visibility disagrees with mode")
        object.__setattr__(self, "visible", expected_visible)
        if self.provider == "codex-app-visible" and self.task_id and not self.host:
            raise ValueError("visible Codex task binding requires hostId")

    @property
    def thread_id(self):
        return self.task_id if self.provider == "codex-app-visible" else None

    @property
    def host_id(self):
        return self.host if self.provider == "codex-app-visible" else None

    @property
    def pending(self):
        return bool(self.client_thread_id and not self.task_id)

    def to_dict(self):
        result = self.__dict__.copy()
        result["limitations"] = list(self.limitations)
        if self.provider == "codex-app-visible" and self.task_id:
            result["threadId"] = self.task_id
            result["hostId"] = self.host
        return result


@dataclass(frozen=True)
class TaskUpdate:
    cursor: Optional[str]
    status: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def token(self):
        return self.cursor


@dataclass(frozen=True)
class VisibilityResult:
    visible: bool
    direct_enter: bool
    limitations: Tuple[str, ...] = ()
    source: Optional[str] = None

    def to_dict(self):
        return {
            "visible": self.visible,
            "direct_enter": self.direct_enter,
            "limitations": list(self.limitations),
            "source": self.source,
        }


class CodexAppBridge:
    """Exact structured wrapper around the public Codex App tool schemas."""

    def __init__(
        self,
        *,
        create_thread: Callable[[Mapping[str, Any]], Any],
        navigate_to_codex_page: Callable[[Mapping[str, Any]], Any],
        send_message_to_thread: Callable[[Mapping[str, Any]], Any],
        wait_threads: Callable[[Mapping[str, Any]], Any],
        list_threads: Optional[Callable[[Mapping[str, Any]], Any]] = None,
    ):
        self._create_thread = create_thread
        self._navigate = navigate_to_codex_page
        self._send = send_message_to_thread
        self._wait = wait_threads
        self._list = list_threads

    def create(self, request: Mapping[str, Any]):
        _require_keys(request, ("prompt", "target"), "create_thread")
        if not isinstance(request["prompt"], str) or not isinstance(request["target"], Mapping):
            raise ProviderUnavailable("Codex create_thread request has invalid types")
        return self._create_thread(dict(request))

    def enter_or_locate(self, binding: TaskBinding):
        self._require_real(binding)
        return self._navigate({"threadId": binding.task_id})

    def resume(self, binding: TaskBinding, prompt: str):
        self._require_real(binding)
        request = {"threadId": binding.task_id, "prompt": prompt}
        if binding.host:
            request["hostId"] = binding.host
        return self._send(request)

    def wait(self, binding: TaskBinding, cursor: Optional[str], timeout_ms: int = 120000):
        self._require_real(binding)
        target = {"threadId": binding.task_id}
        if binding.host:
            target["hostId"] = binding.host
        if cursor:
            target["afterCursor"] = cursor
        raw = self._wait({"targets": [target], "timeoutMs": timeout_ms})
        if isinstance(raw, TaskUpdate):
            return raw
        if not isinstance(raw, Mapping):
            raise ProviderUnavailable("Codex wait_threads returned invalid result")
        polls = raw.get("polls")
        if not isinstance(polls, list):
            raise ProviderUnavailable("Codex wait_threads result has no polls list")
        poll = next(
            (
                item for item in polls
                if isinstance(item, Mapping)
                and isinstance(item.get("thread"), Mapping)
                and item["thread"].get("id") == binding.task_id
                and (not binding.host or item["thread"].get("hostId") == binding.host)
            ),
            None,
        )
        if poll is None:
            raise ProviderUnavailable("Codex wait_threads result has no matching poll")
        latest_turn = poll.get("latestTurn")
        turn_error = latest_turn.get("error") if isinstance(latest_turn, Mapping) else None
        if turn_error:
            status = "error"
        elif poll.get("timedOut") or raw.get("timedOut"):
            status = "timeout"
        else:
            status = _poll_status(poll)
        return TaskUpdate(
            cursor=poll.get("cursor") or cursor,
            status=status,
            payload=dict(poll),
        )

    def visibility(self, binding: TaskBinding):
        if binding.pending:
            return VisibilityResult(False, False, ("任务创建中",), "clientThreadId")
        if not binding.task_id or self._list is None:
            raise ProviderUnavailable("Codex list_threads visibility probe is unavailable")
        raw = self._list({"limit": 100})
        entries = ()
        if isinstance(raw, Mapping):
            entries = tuple(raw.get("pinnedThreads", ())) + tuple(raw.get("threads", ()))
        found = any(
            isinstance(item, Mapping)
            and item.get("id") == binding.task_id
            and (not binding.host or item.get("hostId") in (None, binding.host))
            for item in entries
        )
        return VisibilityResult(found, found, () if found else ("任务未出现在列表",), "list_threads")

    def resolve_pending(self, binding: TaskBinding):
        if not binding.pending:
            return binding
        raise ProviderPending(
            "clientThreadId remains pending; no public client-to-thread mapping is verified"
        )

    @staticmethod
    def _require_real(binding: TaskBinding):
        if binding.pending:
            raise ProviderPending("clientThreadId is pending and is not a threadId")
        if not binding.task_id:
            raise ProviderUnavailable("Codex binding has no threadId")


class VisibleTaskProvider:
    """Provider for user-visible tasks with exact identity and cursor data."""

    mode = "visible"

    def __init__(
        self,
        provider: str,
        bridge: Any = None,
        prompt_factory: Optional[Callable[[str, str, Path], str]] = None,
        routing: Optional[RepositoryTaskRouting] = None,
    ):
        self.provider = provider
        self.bridge = bridge
        self.prompt_factory = prompt_factory
        self.routing = routing

    def create(self, role: str, issue_id: str, contract_path: Path) -> TaskBinding:
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        if self.provider != "codex-app-visible":
            raise ProviderUnavailable("visible provider must be codex-app-visible")
        if not isinstance(self.routing, RepositoryTaskRouting):
            raise ProviderUnavailable("confirmed repository task routing is required")
        if self.routing.environment != "worktree":
            raise ProviderUnavailable("visible task routing must target a worktree")
        if not self.routing.project_id.strip():
            raise ProviderUnavailable("visible task routing requires a project")
        contract_path = Path(contract_path)
        prompt = (
            self.prompt_factory(role, issue_id, contract_path)
            if self.prompt_factory
            else "请执行 %s 任务 %s，合同：%s。" % (role, issue_id, contract_path)
        )
        if isinstance(self.bridge, CodexAppBridge):
            raw = self.bridge.create({"prompt": prompt, "target": self.routing.target()})
        else:
            method = getattr(self.bridge, "create", None)
            if not callable(method):
                raise ProviderUnavailable("verified visible bridge create is missing")
            raw = method(role, issue_id, contract_path)
        binding = self._binding(raw, role, issue_id)
        if not binding.task_id and not binding.client_thread_id:
            raise ProviderUnavailable("visible task creation returned no durable task identity")
        return binding

    def enter_or_locate(self, binding: TaskBinding) -> None:
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        if isinstance(self.bridge, CodexAppBridge):
            self.bridge.enter_or_locate(binding)
            return
        self._invoke(binding, "enter_or_locate")

    def resume(self, binding: TaskBinding, contract_path: Path) -> None:
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        contract_path = Path(contract_path)
        if isinstance(self.bridge, CodexAppBridge):
            self.bridge.resume(binding, "请继续处理合同：%s。" % contract_path)
            return
        self._invoke(binding, "resume", contract_path)

    def wait(self, binding: TaskBinding, cursor: Optional[str] = None) -> TaskUpdate:
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        if isinstance(self.bridge, CodexAppBridge):
            return self.bridge.wait(binding, cursor)
        raw = self._invoke(binding, "wait", cursor)
        if isinstance(raw, TaskUpdate):
            return raw
        if isinstance(raw, Mapping):
            return TaskUpdate(raw.get("cursor"), str(raw.get("status", "unknown")), dict(raw))
        raise ProviderUnavailable("visible task wait returned invalid result")

    def visibility(self, binding: TaskBinding):
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        if isinstance(self.bridge, CodexAppBridge):
            return self.bridge.visibility(binding)
        raw = self._invoke(binding, "visibility")
        if isinstance(raw, VisibilityResult):
            return raw
        if isinstance(raw, Mapping):
            return VisibilityResult(bool(raw.get("visible")), bool(raw.get("direct_enter")))
        return VisibilityResult(bool(raw), bool(raw))

    def resolve_pending(self, binding: TaskBinding):
        if isinstance(self.bridge, CodexAppBridge):
            return self.bridge.resolve_pending(binding)
        return binding

    def _invoke(self, binding: TaskBinding, method_name: str, *args):
        method = getattr(self.bridge, method_name, None)
        if not callable(method):
            raise ProviderUnavailable("verified visible bridge method missing: %s" % method_name)
        return method(binding, *args)

    def _binding(self, raw: Any, role: str, issue_id: str) -> TaskBinding:
        if isinstance(raw, TaskBinding):
            if raw.provider != self.provider or raw.mode != self.mode:
                raise ProviderUnavailable("visible bridge returned mismatched provider binding")
            if raw.role != role or raw.issue_id != issue_id:
                raise ProviderUnavailable("visible bridge returned mismatched role/issue binding")
            if raw.host != self.routing.host_id or raw.worktree != self.routing.worktree or raw.branch != self.routing.branch:
                raise ProviderUnavailable("visible bridge returned mismatched repository routing")
            return raw
        if not isinstance(raw, Mapping):
            raise ProviderUnavailable("visible task creation returned invalid binding")
        task_aliases = [raw.get("task_id"), raw.get("threadId")]
        task_aliases = [value for value in task_aliases if value not in (None, "")]
        if len(set(task_aliases)) > 1:
            raise ProviderUnavailable("visible task identity aliases disagree")
        host_aliases = [raw.get("host"), raw.get("hostId")]
        host_aliases = [value for value in host_aliases if value not in (None, "")]
        if len(set(host_aliases)) > 1:
            raise ProviderUnavailable("visible task host identity aliases disagree")
        task_id = task_aliases[0] if task_aliases else None
        host = host_aliases[0] if host_aliases else None
        if not isinstance(host, str) or not host:
            raise ProviderUnavailable("visible task host is missing")
        if host != self.routing.host_id:
            raise ProviderUnavailable("visible task host does not match confirmed routing")
        worktree = raw.get("worktree")
        branch = raw.get("branch")
        if not isinstance(worktree, str) or not worktree.strip():
            raise ProviderUnavailable("visible task worktree is missing")
        if worktree != self.routing.worktree:
            raise ProviderUnavailable("visible task worktree does not match confirmed routing")
        if not isinstance(branch, str) or not branch.strip():
            raise ProviderUnavailable("visible task branch is missing")
        if branch != self.routing.branch:
            raise ProviderUnavailable("visible task branch does not match confirmed routing")
        client_thread_id = raw.get("client_thread_id") or raw.get("clientThreadId")
        return TaskBinding(
            provider=self.provider,
            mode="visible",
            role=role,
            issue_id=issue_id,
            task_id=task_id,
            host=host,
            worktree=worktree,
            branch=branch,
            status_file=raw.get("status_file"),
            handoff_file=raw.get("handoff_file"),
            cursor=raw.get("cursor"),
            client_thread_id=client_thread_id,
            visible=bool(task_id),
            limitations=("任务创建中",) if client_thread_id and not task_id else (),
        )


class BackgroundTaskProvider:
    mode = "background"

    def __init__(
        self,
        provider: str,
        launcher: Optional[Callable[..., Any]] = None,
        expected_routing: Optional[BackgroundTaskRouting] = None,
    ):
        self.provider = provider
        self.launcher = launcher
        self.expected_routing = expected_routing

    def with_routing(self, expected_routing: BackgroundTaskRouting):
        """Bind an immutable authorized route before task creation."""
        return BackgroundTaskProvider(self.provider, self.launcher, expected_routing)

    def create(self, role: str, issue_id: str, contract_path: Path) -> TaskBinding:
        if not isinstance(self.expected_routing, BackgroundTaskRouting):
            raise ProviderUnavailable("authorized background routing is required")
        launch = self.launcher
        if not callable(launch) and callable(getattr(launch, "launch", None)):
            launch = launch.launch
        if not callable(launch):
            raise ProviderUnavailable("verified background launcher is not configured")
        raw = launch(role, issue_id, Path(contract_path))
        if isinstance(raw, TaskBinding):
            binding = raw
        elif isinstance(raw, Mapping):
            task_id = raw.get("task_id") or raw.get("run_id") or raw.get("handle")
            if not task_id:
                raise ProviderUnavailable("background launcher returned no durable handle")
            returned_role = raw.get("role")
            returned_issue = raw.get("issue_id")
            if returned_role != role or returned_issue != issue_id:
                raise ProviderUnavailable("background launcher returned mismatched role/issue")
            if raw.get("provider") != self.provider or raw.get("mode") != self.mode:
                raise ProviderUnavailable("background launcher returned mismatched provider/mode")
            binding = TaskBinding(
                provider=self.provider,
                mode="background",
                role=role,
                issue_id=issue_id,
                task_id=str(task_id),
                host=raw.get("host"),
                worktree=raw.get("worktree"),
                branch=raw.get("branch"),
                status_file=raw.get("status_file"),
                handoff_file=raw.get("handoff_file"),
                cursor=raw.get("cursor"),
                visible=False,
                limitations=("不可见", "不可直接进入", "返工续接受限"),
            )
        else:
            raise ProviderUnavailable("background launcher returned invalid handle")
        if binding.provider != self.provider or binding.mode != self.mode:
            raise ProviderUnavailable("background launcher returned mismatched provider binding")
        if binding.role != role or binding.issue_id != issue_id:
            raise ProviderUnavailable("background launcher returned mismatched role/issue binding")
        if not binding.task_id:
            raise ProviderUnavailable("background launcher returned no durable handle")
        for name in ("host", "worktree", "branch", "status_file", "handoff_file"):
            if not getattr(binding, name):
                raise ProviderUnavailable("background binding missing routing field: %s" % name)
        if not self.expected_routing.matches(binding):
            raise ProviderUnavailable("background binding does not match authorized routing")
        return binding

    def resume(self, binding: TaskBinding, contract_path: Path) -> None:
        raise ProviderUnavailable("background task cannot be directly resumed")

    def enter_or_locate(self, binding: TaskBinding) -> None:
        raise ProviderUnavailable("background task cannot be directly entered")

    def wait(self, binding: TaskBinding, cursor: Optional[str] = None) -> TaskUpdate:
        raise ProviderUnavailable("background task has no visible wait cursor")

    def visibility(self, binding: TaskBinding):
        return VisibilityResult(False, False, binding.limitations, "background")


def _require_keys(value: Mapping[str, Any], keys, operation: str):
    missing = [key for key in keys if key not in value]
    if missing:
        raise ProviderUnavailable("%s request missing: %s" % (operation, ", ".join(missing)))


def _poll_status(poll: Mapping[str, Any]) -> str:
    thread = poll.get("thread")
    thread_status = thread.get("status") if isinstance(thread, Mapping) else None
    thread_status_type = (
        thread_status.get("type") if isinstance(thread_status, Mapping) else None
    )
    for value in (
        poll.get("status"),
        poll.get("latestTurn", {}).get("status") if isinstance(poll.get("latestTurn"), Mapping) else None,
        thread_status_type,
    ):
        if isinstance(value, str) and value:
            return value
    return "unknown"
