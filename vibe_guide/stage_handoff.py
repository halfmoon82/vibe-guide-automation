"""Builders for the V3 non-authorizing StageHandoff contract."""

from typing import Iterable, Optional
import json
import os
from pathlib import Path
import tempfile

from .models import Action, Phase, PRD, StageHandoff


_PLANNING_FORBIDDEN = [
    "create_spec", "create_issue", "create_dag", "create_authorization_card",
    "create_authorization", "create_run", "create_worker", "authorize", "monitor", "archive", "deploy",
]

_BLOCKED_QUESTION = {
    "blocked_design": "请补充或确认导致设计阻塞的产品决策",
    "blocked_unknown": "请提供可验证的缺失证据或确认下一次最小重试动作",
}


def build_stage_handoff(
    stage,
    status=None,
    plan_id="unplanned",
    plan_revision=1,
    evidence_refs=None,
    required_user_action=None,
    forbidden_automatic_actions=None,
    open_questions=None,
    prompt="",
    context=None,
):
    """Create a validated handoff; it never creates a run or worker."""
    if isinstance(stage, PRD):
        return handoff_for_prd(stage, evidence_refs=evidence_refs, open_questions=open_questions, plan_id=plan_id)
    stage = stage.value if isinstance(stage, Phase) else stage
    if status is None:
        status = "blocked_unknown"
    questions = list(open_questions or [])
    if status in _BLOCKED_QUESTION and not questions:
        questions = [_BLOCKED_QUESTION[status]]
    if required_user_action is None:
        if status in {"blocked_design", "blocked_unknown"}:
            required_user_action = "answer_question"
        elif stage in (Phase.PRD_APPROVED.value, Phase.SPEC_ISSUE_DAG.value):
            required_user_action = Action.CONTINUE_PLANNING.value
        else:
            required_user_action = "none"
    if stage == Phase.PRD_APPROVED.value and status == "approved":
        required_user_action = Action.CONTINUE_PLANNING.value
        prompt = prompt or "继续规划 Spec/Issue/DAG"
    # Caller additions are allowed, but cannot weaken the mandatory safety
    # boundary carried by every non-authorizing handoff.
    forbidden = list(dict.fromkeys(_PLANNING_FORBIDDEN + list(forbidden_automatic_actions or [])))
    return StageHandoff(
        stage=stage,
        status=status,
        plan_id=plan_id,
        plan_revision=plan_revision,
        evidence_refs=list(evidence_refs or []),
        required_user_action=required_user_action,
        forbidden_automatic_actions=forbidden,
        open_questions=questions,
        prompt=prompt,
        context=dict(context or {}),
    )


def handoff_for_prd(prd: PRD, evidence_refs=None, open_questions=None, plan_id="unplanned"):
    if not isinstance(prd, PRD):
        raise TypeError("prd must be a PRD")
    questions = list(open_questions or [])
    status = prd.status
    action = Action.CONTINUE_PLANNING.value if status == "approved" else "none"
    if status in _BLOCKED_QUESTION and not questions:
        questions = [_BLOCKED_QUESTION[status]]
    if status in {"draft", "blocked_design", "blocked_unknown"} and questions:
        action = "answer_question"
    return build_stage_handoff(
        Phase.PRD_APPROVED.value,
        status,
        plan_id,
        prd.revision,
        evidence_refs,
        action,
        open_questions=questions,
        prompt=("继续规划 Spec/Issue/DAG" if status == "approved" else (questions[0] if questions else "补齐 PRD 证据")),
    )


def render_stage_handoff(handoff: StageHandoff) -> str:
    if not isinstance(handoff, StageHandoff):
        raise TypeError("handoff must be a StageHandoff")
    return handoff.render()


stage_handoff_for_prd = handoff_for_prd


def save_stage_handoff(path, handoff: StageHandoff):
    """Atomically persist a JSON-safe handoff for later readback."""
    if not isinstance(handoff, StageHandoff):
        raise TypeError("handoff must be a StageHandoff")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + target.name + ".", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(handoff.to_dict(), stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_stage_handoff(path) -> StageHandoff:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise FileNotFoundError(str(target))
    return StageHandoff.from_dict(json.loads(target.read_text(encoding="utf-8")))


__all__ = ["StageHandoff", "build_stage_handoff", "handoff_for_prd", "stage_handoff_for_prd", "render_stage_handoff", "save_stage_handoff", "load_stage_handoff"]
