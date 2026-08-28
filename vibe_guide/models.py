"""JSON-safe shared data models for the guide contracts."""

from dataclasses import asdict, dataclass, field
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
    }
)
_PLAN_STATUSES = frozenset({"draft", "authorized", "running", "complete", "blocked", "failed"})
_CAPABILITY_LEVELS = frozenset({"guide", "background", "full"})
EVIDENCE_PRIORITY = (
    "current_user",
    "approved_prd",
    "authorization",
    "issue_contract",
    "implementation",
)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


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
    # V2 metadata is optional at construction time for V1 compatibility.  The
    # V2 DAG audit requires these values (or their contract equivalents).
    risk_tags: List[str] = field(default_factory=list)
    writer: str = ""
    worktree: str = ""
    allowlist: List[str] = field(default_factory=list)

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
        if not all(isinstance(item, str) and item.strip() for item in self.allowlist):
            raise TypeError("allowlist must be a list of non-empty strings")
        for name in ("writer", "worktree"):
            value = getattr(self, name)
            if not isinstance(value, str):
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

    def __post_init__(self):
        for name in ("worker", "model", "reasoning"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError("%s must be non-empty" % name)
        if not isinstance(self.fallbacks, list) or not all(isinstance(x, dict) for x in self.fallbacks):
            raise ValueError("fallbacks must be a list of dictionaries")
        if not isinstance(self.selection_basis, dict):
            raise ValueError("selection_basis must be a dictionary")

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkerProfile":
        if not isinstance(data, dict):
            raise TypeError("WorkerProfile data must be a dictionary")
        return cls(**data)


@dataclass(frozen=True)
class IssueComplexity:
    """Evidence for routing one concrete Issue/Spec to a worker."""

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
        if self.complexity_band not in {"simple", "light_plan", "complex"}:
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
    """Observable local model probe; ``None`` means unverifiable."""

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
        if isinstance(self.context_limit, bool) or not isinstance(self.context_limit, int) or self.context_limit <= 0:
            raise ValueError("context_limit must be positive")
        if not isinstance(self.reasoning_levels, list) or not all(
            isinstance(item, str) and item in {"normal", "deep"}
            for item in self.reasoning_levels
        ):
            raise ValueError("reasoning_levels must contain normal or deep")
        if self.available is not None and not isinstance(self.available, bool):
            raise ValueError("available must be true, false, or unknown")

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LocalModel":
        if not isinstance(data, dict):
            raise TypeError("LocalModel data must be a dictionary")
        return cls(**data)
