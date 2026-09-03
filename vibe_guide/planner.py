"""Deterministic task routing and product-decision gates."""

from dataclasses import dataclass, field, replace
import hashlib
import json
import re
from typing import Any, Dict, List, Optional

from .models import EVIDENCE_PRIORITY, IssueComplexity, TargetContract


@dataclass(frozen=True)
class S0Result:
    simple: bool
    needs_s1: bool
    route: str
    rationale: str


@dataclass(frozen=True)
class TaskContext:
    steps: int
    domains: int
    uncertainty: int
    failure_cost: int
    toolchain: int
    rationale: Dict[str, str] = field(default_factory=dict)
    force_upgrade_flags: List[str] = field(default_factory=list)

    def __post_init__(self):
        for name in ("steps", "domains", "uncertainty", "failure_cost", "toolchain"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 5:
                raise ValueError("S1 dimensions must be integers from 0 to 5")
        if not isinstance(self.force_upgrade_flags, list) or not all(isinstance(item, str) and item.strip() for item in self.force_upgrade_flags):
            raise TypeError("force_upgrade_flags must be a list of non-empty strings")
        if len(set(self.force_upgrade_flags)) != len(self.force_upgrade_flags):
            raise ValueError("force_upgrade_flags must be unique")


@dataclass(frozen=True)
class S1Score:
    total: int
    steps: int
    domains: int
    uncertainty: int
    failure_cost: int
    toolchain: int
    rationale: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteResult:
    """Machine-readable S0/S1 route, safe to persist before execution."""

    route: str
    complexity_band: str
    screen: str = "s1"
    score: Optional[int] = None
    dimensions: Dict[str, int] = field(default_factory=dict)
    force_upgrade_flags: List[str] = field(default_factory=list)
    evidence_ref: str = "planner:s0-s1"

    def __post_init__(self):
        if self.route not in {"simple", "light_plan", "complex"} or self.complexity_band != self.route:
            raise ValueError("unsupported route result")
        if self.screen not in {"s0", "s1"}:
            raise ValueError("unsupported routing screen")
        if self.score is not None and (isinstance(self.score, bool) or not isinstance(self.score, int) or self.score < 0):
            raise ValueError("route score must be a non-negative integer")
        if self.score is not None and not self.force_upgrade_flags:
            expected = "simple" if self.score <= 8 else "light_plan" if self.score <= 15 else "complex"
            if self.route != expected:
                raise ValueError("route score does not match route")
        if self.force_upgrade_flags and self.route != "complex":
            raise ValueError("force upgrade flags require complex route")
        if not isinstance(self.dimensions, dict) or not all(isinstance(k, str) and isinstance(v, int) for k, v in self.dimensions.items()):
            raise TypeError("route dimensions must be a string/integer dictionary")
        if not isinstance(self.force_upgrade_flags, list) or not all(isinstance(item, str) and item.strip() for item in self.force_upgrade_flags):
            raise TypeError("force_upgrade_flags must be a list of strings")
        if len(self.force_upgrade_flags) != len(set(self.force_upgrade_flags)):
            raise ValueError("force_upgrade_flags must be unique")
        if not isinstance(self.evidence_ref, str) or not self.evidence_ref.strip():
            raise ValueError("evidence_ref must be non-empty")

    def to_dict(self) -> Dict[str, Any]:
        return {"route": self.route, "complexity_band": self.complexity_band, "screen": self.screen, "score": self.score, "dimensions": dict(self.dimensions), "force_upgrade_flags": list(self.force_upgrade_flags), "evidence_ref": self.evidence_ref}

    @property
    def band(self) -> str:
        return self.complexity_band

    @property
    def s1_total(self) -> Optional[int]:
        return self.score

    @property
    def persisted(self) -> bool:
        return True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RouteResult":
        if not isinstance(data, dict):
            raise TypeError("RouteResult data must be a dictionary")
        return cls(**data)


@dataclass(frozen=True)
class ProductQuestion:
    question: str
    options: List[str]
    impact: str
    recommendation: Optional[str] = None

    def __post_init__(self):
        if not self.question.strip() or len(self.options) < 2:
            raise ValueError("a product question requires text and at least two options")
        if len(self.options) != len(set(self.options)):
            raise ValueError("product decision options must be unique")
        if self.recommendation is not None and self.recommendation not in self.options:
            raise ValueError("recommendation must be one of the options")


@dataclass(frozen=True)
class DecisionCard:
    question: str
    options: List[str]
    impact: str
    recommendation: str
    status: str = "unresolved"
    selected: Optional[str] = None
    id: str = ""
    field: str = ""
    revision: int = 1

    def __post_init__(self):
        if not isinstance(self.id, str):
            raise TypeError("decision id must be a string")
        if not self.id:
            digest = hashlib.sha256(
                (self.question + "\0" + self.field).encode("utf-8")
            ).hexdigest()[:16]
            object.__setattr__(self, "id", "decision-" + digest)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.id):
            raise ValueError("decision id must be a simple identifier")
        if not isinstance(self.field, str):
            raise TypeError("decision field must be a string")
        if self.field and not re.fullmatch(r"[A-Za-z0-9_.-]+", self.field):
            raise ValueError("decision field must be a contract path")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValueError("decision revision must be a positive integer")

    def render(self) -> str:
        option_lines = "\n".join("- {}".format(option) for option in self.options)
        status = "已决定：{}".format(self.selected) if self.status == "approved" else "待决定"
        return "{}\n选项：\n{}\n影响：{}\n建议：{}\n状态：{}".format(
            self.question,
            option_lines,
            self.impact,
            self.recommendation,
            status,
        )


@dataclass(frozen=True)
class PRD:
    title: str
    objective: str
    status: str = "draft"


@dataclass(frozen=True)
class PRDResult:
    prd: PRD
    approved: bool
    blockers: List[str]


@dataclass(frozen=True)
class ConsistencyResolution:
    field: str
    value: Any
    source: str
    action: str
    files: List[str]
    consistency_binding: Dict[str, Any]
    decision: Optional[Dict[str, Any]] = None


def _decision_reference(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    decision_id = item.get("id")
    field = item.get("field")
    revision = item.get("revision")
    status = item.get("status")
    if (
        not isinstance(decision_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", decision_id)
        or not isinstance(field, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", field)
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(status, str)
    ):
        return None
    return {
        "id": decision_id,
        "field": field,
        "revision": revision,
        "status": status,
        "selected": item.get("selected"),
    }


def resolve_consistency(
    inconsistency: Any,
    decisions: List[Dict[str, Any]],
    issue_contract: Dict[str, Any],
    authorized_actions: List[str],
    authorized_files: List[str],
    expected_binding: Dict[str, Any],
) -> Optional[ConsistencyResolution]:
    """Resolve only one evidence-determined, authorized non-deploy correction."""

    if not isinstance(inconsistency, dict):
        return None
    field_name = inconsistency.get("field")
    action = inconsistency.get("action")
    files = inconsistency.get("files")
    candidates = inconsistency.get("candidates")
    if (
        not isinstance(field_name, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", field_name)
        or action not in set(authorized_actions)
        or not isinstance(files, list)
        or not files
        or any(item not in set(authorized_files) for item in files)
        or not isinstance(candidates, list)
        or not candidates
    ):
        return None
    normalized = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("source") not in EVIDENCE_PRIORITY:
            return None
        source = candidate["source"]
        allowed_keys = {"source", "value"}
        if source != "implementation":
            allowed_keys.add("binding")
            if candidate.get("binding") != expected_binding:
                return None
            if source in {"current_user", "approved_prd"}:
                allowed_keys.add("decision")
                if not isinstance(candidate.get("decision"), dict):
                    return None
        elif "binding" in candidate:
            allowed_keys.add("binding")
            if candidate["binding"] != expected_binding:
                return None
        if set(candidate) != allowed_keys:
            return None
        try:
            value_key = json.dumps(
                candidate["value"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return None
        normalized.append(
            (source, candidate["value"], value_key, candidate.get("decision"))
        )
    highest = min(EVIDENCE_PRIORITY.index(item[0]) for item in normalized)
    winners = [
        item for item in normalized if EVIDENCE_PRIORITY.index(item[0]) == highest
    ]
    if len({item[2] for item in winners}) != 1:
        return None
    source, value, _key, decision = winners[0]
    if source == "implementation":
        return None
    approved_values = {
        json.dumps(
            item.get("selected"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for item in decisions
        if isinstance(item, dict) and item.get("status") == "approved"
    }
    decision_reference = None
    if source in {"current_user", "approved_prd"}:
        decision_reference = _decision_reference(decision)
        if decision_reference is None:
            return None
        matching = [
            _decision_reference(item)
            for item in decisions
            if _decision_reference(item) == decision_reference
        ]
        if (
            not matching
            or decision_reference["status"] != "approved"
            or decision_reference["field"] != field_name
            or decision_reference["selected"] != value
        ):
            return None
    if source == "authorization":
        contract_value = issue_contract.get(field_name)
        if _key not in approved_values and contract_value != value:
            return None
    if source == "issue_contract" and issue_contract.get(field_name) != value:
        return None
    return ConsistencyResolution(
        field_name,
        value,
        source,
        action,
        list(files),
        dict(expected_binding),
        decision_reference,
    )


_S1_MARKERS = (
    "并",
    "然后",
    "以及",
    "同时",
    "设计",
    "实现",
    "开发",
    "重构",
    "迁移",
    "集成",
    "测试",
    "部署",
    "系统",
    "多个",
    "workflow",
    "pipeline",
)

_ENGLISH_ACTIONS = {
    "add",
    "build",
    "create",
    "deploy",
    "design",
    "fix",
    "implement",
    "integrate",
    "migrate",
    "refactor",
    "rename",
    "test",
    "update",
    "write",
}


def classify_s0(message: str) -> S0Result:
    """Apply a cheap rule screen; uncertain or multi-step text proceeds to S1."""
    normalized = " ".join(message.strip().lower().split())
    if not normalized:
        return S0Result(False, True, "s1", "任务内容为空，需要进一步判断")
    markers = [marker for marker in _S1_MARKERS if marker in normalized]
    english_actions = [word for word in re.findall(r"[a-z]+", normalized) if word in _ENGLISH_ACTIONS]
    if len(english_actions) >= 2:
        markers.extend(english_actions)
    if markers:
        return S0Result(False, True, "s1", "检测到可能的多步骤或复杂任务：{}".format("、".join(markers)))
    return S0Result(True, False, "simple", "未检测到复杂任务标记")


def score_s1(context: TaskContext) -> S1Score:
    values = (context.steps, context.domains, context.uncertainty, context.failure_cost, context.toolchain)
    return S1Score(sum(values), *values, rationale=dict(context.rationale))


def _route_result_from_score(score: S1Score, force_upgrade_flags: Optional[List[str]] = None) -> RouteResult:
    if score.total < 0:
        raise ValueError("S1 total cannot be negative")
    flags = list(dict.fromkeys(force_upgrade_flags or []))
    route = "complex" if flags else ("simple" if score.total <= 8 else "light_plan" if score.total <= 15 else "complex")
    return RouteResult(
        route=route,
        complexity_band=route,
        score=score.total,
        dimensions={"steps": score.steps, "domains": score.domains, "uncertainty": score.uncertainty, "failure_cost": score.failure_cost, "toolchain": score.toolchain},
        force_upgrade_flags=flags,
    )


def route_task(context: Any):
    """Route a TaskContext to a persisted result; retain legacy S1Score API."""
    if isinstance(context, TaskContext):
        return _route_result_from_score(score_s1(context), context.force_upgrade_flags)
    if isinstance(context, S1Score):
        # Existing CLI/V2 callers consume the string route.
        return _route_result_from_score(context).route
    raise TypeError("route_task expects TaskContext or S1Score")


def classify_v310_task(s0: Any, s1: Optional[S1Score], force_upgrade_flags: Optional[List[str]] = None) -> IssueComplexity:
    """Create an evidence-bound IssueComplexity with an immutable band."""
    flags = list(force_upgrade_flags or [])
    if isinstance(s1, TaskContext):
        score = score_s1(s1)
        flags = list(dict.fromkeys(list(s1.force_upgrade_flags) + flags))
    elif isinstance(s1, S1Score):
        score = s1
    else:
        raise TypeError("s1 must be S1Score or TaskContext")
    band = _route_result_from_score(score, flags).route
    issue_band = "light" if band == "light_plan" else band
    if isinstance(s0, str) and s0 == "simple" and not flags and score.total <= 8:
        band = "simple"
    return IssueComplexity(
        issue_id="task",
        spec_ref="planner:s0-s1",
        steps=max(1, score.steps), domains=max(1, score.domains), uncertainty=max(1, score.uncertainty),
        failure_cost=max(1, score.failure_cost), toolchain=max(1, score.toolchain), context_demand="unknown",
        risk_tags=list(dict.fromkeys(flags)), complexity_band=issue_band, evidence_ref="planner:s0-s1",
    )


_TARGET_FIELDS = ("provider", "repository", "project", "target_branch", "issue_type", "source_branch", "file_scope", "merge_method")
_TARGET_ALIASES = {"repository_project": "repository", "repo": "repository", "repositories": "repository", "branch": "target_branch", "target_branches": "target_branch", "change_type": "issue_type", "issue_or_pr_type": "issue_type", "files": "file_scope", "scope": "file_scope", "method": "merge_method"}


def _candidate(environment: Dict[str, Any], field_name: str):
    aliases = [field_name] + [key for key, value in _TARGET_ALIASES.items() if value == field_name]
    found = [environment[key] for key in aliases if key in environment]
    if found:
        normalized = []
        for value in found:
            if field_name == "file_scope":
                if isinstance(value, str):
                    candidate = [value.strip()] if value.strip() else None
                elif isinstance(value, (list, tuple)) and all(isinstance(item, str) and item.strip() for item in value):
                    candidate = [item.strip() for item in value]
                else:
                    candidate = None
            elif isinstance(value, str) and value.strip():
                candidate = value.strip()
            elif isinstance(value, (list, tuple, set)) and len(value) == 1 and isinstance(next(iter(value)), str) and next(iter(value)).strip():
                candidate = next(iter(value)).strip()
            else:
                candidate = None
            if candidate is None:
                return None
            normalized.append(candidate)
        if all(item == normalized[0] for item in normalized[1:]):
            return normalized[0]
        return None
    return None


def collect_target_contract(environment: Dict[str, Any], user_selection: Optional[Dict[str, Any]] = None) -> TargetContract:
    """Collect target fields once, auto-filling only uniquely observed values."""
    if not isinstance(environment, dict):
        raise TypeError("environment must be a dictionary")
    if isinstance(environment.get("target_contract"), dict) and user_selection is None:
        return TargetContract.from_dict(environment["target_contract"])
    selection = user_selection if isinstance(user_selection, dict) else {}
    values = {}
    missing = []
    for field_name in _TARGET_FIELDS:
        # A target may be identified by either repository or project; the
        # provider-specific side that is absent is not a missing choice.
        if field_name == "project" and ("repository" in values or any(key in selection for key in ("repository", "repo", "repository_project")) or any(key in environment for key in ("repository", "repo", "repository_project"))):
            continue
        if field_name == "repository" and ("project" in values or _candidate(selection, "project") is not None or _candidate(environment, "project") is not None):
            continue
        selected = _candidate(selection, field_name)
        value = selected if selected is not None else _candidate(environment, field_name)
        if value is None:
            missing.append(field_name)
        else:
            values[field_name] = value
    return TargetContract(**values, frozen=not missing, missing_fields=missing, status="frozen" if not missing else "pending_selection")


def create_decision_card(question: ProductQuestion) -> DecisionCard:
    recommendation = question.recommendation or question.options[0]
    return DecisionCard(
        question.question,
        list(question.options),
        question.impact,
        recommendation,
    )


def approve_prd(prd: PRD, decisions: List[DecisionCard]) -> PRDResult:
    blockers = []
    for card in decisions:
        if (
            card.status != "approved"
            or card.selected not in card.options
            or not card.field
        ):
            blockers.append(card.question)
    if blockers:
        return PRDResult(replace(prd, status="blocked_decision"), False, blockers)
    return PRDResult(replace(prd, status="approved"), True, [])
