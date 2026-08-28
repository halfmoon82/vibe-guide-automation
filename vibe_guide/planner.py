"""Deterministic task routing and product-decision gates."""

from dataclasses import dataclass, field, replace
import hashlib
import json
import re
from typing import Any, Dict, List, Optional

from .models import EVIDENCE_PRIORITY, PRD, PRDCheckpoint, SkillProfile, StageHandoff
from .prd_profiles import evaluate_prd_checkpoints, select_prd_profiles


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

    def __post_init__(self):
        for name in ("steps", "domains", "uncertainty", "failure_cost", "toolchain"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 5:
                raise ValueError("S1 dimensions must be integers from 0 to 5")


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
class PRDResult:
    prd: PRD
    approved: bool
    blockers: List[str]
    status: str = "draft"
    questions: List[str] = field(default_factory=list)
    continue_planning: bool = False
    downstream_artifact: Optional[Any] = None


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


def route_task(score: S1Score) -> str:
    if score.total < 0:
        raise ValueError("S1 total cannot be negative")
    if score.total <= 8:
        return "simple"
    if score.total <= 15:
        return "light_plan"
    return "complex"


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
        return PRDResult(replace(prd, status="blocked_design"), False, blockers, "blocked_design", blockers[:1], False, None)
    return PRDResult(replace(prd, status="approved"), True, [], "approved", [], True, None)


def build_stage_handoff(
    prd: PRD,
    open_questions: List[str],
    evidence_refs: List[str],
) -> StageHandoff:
    """Build a read-only PRD-to-planning handoff; it never grants authorization."""
    questions = [str(item) for item in open_questions if str(item).strip()]
    if prd.status == "approved" and not questions:
        readiness, action = "ready", "continue_planning"
        prompt = "PRD 已批准；如需进入 Spec/Issue/DAG，请继续规划。"
    elif prd.status in {"blocked_design", "blocked_decision"} or questions:
        readiness, action = "blocked_design", "answer_question"
        questions = questions[:1] or ["请回答未闭合的产品问题"]
        prompt = "请先回答一个高信息产品问题：{}".format(questions[0])
    elif prd.status == "blocked_unknown":
        readiness, action = "blocked_unknown", "answer_question"
        prompt = "请补充可验证的 PRD 证据后再继续规划。"
    elif prd.status == "review_required":
        readiness, action = "awaiting_user", "confirm_plan"
        prompt = "请确认 PRD 检查点后再发布规划产物。"
    else:
        readiness, action = "awaiting_user", "continue_planning"
        prompt = "请确认 PRD 检查点后继续规划。"
    return StageHandoff(
        from_stage="prd",
        from_status="blocked_design" if prd.status == "blocked_decision" else prd.status,
        to_stage="spec_issue_dag",
        readiness=readiness,
        evidence_refs=list(evidence_refs),
        open_questions=questions,
        required_user_action=action,
        forbidden_automatic_actions=["create_spec", "create_issue", "create_dag", "create_worker", "authorize", "deploy"],
        prompt=prompt,
        prd_revision=prd.revision,
    )


def render_stage_handoff(handoff: StageHandoff) -> str:
    return handoff.render()


def build_runtime_stage_handoff(
    from_status: str,
    evidence_refs: List[str],
    prompt: str,
    *,
    to_stage: str = "monitor",
    readiness: str = "ready",
    open_questions: Optional[List[str]] = None,
    required_user_action: str = "none",
    prd_revision: Optional[int] = None,
) -> StageHandoff:
    return StageHandoff(
        from_stage="monitor", from_status=from_status, to_stage=to_stage,
        readiness=readiness, evidence_refs=list(evidence_refs),
        open_questions=list(open_questions or []),
        required_user_action=required_user_action, prompt=prompt,
        forbidden_automatic_actions=["expand_scope", "create_worker", "authorize", "deploy"],
        prd_revision=prd_revision,
    )
