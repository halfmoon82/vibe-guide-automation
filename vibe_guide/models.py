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
        if not isinstance(self.allowlist, list) or not all(
            isinstance(item, str) and item.strip() for item in self.allowlist
        ):
            raise TypeError("allowlist must be a list of non-empty strings")
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
    # Optional Deploy selection is metadata only; execution requires a
    # separate manifest authorization and is never part of normal actions.
    deploy: Optional[Dict[str, Any]] = None
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
        if self.deploy is not None:
            if not isinstance(self.deploy, dict):
                raise TypeError("plan deploy metadata must be a dictionary")
            self.deploy = _json_safe(self.deploy)
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

    @property
    def deploy_manifest(self) -> Optional[Dict[str, Any]]:
        """Compatibility name used by release-stage callers."""
        return self.deploy

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
    model_id: str
    capabilities: List[str]
    context_limit: int
    reasoning_levels: List[str]
    available: Optional[bool]

    def __post_init__(self):
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("model_id must be non-empty")
        if not isinstance(self.capabilities, list) or not all(isinstance(item, str) for item in self.capabilities):
            raise TypeError("capabilities must be a list of strings")
        if isinstance(self.context_limit, bool) or not isinstance(self.context_limit, int) or self.context_limit < 0:
            raise ValueError("context_limit must be a non-negative integer")
        if not isinstance(self.reasoning_levels, list) or not all(isinstance(item, str) for item in self.reasoning_levels):
            raise TypeError("reasoning_levels must be a list of strings")
        if self.available is not None and type(self.available) is not bool:
            raise TypeError("available must be a bool or None")

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LocalModel":
        if not isinstance(data, dict):
            raise TypeError("LocalModel data must be a dictionary")
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
        if self.status not in {"draft", "approved", "blocked_design", "blocked_decision", "review_required", "blocked_unknown"}:
            raise ValueError("unsupported PRD status")

    @property
    def continue_planning(self) -> bool:
        return self.status == "approved"

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PRD":
        if not isinstance(data, dict):
            raise TypeError("PRD data must be a dictionary")
        return cls(**data)


@dataclass
class PRDCheckpoint:
    kind: str
    fields: Dict[str, Any]
    evidence: List[Any]
    status: str

    def __post_init__(self):
        if self.kind not in {"framing", "solution", "solution_tradeoffs", "flow", "flow_rules", "acceptance", "acceptance_handoff", "decision_pending", "open_question"}:
            raise ValueError("unsupported PRD checkpoint kind")
        if not isinstance(self.fields, dict):
            raise TypeError("PRD checkpoint fields must be a dictionary")
        if not isinstance(self.evidence, list):
            raise TypeError("PRD checkpoint evidence must be a list")
        if self.status not in {"approved", "ready", "blocked_design", "review_required", "blocked_unknown"}:
            raise ValueError("unsupported PRD checkpoint status")
        self.fields = _json_safe(self.fields)
        self.evidence = _json_safe(self.evidence)

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PRDCheckpoint":
        if not isinstance(data, dict):
            raise TypeError("PRDCheckpoint data must be a dictionary")
        return cls(**data)


@dataclass
class SkillProfile:
    name: str
    source_url: str
    commit_sha: str
    license: str
    selected_paths: List[str]
    status: str = "candidate"
    installed_at: str = ""
    verification_status: str = "unverified"
    install_time: Optional[str] = None

    def __post_init__(self):
        self.name = _identifier(self.name, "Skill profile name")
        if not isinstance(self.source_url, str):
            raise TypeError("Skill profile source_url must be a string")
        if not isinstance(self.commit_sha, str):
            raise TypeError("Skill profile commit_sha must be a string")
        if not isinstance(self.license, str):
            raise TypeError("Skill profile license must be a string")
        if not isinstance(self.selected_paths, list):
            raise TypeError("Skill profile selected_paths must be a list")
        if self.status not in {"candidate", "selected", "skipped", "later", "deferred", "needs_recheck"}:
            raise ValueError("unsupported Skill profile status")
        for value, field_name in ((self.installed_at, "installed_at"), (self.verification_status, "verification_status")):
            if not isinstance(value, str):
                raise TypeError("Skill profile %s must be a string" % field_name)
        if self.install_time is not None and not isinstance(self.install_time, str):
            raise TypeError("Skill profile install_time must be a string or None")

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillProfile":
        if not isinstance(data, dict):
            raise TypeError("SkillProfile data must be a dictionary")
        return cls(**data)


@dataclass
class StageHandoff:
    from_stage: str
    from_status: str
    to_stage: str
    readiness: str
    evidence_refs: List[str]
    open_questions: List[str]
    required_user_action: str
    prompt: str
    forbidden_automatic_actions: List[str] = field(default_factory=lambda: [
        "create_spec", "create_issue", "create_dag", "create_worker", "authorize", "deploy"
    ])
    prd_revision: Optional[int] = None

    @property
    def authorizes(self) -> bool:
        return False

    @property
    def creates_worker(self) -> bool:
        return False

    def __post_init__(self):
        if self.from_stage not in {"prd", "monitor", "acceptance", "change_request", "deploy"}:
            raise ValueError("unsupported StageHandoff source stage")
        if self.from_status not in {"approved", "review_required", "blocked_design", "blocked_unknown", "auto_corrected", "retry_pending", "running", "rework", "delivered", "review", "accepted"}:
            raise ValueError("unsupported StageHandoff source status")
        if self.to_stage not in {"spec_issue_dag", "development_plan", "authorization", "monitor", "acceptance", "change_request", "deploy"}:
            raise ValueError("unsupported StageHandoff target stage")
        if self.readiness not in {"ready", "blocked_design", "blocked_dag", "blocked_unknown", "awaiting_user"}:
            raise ValueError("unsupported StageHandoff readiness")
        if self.required_user_action not in {"continue_planning", "answer_question", "confirm_plan", "authorize", "confirm_authorization_card", "none"}:
            raise ValueError("unsupported StageHandoff user action")
        if not isinstance(self.evidence_refs, list) or not all(isinstance(item, str) for item in self.evidence_refs):
            raise TypeError("evidence_refs must be a list of strings")
        if not isinstance(self.open_questions, list) or not all(isinstance(item, str) for item in self.open_questions):
            raise TypeError("open_questions must be a list of strings")
        if not isinstance(self.forbidden_automatic_actions, list) or not all(isinstance(item, str) for item in self.forbidden_automatic_actions):
            raise TypeError("forbidden_automatic_actions must be a list of strings")
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("StageHandoff prompt must be non-empty")
        if self.prd_revision is not None and (isinstance(self.prd_revision, bool) or not isinstance(self.prd_revision, int) or self.prd_revision < 1):
            raise ValueError("StageHandoff PRD revision must be a positive integer")
        if "create_worker" not in self.forbidden_automatic_actions or "authorize" not in self.forbidden_automatic_actions:
            raise ValueError("StageHandoff must forbid worker creation and authorization")

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StageHandoff":
        if not isinstance(data, dict):
            raise TypeError("StageHandoff data must be a dictionary")
        return cls(**data)

    @classmethod
    def for_blocked_prd(cls, evidence_refs: List[str], question: str, prd_revision: Optional[int] = None) -> "StageHandoff":
        return cls(
            from_stage="prd", from_status="blocked_design", to_stage="spec_issue_dag",
            readiness="blocked_design", evidence_refs=list(evidence_refs),
            open_questions=[question], required_user_action="answer_question",
            prompt=question, prd_revision=prd_revision,
        )

    def render(self) -> str:
        questions = "；".join(self.open_questions) if self.open_questions else "无"
        return (
            "阶段衔接：{} (revision={}) ({}) → {}\n状态：{}\n证据：{}\n开放问题：{}\n"
            "用户下一步：{}\n提示：{}\n禁止自动动作：{}\n不会自动创建 Spec/Issue/DAG、授权卡或 Worker。"
        ).format(
            self.from_stage, self.prd_revision if self.prd_revision is not None else "unknown",
            self.from_status, self.to_stage, self.readiness,
            ", ".join(self.evidence_refs) or "无", questions,
            self.required_user_action, self.prompt,
            ", ".join(self.forbidden_automatic_actions),
        )
