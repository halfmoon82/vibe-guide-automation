"""Guided PRD checkpoints and optional, reference-only Skill profiles."""

from dataclasses import replace
import re
from typing import Any, Dict, Iterable, List

from .models import PRDCheckpoint, SkillProfile


PROFILE_NAMES = ("prd-discovery", "prd-critic", "prd-acceptance")
_FALLBACK_OPEN_QUESTION = "请补充未决产品选择"
_INVALID_EVIDENCE_LABELS = ("unverified_fact", "not_verified_fact")


def _as_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {"value": value}


def _question_from_text(value: Any) -> Any:
    """Extract the answerable text from an inline unresolved-evidence label."""

    if not isinstance(value, str):
        return value
    if value.strip().lower() in {"decision_pending", "open_question"}:
        return {"question": _FALLBACK_OPEN_QUESTION}
    match = re.search(r"(?:decision_pending|open_question)\s*[:：-]\s*(.+)", value, re.IGNORECASE)
    if match:
        return {"question": match.group(1).strip()}
    return value


def _unresolved_question(context: Any) -> Any:
    rationale = getattr(context, "rationale", {}) or {}
    question = rationale.get("product_question") if isinstance(rationale, dict) else None
    if question is not None:
        return question
    for key in ("decision_pending", "open_question"):
        value = rationale.get(key) if isinstance(rationale, dict) else None
        if value:
            return _question_from_text(value)
    if isinstance(rationale, dict):
        for value in rationale.values():
            if isinstance(value, dict) and value.get("status") in {"decision_pending", "open_question"}:
                return value
            if isinstance(value, str) and re.search(r"(?:decision_pending|open_question)\s*[:：-]", value, re.IGNORECASE):
                return _question_from_text(value)
            if isinstance(value, str) and value.strip().lower() in {"decision_pending", "open_question"}:
                return _question_from_text(value)
    return None


def _labels(text: Any) -> List[str]:
    text = str(text)
    return [
        label
        for label in ("verified_fact", "assumption", "decision_pending", "open_question")
        if re.search(
            r"(?<![A-Za-z0-9_]){}(?![A-Za-z0-9_])".format(re.escape(label)),
            text,
        )
    ]


def _has_invalid_evidence_label(text: Any) -> bool:
    text = str(text)
    return any(
        re.search(
            r"(?<![A-Za-z0-9_]){}(?![A-Za-z0-9_])".format(re.escape(label)),
            text,
        )
        for label in _INVALID_EVIDENCE_LABELS
    )


def validate_skill_profile(profile: SkillProfile) -> SkillProfile:
    if profile.name not in PROFILE_NAMES:
        raise ValueError("unsupported PRD Skill profile")
    if not re.fullmatch(r"https://github\.com/[^/]+/[^/]+(?:/.*)?", profile.source_url):
        raise ValueError("Skill source must be a GitHub HTTPS URL")
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", profile.commit_sha):
        raise ValueError("Skill commit SHA must be complete")
    if not profile.license.strip():
        raise ValueError("Skill license is required")
    if not isinstance(profile.selected_paths, list):
        raise TypeError("selected_paths must be a list")
    return profile


def evaluate_prd_checkpoints(context: Any) -> List[PRDCheckpoint]:
    """Evaluate the four PRD checkpoints without inventing product decisions."""

    unresolved = _unresolved_question(context)
    if unresolved is not None:
        if hasattr(unresolved, "question"):
            fields = {
                "question": unresolved.question,
                "options": list(unresolved.options),
                "impact": unresolved.impact,
            }
        else:
            fields = _as_mapping(unresolved)
            fields.setdefault("question", str(fields.get("value", "请补充未决产品选择")))
            fields.setdefault("options", [])
            fields.setdefault("impact", "该选择会影响后续 PRD 与技术规划")
        fields["required_user_action"] = "answer_question"
        return [
            PRDCheckpoint(
                kind="decision_pending",
                fields=fields,
                evidence=["decision_pending"],
                status="blocked_design",
            )
        ]

    rationale = getattr(context, "rationale", {}) or {}
    mapping = (
        ("framing", "framing"),
        ("solution_tradeoffs", "tradeoffs"),
        ("flow_rules", "flow"),
        ("acceptance_handoff", "acceptance"),
    )
    checkpoints = []
    global_evidence = []
    global_invalid = False
    if isinstance(rationale, dict):
        for key, value in rationale.items():
            global_invalid = global_invalid or _has_invalid_evidence_label(key) or _has_invalid_evidence_label(value)
            if key in {"verified_fact", "assumption", "decision_pending", "open_question"}:
                global_evidence.append(key)
            global_evidence.extend(_labels(value))
    global_evidence = list(dict.fromkeys(global_evidence))
    for kind, key in mapping:
        value = rationale.get(key, "") if isinstance(rationale, dict) else ""
        evidence = _labels(value)
        invalid = global_invalid or _has_invalid_evidence_label(value)
        if invalid:
            evidence = []
        elif not evidence and global_evidence:
            evidence = list(global_evidence)
        if invalid:
            status = "review_required"
        elif any(label in evidence for label in ("decision_pending", "open_question")):
            status = "blocked_design"
        elif evidence:
            status = "approved"
        else:
            status = "review_required"
        checkpoints.append(
            PRDCheckpoint(
                kind=kind,
                fields=(
                    ({"summary": str(value)} if value else {})
                    | ({"required_user_action": "continue_planning"} if status == "approved" else {})
                ),
                evidence=evidence,
                status=status,
            )
        )
    return checkpoints


def select_prd_profiles(selection: Dict[str, str], candidates: Iterable[SkillProfile]) -> List[SkillProfile]:
    """Select, skip, or defer reference records; never installs or copies Skill code."""

    if not isinstance(selection, dict):
        raise TypeError("profile selection must be a dictionary")
    by_name = {candidate.name: candidate for candidate in candidates}
    unknown = set(selection) - set(by_name)
    if unknown:
        raise ValueError("unknown Skill profile: " + ", ".join(sorted(unknown)))
    result = []
    for name in PROFILE_NAMES:
        candidate = by_name.get(name)
        if candidate is None:
            continue
        candidate = validate_skill_profile(candidate)
        action = selection.get(name)
        if action is None:
            action = "later"
        if action in {"select", "install"}:
            result.append(
                replace(
                    candidate,
                    status="selected",
                    installed_at="",
                    verification_status="recheck_before_install",
                    install_time=None,
                )
            )
        elif action == "skip":
            result.append(replace(candidate, status="skipped", installed_at="", verification_status="unverified", install_time=None))
        elif action in {"later", "defer", "deferred"}:
            result.append(replace(candidate, status="later", installed_at="", verification_status="unverified", install_time=None))
        else:
            raise ValueError("unsupported Skill profile selection: %s" % action)
    return result


def build_prd_guide_draft(request: str, code_evidence=None) -> Dict[str, Any]:
    """Create a source-labelled PRD draft from natural language and read-only evidence."""
    if not isinstance(request, str) or not request.strip():
        raise ValueError("request is required")
    evidence = list(code_evidence or [])
    return {
        "objective": {"value": request.strip(), "source": "user_confirmed"},
        "user_scenarios": [{"value": "用户希望解决所描述的问题并获得可验证结果", "source": "system_inferred"}],
        "success_criteria": [{"value": "结果可通过运行时验收证明", "source": "system_inferred"}],
        "code_evidence": [{"value": item, "source": "unverified"} for item in evidence],
        "non_goals": [{"value": "未明确的外部或部署动作", "source": "system_inferred"}],
    }


def render_planning_brief(plan_id: str, draft: Dict[str, Any], traceability=None) -> str:
    rows = traceability or []
    lines = ["# Planning Brief", "", "## 产品目标", str(draft.get("objective", {})), "", "## 用户场景", str(draft.get("user_scenarios", [])), "", "## 代码现状", str(draft.get("code_evidence", [])), "", "## 方案", "按已确认 PRD 目标生成最小变更方案。", "", "## 非目标", str(draft.get("non_goals", [])), "", "## Spec/Issue 映射与运行时验收", "| Goal | User scenario | Current evidence | Spec | Issue | DAG node | Runtime acceptance |", "|---|---|---|---|---|---|---|"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(k, "")) for k in ("goal","scenario","current_evidence","spec","issue","dag_node","runtime_acceptance")) + " |")
    return "\n".join(lines) + "\n"
