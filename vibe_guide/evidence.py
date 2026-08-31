"""Append-only generation evidence and repeated-rework classification."""

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .paths import ProjectPaths
from .state import _atomic_bytes, run_dir


@dataclass(frozen=True)
class GenerationEvidence:
    run_id: str
    issue_id: str
    generation: int
    task_id: str
    cursor: str
    worktree: str
    branch: str
    base_sha: str
    status: str

    def to_dict(self):
        return {"run_id": self.run_id, "issue_id": self.issue_id, "generation": self.generation,
                "task_id": self.task_id, "cursor": self.cursor, "worktree": self.worktree,
                "branch": self.branch, "base_sha": self.base_sha, "status": self.status}


@dataclass(frozen=True)
class IssueSummary:
    generations: List[int]
    original_task_id: str
    original_worktree: str = ""
    original_branch: str = ""


class ReworkDecision(str, Enum):
    CONTINUE_SAME_WORKER = "continue_same_worker"
    CONTRACT_OR_CALL_CHAIN_REVIEW_REQUIRED = "contract_or_call_chain_review_required"


@dataclass(frozen=True)
class ReviewResult:
    severity: str
    root_cause: str
    status: str


def _path(paths: ProjectPaths, evidence: GenerationEvidence) -> Path:
    directory = run_dir(paths, evidence.run_id, create=True) / "evidence" / evidence.issue_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("generation-%d.json" % evidence.generation)
    if path.is_symlink():
        raise ValueError("generation evidence may not be a symlink")
    return path


def write_generation_evidence(paths: ProjectPaths, evidence: GenerationEvidence) -> None:
    path = _path(paths, evidence)
    payload = (json.dumps(evidence.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError("historical generation evidence is immutable")
        return
    _atomic_bytes(path, payload)


def replay_summary(paths: ProjectPaths, run_id: str, issue_id: str) -> IssueSummary:
    directory = run_dir(paths, run_id, create=False) / "evidence" / issue_id
    entries = []
    for path in sorted(directory.glob("generation-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        entries.append(data)
    if not entries:
        raise FileNotFoundError(str(directory))
    entries.sort(key=lambda item: item["generation"])
    first = entries[0]
    return IssueSummary([item["generation"] for item in entries], first["task_id"],
                        first["worktree"], first["branch"])


def classify_rework(history: Sequence[ReviewResult]) -> ReworkDecision:
    counts = {}
    for result in history:
        key = (result.severity, result.root_cause)
        counts[key] = counts.get(key, 0) + 1
    return (ReworkDecision.CONTRACT_OR_CALL_CHAIN_REVIEW_REQUIRED
            if any(count >= 2 for count in counts.values())
            else ReworkDecision.CONTINUE_SAME_WORKER)
