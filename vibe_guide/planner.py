"""Deterministic S0/S1 routing and product-decision helpers.

The planner is deliberately side-effect free.  It only classifies and describes
what artifacts a route may create; publishing and monitoring stay in the CLI.
"""

from dataclasses import dataclass, field, replace
import hashlib
import re
from typing import Any, Dict, List, Optional

from .models import PRD


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
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 5:
                raise ValueError("S1 dimensions must be integers from 0 to 5")
        if not isinstance(self.rationale, dict):
            raise TypeError("rationale must be a dictionary")


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
        if not isinstance(self.question, str) or not self.question.strip():
            raise ValueError("a product question requires text")
        if not isinstance(self.options, list) or len(self.options) < 2:
            raise ValueError("a product question requires at least two options")
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
        if not isinstance(self.options, list) or len(self.options) < 2:
            raise ValueError("decision options must contain at least two values")
        if len(self.options) != len(set(self.options)):
            raise ValueError("decision options must be unique")
        if self.recommendation not in self.options:
            raise ValueError("recommendation must be one of the options")
        if self.status not in {"unresolved", "approved"}:
            raise ValueError("unsupported decision status")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("decision revision must be a positive integer")
        if not self.id:
            object.__setattr__(self, "id", "decision-" + hashlib.sha256((self.question + "\0" + self.field).encode()).hexdigest()[:16])

    def render(self) -> str:
        status = "已决定：{}".format(self.selected) if self.status == "approved" else "待决定"
        return "{}\n选项：\n{}\n影响：{}\n建议：{}\n状态：{}".format(
            self.question, "\n".join("- " + option for option in self.options),
            self.impact, self.recommendation, status,
        )


@dataclass(frozen=True)
class PRDResult:
    prd: PRD
    approved: bool
    blockers: List[str]


_S1_MARKERS = (
    "并", "然后", "以及", "同时", "设计", "实现", "开发", "重构", "迁移",
    "集成", "测试", "部署", "系统", "多个", "workflow", "pipeline",
)
_ENGLISH_ACTIONS = {
    "add", "build", "create", "deploy", "design", "fix", "implement",
    "integrate", "migrate", "refactor", "rename", "test", "update", "write",
}


def classify_s0(message: str) -> S0Result:
    """Apply a cheap deterministic screen; uncertain requests proceed to S1."""
    normalized = " ".join(str(message).strip().lower().split())
    if not normalized:
        return S0Result(False, True, "s1", "任务内容为空，需要进一步判断")
    markers = [marker for marker in _S1_MARKERS if marker in normalized]
    english = [word for word in re.findall(r"[a-z]+", normalized) if word in _ENGLISH_ACTIONS]
    if len(english) >= 2:
        markers.extend(english)
    if markers:
        return S0Result(False, True, "s1", "检测到可能的多步骤或复杂任务：" + "、".join(markers))
    return S0Result(True, False, "simple", "未检测到复杂任务标记")


def score_s1(context: TaskContext) -> S1Score:
    values = (context.steps, context.domains, context.uncertainty, context.failure_cost, context.toolchain)
    return S1Score(sum(values), *values, rationale=dict(context.rationale))


def route_task(score: S1Score) -> str:
    if not isinstance(score, S1Score):
        raise TypeError("score must be an S1Score")
    if score.total < 0:
        raise ValueError("S1 total cannot be negative")
    if score.total <= 8:
        return "simple"
    if score.total <= 15:
        return "light_plan"
    return "complex"


_ARTIFACT_KEYS = ("plan.json", "nodes.json", "authorization-card.json", "monitor")


def artifact_policy(route: str, unavailable: bool = False) -> Dict[str, str]:
    """Return the contract state for every planning/monitor artifact."""
    if route not in {"simple", "light_plan", "complex"}:
        raise ValueError("unsupported route")
    if unavailable:
        return {key: "unavailable_without_complex_replan" for key in _ARTIFACT_KEYS}
    if route == "complex":
        return {
            "plan.json": "generated_after_plan_id_and_node_spec",
            "nodes.json": "generated_after_plan_id_and_node_spec",
            "authorization-card.json": "generated_after_plan_confirmation",
            "monitor": "available_after_authorization",
        }
    return {
        "plan.json": "not_generated",
        "nodes.json": "not_generated",
        "authorization-card.json": "not_generated",
        "monitor": "unavailable_without_complex_replan" if route == "light_plan" else "not_available",
    }


def build_routing_decision(
    screen: S0Result,
    score: Optional[S1Score] = None,
    *,
    unavailable: bool = False,
    next_action: Optional[str] = None,
    evidence_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the Revision 3 routing contract from the S0 screen and S1 score."""
    if not isinstance(screen, S0Result):
        raise TypeError("screen must be an S0Result")
    if screen.simple:
        return {
            "screen": "s0",
            "route": "simple",
            "score": None,
            "dimensions": None,
            "artifact_policy": artifact_policy("simple"),
            "next_action": next_action or "直接执行轻量任务",
            "evidence_ref": evidence_ref or "planner:s0",
            "thresholds": {"simple_max": 8, "light_plan_min": 9, "light_plan_max": 15, "complex_min": 16},
        }
    if not isinstance(score, S1Score):
        raise ValueError("S1 score is required for non-simple routing")
    route = route_task(score)
    if route == "light_plan":
        default_next_action = "轻规划直接执行；如需监工，请重新规划为 complex"
    else:
        default_next_action = "提供 plan_id 和 node_spec，继续复杂规划"
    return {
        "screen": "s1",
        "route": route,
        "score": score.total,
        "dimensions": {
            "steps": score.steps,
            "domains": score.domains,
            "uncertainty": score.uncertainty,
            "failure_cost": score.failure_cost,
            "toolchain": score.toolchain,
        },
        "artifact_policy": artifact_policy(route, unavailable=unavailable),
        "next_action": next_action or default_next_action,
        "evidence_ref": evidence_ref or "planner:s1:{}".format(score.total),
        "thresholds": {"simple_max": 8, "light_plan_min": 9, "light_plan_max": 15, "complex_min": 16},
    }


def routing_decision(
    score: Optional[S1Score] = None,
    route: Optional[str] = None,
    rationale: str = "",
    *,
    screen: Optional[str] = None,
    next_action: Optional[str] = None,
    evidence_ref: Optional[str] = None,
    unavailable: bool = False,
) -> Dict[str, Any]:
    """Build a JSON-safe, visible routing decision payload."""
    if score is None and route is None:
        raise ValueError("score or route is required")
    selected = route or route_task(score)  # type: ignore[arg-type]
    if score is None:
        if selected != "simple":
            raise ValueError("S1 score is required for non-simple routing")
        base = build_routing_decision(S0Result(True, False, "simple", rationale))
    else:
        if selected != route_task(score):
            raise ValueError("route does not match S1 score")
        base = build_routing_decision(
            S0Result(False, True, "s1", rationale), score,
            unavailable=unavailable,
            next_action=next_action,
            evidence_ref=evidence_ref,
        )
    if screen is not None:
        if screen not in {"s0", "s1"}:
            raise ValueError("screen must be s0 or s1")
        base["screen"] = screen
    if next_action is not None:
        base["next_action"] = next_action
    if evidence_ref is not None:
        base["evidence_ref"] = evidence_ref
    if unavailable and selected == "simple":
        base["artifact_policy"] = artifact_policy(selected, unavailable=True)
    return base


def render_routing_decision(decision: Dict[str, Any]) -> str:
    """Render a routing payload without implying authorization or execution."""
    if not isinstance(decision, dict) or decision.get("route") not in {"simple", "light_plan", "complex"}:
        raise ValueError("routing decision is invalid")
    policy = decision.get("artifact_policy", {})
    if set(policy) != set(_ARTIFACT_KEYS) or any(not isinstance(value, str) for value in policy.values()):
        raise ValueError("routing artifact policy is invalid")
    states = ", ".join("{}={}".format(key, policy[key]) for key in _ARTIFACT_KEYS)
    return "路由：{}（screen={}，S1 总分 {}）；下一步：{}；证据：{}；产物状态：{}".format(
        decision["route"], decision.get("screen", "未知"), decision.get("score", "未知"),
        decision.get("next_action", "未知"), decision.get("evidence_ref", "未知"), states,
    )


def render_planning_required_diagnostic(diagnostic: Dict[str, Any]) -> str:
    """Render the actionable, non-authorizing missing-plan diagnostic."""
    required = ("reason", "route_hint", "run_created", "worker_created", "provider_action_created")
    if not isinstance(diagnostic, dict) or diagnostic.get("status") != "planning_required" or any(key not in diagnostic for key in required):
        raise ValueError("planning-required diagnostic is invalid")
    return (
        "planning_required：reason={}；route_hint={}；run_created={}；worker_created={}；provider_action_created={}"
    ).format(*(diagnostic[key] for key in required))


def create_decision_card(question: ProductQuestion) -> DecisionCard:
    return DecisionCard(question.question, list(question.options), question.impact, question.recommendation or question.options[0])


def approve_prd(prd: PRD, decisions: List[DecisionCard]) -> PRDResult:
    blockers = [card.question for card in decisions if card.status != "approved" or card.selected not in card.options or not card.field]
    return PRDResult(replace(prd, status="approved" if not blockers else "blocked_decision"), not blockers, blockers)


def planning_required_diagnostic(plan_id: str, missing=None, reason: str = "") -> Dict[str, Any]:
    """Describe a missing/incomplete plan; callers must not start a run."""
    missing_items = list(missing or ["plan"])
    if not all(isinstance(item, str) and item for item in missing_items):
        raise ValueError("missing plan evidence must be non-empty strings")
    return {
        "status": "planning_required",
        "plan_id": plan_id,
        "missing": missing_items,
        "reason": "plan_artifact_missing",
        "route_hint": "light_plan_or_unpublished_complex_plan",
        "run_created": False,
        "worker_created": False,
        "provider_action_created": False,
    }


__all__ = [
    "S0Result", "TaskContext", "S1Score", "ProductQuestion", "DecisionCard", "PRD", "PRDResult",
    "classify_s0", "score_s1", "route_task", "artifact_policy", "build_routing_decision", "routing_decision", "render_routing_decision",
    "planning_required_diagnostic", "render_planning_required_diagnostic", "create_decision_card", "approve_prd",
]
