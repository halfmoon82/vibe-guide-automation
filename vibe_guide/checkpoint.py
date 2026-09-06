"""Context budget estimation and durable monitor checkpoints.

The module deliberately keeps the checkpoint small: it records the facts a
new monitor session needs to resume, while the run event log remains the
complete source of history.
"""

from dataclasses import dataclass, field, asdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Union

from .paths import ProjectPaths
from .state import RunSnapshot, load_snapshot


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CHECKPOINT_NAME = "monitor_checkpoint.json"


@dataclass(frozen=True)
class ContextBudgetPolicy:
    context_limit_tokens: Union[int, str]
    reserve_tokens: Optional[int] = None
    warning_ratio: float = 0.70
    checkpoint_ratio: float = 0.80
    hard_stop_ratio: float = 0.90

    def __post_init__(self) -> None:
        limit = self.context_limit_tokens
        if limit != "observed-model-limit":
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError("context_limit_tokens must be positive or observed-model-limit")
        if self.reserve_tokens is not None:
            if isinstance(self.reserve_tokens, bool) or not isinstance(self.reserve_tokens, int) or self.reserve_tokens < 0:
                raise ValueError("reserve_tokens must be a non-negative integer")
            if isinstance(limit, int) and self.reserve_tokens > limit:
                raise ValueError("reserve_tokens cannot exceed context limit")
        for name in ("warning_ratio", "checkpoint_ratio", "hard_stop_ratio"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not 0 < value <= 1:
                raise ValueError(name + " must be between 0 and 1")
        if not self.warning_ratio < self.checkpoint_ratio < self.hard_stop_ratio:
            raise ValueError("context thresholds must be increasing")
        if self.reserve_tokens is None and isinstance(limit, int):
            object.__setattr__(self, "reserve_tokens", max(8192, int(math.ceil(limit * 0.15))))

    @property
    def effective_reserve_tokens(self) -> Optional[int]:
        if self.reserve_tokens is not None:
            return self.reserve_tokens
        if isinstance(self.context_limit_tokens, int):
            return max(8192, int(math.ceil(self.context_limit_tokens * 0.15)))
        return None


@dataclass(frozen=True)
class BudgetEstimate:
    total_tokens: int
    limit_tokens: Optional[int]
    reserve_tokens: Optional[int]
    ratio: Optional[float]
    source: str
    breakdown: Dict[str, int]
    status: str

    @property
    def warning(self) -> bool:
        return self.status in {"warning", "checkpoint", "hard_stop", "blocked_unknown"}

    @property
    def should_checkpoint(self) -> bool:
        return self.status in {"checkpoint", "hard_stop", "blocked_unknown"}

    @property
    def hard_stop(self) -> bool:
        return self.status in {"hard_stop", "blocked_unknown"}

    @property
    def estimated_tokens(self) -> int:
        return self.total_tokens

    @property
    def method(self) -> str:
        return self.source


class ContextBudgetEstimator:
    def __init__(self, policy: ContextBudgetPolicy):
        self.policy = policy

    @staticmethod
    def _token_count(value: str, tokenizer: Any) -> Optional[int]:
        if not isinstance(value, str):
            raise TypeError("context parts must be strings")
        if tokenizer is None:
            return None
        try:
            if hasattr(tokenizer, "encode"):
                result = tokenizer.encode(value)
            elif hasattr(tokenizer, "count_tokens"):
                result = tokenizer.count_tokens(value)
            elif callable(tokenizer):
                result = tokenizer(value)
            else:
                return None
            if isinstance(result, bool):
                return None
            if isinstance(result, int):
                return max(0, result)
            return len(result)
        except Exception:
            return None

    @staticmethod
    def _conservative_count(value: str) -> int:
        # CJK/symbol characters are conservatively one token each. ASCII runs
        # use four characters per token, rounded up.
        ascii_count = sum(1 for char in value if ord(char) < 128)
        other_count = len(value) - ascii_count
        return other_count + int(math.ceil(ascii_count / 4.0)) if value else 0

    def classify(self, total_tokens: int, next_action_tokens: int = 0) -> str:
        if not isinstance(total_tokens, int) or total_tokens < 0:
            raise ValueError("total_tokens must be a non-negative integer")
        limit = self.policy.context_limit_tokens
        if limit == "observed-model-limit":
            return "blocked_unknown"
        ratio = total_tokens / float(limit)
        reserve = self.policy.effective_reserve_tokens or 0
        if ratio >= self.policy.hard_stop_ratio:
            return "hard_stop"
        if ratio >= self.policy.checkpoint_ratio or (
            next_action_tokens > 0 and total_tokens + next_action_tokens > limit - reserve
        ):
            return "checkpoint"
        if ratio >= self.policy.warning_ratio:
            return "warning"
        return "normal"

    def estimate(
        self,
        system_prompt: str,
        current_input: str,
        event_summary: str,
        checkpoint: str,
        expected_output: str,
        tokenizer: Any = None,
        next_action_tokens: int = 0,
    ) -> BudgetEstimate:
        values = {
            "system_prompt": system_prompt,
            "current_input": current_input,
            "event_summary": event_summary,
            "checkpoint": checkpoint,
            "expected_output": expected_output,
        }
        counts = {name: self._token_count(value, tokenizer) for name, value in values.items()}
        source = "tokenizer"
        if any(value is None for value in counts.values()):
            source = "conservative_characters"
            breakdown = {name: self._conservative_count(value) for name, value in values.items()}
        else:
            breakdown = {name: int(value) for name, value in counts.items()}
        total = sum(breakdown.values())
        limit = self.policy.context_limit_tokens
        limit_tokens = limit if isinstance(limit, int) else None
        ratio = total / float(limit_tokens) if limit_tokens else None
        status = self.classify(total, next_action_tokens) if limit_tokens else "blocked_unknown"
        return BudgetEstimate(total, limit_tokens, self.policy.effective_reserve_tokens, ratio, source, breakdown, status)


@dataclass
class MonitorCheckpoint:
    run_id: str
    plan_revision: str
    state_version: int
    last_event_seq: int
    next_action: str
    stop_conditions: List[str]
    sha256: str = ""
    authorization_digest: str = ""
    node_contract_digest: str = ""
    capability_contract_digest: str = ""
    nodes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    handles: Dict[str, str] = field(default_factory=dict)
    worker_profiles: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    evidence: List[Any] = field(default_factory=list)
    estimate: Optional[Dict[str, Any]] = None

    def _payload(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload.pop("sha256", None)
        return payload

    def _computed_sha(self) -> str:
        raw = json.dumps(self._payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id is required")
        if not isinstance(self.plan_revision, str) or not self.plan_revision:
            raise ValueError("plan_revision is required")
        if isinstance(self.state_version, bool) or not isinstance(self.state_version, int) or self.state_version < 1:
            raise ValueError("state_version must be positive")
        if isinstance(self.last_event_seq, bool) or not isinstance(self.last_event_seq, int) or self.last_event_seq < 0:
            raise ValueError("last_event_seq must be non-negative")
        if not isinstance(self.next_action, str) or not self.next_action.strip():
            raise ValueError("next_action is required")
        if not isinstance(self.stop_conditions, list) or not all(isinstance(x, str) and x for x in self.stop_conditions):
            raise ValueError("stop_conditions must be non-empty strings")
        for name in ("authorization_digest", "node_contract_digest", "capability_contract_digest"):
            value = getattr(self, name)
            if value and (not isinstance(value, str) or not _DIGEST.fullmatch(value)):
                raise ValueError(name + " must be a sha256 digest")
        computed = self._computed_sha()
        if self.sha256 and self.sha256 != computed:
            raise ValueError("checkpoint sha256 mismatch")
        self.sha256 = computed

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["sha256"] = self._computed_sha()
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MonitorCheckpoint":
        if not isinstance(data, dict):
            raise ValueError("checkpoint must be an object")
        return cls(**data)


def _checkpoint_path(paths: ProjectPaths, run_id: str) -> Path:
    # run_dir is intentionally not imported to keep this module's path check
    # small; load_snapshot performs the same run-id validation.
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id):
        raise ValueError("invalid run id")
    directory = paths.root / ".vibe" / "runs" / run_id
    if any(component.is_symlink() for component in (
        paths.root / ".vibe", paths.root / ".vibe" / "runs", directory
    )):
        raise ValueError("checkpoint path may not traverse a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / _CHECKPOINT_NAME


def write_checkpoint(paths: ProjectPaths, checkpoint: MonitorCheckpoint) -> None:
    path = _checkpoint_path(paths, checkpoint.run_id)
    previous = path.with_name("monitor_checkpoint.previous.json")
    if path.is_symlink() or previous.is_symlink():
        raise ValueError("checkpoint path may not be a symlink")
    payload = json.dumps(checkpoint.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() and not path.is_symlink():
            # Keep the last complete package available if the final replace
            # is interrupted.  The current file is left untouched until the
            # new temporary file is ready.
            shutil.copyfile(str(path), str(previous))
        os.replace(temporary_name, str(path))
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def load_checkpoint(paths: ProjectPaths, run_id: str) -> MonitorCheckpoint:
    path = _checkpoint_path(paths, run_id)
    failures = []
    for candidate in (path, path.with_name("monitor_checkpoint.previous.json")):
        try:
            raw = candidate.read_bytes()
            checkpoint = MonitorCheckpoint.from_dict(json.loads(raw.decode("utf-8")))
            if checkpoint.run_id != run_id:
                raise ValueError("checkpoint run id does not match its path")
            return checkpoint
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            failures.append(error)
    raise ValueError("no valid checkpoint for run " + str(run_id)) from (failures[-1] if failures else None)


def resume_from_checkpoint(paths: ProjectPaths, run_id: str) -> RunSnapshot:
    checkpoint = load_checkpoint(paths, run_id)
    snapshot = load_snapshot(paths, run_id)
    if checkpoint.state_version != snapshot.schema_version:
        raise ValueError("checkpoint state version does not match snapshot")
    if checkpoint.last_event_seq > snapshot.event_sequence:
        raise ValueError("checkpoint event sequence is ahead of snapshot")
    if checkpoint.handles != snapshot.handles:
        raise ValueError("checkpoint handles do not match snapshot")
    if checkpoint.nodes and set(checkpoint.nodes) != set(snapshot.nodes):
        raise ValueError("checkpoint node set does not match snapshot")
    for node_id, state in checkpoint.nodes.items():
        if not isinstance(state, dict) or state.get("status") != snapshot.nodes[node_id].get("status"):
            raise ValueError("checkpoint node state does not match snapshot")
    if checkpoint.authorization_digest and checkpoint.authorization_digest != snapshot.authorization_digest:
        raise ValueError("checkpoint authorization digest does not match snapshot")
    if checkpoint.node_contract_digest and checkpoint.node_contract_digest != snapshot.node_contract_digest:
        raise ValueError("checkpoint node contract digest does not match snapshot")
    if checkpoint.capability_contract_digest and checkpoint.capability_contract_digest != snapshot.capability_contract_digest:
        raise ValueError("checkpoint capability contract digest does not match snapshot")
    expected_revision = "{}@{}".format(snapshot.plan_id, snapshot.plan_version)
    if checkpoint.plan_revision not in {expected_revision, str(snapshot.plan_version), snapshot.plan_id}:
        raise ValueError("checkpoint plan revision does not match snapshot")
    return snapshot


__all__ = [
    "BudgetEstimate", "ContextBudgetEstimator", "ContextBudgetPolicy",
    "MonitorCheckpoint", "load_checkpoint", "resume_from_checkpoint", "write_checkpoint",
]
