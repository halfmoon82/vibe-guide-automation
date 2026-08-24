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
