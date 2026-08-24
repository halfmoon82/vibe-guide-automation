from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional
import re

_NODE_STATUSES = {"pending", "ready", "running", "blocked", "complete", "failed"}
_PLAN_STATUSES = {"draft", "authorized", "running", "complete", "blocked", "failed"}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


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
        if not _ID.match(self.id):
            raise ValueError("node id must be a simple identifier")
        if self.status not in _NODE_STATUSES:
            raise ValueError("unsupported node status")
        for values in (self.depends_on, self.integration_after):
            if len(values) != len(set(values)):
                raise ValueError("duplicate dependencies are not allowed")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


@dataclass
class Plan:
    plan_id: str
    version: int
    prd_path: str
    node_ids: List[str]
    status: str

    def __post_init__(self):
        if self.version < 1 or self.status not in _PLAN_STATUSES:
            raise ValueError("invalid plan version or status")
        if len(self.node_ids) != len(set(self.node_ids)):
            raise ValueError("duplicate node ids are not allowed")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
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

