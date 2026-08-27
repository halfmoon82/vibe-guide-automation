"""Evidence-bound context budgeting and monitor checkpoint persistence."""

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Dict, List, Optional, Union


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_NAME = "monitor_checkpoint.json"


@dataclass(frozen=True)
class ContextBudgetPolicy:
    context_limit_tokens: Union[int, str]
    reserve_tokens: Optional[int] = None
    warning_ratio: float = 0.70
    checkpoint_ratio: float = 0.80
    hard_stop_ratio: float = 0.90

    def __post_init__(self):
        limit = self.context_limit_tokens
        if limit != "observed-model-limit" and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        ):
            raise ValueError("context limit must be positive or observed-model-limit")
        if self.reserve_tokens is None and isinstance(limit, int):
            object.__setattr__(self, "reserve_tokens", max(8192, int(math.ceil(limit * 0.15))))
        if self.reserve_tokens is not None and (
            isinstance(self.reserve_tokens, bool)
            or not isinstance(self.reserve_tokens, int)
            or self.reserve_tokens < 0
        ):
            raise ValueError("reserve tokens must be non-negative")
        if (
            self.reserve_tokens is not None
            and isinstance(limit, int)
            and self.reserve_tokens > limit
        ):
            raise ValueError("reserve tokens cannot exceed context limit")
        if not 0 < self.warning_ratio < self.checkpoint_ratio < self.hard_stop_ratio <= 1:
            raise ValueError("budget thresholds must increase within (0, 1]")

    @property
    def effective_reserve_tokens(self):
        return self.reserve_tokens


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
    def estimated_tokens(self):
        return self.total_tokens

    @property
    def method(self):
        return self.source

    @property
    def warning(self):
        return self.status in {"warning", "checkpoint", "hard_stop", "blocked_unknown"}

    @property
    def should_checkpoint(self):
        return self.status in {"checkpoint", "hard_stop", "blocked_unknown"}

    @property
    def hard_stop(self):
        return self.status in {"hard_stop", "blocked_unknown"}


class ContextBudgetEstimator:
    _PARTS = ("system_prompt", "current_input", "event_summary", "checkpoint", "expected_output")

    def __init__(self, policy: ContextBudgetPolicy):
        self.policy = policy

    @staticmethod
    def _tokenize(value: str, tokenizer: Any) -> Optional[int]:
        if not isinstance(value, str):
            raise TypeError("context sections must be strings")
        try:
            if tokenizer is None:
                return None
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
            return max(0, result) if isinstance(result, int) else len(result)
        except Exception:
            return None

    @staticmethod
    def _conservative(value: str) -> int:
        ascii_chars = sum(ord(c) < 128 for c in value)
        non_ascii = len(value) - ascii_chars
        return non_ascii + int(math.ceil(ascii_chars / 4.0)) if value else 0

    def classify(self, total_tokens: int, next_action_tokens: int = 0) -> str:
        if not isinstance(total_tokens, int) or total_tokens < 0:
            raise ValueError("total tokens must be non-negative")
        if self.policy.context_limit_tokens == "observed-model-limit":
            return "blocked_unknown"
        limit = self.policy.context_limit_tokens
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

    def estimate(self, system_prompt: str, current_input: str, event_summary: str,
                 checkpoint: str, expected_output: str, tokenizer: Any = None,
                 next_action_tokens: int = 0) -> BudgetEstimate:
        values = dict(zip(self._PARTS, (system_prompt, current_input, event_summary, checkpoint, expected_output)))
        counts = {key: self._tokenize(value, tokenizer) for key, value in values.items()}
        source = "tokenizer"
        if any(value is None for value in counts.values()):
            source = "conservative_characters"
            breakdown = {key: self._conservative(value) for key, value in values.items()}
        else:
            breakdown = {key: int(value) for key, value in counts.items()}
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

    @property
    def node_states(self):
        """Backward-compatible alias for callers using the descriptive name."""
        return self.nodes

    def _payload(self):
        data = asdict(self)
        data.pop("sha256", None)
        return data

    def _digest(self):
        encoded = json.dumps(self._payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def __post_init__(self):
        if not isinstance(self.run_id, str) or not _RUN_ID.fullmatch(self.run_id):
            raise ValueError("invalid run id")
        if not isinstance(self.plan_revision, str) or not self.plan_revision:
            raise ValueError("plan revision is required")
        if not isinstance(self.state_version, int) or self.state_version < 1:
            raise ValueError("state version must be positive")
        if not isinstance(self.last_event_seq, int) or self.last_event_seq < 0:
            raise ValueError("event sequence must be non-negative")
        if not isinstance(self.next_action, str) or not self.next_action.strip():
            raise ValueError("next action is required")
        if not isinstance(self.stop_conditions, list) or not all(isinstance(x, str) and x for x in self.stop_conditions):
            raise ValueError("stop conditions must be non-empty strings")
        for value in (self.authorization_digest, self.node_contract_digest, self.capability_contract_digest):
            if value and not _DIGEST.fullmatch(value):
                raise ValueError("digest must be sha256")
        calculated = self._digest()
        if self.sha256 and self.sha256 != calculated:
            raise ValueError("checkpoint digest mismatch")
        self.sha256 = calculated

    def to_dict(self):
        data = asdict(self)
        data["sha256"] = self._digest()
        return data

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("checkpoint must be an object")
        return cls(**data)


def _path(paths: Any, run_id: str) -> Path:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("invalid run id")
    root = Path(paths if isinstance(paths, (str, Path)) else paths.root)
    vibe = root / ".vibe"
    runs = vibe / "runs"
    directory = runs / run_id
    for component in (vibe, runs, directory):
        if component.is_symlink():
            raise ValueError("checkpoint path may not traverse a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / _NAME


def write_checkpoint(paths: Any, checkpoint: MonitorCheckpoint) -> None:
    destination = _path(paths, checkpoint.run_id)
    previous = destination.with_name("monitor_checkpoint.previous.json")
    if destination.is_symlink():
        raise ValueError("checkpoint path may not be a symlink")
    fd, temporary = tempfile.mkstemp(prefix="." + _NAME + ".", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(checkpoint.to_dict(), stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        if destination.exists():
            if previous.is_symlink():
                raise ValueError("checkpoint path may not be a symlink")
            os.replace(destination, previous)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_checkpoint(paths: Any, run_id: str) -> MonitorCheckpoint:
    destination = _path(paths, run_id)
    candidates = (destination, destination.with_name("monitor_checkpoint.previous.json"))
    errors = []
    for candidate in candidates:
        try:
            checkpoint = MonitorCheckpoint.from_dict(json.loads(candidate.read_text(encoding="utf-8")))
            if checkpoint.run_id != run_id:
                raise ValueError("checkpoint run id mismatch")
            return checkpoint
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            errors.append(error)
    raise ValueError("no valid checkpoint for run " + run_id) from (errors[-1] if errors else None)


def resume_from_checkpoint(paths: Any, run_id: str):
    checkpoint = load_checkpoint(paths, run_id)
    from .state import load_snapshot
    snapshot = load_snapshot(paths, run_id)
    if checkpoint.state_version != snapshot.schema_version:
        raise ValueError("checkpoint state version mismatch")
    if checkpoint.last_event_seq > snapshot.event_sequence:
        raise ValueError("checkpoint event sequence is ahead")
    if checkpoint.handles != snapshot.handles:
        raise ValueError("checkpoint handles mismatch")
    if checkpoint.nodes and set(checkpoint.nodes) != set(snapshot.nodes):
        raise ValueError("checkpoint node set mismatch")
    for node_id, state in checkpoint.nodes.items():
        if not isinstance(state, dict) or state.get("status") != snapshot.nodes[node_id].get("status"):
            raise ValueError("checkpoint node state mismatch")
    if checkpoint.authorization_digest and checkpoint.authorization_digest != snapshot.authorization_digest:
        raise ValueError("checkpoint authorization mismatch")
    if checkpoint.node_contract_digest and checkpoint.node_contract_digest != snapshot.node_contract_digest:
        raise ValueError("checkpoint node contract mismatch")
    if checkpoint.capability_contract_digest and checkpoint.capability_contract_digest != snapshot.capability_contract_digest:
        raise ValueError("checkpoint capability contract mismatch")
    expected_revision = "{}@{}".format(snapshot.plan_id, snapshot.plan_version)
    if checkpoint.plan_revision not in {expected_revision, str(snapshot.plan_version), snapshot.plan_id}:
        raise ValueError("checkpoint plan revision mismatch")
    return snapshot


__all__ = ["BudgetEstimate", "ContextBudgetEstimator", "ContextBudgetPolicy", "MonitorCheckpoint", "write_checkpoint", "load_checkpoint", "resume_from_checkpoint"]
