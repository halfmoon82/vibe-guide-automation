"""Small JSON-safe models shared by the V2 and V3 workflow contracts."""

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_DEPLOY_STATUSES = frozenset({
    "deploy_planned", "deploy_ready", "deploy_running", "deployed",
    "blocked_deploy", "blocked_unknown", "rolled_back",
})
_NODE_STATUSES = frozenset({
    "planned", "ready", "running", "delivered", "review", "accepted",
    "rework", "blocked_design", "blocked_deploy", "blocked_unknown",
})
_PLAN_STATUSES = frozenset({"draft", "authorized", "running", "complete", "blocked", "failed"})
_CAPABILITY_LEVELS = frozenset({"guide", "background", "full"})
EVIDENCE_PRIORITY = ("current_user", "approved_prd", "authorization", "issue_contract", "implementation")
_BLOCKED_HANDOFF_QUESTIONS = {
    "blocked_design": "请补充或确认导致设计阻塞的产品决策",
    "blocked_unknown": "请提供可验证的缺失证据或确认下一次最小重试动作",
}


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("%s must be a simple identifier" % field_name)
    return value


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    raise TypeError("value of type %s is not JSON serializable" % type(value).__name__)


def _json_dict(instance: Any) -> Dict[str, Any]:
    return _json_safe(asdict(instance)) if hasattr(instance, "__dataclass_fields__") else _json_safe(instance.to_dict())


class Phase(str, Enum):
    PRD_APPROVED = "prd_approved"
    SPEC_ISSUE_DAG = "spec_issue_dag"
    DEVELOPMENT_PLAN_CONFIRMATION = "development_plan_confirmation"
    AUTHORIZATION = "authorization"
    MONITOR = "monitor"


WorkflowPhase = Phase
Stage = Phase


class Action(str, Enum):
    CONTINUE_PLANNING = "continue_planning"
    CONFIRM_PLAN = "confirm_plan"
    AUTHORIZE_EXECUTION = "authorize_execution"


WorkflowAction = Action
UserAction = Action


@dataclass
class DAGNode:
    id: str
    title: str
    depends_on: List[str]
    integration_after: List[str]
    parallel_group: Optional[str]
    contract: Dict[str, Any]
    status: str
    risk_tags: List[str] = field(default_factory=list)
    writer: str = ""
    worktree: str = ""
    allowlist: List[str] = field(default_factory=list)

    def __post_init__(self):
        self.id = _identifier(self.id, "node id")
        if not isinstance(self.title, str):
            raise TypeError("title must be a string")
        for name in ("depends_on", "integration_after"):
            values = getattr(self, name)
            if not isinstance(values, list) or any(not isinstance(x, str) for x in values):
                raise TypeError("%s must be a list of strings" % name)
            normalized = [_identifier(value, "%s item" % name) for value in values]
            if len(normalized) != len(set(normalized)):
                raise ValueError("duplicate %s are not allowed" % name)
            setattr(self, name, normalized)
        if self.parallel_group is not None:
            self.parallel_group = _identifier(self.parallel_group, "parallel group")
        if not isinstance(self.contract, dict):
            raise TypeError("contract must be a dictionary")
        self.contract = _json_safe(self.contract)
        if not isinstance(self.status, str) or self.status not in _NODE_STATUSES:
            raise ValueError("unsupported node status")
        self.risk_tags = list(self.risk_tags or [])
        self.allowlist = list(self.allowlist or [])

    def to_dict(self):
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


@dataclass(frozen=True)
class TopologyValidation:
    """Evidence-bounded S1 execution topology decision."""

    status: str
    reasons: Tuple[str, ...] = ()
    ready_nodes: Tuple[str, ...] = ()
    parallel_groups: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    applies: bool = True

    def __post_init__(self):
        if self.status not in {"valid", "governance_pending", "bypassed"}:
            raise ValueError("unsupported topology status")
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))
        object.__setattr__(self, "ready_nodes", tuple(str(item) for item in self.ready_nodes))
        object.__setattr__(self, "parallel_groups", {
            str(key): tuple(str(item) for item in values)
            for key, values in (self.parallel_groups or {}).items()
        })

    @property
    def valid(self):
        return self.status in {"valid", "bypassed"}

    def to_dict(self):
        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "ready_nodes": list(self.ready_nodes),
            "parallel_groups": {key: list(values) for key, values in self.parallel_groups.items()},
            "applies": self.applies,
        }


@dataclass(frozen=True)
class DispatchedPair:
    node_id: str
    developer: Any
    reviewer: Any


class TopologyError(ValueError):
    """Raised when an S1 topology cannot be dispatched safely."""


def _topology_value(node: DAGNode, name: str, default=None):
    contract = node.contract if isinstance(node.contract, Mapping) else {}
    value = contract.get(name)
    if value in (None, "", []):
        value = getattr(node, name, default)
    profile = contract.get("worker_profile")
    if value in (None, "", []) and isinstance(profile, Mapping):
        value = profile.get(name, default)
    if value in (None, "", []) and isinstance(contract.get("routing"), Mapping):
        value = contract["routing"].get(name, default)
    if value in (None, "", []) and isinstance(contract.get("repository_routing"), Mapping):
        value = contract["repository_routing"].get(name, default)
    return value


def _node_scope(node: DAGNode):
    value = _topology_value(node, "allowlist", [])
    return {str(item) for item in value} if isinstance(value, (list, tuple, set)) else set()


def _ready_node_ids(nodes):
    by_id = {node.id: node for node in nodes}
    return [
        node.id for node in nodes
        if node.status in {"planned", "ready"}
        and all(by_id.get(dep) is not None and by_id[dep].status in {"accepted", "delivered"}
                for dep in node.depends_on)
    ]


def validate_topology(plan_or_nodes, complexity="complex", supervisor=None,
                      host=None, parent_run_id=None):
    """Validate S1 role separation and expose the ready set for dispatch.

    Simple and light-plan work deliberately bypasses this governance layer.
    The function accepts either a Plan or an iterable of DAG nodes.
    """
    inferred = getattr(plan_or_nodes, "complexity", None) or getattr(plan_or_nodes, "complexity_band", None)
    mode = str(inferred or complexity or "complex").strip().casefold()
    nodes = list(getattr(plan_or_nodes, "nodes", plan_or_nodes) or [])
    if mode in {"simple", "light", "light_plan", "light-plan"}:
        return TopologyValidation("bypassed", applies=False)
    if mode not in {"complex", "s1", "s1_complex", "s1-complex"}:
        return TopologyValidation("governance_pending", ("complexity band is unknown",), applies=True)

    reasons = []
    if not nodes:
        reasons.append("topology has no nodes")
    if supervisor in (None, ""):
        supervisor = getattr(plan_or_nodes, "supervisor", None)
    if supervisor in (None, "") and nodes:
        supervisor = _topology_value(nodes[0], "supervisor")
    if not isinstance(supervisor, str) or not supervisor.strip():
        reasons.append("missing supervisor")
        supervisor = None

    writers = []
    worktrees = []
    for node in nodes:
        writer = _topology_value(node, "writer")
        reviewer = _topology_value(node, "reviewer")
        if not isinstance(writer, str) or not writer.strip():
            reasons.append("node %s missing writer" % node.id)
        else:
            writers.append(writer)
        if not isinstance(reviewer, str) or not reviewer.strip():
            reasons.append("node %s missing reviewer" % node.id)
        if supervisor and (writer == supervisor or reviewer == supervisor):
            reasons.append("supervisor must be independent from node %s" % node.id)
        if writer and reviewer and writer == reviewer:
            reasons.append("node %s writer and reviewer must be distinct" % node.id)
        provider = _topology_value(node, "provider")
        adapter_id = _topology_value(node, "adapter_id")
        if adapter_id != "codex":
            reasons.append("node %s adapter_id must be codex" % node.id)
        environment = _topology_value(node, "environment")
        if provider != "codex-app-visible":
            reasons.append("node %s provider must be codex-app-visible" % node.id)
        if environment != "worktree":
            reasons.append("node %s environment must be worktree" % node.id)
        worktree = _topology_value(node, "worktree")
        if not isinstance(worktree, str) or not worktree.strip():
            reasons.append("node %s missing worktree" % node.id)
        else:
            worktrees.append(worktree)
        branch = _topology_value(node, "branch")
        if not isinstance(branch, str) or not branch.strip():
            reasons.append("node %s missing branch" % node.id)
        node_host = _topology_value(node, "host")
        if not isinstance(node_host, str) or not node_host.strip():
            reasons.append("node %s missing host" % node.id)
        elif host is not None and node_host != host:
            reasons.append("node %s host does not match parent binding" % node.id)
        node_parent_run_id = _topology_value(node, "parent_run_id")
        if not isinstance(node_parent_run_id, str) or not node_parent_run_id.startswith("run-"):
            reasons.append("node %s parent-run binding is invalid" % node.id)
        elif parent_run_id is not None and node_parent_run_id != parent_run_id:
            reasons.append("node %s parent-run binding does not match" % node.id)
        allowlist = _topology_value(node, "allowlist", [])
        if not isinstance(allowlist, (list, tuple)) or not allowlist:
            reasons.append("node %s missing allowlist" % node.id)

    if len(writers) != len(set(writers)):
        reasons.append("duplicate writer")
    if len(worktrees) != len(set(worktrees)):
        reasons.append("duplicate worktree")
    if supervisor and supervisor in writers:
        reasons.append("writer-as-supervisor")

    by_id = {node.id: node for node in nodes}
    for node in nodes:
        child_scope = _node_scope(node)
        for dependency in node.depends_on:
            parent = by_id.get(dependency)
            if parent is None:
                continue
            if child_scope.isdisjoint(_node_scope(parent)):
                contract = node.contract if isinstance(node.contract, Mapping) else {}
                justification = (contract.get("dependency_reason") or
                                 contract.get("serialization_justification") or
                                 contract.get("dependency_justification"))
                if not justification:
                    reasons.append("unjustified serialization %s -> %s" % (dependency, node.id))

    ready = _ready_node_ids(nodes)
    groups = {}
    for node in nodes:
        if node.id in ready:
            groups.setdefault(node.parallel_group or "default", []).append(node.id)
    status = "governance_pending" if reasons else "valid"
    return TopologyValidation(status, tuple(dict.fromkeys(reasons)), tuple(ready), groups)


def dispatch_ready_nodes(nodes: Iterable[DAGNode], provider, contract_path,
                        complexity="complex", supervisor=None):
    """Create visible developer/reviewer pairs for every ready node."""
    nodes = list(nodes or [])
    validation = validate_topology(nodes, complexity=complexity, supervisor=supervisor)
    if not validation.valid:
        raise TopologyError("topology is %s: %s" %
                            (validation.status, "; ".join(validation.reasons)))
    if not validation.applies:
        return []
    if getattr(provider, "provider", None) != "codex-app-visible" or getattr(provider, "mode", None) != "visible":
        raise TopologyError("S1 topology requires codex-app-visible provider")
    routing = getattr(provider, "routing", None)
    if getattr(routing, "environment", None) != "worktree" or not getattr(routing, "project_id", None):
        raise TopologyError("S1 topology requires a project worktree route")
    for node_id in validation.ready_nodes:
        node = next(item for item in nodes if item.id == node_id)
        if (_topology_value(node, "host") != getattr(routing, "host_id", None)
                or _topology_value(node, "worktree") != getattr(routing, "worktree", None)
                or _topology_value(node, "branch") != getattr(routing, "branch", None)):
            raise TopologyError("node and provider routing do not match")
    developers = {
        node_id: provider.create("developer", node_id, contract_path)
        for node_id in validation.ready_nodes
    }
    reviewers = {
        node_id: provider.create("reviewer", node_id, contract_path)
        for node_id in validation.ready_nodes
    }
    pairs = []
    for node_id in validation.ready_nodes:
        developer = developers[node_id]
        reviewer = reviewers[node_id]
        developer_id = getattr(developer, "task_id", None)
        reviewer_id = getattr(reviewer, "task_id", None)
        if not developer_id or not reviewer_id or developer_id == reviewer_id:
            raise TopologyError("developer and reviewer tasks must be distinct")
        if not getattr(developer, "visible", False) or not getattr(reviewer, "visible", False):
            raise TopologyError("developer and reviewer tasks must be visible")
        node = next(item for item in nodes if item.id == node_id)
        for task in (developer, reviewer):
            if any(getattr(task, name, None) != _topology_value(node, name)
                   for name in ("host", "worktree", "branch")):
                raise TopologyError("provider returned route does not match node contract")
        pairs.append(DispatchedPair(node_id, developer, reviewer))
    return pairs


def assert_supervisor_boundary(caller_identity, plan_or_nodes):
    """Require the caller to be the explicitly bound independent supervisor."""
    nodes = list(getattr(plan_or_nodes, "nodes", plan_or_nodes) or [])
    supervisors = {_topology_value(node, "supervisor") for node in nodes}
    writers = {_topology_value(node, "writer") for node in nodes}
    if len(supervisors) != 1 or not caller_identity or caller_identity in writers or caller_identity not in supervisors:
        raise PermissionError("monitor caller must be the designated independent supervisor")
    return True


validate_execution_topology = validate_topology


def dispatch_ready_set(*args, **kwargs):
    """Fail-closed compatibility name; dispatch belongs to Monitor runtime."""
    raise PermissionError("dispatch_ready_set requires an authorized Monitor run")


@dataclass
class Plan:
    plan_id: str
    version: int
    prd_path: str
    node_ids: List[str]
    status: str
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    evidence_priority: List[str] = field(default_factory=lambda: list(EVIDENCE_PRIORITY))
    deploy: Optional[Dict[str, Any]] = None
    nodes: List[DAGNode] = field(default_factory=list)

    def __post_init__(self):
        self.plan_id = _identifier(self.plan_id, "plan id")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("plan version must be a positive integer")
        if not isinstance(self.node_ids, list):
            raise TypeError("node_ids must be a list")
        self.node_ids = [_identifier(value, "node id") for value in self.node_ids]
        if len(self.node_ids) != len(set(self.node_ids)):
            raise ValueError("duplicate node ids are not allowed")
        if self.status not in _PLAN_STATUSES:
            raise ValueError("unsupported plan status")
        if self.evidence_priority != list(EVIDENCE_PRIORITY):
            raise ValueError("plan evidence priority is fixed")
        self.decisions = _json_safe(self.decisions)
        self.nodes = [item if isinstance(item, DAGNode) else DAGNode.from_dict(item) for item in self.nodes]

    def to_dict(self):
        return _json_dict(self)

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

    def __post_init__(self):
        self.agent_id = _identifier(self.agent_id, "agent id")
        for name in ("shell", "subprocess", "worktree", "background", "session_resume"):
            if type(getattr(self, name)) is not bool:
                raise TypeError("%s must be a bool" % name)
        if self.level not in _CAPABILITY_LEVELS:
            raise ValueError("unsupported capability level")

    def to_dict(self):
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data):
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
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("%s must be non-empty" % name)
        if not isinstance(self.fallbacks, list) or not all(isinstance(item, dict) for item in self.fallbacks):
            raise ValueError("fallbacks must be a list of dictionaries")
        if not isinstance(self.selection_basis, dict):
            raise ValueError("selection_basis must be a dictionary")

    def to_dict(self):
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


@dataclass(frozen=True)
class IssueComplexity:
    """Evidence-bound complexity for one dispatched Issue/Spec."""

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
        if not isinstance(self.risk_tags, list) or not all(isinstance(tag, str) and tag.strip() for tag in self.risk_tags):
            raise ValueError("risk_tags must be a list of non-empty strings")
        if len(self.risk_tags) != len(set(self.risk_tags)):
            raise ValueError("risk_tags must be unique")
        if self.complexity_band not in {"simple", "light", "complex"}:
            raise ValueError("unsupported complexity band")
        if not isinstance(self.evidence_ref, str) or not self.evidence_ref.strip():
            raise ValueError("evidence_ref must be non-empty")

    def to_dict(self):
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data):
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
        if not isinstance(self.capabilities, list) or not all(isinstance(item, str) and item.strip() for item in self.capabilities):
            raise ValueError("capabilities must be a list of non-empty strings")
        if not isinstance(self.context_limit, int) or isinstance(self.context_limit, bool) or self.context_limit <= 0:
            raise ValueError("context_limit must be positive")
        if not isinstance(self.reasoning_levels, list) or not all(isinstance(item, str) and item in {"normal", "deep"} for item in self.reasoning_levels):
            raise ValueError("reasoning_levels must contain normal or deep")
        if self.available not in (True, False, None):
            raise ValueError("available must be true, false, or unknown")

    def to_dict(self):
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data):
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
        commands = []
        for command in self.command_allowlist:
            if not isinstance(command, str) or not command.strip():
                raise ValueError("deploy commands must be non-empty strings")
            command = command.strip()
            if len(command) > 512 or any(ch in command for ch in "\x00\r\n;`|&><") or "$(" in command:
                raise ValueError("deploy command contains unsafe shell syntax")
            commands.append(command)
        if len(set(commands)) != len(commands):
            raise ValueError("command_allowlist contains duplicates")
        object.__setattr__(self, "command_allowlist", commands)
        if not isinstance(self.health_checks, list) or not self.health_checks:
            raise ValueError("health_checks must be a non-empty list")
        if len(self.health_checks) > 32 or not all(isinstance(item, dict) for item in self.health_checks):
            raise ValueError("health_checks must be a bounded list of objects")
        for check in self.health_checks:
            self._validate_object(check, "health check")
        object.__setattr__(self, "health_checks", _json_safe(self.health_checks))
        if not isinstance(self.rollback, dict) or not self.rollback:
            raise ValueError("rollback must be a non-empty object")
        self._validate_object(self.rollback, "rollback")
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

    @staticmethod
    def _validate_object(value, label):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(secret in normalized for secret in ("token", "password", "secret", "credential", "private_key", "api_key")):
                raise ValueError("raw secret fields are forbidden in Deploy manifest")
            if normalized == "command" and isinstance(item, str) and any(ch in item for ch in "\x00\r\n;`|&><"):
                raise ValueError("%s command contains unsafe shell syntax" % label)

    @property
    def digest(self):
        import hashlib
        import json
        payload = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self):
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data):
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

    def to_dict(self):
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise TypeError("DeployState data must be a dictionary")
        return cls(**data)


@dataclass
class PRD:
    title: str
    objective: str
    revision: int = 1
    status: str = "draft"

    def __post_init__(self):
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("PRD title must be non-empty")
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise ValueError("PRD objective must be non-empty")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("PRD revision must be a positive integer")

    @property
    def continue_planning(self):
        return self.status == "approved"

    def to_dict(self):
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


@dataclass
class PRDCheckpoint:
    kind: str
    fields: Dict[str, Any]
    evidence: List[Any]
    status: str

    def to_dict(self):
        return _json_dict(self)


@dataclass
class StageHandoff:
    """Durable, non-authorizing transition card for a V3 stage."""

    stage: str = "prd_approved"
    status: str = "blocked_unknown"
    plan_id: str = "unplanned"
    plan_revision: int = 1
    evidence_refs: List[str] = field(default_factory=list)
    required_user_action: str = "none"
    forbidden_automatic_actions: List[str] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)
    # Compatibility/display fields retained as non-authorizing metadata.
    prompt: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    # V2 aliases accepted on read for compatibility with historical cards.
    from_stage: Optional[str] = None
    from_status: Optional[str] = None
    to_stage: Optional[str] = None
    readiness: Optional[str] = None
    prd_revision: Optional[int] = None

    def __post_init__(self):
        if self.from_stage is not None:
            self.stage = {
                "prd": Phase.PRD_APPROVED.value,
                "development_plan": Phase.DEVELOPMENT_PLAN_CONFIRMATION.value,
            }.get(self.from_stage, self.from_stage)
        if self.from_status is not None:
            self.status = self.from_status
        if self.prd_revision is not None:
            self.plan_revision = self.prd_revision
        if self.stage == "prd" :
            self.stage = Phase.PRD_APPROVED.value
        if self.from_stage is None:
            self.from_stage = "prd" if self.stage == Phase.PRD_APPROVED.value else self.stage
        if self.from_status is None:
            self.from_status = self.status
        if self.to_stage is None:
            next_index = min(tuple(item.value for item in Phase).index(self.stage) + 1, len(Phase) - 1)
            self.to_stage = tuple(item.value for item in Phase)[next_index]
        if self.readiness is None:
            self.readiness = "ready" if self.status in {"approved", "ready_for_plan_confirmation", "ready_for_authorization", "monitor_ready"} else self.status
        if self.status in _BLOCKED_HANDOFF_QUESTIONS:
            if not self.open_questions:
                self.open_questions = [_BLOCKED_HANDOFF_QUESTIONS[self.status]]
            # A blocked card must always stop for an answer, even when a
            # caller supplied an incompatible action.
            self.required_user_action = "answer_question"
        if self.stage not in {item.value for item in Phase}:
            raise ValueError("unsupported stage")
        if not isinstance(self.status, str) or not self.status.strip():
            raise ValueError("status must be non-empty")
        self.plan_id = _identifier(self.plan_id, "plan id")
        if isinstance(self.plan_revision, bool) or not isinstance(self.plan_revision, int) or self.plan_revision < 1:
            raise ValueError("plan_revision must be a positive integer")
        if self.required_user_action not in {item.value for item in Action} | {"none", "answer_question", "authorize", "confirm_authorization_card"}:
            raise ValueError("unsupported required_user_action")
        for name in ("evidence_refs", "forbidden_automatic_actions", "open_questions"):
            values = getattr(self, name)
            if not isinstance(values, list) or any(not isinstance(x, str) for x in values):
                raise TypeError("%s must be a list of strings" % name)
            setattr(self, name, list(values))
        if not self.forbidden_automatic_actions:
            self.forbidden_automatic_actions = [
                "create_spec", "create_issue", "create_dag",
                "create_authorization_card", "create_authorization",
                "create_run", "create_worker", "authorize", "monitor",
                "archive", "deploy",
            ]
        if not isinstance(self.prompt, str):
            raise TypeError("prompt must be a string")
        if not isinstance(self.context, dict):
            raise TypeError("context must be a dictionary")
        self.context = _json_safe(self.context)

    @property
    def authorizes(self):
        return False

    @property
    def creates_worker(self):
        return False

    @property
    def next_stage(self):
        """The stage this card hands control to, without implying execution."""
        return self.to_stage

    @property
    def ready(self):
        """Whether the card reports a stage ready for its next gated action."""
        return self.readiness == "ready"

    def to_dict(self):
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)

    @classmethod
    def for_blocked_prd(cls, evidence_refs, question, prd_revision=None):
        return cls(
            stage=Phase.PRD_APPROVED.value,
            status="blocked_design",
            plan_id="unplanned",
            plan_revision=prd_revision or 1,
            evidence_refs=list(evidence_refs),
            required_user_action="answer_question",
            open_questions=[question],
            prompt=question,
            readiness="blocked_design",
            prd_revision=prd_revision,
        )
    def render(self):
        return (
            "阶段：{stage}\n状态：{status}\n计划：{plan_id}@{revision} (revision={revision})\n"
            "下一阶段：{next_stage}\n就绪：{readiness}\n"
            "证据：{evidence}\n用户下一步：{action}\n开放问题：{questions}\n"
            "禁止自动动作：{forbidden}"
        ).format(
            stage=self.stage,
            status=self.status,
            plan_id=self.plan_id,
            revision=self.plan_revision,
            next_stage=self.to_stage,
            readiness=self.readiness,
            evidence=", ".join(self.evidence_refs) or "无",
            action=self.required_user_action,
            questions="；".join(self.open_questions) or "无",
            forbidden=", ".join(self.forbidden_automatic_actions),
        )
