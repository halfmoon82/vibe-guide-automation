"""Prompt fragments built from on-disk run artifacts.

The developer rework continuation (#275) must carry this round's reviewer
findings verbatim so the session sees what to fix instead of re-deriving it
from the business contract.  The runner reads the reviewer delivery archive
under ``.vibe/provider-actions/deliveries/``; when no matching delivery or
finding exists the prompt is emitted unchanged -- nothing is fabricated.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .state import durable_projection

#: Every auto-scope sentence starts with this, so tests can find it (#381).
AUTO_SCOPE_RULE_PREFIX = "自动放行："


def auto_scope_rule_sentence(project_root: Any) -> str:
    """One sentence telling a developer which out-of-scope files the gate accepts (#381).

    Mirrors ``scope_landing.landing_report``: ``tests/`` files, the project's
    ``auto_scope_paths``, and -- only when the project approved it -- any
    repository file that changes no approved acceptance verdict, each provided
    no other active node holds it.  An unreadable or invalid config never
    widens the sentence (same fallback as the monitor).
    """
    from .config import load_project_config

    paths: tuple = ()
    approved = False
    if project_root is not None:
        try:
            config = load_project_config(Path(project_root))
            paths, approved = tuple(config.auto_scope_paths), bool(config.acceptance_scope_rule_approved)
        except (OSError, ValueError, TypeError):
            paths, approved = (), False
    targets = "tests/ 下的文件"
    if paths:
        targets += "、项目登记的 " + "、".join(paths)
    if approved:
        targets += "，以及不改变任何已批准验收例子判定结果的同仓库文件"
    return (
        AUTO_SCOPE_RULE_PREFIX + targets
        + "，只要没有被其他活跃节点占用，就可以直接修改并随本次交付提交，交付门会自动放行并记录，"
        "不需要监工中转（tests/ 外的依赖清单、部署、凭据、外部系统配置不在此列）。"
    )

#: Severities that never send a node back to the developer.  This mirrors
#: ``Monitor._review_finding_disposition``: only P3 is an observation; P0-P2,
#: P4/P5 and any missing or malformed severity all return to the developer,
#: so every one of them must ride along in the rework prompt.
_NON_REWORK_SEVERITIES = {"P3"}

_REVIEWER_DELIVERY = re.compile(r"-reviewer-g(\d+)\.json$")

#: Carriers ``Monitor._review_finding_severity`` inspects for a finding:
#: ``finding`` and ``review_finding`` at payload level, each optionally
#: holding a one-level nested ``finding``.
_FINDING_CARRIERS = ("finding", "review_finding")


def _finding_severity(finding: Any) -> Optional[str]:
    if isinstance(finding, dict):
        value = finding.get("severity")
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def _payload_severity(payload: Dict[str, Any]) -> Optional[str]:
    """Extract the round severity the way ``Monitor`` does."""
    values: List[Any] = [payload.get("severity")]
    for key in _FINDING_CARRIERS:
        value = payload.get(key)
        if isinstance(value, dict):
            values.append(value.get("severity"))
            nested = value.get("finding")
            if isinstance(nested, dict):
                values.append(nested.get("severity"))
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def _string_finding_entry(text: str, severity: Optional[str]) -> Dict[str, Any]:
    """Wrap a free-text finding so it reaches the reworked session.

    ``finding`` itself is a provider-text key and would be masked by the
    durable projection, so the verbatim text lives under ``summary``.
    """
    entry: Dict[str, Any] = {"summary": text}
    if severity is not None:
        entry["severity"] = severity
    return entry


def _payload_findings(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Collect this round's reviewer findings from the supported carriers."""
    round_severity = _payload_severity(payload)
    findings: List[Dict[str, Any]] = []
    for key in _FINDING_CARRIERS:
        value = payload.get(key)
        if isinstance(value, dict):
            findings.append(value)
            nested = value.get("finding")
            if isinstance(nested, dict):
                findings.append(nested)
            elif isinstance(nested, str) and nested.strip():
                findings.append(
                    _string_finding_entry(
                        nested, _finding_severity(value) or round_severity
                    )
                )
        elif isinstance(value, str) and value.strip():
            findings.append(_string_finding_entry(value, round_severity))
    extra = payload.get("extra_findings")
    if isinstance(extra, list):
        findings.extend(item for item in extra if isinstance(item, dict))
    evidence = payload.get("evidence")
    if isinstance(evidence, dict):
        nested = evidence.get("findings")
        if isinstance(nested, list):
            findings.extend(item for item in nested if isinstance(item, dict))
    seen = set()
    kept: List[Dict[str, Any]] = []
    for item in findings:
        if _finding_severity(item) in _NON_REWORK_SEVERITIES:
            continue
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        kept.append(item)
    return kept


def _latest_reviewer_findings(contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the newest ``review_finding`` delivery's findings, or [].

    Selection is by reviewer generation: the highest ``-reviewer-g<N>.json``
    whose payload reports ``review_finding`` is the round this rework answers.
    Any read/parse miss yields [] so the prompt is emitted unchanged.
    """
    run_id = contract.get("run_id")
    node_id = contract.get("node_id")
    project_root = contract.get("project_root")
    if not (isinstance(run_id, str) and run_id and isinstance(node_id, str) and node_id):
        return []
    if not (isinstance(project_root, str) and project_root):
        return []
    deliveries = Path(project_root) / ".vibe" / "provider-actions" / "deliveries"
    if not deliveries.is_dir():
        return []
    best_generation = -1
    best_findings: List[Dict[str, Any]] = []
    for path in deliveries.glob("{}-{}-reviewer-g*.json".format(run_id, node_id)):
        match = _REVIEWER_DELIVERY.search(path.name)
        if match is None:
            continue
        generation = int(match.group(1))
        if generation <= best_generation:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        payload = data.get("payload") if isinstance(data, dict) else None
        if not isinstance(payload, dict) or payload.get("event") != "review_finding":
            continue
        best_generation = generation
        best_findings = _payload_findings(payload)
    return best_findings


#: Dispatch-prompt note for the integration reviewer: how a defect reaches the
#: upstream node that owns it (copyable payload).  Without ``target_node_id``
#: the finding only sends this reviewer round again and parks after three.
INTEGRATION_REVIEW_ROUTING_NOTE = (
    "整合审查发现缺陷时用 review_finding 上报，并在 worker-deliver 的 payload 顶层带 target_node_id"
    "（缺陷所在的已验收上游节点 id）；缺陷文件都在该节点原范围内时监控直接让它返工，"
    "需要新增文件再带 scope_expansion。可复制示例："
    "{\"event\": \"review_finding\", \"finding\": {\"severity\": \"p1\", \"detail\": \"<一句话>\"}, "
    "\"target_node_id\": \"<上游节点 id>\", "
    "\"scope_expansion\": {\"files\": [\"<新增文件>\"], \"reason\": \"<为什么>\", "
    "\"acceptance_examples_unchanged\": true, \"adds_third_party_dependency\": false, "
    "\"visibility_downgrade\": false}}。无需新增文件时省略 scope_expansion。\n"
)


def _integration_findings(contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The integration reviewer's finding that sent this node back, or [].

    The monitor stamps ``integration_finding_ref`` (reviewer node and
    generation) on the rework contract; the finding itself is read from that
    reviewer's archived report.  Any miss yields [].
    """
    ref = contract.get("integration_finding_ref")
    run_id = contract.get("run_id")
    project_root = contract.get("project_root")
    if not (isinstance(ref, dict) and isinstance(ref.get("node_id"), str) and ref["node_id"]
            and type(ref.get("generation")) is int
            and isinstance(run_id, str) and run_id
            and isinstance(project_root, str) and project_root):
        return []
    path = (Path(project_root) / ".vibe" / "provider-actions" / "deliveries"
            / "{}-{}-reviewer-g{}.json".format(run_id, ref["node_id"], ref["generation"]))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    payload = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(payload, dict) or payload.get("event") != "review_finding":
        return []
    return _payload_findings(payload)


def rework_findings_section(contract: Dict[str, Any]) -> str:
    """Append this round's reviewer findings to a developer rework prompt.

    Returns an empty string -- leaving the prompt byte-identical to before --
    for non-developer roles, non-rework phases, missing deliveries, and rounds
    with no findings.  Provider free text inside a finding is projected with
    the durable key-name rules: sensitive keys are dropped, provider-text keys
    (``detail``/``evidence``/...) are replaced by the placeholder, and every
    other field keeps its original text.
    """
    if contract.get("role") == "reviewer" and contract.get("node_id") == "integration-review":
        return INTEGRATION_REVIEW_ROUTING_NOTE
    if contract.get("role") != "developer" or contract.get("phase") != "rework":
        return ""
    findings = _latest_reviewer_findings(contract) + _integration_findings(contract)
    note = contract.get("patch_rework_note")
    note_text = ""
    if isinstance(note, str) and note.strip():
        # Supervisor-stated defect of an accepted-patch rework (scope-expand).
        note_text = "监工指出的待修缺陷（必须逐条修复并在交付证据里说明）：{}\n".format(note.strip())
    if not findings:
        return note_text
    projected = [durable_projection(item) for item in findings]
    return note_text + (
        "本轮 reviewer findings（逐条修复并在交付证据里说明每条处理结果）：{}\n".format(
            json.dumps(projected, ensure_ascii=False, sort_keys=True)
        )
    )
