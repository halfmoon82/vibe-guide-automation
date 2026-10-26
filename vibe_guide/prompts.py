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


def rework_findings_section(contract: Dict[str, Any]) -> str:
    """Append this round's reviewer findings to a developer rework prompt.

    Returns an empty string -- leaving the prompt byte-identical to before --
    for non-developer roles, non-rework phases, missing deliveries, and rounds
    with no findings.  Provider free text inside a finding is projected with
    the durable key-name rules: sensitive keys are dropped, provider-text keys
    (``detail``/``evidence``/...) are replaced by the placeholder, and every
    other field keeps its original text.
    """
    if contract.get("role") != "developer" or contract.get("phase") != "rework":
        return ""
    findings = _latest_reviewer_findings(contract)
    if not findings:
        return ""
    projected = [durable_projection(item) for item in findings]
    return (
        "本轮 reviewer findings（逐条修复并在交付证据里说明每条处理结果）：{}\n".format(
            json.dumps(projected, ensure_ascii=False, sort_keys=True)
        )
    )
