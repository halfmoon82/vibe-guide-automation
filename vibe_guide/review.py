"""Review matrix and root-cause finding bundles."""

from dataclasses import dataclass
from typing import List, Sequence


@dataclass(frozen=True)
class ReviewFinding:
    invariant_id: str
    severity: str
    root_cause: str
    symptom: str
    verification_command: str


@dataclass(frozen=True)
class FindingBundle:
    root_cause: str
    invariant_ids: List[str]
    findings: List[ReviewFinding]
    verification_command: str
    severity: str


@dataclass(frozen=True)
class ReviewResult:
    status: str
    bundles: List[FindingBundle]


def bundle_findings(findings: Sequence[ReviewFinding]) -> List[FindingBundle]:
    grouped = {}
    for finding in findings:
        grouped.setdefault(finding.root_cause, []).append(finding)
    result = []
    for root in sorted(grouped):
        items = grouped[root]
        commands = sorted({item.verification_command for item in items if item.verification_command})
        severity = sorted((item.severity for item in items), reverse=True)[0]
        result.append(FindingBundle(root, [item.invariant_id for item in items], list(items),
                                    commands[0] if commands else "", severity))
    return result


def accept_review(matrix) -> ReviewResult:
    if not matrix:
        return ReviewResult("blocked_unknown", [])
    findings = matrix if isinstance(matrix, (list, tuple)) else getattr(matrix, "findings", [])
    bundles = bundle_findings(findings)
    return ReviewResult("accepted" if not bundles else "rework_required", bundles)
