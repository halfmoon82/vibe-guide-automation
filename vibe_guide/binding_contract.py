"""Two-phase binding contracts used by the V4.4 monitor.

The intent is immutable supervisor-owned identity.  A proof is provider-owned
runtime evidence and may be refreshed (for example, with a new cursor) while
remaining bound to the same intent.
"""

from dataclasses import dataclass, fields
import hashlib
import json
import re
from typing import Any, Dict


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_SHA1 = re.compile(r"^[0-9a-fA-F]{40}$")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("%s must be a non-empty string" % name)
    return value


def _digest(value: Any, name: str) -> str:
    value = _text(value, name)
    if not _SHA256.fullmatch(value):
        raise ValueError("%s must be a SHA-256 digest" % name)
    return value.lower()


@dataclass(frozen=True)
class BindingIntent:
    """Minimal identity envelope created before provider dispatch."""

    run_id: str
    plan_id: str
    plan_revision: int
    node_id: str
    task_id: str
    role: str
    generation: int
    writer: str
    worktree: str
    branch: str
    base_sha: str
    authorization_digest: str
    node_contract_digest: str

    def __post_init__(self) -> None:
        for name in ("run_id", "plan_id", "node_id", "task_id", "role", "writer", "worktree", "branch"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        for name in ("plan_revision", "generation"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("%s must be a positive integer" % name)
        base = _text(self.base_sha, "base_sha")
        if not _SHA1.fullmatch(base):
            raise ValueError("base_sha must be a 40 character SHA-1")
        object.__setattr__(self, "base_sha", base.lower())
        for name in ("authorization_digest", "node_contract_digest"):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
    def to_dict(self) -> Dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @property
    def digest(self) -> str:
        payload = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BindingIntent":
        if not isinstance(data, dict):
            raise TypeError("BindingIntent data must be a dictionary")
        values = dict(data)
        supplied = values.pop("digest", None)
        allowed = {field.name for field in fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError("unknown BindingIntent fields: %s" % ", ".join(sorted(unknown)))
        result = cls(**values)
        if supplied is not None and supplied != result.digest:
            raise ValueError("binding intent digest mismatch")
        return result


@dataclass(frozen=True)
class BindingProof:
    """Structured provider evidence collected after dispatch."""

    provider_task_id: str
    provider_host: str
    lease_id: str
    cursor: str
    observed_worktree: str
    observed_branch: str
    observed_writer: str
    observed_role: str
    observed_generation: int
    observed_contract_digest: str

    def __post_init__(self) -> None:
        for name in ("provider_task_id", "provider_host", "lease_id", "cursor", "observed_worktree", "observed_branch", "observed_writer", "observed_role"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if isinstance(self.observed_generation, bool) or not isinstance(self.observed_generation, int) or self.observed_generation < 1:
            raise ValueError("observed_generation must be a positive integer")
        object.__setattr__(self, "observed_contract_digest", _digest(self.observed_contract_digest, "observed_contract_digest"))

    def to_dict(self) -> Dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BindingProof":
        if not isinstance(data, dict):
            raise TypeError("BindingProof data must be a dictionary")
        allowed = {field.name for field in fields(cls)}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError("unknown BindingProof fields: %s" % ", ".join(sorted(unknown)))
        return cls(**data)

    def matches(self, intent: BindingIntent) -> bool:
        if not isinstance(intent, BindingIntent):
            raise TypeError("intent must be a BindingIntent")
        return (
            self.observed_worktree == intent.worktree
            and self.observed_branch == intent.branch
            and self.observed_writer == intent.writer
            and self.observed_role == intent.role
            and self.observed_generation == intent.generation
            and self.observed_contract_digest == intent.node_contract_digest
        )
