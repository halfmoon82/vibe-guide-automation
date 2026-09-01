"""JSON-safe shared data models for the guide contracts."""

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import re


_NODE_STATUSES = frozenset(
    {
        "planned",
        "ready",
        "running",
        "delivered",
        "review",
        "accepted",
        "rework",
        "blocked_design",
        "blocked_deploy",
        "blocked_unknown",
        "brief_pending",
    }
)
_PLAN_STATUSES = frozenset({"draft", "authorized", "running", "complete", "blocked", "failed"})
_CAPABILITY_LEVELS = frozenset({"guide", "background", "full"})
_DEPLOY_STATUSES = frozenset(
    {
        "deploy_planned",
        "deploy_ready",
        "deploy_running",
        "deployed",
        "blocked_deploy",
        "blocked_unknown",
        "rolled_back",
    }
)
EVIDENCE_PRIORITY = (
    "current_user",
    "approved_prd",
    "authorization",
    "issue_contract",
    "implementation",
)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("%s must be a simple identifier" % field)
    return value


def _identifier_list(values: Any, field: str) -> List[str]:
    if not isinstance(values, list):
        raise TypeError("%s must be a list" % field)
    result = [_identifier(value, "%s item" % field) for value in values]
    if len(result) != len(set(result)):
        raise ValueError("duplicate %s are not allowed" % field)
    return result


def _json_safe(value: Any) -> Any:
    """Convert supported container/path values or reject ambiguous objects."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        converted = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            converted[key] = _json_safe(item)
        return converted
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    raise TypeError("value of type %s is not JSON serializable" % type(value).__name__)


def _json_dict(instance: Any) -> Dict[str, Any]:
    return _json_safe(asdict(instance))


@dataclass
class DAGNode:
    id: str
    title: str
    depends_on: List[str]
    integration_after: List[str]
    parallel_group: Optional[str]
    contract: Dict[str, Any]
    status: str
    # Optional V2 metadata remains compatible with V1 constructors.  The DAG
    # audit accepts these fields, legacy contract keys, or worker_profile keys.
    risk_tags: List[str] = field(default_factory=list)
    writer: str = ""
    worktree: str = ""
    allowlist: List[str] = field(default_factory=list)
    owned_paths: List[str] = field(default_factory=list)
    read_paths: List[str] = field(default_factory=list)

    def __post_init__(self):
        self.id = _identifier(self.id, "node id")
        if not isinstance(self.title, str):
            raise TypeError("title must be a string")
        self.depends_on = _identifier_list(self.depends_on, "dependencies")
        self.integration_after = _identifier_list(self.integration_after, "integration dependencies")
        if self.parallel_group is not None:
            self.parallel_group = _identifier(self.parallel_group, "parallel group")
        if not isinstance(self.contract, dict):
            raise TypeError("contract must be a dictionary")
        self.contract = _json_safe(self.contract)
        if not isinstance(self.status, str) or self.status not in _NODE_STATUSES:
            raise ValueError("unsupported node status")
        if not isinstance(self.risk_tags, list) or not all(
            isinstance(item, str) and item.strip() for item in self.risk_tags
        ):
            raise TypeError("risk_tags must be a list of non-empty strings")
        if not isinstance(self.allowlist, list) or not all(
            isinstance(item, str) and item.strip() for item in self.allowlist
        ):
            raise TypeError("allowlist must be a list of non-empty strings")
        for field_name in ("owned_paths", "read_paths"):
            value = getattr(self, field_name)
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise TypeError("%s must be a list of non-empty strings" % field_name)
        for name in ("writer", "worktree"):
            if not isinstance(getattr(self, name), str):
                raise TypeError("%s must be a string" % name)

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DAGNode":
        if not isinstance(data, dict):
            raise TypeError("DAGNode data must be a dictionary")
        return cls(**data)


@dataclass
class Plan:
    plan_id: str
    version: int
    prd_path: str
    node_ids: List[str]
    status: str
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    evidence_priority: List[str] = field(
        default_factory=lambda: list(EVIDENCE_PRIORITY)
    )
    nodes: List[DAGNode] = field(default_factory=list)

    def __post_init__(self):
        self.plan_id = _identifier(self.plan_id, "plan id")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("plan version must be a positive integer")
        if not isinstance(self.prd_path, str):
            raise TypeError("prd_path must be a string")
        self.node_ids = _identifier_list(self.node_ids, "node ids")
        if not isinstance(self.status, str) or self.status not in _PLAN_STATUSES:
            raise ValueError("unsupported plan status")
        if not isinstance(self.decisions, list) or not all(
            isinstance(item, dict) for item in self.decisions
        ):
            raise TypeError("plan decisions must be a list of dictionaries")
        self.decisions = _json_safe(self.decisions)
        if self.evidence_priority != list(EVIDENCE_PRIORITY):
            raise ValueError("plan evidence priority is fixed")
        if not isinstance(self.nodes, list):
            raise TypeError("plan nodes must be a list")
        normalized_nodes = []
        for item in self.nodes:
            if isinstance(item, DAGNode):
                normalized_nodes.append(item)
            elif isinstance(item, dict):
                normalized_nodes.append(DAGNode.from_dict(item))
            else:
                raise TypeError("plan nodes must contain DAGNode values")
        self.nodes = normalized_nodes

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Plan":
        if not isinstance(data, dict):
            raise TypeError("Plan data must be a dictionary")
        return cls(**data)


@dataclass
class AgentCapabilities:
    agent_id: str
    shell: bool
    subprocess: bool
    worktree: bool
    background: bool
    session_resume: bool
    level: str

    def __post_init__(self):
        self.agent_id = _identifier(self.agent_id, "agent id")
        for field in ("shell", "subprocess", "worktree", "background", "session_resume"):
            if type(getattr(self, field)) is not bool:
                raise TypeError("%s must be a bool" % field)
        if not isinstance(self.level, str) or self.level not in _CAPABILITY_LEVELS:
            raise ValueError("unsupported capability level")

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentCapabilities":
        if not isinstance(data, dict):
            raise TypeError("AgentCapabilities data must be a dictionary")
        return cls(**data)


@dataclass(frozen=True)
class WorkerProfile:
    worker: str
    model: str
    reasoning: str
    fallbacks: List[Dict[str, Any]]
    selection_basis: Dict[str, Any]
    worktree: str = ""
    branch: str = ""
    allowlist: List[str] = field(default_factory=list)
    writer: str = ""
    route_digest: str = ""

    def __post_init__(self):
        for name in ("worker", "model", "reasoning"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError("%s must be non-empty" % name)
        if not isinstance(self.fallbacks, list) or not all(isinstance(x, dict) for x in self.fallbacks):
            raise ValueError("fallbacks must be a list of dictionaries")
        if not isinstance(self.selection_basis, dict):
            raise ValueError("selection_basis must be a dictionary")
        digest = hashlib.sha256(
            json.dumps(
                self.selection_basis,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if self.route_digest:
            if (
                not isinstance(self.route_digest, str)
                or len(self.route_digest) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in self.route_digest)
                or self.route_digest.lower() != digest
            ):
                raise ValueError("route_digest does not match selection_basis")
        object.__setattr__(self, "route_digest", digest)

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkerProfile":
        if not isinstance(data, dict):
            raise TypeError("WorkerProfile data must be a dictionary")
        return cls(**data)


def _binding_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty string" % field)
    if "\x00" in value:
        raise ValueError("%s contains NUL" % field)
    return value


def _binding_sha(value: Any, field: str) -> str:
    value = _binding_text(value, field)
    if not _SHA40.fullmatch(value):
        raise ValueError("%s must be a 40-hex SHA" % field)
    return value.lower()


_PROVENANCE_TOKEN = object()


@dataclass(frozen=True)
class SupervisorLeaseObservation:
    """In-memory lease evidence issued only by ``read_writer_lease``."""

    active: bool
    schema_version: int
    node_id: str
    worktree: str
    run_id: str
    lease_id: str
    status: str
    source: str = "supervisor.read_writer_lease"
    proof: str = "read_writer_lease"
    _provenance_token: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._provenance_token is not _PROVENANCE_TOKEN:
            raise TypeError("lease evidence must come from read_writer_lease")
        if type(self.active) is not bool or not isinstance(self.schema_version, int):
            raise TypeError("lease evidence types are invalid")
        for name in ("node_id", "worktree", "run_id", "lease_id", "status", "source", "proof"):
            object.__setattr__(self, name, _binding_text(getattr(self, name), name))
        expected = hashlib.sha256(
            (self.node_id + "\0" + self.worktree + "\0" + self.run_id).encode("utf-8")
        ).hexdigest()
        if self.lease_id != expected:
            raise ValueError("lease evidence id is invalid")
        if self.status != "active" or self.source != "supervisor.read_writer_lease" or self.proof != "read_writer_lease":
            raise ValueError("lease evidence provenance is invalid")

    @classmethod
    def _from_read(
        cls, payload: Dict[str, Any], *, _token: Any = None
    ) -> "SupervisorLeaseObservation":
        if _token is not _PROVENANCE_TOKEN:
            raise TypeError("lease evidence must be constructed by the reader")
        if not isinstance(payload, dict):
            raise TypeError("lease payload must be a dictionary")
        return cls(
            active=payload.get("active") is True,
            schema_version=payload["schema_version"],
            node_id=payload["node_id"],
            worktree=payload["worktree"],
            run_id=payload["run_id"],
            lease_id=payload["lease_id"],
            status=payload["status"],
            _provenance_token=_PROVENANCE_TOKEN,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active": self.active,
            "schema_version": self.schema_version,
            "node_id": self.node_id,
            "worktree": self.worktree,
            "run_id": self.run_id,
            "lease_id": self.lease_id,
            "status": self.status,
            "source": self.source,
            "proof": self.proof,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


@dataclass(frozen=True)
class WaitThreadsCursorObservation:
    """In-memory cursor evidence issued by a structured wait_threads poll."""

    task_id: str
    host_id: str
    cursor: str
    lineage: str
    source: str = "codex_app__wait_threads"
    _provenance_token: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._provenance_token is not _PROVENANCE_TOKEN:
            raise TypeError("cursor evidence must come from wait_threads")
        for name in ("task_id", "host_id", "cursor", "lineage", "source"):
            object.__setattr__(self, name, _binding_text(getattr(self, name), name))
        if self.source != "codex_app__wait_threads":
            raise ValueError("cursor evidence source is invalid")
        expected = "wait_threads:{}:{}:{}".format(self.task_id, self.host_id, self.cursor)
        if self.lineage != expected:
            raise ValueError("cursor evidence lineage is invalid")

    @classmethod
    def _from_wait_threads(
        cls,
        task_id: str,
        host_id: str,
        cursor: str,
        lineage: Optional[str] = None,
        *,
        _token: Any = None,
    ) -> "WaitThreadsCursorObservation":
        if _token is not _PROVENANCE_TOKEN:
            raise TypeError("cursor evidence must be constructed by wait_threads reader")
        if lineage is None:
            lineage = "wait_threads:{}:{}:{}".format(task_id, host_id, cursor)
        return cls(
            task_id=task_id,
            host_id=host_id,
            cursor=cursor,
            lineage=lineage,
            _provenance_token=_PROVENANCE_TOKEN,
        )

    @classmethod
    def from_wait_threads(
        cls, task_id: str, host_id: str, cursor: str, lineage: Optional[str] = None
    ) -> "WaitThreadsCursorObservation":
        """Create structured evidence from one provider wait_threads result."""
        return cls._from_wait_threads(
            task_id, host_id, cursor, lineage, _token=_PROVENANCE_TOKEN
        )


@dataclass(frozen=True)
class BindingIntent:
    """Supervisor-owned expected identity for one provider task.

    ``cursor`` is optional for compatibility, but a cursor in an intent never
    substitutes for the current ``wait_threads`` observation.
    """

    project_id: str
    task_id: str
    host_id: str
    worktree: str
    managed_root: str
    branch: str
    base_sha: str
    node_id: Optional[str] = None
    lease_id: Optional[str] = None
    cursor: Optional[str] = None
    head_sha: Optional[str] = None
    clean: Optional[bool] = None
    lease: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.node_id is not None:
            object.__setattr__(self, "node_id", _binding_text(self.node_id, "node_id"))
        for name in ("project_id", "task_id", "host_id", "worktree", "managed_root", "branch"):
            object.__setattr__(self, name, _binding_text(getattr(self, name), name))
        object.__setattr__(self, "base_sha", _binding_sha(self.base_sha, "base_sha"))
        for name in ("lease_id", "cursor"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _binding_text(value, name))
        if self.head_sha is not None:
            object.__setattr__(self, "head_sha", _binding_sha(self.head_sha, "head_sha"))
        if self.clean is not None and type(self.clean) is not bool:
            raise TypeError("clean must be a bool or None")
        if self.lease is not None:
            if not isinstance(self.lease, dict):
                raise TypeError("lease must be a JSON object or None")
            object.__setattr__(self, "lease", _json_safe(self.lease))

    @property
    def project(self) -> str:
        return self.project_id

    @property
    def task(self) -> str:
        return self.task_id

    @property
    def host(self) -> str:
        return self.host_id

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BindingIntent":
        if not isinstance(data, dict):
            raise TypeError("BindingIntent data must be a dictionary")
        normalized = dict(data)
        for alias, canonical in (("project", "project_id"), ("task", "task_id"), ("host", "host_id")):
            if canonical not in normalized and alias in normalized:
                normalized[canonical] = normalized.pop(alias)
        return cls(**normalized)

    from_mapping = from_dict


@dataclass(frozen=True)
class BindingObservation:
    """Observed task/provider/Git state used by the binding gate."""

    project_id: str
    task_id: str
    host_id: str
    worktree: str
    managed_root: str
    branch: str
    base_sha: str
    head_sha: str
    clean: bool
    lease: Optional[Any]
    cursor: Optional[str]
    source: str = ""
    observed_at: str = ""
    node_id: Optional[str] = None
    cursor_source: Optional[str] = None
    cursor_task_id: Optional[str] = None
    cursor_host_id: Optional[str] = None
    cursor_lineage: Optional[str] = None
    cursor_observation: Optional[WaitThreadsCursorObservation] = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self):
        if self.node_id is not None:
            object.__setattr__(self, "node_id", _binding_text(self.node_id, "node_id"))
        for name in ("project_id", "task_id", "host_id", "worktree", "managed_root", "branch"):
            object.__setattr__(self, name, _binding_text(getattr(self, name), name))
        object.__setattr__(self, "base_sha", _binding_sha(self.base_sha, "base_sha"))
        object.__setattr__(self, "head_sha", _binding_sha(self.head_sha, "head_sha"))
        if type(self.clean) is not bool:
            raise TypeError("clean must be a bool")
        if self.lease is not None and not isinstance(
            self.lease, (dict, SupervisorLeaseObservation)
        ):
            raise TypeError("lease must be a lease evidence object or JSON object")
        if isinstance(self.lease, dict):
            object.__setattr__(self, "lease", _json_safe(self.lease))
        if self.cursor is not None:
            object.__setattr__(self, "cursor", _binding_text(self.cursor, "cursor"))
        for name in ("cursor_source", "cursor_task_id", "cursor_host_id", "cursor_lineage"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _binding_text(value, name))
        if not isinstance(self.source, str) or "\x00" in self.source:
            raise TypeError("source must be a string")
        if not isinstance(self.observed_at, str) or "\x00" in self.observed_at:
            raise TypeError("observed_at must be a string")
        if self.cursor_observation is not None and not isinstance(
            self.cursor_observation, WaitThreadsCursorObservation
        ):
            raise TypeError("cursor_observation must be wait_threads evidence")

    @property
    def project(self) -> str:
        return self.project_id

    @property
    def task(self) -> str:
        return self.task_id

    @property
    def host(self) -> str:
        return self.host_id

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "project_id": self.project_id,
            "task_id": self.task_id,
            "host_id": self.host_id,
            "worktree": self.worktree,
            "managed_root": self.managed_root,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "clean": self.clean,
            "lease": self.lease.to_dict() if isinstance(self.lease, SupervisorLeaseObservation) else self.lease,
            "cursor": self.cursor,
            "source": self.source,
            "observed_at": self.observed_at,
            "node_id": self.node_id,
            "cursor_source": self.cursor_source,
            "cursor_task_id": self.cursor_task_id,
            "cursor_host_id": self.cursor_host_id,
            "cursor_lineage": self.cursor_lineage,
        }
        return _json_safe(result)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BindingObservation":
        if not isinstance(data, dict):
            raise TypeError("BindingObservation data must be a dictionary")
        normalized = dict(data)
        for alias, canonical in (("project", "project_id"), ("task", "task_id"), ("host", "host_id")):
            if canonical not in normalized and alias in normalized:
                normalized[canonical] = normalized.pop(alias)
        return cls(**normalized)

    from_mapping = from_dict


@dataclass(frozen=True)
class BindingVerification:
    binding_state: str
    business_write_allowed: bool
    missing: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.binding_state not in {"binding_verified", "blocked_unknown"}:
            raise ValueError("unsupported binding state")
        if type(self.business_write_allowed) is not bool:
            raise TypeError("business_write_allowed must be a bool")
        for name in ("missing", "conflicts"):
            values = getattr(self, name)
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise TypeError("%s must be a list of strings" % name)

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @property
    def verified(self) -> bool:
        return self.binding_state == "binding_verified" and self.business_write_allowed

    @property
    def is_verified(self) -> bool:
        return self.verified


@dataclass(frozen=True)
class IssueComplexity:
    """Evidence-bound complexity for one dispatched Issue/Spec.

    This deliberately does not include the project-level S1 score: S1 decides
    whether a request enters the complex flow, while this record is the only
    input to worker model selection.
    """

    issue_id: str
    spec_ref: str
    steps: int
    domains: int
    uncertainty: int
    failure_cost: int
    toolchain: int
    context_demand: str
    risk_tags: List[str]
    complexity_band: str
    evidence_ref: str

    def __post_init__(self):
        object.__setattr__(self, "issue_id", _identifier(self.issue_id, "issue id"))
        if not isinstance(self.spec_ref, str) or not self.spec_ref.strip():
            raise ValueError("spec_ref must be non-empty")
        for name in ("steps", "domains", "uncertainty", "failure_cost", "toolchain"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
                raise ValueError("%s must be an integer from 1 to 5" % name)
        if self.context_demand not in {"small", "medium", "large", "unknown"}:
            raise ValueError("unsupported context demand")
        if not isinstance(self.risk_tags, list) or not all(
            isinstance(tag, str) and tag.strip() for tag in self.risk_tags
        ):
            raise ValueError("risk_tags must be a list of non-empty strings")
        if len(self.risk_tags) != len(set(self.risk_tags)):
            raise ValueError("risk_tags must be unique")
        if self.complexity_band not in {"simple", "light", "complex"}:
            raise ValueError("unsupported complexity band")
        if not isinstance(self.evidence_ref, str) or not self.evidence_ref.strip():
            raise ValueError("evidence_ref must be non-empty")

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IssueComplexity":
        if not isinstance(data, dict):
            raise TypeError("IssueComplexity data must be a dictionary")
        return cls(**data)


@dataclass(frozen=True)
class LocalModel:
    """A model probe result; ``None`` means availability is unverifiable."""

    model_id: str
    capabilities: List[str]
    context_limit: int
    reasoning_levels: List[str]
    available: Optional[bool]

    def __post_init__(self):
        object.__setattr__(self, "model_id", _identifier(self.model_id, "model id"))
        if not isinstance(self.capabilities, list) or not all(
            isinstance(item, str) and item.strip() for item in self.capabilities
        ):
            raise ValueError("capabilities must be a list of non-empty strings")
        if not isinstance(self.context_limit, int) or isinstance(self.context_limit, bool) or self.context_limit <= 0:
            raise ValueError("context_limit must be positive")
        if not isinstance(self.reasoning_levels, list) or not all(
            isinstance(item, str) and item in {"normal", "deep"}
            for item in self.reasoning_levels
        ):
            raise ValueError("reasoning_levels must contain normal or deep")
        if self.available not in (True, False, None):
            raise ValueError("available must be true, false, or unknown")

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LocalModel":
        if not isinstance(data, dict):
            raise TypeError("LocalModel data must be a dictionary")
        return cls(**data)


@dataclass(frozen=True)
class DeployManifest:
    """Bounded, JSON-safe description of an explicitly selected Deploy stage."""

    target: str
    commit: str
    command_allowlist: List[str]
    health_checks: List[Dict[str, Any]]
    rollback: Dict[str, Any]
    # Optional richer release-boundary fields.  ``stop_conditions`` is checked
    # when planning, so older callers can still deserialize a five-field
    # manifest and receive a bounded blocked_deploy state.
    tree: str = ""
    config_refs: List[str] = field(default_factory=list)
    migration_steps: List[str] = field(default_factory=list)
    observation_window: str = ""
    stop_conditions: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not isinstance(self.target, str) or not self.target.strip():
            raise ValueError("deploy target must be non-empty")
        if len(self.target.strip()) > 128 or any(ch in self.target for ch in "\x00\r\n"):
            raise ValueError("deploy target is invalid")
        if not isinstance(self.commit, str) or not self.commit.strip():
            raise ValueError("deploy commit must be non-empty")
        if len(self.commit.strip()) > 128 or any(ch in self.commit for ch in "\x00\r\n"):
            raise ValueError("deploy commit is invalid")
        if not isinstance(self.command_allowlist, list) or not self.command_allowlist:
            raise ValueError("command_allowlist must be a non-empty list")
        if len(self.command_allowlist) > 32:
            raise ValueError("command_allowlist is too large")
        normalized_commands = []
        for command in self.command_allowlist:
            if not isinstance(command, str) or not command.strip():
                raise ValueError("deploy commands must be non-empty strings")
            command = command.strip()
            if len(command) > 512 or any(ch in command for ch in "\x00\r\n;`|&><") or "$ (".replace(" ", "") in command:
                raise ValueError("deploy command contains unsafe shell syntax")
            normalized_commands.append(command)
        if len(set(normalized_commands)) != len(normalized_commands):
            raise ValueError("command_allowlist contains duplicates")
        object.__setattr__(self, "command_allowlist", normalized_commands)
        if not isinstance(self.health_checks, list) or not self.health_checks:
            raise ValueError("health_checks must be a non-empty list")
        if len(self.health_checks) > 32 or not all(isinstance(item, dict) for item in self.health_checks):
            raise ValueError("health_checks must be a bounded list of objects")
        for check in self.health_checks:
            for key, value in check.items():
                normalized_key = str(key).casefold().replace("-", "_")
                if any(secret in normalized_key for secret in ("token", "password", "secret", "credential", "private_key", "api_key")):
                    raise ValueError("raw secret fields are forbidden in Deploy manifest")
                if normalized_key == "command" and isinstance(value, str) and any(ch in value for ch in "\x00\r\n;`|&><"):
                    raise ValueError("health check command contains unsafe shell syntax")
        object.__setattr__(self, "health_checks", _json_safe(self.health_checks))
        if not isinstance(self.rollback, dict) or not self.rollback:
            raise ValueError("rollback must be a non-empty object")
        for key, value in self.rollback.items():
            normalized_key = str(key).casefold().replace("-", "_")
            if any(secret in normalized_key for secret in ("token", "password", "secret", "credential", "private_key", "api_key")):
                raise ValueError("raw secret fields are forbidden in Deploy manifest")
            if normalized_key == "command" and isinstance(value, str) and any(ch in value for ch in "\x00\r\n;`|&><"):
                raise ValueError("rollback command contains unsafe shell syntax")
        object.__setattr__(self, "rollback", _json_safe(self.rollback))
        if not isinstance(self.tree, str) or len(self.tree) > 128 or any(ch in self.tree for ch in "\x00\r\n"):
            raise ValueError("deploy tree is invalid")
        for field_name in ("config_refs", "migration_steps", "stop_conditions"):
            values = getattr(self, field_name)
            if not isinstance(values, list) or len(values) > 64 or not all(isinstance(item, str) and item.strip() for item in values):
                raise ValueError("%s must be a bounded list of strings" % field_name)
            object.__setattr__(self, field_name, [item.strip() for item in values])
        if not isinstance(self.observation_window, str) or len(self.observation_window) > 128:
            raise ValueError("observation_window is invalid")

    @property
    def digest(self) -> str:
        import hashlib
        import json

        payload = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeployManifest":
        if not isinstance(data, dict):
            raise TypeError("DeployManifest data must be a dictionary")
        return cls(**data)


@dataclass(frozen=True)
class DeployState:
    """Observable Deploy lifecycle state; it never implies business approval."""

    status: str
    manifest_digest: str
    target: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    authorization_digest: str = ""
    reason: str = ""

    def __post_init__(self):
        if self.status not in _DEPLOY_STATUSES:
            raise ValueError("unsupported deploy status")
        if not isinstance(self.manifest_digest, str) or not self.manifest_digest.strip():
            raise ValueError("manifest_digest must be non-empty")
        if not isinstance(self.target, str) or not self.target.strip():
            raise ValueError("deploy state target must be non-empty")
        if not isinstance(self.evidence, dict):
            raise TypeError("deploy evidence must be a dictionary")
        object.__setattr__(self, "evidence", _json_safe(self.evidence))
        if not isinstance(self.authorization_digest, str) or not isinstance(self.reason, str):
            raise TypeError("deploy state text fields must be strings")

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeployState":
        if not isinstance(data, dict):
            raise TypeError("DeployState data must be a dictionary")
        return cls(**data)
