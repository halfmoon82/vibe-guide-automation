"""V4 canonical project/provider binding lifecycle and recovery safety."""

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Iterable, Optional, Tuple

LIFECYCLE_STATES = ("policy_ready", "dispatch_pending", "binding_observed", "binding_verified", "binding_drift", "isolated", "blocked_unknown")
V4_RECOVERY_ORDER = ("probe", "same_task", "native", "bootstrap", "local", "isolate")
_SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
_UNSAFE_RECOVERY_KEYS = {"stash", "reset", "clean", "manual_stash", "manual_reset", "manual_clean", "host_checkout", "host_checkout_write", "host_write", "successor", "successor_candidate", "second_writer", "second_writer_candidate", "new_writer", "target", "target_contract", "target_change", "target_contract_change", "allowlist_change"}

def _text(value, name):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("%s is required" % name)
    return value

def _absolute(value, name):
    value = _text(value, name)
    if not Path(value).is_absolute():
        raise ValueError("%s must be absolute" % name)
    return str(Path(value))

def _sha(value, name):
    value = _text(value, name)
    if not _SHA40.fullmatch(value):
        raise ValueError("%s must be a 40-hex SHA" % name)
    return value.lower()

def _paths(values: Iterable[str]) -> Tuple[str, ...]:
    result = tuple(_text(item, "allowlist item") for item in values)
    if len(result) != len(set(result)):
        raise ValueError("allowlist contains duplicates")
    return result

@dataclass(frozen=True)
class RequestedBindingPolicy:
    project_root: str; issue_id: str; developer_task: str; reviewer_task: str; provider: str; mode: str; worktree: str; branch: str; base_sha: str; allowlist: Tuple[str, ...] = field(default_factory=tuple); plan_revision: int = 1; writer: str = ""
    @property
    def status(self): return "policy_ready"
    def __post_init__(self):
        object.__setattr__(self, "project_root", _absolute(self.project_root, "project_root")); object.__setattr__(self, "worktree", _absolute(self.worktree, "worktree"))
        for name in ("issue_id", "developer_task", "reviewer_task", "provider", "mode", "branch"): object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.mode not in {"visible", "background"}: raise ValueError("mode must be visible or background")
        if self.developer_task == self.reviewer_task: raise ValueError("developer and reviewer tasks must be distinct")
        if self.branch == "detached": raise ValueError("policy branch cannot be detached")
        object.__setattr__(self, "base_sha", _sha(self.base_sha, "base_sha")); object.__setattr__(self, "allowlist", _paths(self.allowlist))
        if isinstance(self.plan_revision, bool) or not isinstance(self.plan_revision, int) or self.plan_revision < 1: raise ValueError("plan_revision must be positive")
        writer = self.writer or self.developer_task
        if writer not in {self.developer_task, self.reviewer_task}: raise ValueError("writer must be one of the task identities")
        object.__setattr__(self, "writer", writer)

@dataclass(frozen=True)
class ProviderRuntimeBinding:
    task_id: Optional[str]; host: Optional[str]; mode: Optional[str]; project_root: Optional[str]; checkout_worktree: Optional[str]; branch: Optional[str]; base_sha: Optional[str]; head_sha: Optional[str]; allowlist: Tuple[str, ...] = field(default_factory=tuple); provider: Optional[str] = None; developer_task_id: Optional[str] = None; reviewer_task_id: Optional[str] = None; lease: Optional[str] = None; cursor: Optional[str] = None; evidence_refs: Tuple[str, ...] = field(default_factory=tuple); ownership: Optional[str] = None; continuation: Optional[str] = None; baseline: Optional[str] = None; managed_root: Optional[str] = None
    @property
    def status(self): return "binding_observed"
    def __post_init__(self):
        for name in ("task_id", "host", "mode", "project_root", "checkout_worktree", "branch", "base_sha", "head_sha", "provider", "developer_task_id", "reviewer_task_id", "lease", "cursor", "ownership", "continuation", "baseline", "managed_root"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip() or "\x00" in value): raise ValueError("%s is invalid" % name)
        for name in ("project_root", "checkout_worktree", "managed_root"):
            value = getattr(self, name)
            if value is not None and not Path(value).is_absolute(): raise ValueError("%s must be absolute" % name)
        for name in ("base_sha", "head_sha"):
            value = getattr(self, name)
            if value is not None and not _SHA40.fullmatch(value): raise ValueError("%s must be a 40-hex SHA" % name)
        object.__setattr__(self, "allowlist", _paths(self.allowlist))

@dataclass(frozen=True)
class BindingVerificationResult:
    status: str; business_write_allowed: bool; missing: Tuple[str, ...] = field(default_factory=tuple); conflicts: Tuple[str, ...] = field(default_factory=tuple)
    def __post_init__(self):
        if self.status not in LIFECYCLE_STATES: raise ValueError("unsupported lifecycle state")
        if type(self.business_write_allowed) is not bool: raise TypeError("business_write_allowed must be bool")

def verify_binding(policy, observed, prior_task_id=None):
    if not isinstance(policy, RequestedBindingPolicy): raise TypeError("policy must be RequestedBindingPolicy")
    if observed is None: return BindingVerificationResult("dispatch_pending", False)
    if not isinstance(observed, ProviderRuntimeBinding): return BindingVerificationResult("blocked_unknown", False, ("provider_observation",))
    missing = ["task_id"] if observed.task_id in (None, "") else []
    conflicts = []
    if prior_task_id not in (None, "") and observed.task_id not in (None, prior_task_id): conflicts.append("task_id")
    for name in ("mode", "project_root", "branch"):
        if getattr(observed, name) not in (None, "") and getattr(observed, name) != getattr(policy, name): conflicts.append(name)
    if observed.provider not in (None, "") and observed.provider != policy.provider:
        conflicts.append("provider")
    if observed.base_sha not in (None, "") and observed.base_sha != policy.base_sha: conflicts.append("base_sha")
    if observed.baseline not in (None, "") and observed.baseline != policy.base_sha: conflicts.append("baseline")
    if observed.checkout_worktree not in (None, "") and observed.checkout_worktree != policy.worktree:
        try:
            if observed.ownership not in {"verified", "attested", "owned"} and not observed.managed_root:
                conflicts.append("checkout_worktree")
            elif observed.managed_root:
                Path(observed.checkout_worktree).resolve().relative_to(Path(observed.managed_root).resolve())
        except (ValueError, OSError):
            conflicts.append("checkout_worktree")
    if observed.allowlist and observed.allowlist != policy.allowlist: conflicts.append("allowlist")
    if observed.developer_task_id not in (None, policy.developer_task): conflicts.append("developer_task_id")
    if observed.reviewer_task_id not in (None, policy.reviewer_task): conflicts.append("reviewer_task_id")
    if observed.developer_task_id and observed.developer_task_id == observed.reviewer_task_id: conflicts.append("developer_reviewer_separation")
    if observed.branch == "detached": conflicts.append("branch")
    missing, conflicts = tuple(dict.fromkeys(missing)), tuple(dict.fromkeys(conflicts))
    if "task_id" in conflicts or missing: return BindingVerificationResult("blocked_unknown", False, missing, conflicts)
    if conflicts: return BindingVerificationResult("binding_drift", False, (), conflicts)
    if observed.lease in (None, "") and observed.cursor in (None, "") and observed.continuation in (None, ""):
        if observed.checkout_worktree not in (None, policy.worktree) and observed.ownership not in {"verified", "attested", "owned"}:
            return BindingVerificationResult("blocked_unknown", False, ("continuation",), ())
    return BindingVerificationResult("binding_verified", True)

def recovery_observation_is_safe(observation):
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                key = re.sub(r"[\s\-]+", "_", str(key).strip().casefold())
                if key in _UNSAFE_RECOVERY_KEYS and item not in (False, None, "", [], {}): return key
                if key in {"action", "operation", "command", "method"} and isinstance(item, str):
                    text = item.strip().casefold()
                    token_text = re.sub(r"[\s_-]+", "-", text)
                    if re.search(r"(^|[^a-z])(reset|stash|clean)([^a-z]|$)", text) or any(token in token_text for token in ("host-checkout", "second-writer", "successor", "target-change", "target-contract")) or ("host" in text and "write" in text):
                        return key + ":" + item
                found = visit(item)
                if found: return found
        elif isinstance(value, (list, tuple)):
            for item in value:
                found = visit(item)
                if found: return found
        return None
    reason = visit(observation)
    return reason is None, reason or ""

__all__ = ["LIFECYCLE_STATES", "V4_RECOVERY_ORDER", "RequestedBindingPolicy", "ProviderRuntimeBinding", "BindingVerificationResult", "verify_binding", "recovery_observation_is_safe"]
