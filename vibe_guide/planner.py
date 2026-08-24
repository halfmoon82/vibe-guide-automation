"""Deterministic task routing and product-decision gates."""

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional


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


def classify_s0(message: str) -> S0Result:
    """Apply a cheap rule screen; uncertain or multi-step text proceeds to S1."""
    normalized = " ".join(message.strip().lower().split())
    if not normalized:
        return S0Result(False, True, "s1", "任务内容为空，需要进一步判断")
    markers = [marker for marker in _S1_MARKERS if marker in normalized]
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
        if card.status != "approved" or card.selected not in card.options:
            blockers.append(card.question)
    if blockers:
        return PRDResult(replace(prd, status="blocked_decision"), False, blockers)
    return PRDResult(replace(prd, status="approved"), True, [])
