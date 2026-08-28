"""Evidence-bounded Change Request classification and merge records."""

from dataclasses import dataclass, asdict
from typing import Any, Dict, Mapping, Optional

from .authorization import is_action_authorized, _scope_from

CAPABILITIES = {
    "verified_remote",
    "denied_remote",
    "unsupported_remote",
    "unknown_remote",
}


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value.strip()) != 40:
        raise ValueError("{} must be a 40-character SHA".format(field))
    value = value.strip().lower()
    if any(char not in "0123456789abcdef" for char in value):
        raise ValueError("{} must be a hexadecimal SHA".format(field))
    return value


@dataclass(frozen=True)
class ChangeRequest:
    provider: str
    kind: str
    source: str
    target: str
    head_sha: str
    tree_sha: str
    merge_capability: str
    status: str = ""
    issue_id: str = ""
    change_request_id: str = ""

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (self.provider, self.source, self.target)):
            raise ValueError("provider, source and target are required")
        kind = self.kind.strip().lower() if isinstance(self.kind, str) else ""
        object.__setattr__(self, "kind", {"pr": "PR", "mr": "MR"}.get(kind, "other"))
        object.__setattr__(self, "provider", self.provider.strip())
        object.__setattr__(self, "source", self.source.strip())
        object.__setattr__(self, "target", self.target.strip())
        object.__setattr__(self, "head_sha", _sha(self.head_sha, "head_sha"))
        object.__setattr__(self, "tree_sha", _sha(self.tree_sha, "tree_sha"))
        capability = self.merge_capability.strip().lower() if isinstance(self.merge_capability, str) else ""
        if capability not in CAPABILITIES:
            raise ValueError("unsupported merge capability")
        object.__setattr__(self, "merge_capability", capability)
        for field in ("status", "issue_id", "change_request_id"):
            value = getattr(self, field)
            if field == "status" and value is None:
                value = ""
            if not isinstance(value, str):
                raise ValueError("{} must be a string".format(field))
            object.__setattr__(self, field, value.strip())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def change_request(self) -> str:
        return self.change_request_id

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChangeRequest":
        if not isinstance(data, Mapping):
            raise TypeError("ChangeRequest data must be a mapping")
        required = {"provider", "kind", "source", "target", "head_sha", "tree_sha"}
        if not required.issubset(data):
            raise ValueError("ChangeRequest data is missing required fields")
        return cls(
            data["provider"], data["kind"], data["source"], data["target"],
            data["head_sha"], data["tree_sha"], data.get("merge_capability", "unknown_remote"),
            data.get("status", ""), data.get("issue_id", data.get("issue", "")),
            data.get(
                "change_request_id",
                data.get("change_request", data.get("request_id", data.get("mr_id", data.get("pr_id", "")))),
            ),
        )


@dataclass(frozen=True)
class LocalMergeEvidence:
    status: str
    remote_capability: str
    pushed: bool
    remote_mutated: bool
    provider: str = ""
    kind: str = ""
    target_ref: str = ""
    source_sha: str = ""
    merge_base: str = ""
    merge_commit: str = ""
    merge_tree: str = ""
    tests: tuple = ()
    issue_id: str = ""
    change_request_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["tests"] = list(self.tests)
        # Keep both the canonical field and the provider-neutral display alias
        # used by older Change Request payloads.
        result["change_request"] = self.change_request_id
        result["target_branch"] = self.target_ref
        return result

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    @property
    def change_request(self) -> str:
        return self.change_request_id

    @property
    def target_branch(self) -> str:
        return self.target_ref

    @property
    def merge_scope(self) -> Dict[str, str]:
        return {
            "issue_id": self.issue_id,
            "source_sha": self.source_sha,
            "target_branch": self.target_ref,
            "change_request_id": self.change_request_id,
        }


class RemoteMergeEvidence(LocalMergeEvidence):
    """Evidence for a verified remote merge; never produced by local merge."""


MergeEvidence = LocalMergeEvidence


def _nested_values(facts: Mapping[str, Any], key: str):
    if key in facts:
        yield facts[key]
    for value in facts.values():
        if isinstance(value, Mapping):
            yield from _nested_values(value, key)


def _nested_truthy(facts: Mapping[str, Any], *keys: str) -> bool:
    return any(value is True for key in keys for value in _nested_values(facts, key))


def _push_contract_conflict(facts: Mapping[str, Any]) -> bool:
    """Detect explicit push activity before producing merge success evidence."""
    for key in ("pushed", "push_attempted", "push_succeeded"):
        for value in _nested_values(facts, key):
            if value is True:
                return True
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value != 0:
                return True
            if isinstance(value, str) and value.strip().casefold() in {
                "true", "yes", "1", "succeeded", "success",
            }:
                return True
    for value in _nested_values(facts, "push_status"):
        if value is True:
            return True
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return True
        if isinstance(value, str) and value.strip().casefold() not in {
            "", "false", "not_attempted", "not_attempted_or_unknown", "none", "no",
        }:
            return True
    return False


def _nested_binding_value(
    facts: Mapping[str, Any], keys: tuple, field: str, *, sha: bool = False
) -> str:
    """Resolve one binding value across all provider response layers."""
    values = []
    for key in keys:
        for value in _nested_values(facts, key):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("{} must be a non-empty string".format(field))
            value = value.strip()
            if sha:
                value = _sha(value, field)
            if value not in values:
                values.append(value)
    if len(values) > 1:
        raise ValueError("merge binding aliases conflict")
    return values[0] if values else ""


def _status_text(facts: Mapping[str, Any]) -> str:
    values = []
    for key in ("provider_status", "remote_status", "status", "error", "reason"):
        for value in _nested_values(facts, key):
            values.append(str(value).lower())
    return " ".join(values)


def _fact_values(facts: Mapping[str, Any], key: str):
    values = []
    for value in _nested_values(facts, key):
        if isinstance(value, str) and value.strip():
            value = value.strip()
            if value not in values:
                values.append(value)
    return tuple(values)


def _facts_are_corroborated(facts: Mapping[str, Any]) -> bool:
    values = {
        key: _fact_values(facts, key)
        for key in ("source", "target", "head_sha", "tree_sha")
    }
    if any(len(values[key]) != 1 for key in values):
        return False
    try:
        _sha(values["head_sha"][0], "head_sha")
        _sha(values["tree_sha"][0], "tree_sha")
    except (IndexError, ValueError):
        return False
    return True


def classify_merge_capability(observed_facts: Mapping[str, Any]) -> str:
    """Classify only when all provider response layers corroborate the facts."""
    if not isinstance(observed_facts, Mapping):
        raise TypeError("observed_facts must be a mapping")
    status = _status_text(observed_facts)
    if _nested_truthy(observed_facts, "permission_denied", "remote_denied", "denied") or any(
        marker in status
        for marker in ("401", "403", "forbidden", "permission denied", "policy denied")
    ):
        return "denied_remote"
    if (
        any(value is False for value in _nested_values(observed_facts, "remote_merge_supported"))
        or any(value is False for value in _nested_values(observed_facts, "provider_supports_merge"))
        or "unsupported" in status
    ):
        return "unsupported_remote"
    corroborated = _facts_are_corroborated(observed_facts)
    explicitly_supported = _nested_truthy(
        observed_facts, "remote_merge_supported", "provider_supports_merge"
    )
    verified = _nested_truthy(observed_facts, "remote_merge_verified", "verified_remote")
    provider_response = observed_facts.get("provider_response")
    if corroborated and (
        (explicitly_supported and verified)
        or (isinstance(provider_response, Mapping) and _nested_truthy(observed_facts, "merge_allowed"))
    ):
        return "verified_remote"
    return "unknown_remote"


def _authorized_actions(authorization: Any) -> set:
    if isinstance(authorization, Mapping):
        values = authorization.get("allowed_actions", ())
    else:
        values = getattr(authorization, "allowed_actions", ())
    if not isinstance(values, (tuple, list, set, frozenset)):
        return set()
    return {str(action).strip().lower() for action in values}


def _test_evidence(value: Any) -> tuple:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("local merge requires non-empty list or tuple test evidence")
    normalized = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    if len(normalized) != len(value):
        raise ValueError("local merge test evidence items must be non-empty strings")
    return normalized


def _scope_value(facts: Mapping[str, Any], *keys: str) -> str:
    values = []
    for key in keys:
        if key not in facts:
            continue
        value = facts[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("merge binding fields must be non-empty strings")
        value = value.strip()
        if value not in values:
            values.append(value)
    if len(values) > 1:
        raise ValueError("merge binding aliases conflict")
    return values[0] if values else ""


def _merge_binding(
    change_request: ChangeRequest,
    authorization: Any,
    facts: Mapping[str, Any],
    *,
    require_scope: bool,
) -> Dict[str, str]:
    """Resolve and verify one exact Issue/Change Request merge scope."""
    try:
        authorized_scope = _scope_from(authorization)
    except (TypeError, ValueError) as error:
        raise ValueError("authorization merge scope is invalid") from error
    if require_scope and authorized_scope is None:
        raise ValueError("merge authorization requires an exact merge scope")

    issue_id = _scope_value(facts, "issue_id", "issue")
    change_request_id = _scope_value(
        facts, "change_request_id", "change_request", "request_id", "mr_id", "pr_id", "name"
    )
    source_ref = _scope_value(facts, "source")
    if source_ref and source_ref != change_request.source:
        raise ValueError("merge source ref does not match Change Request source")
    source_sha = _scope_value(facts, "source_sha", "head_sha") or change_request.head_sha
    target_branch = _scope_value(facts, "target_branch", "target_ref", "target") or change_request.target
    if authorized_scope is not None:
        expected = authorized_scope
        if issue_id and issue_id != expected["issue_id"]:
            raise ValueError("merge Issue does not match authorization scope")
        if change_request_id and change_request_id != expected["change_request_id"]:
            raise ValueError("merge Change Request does not match authorization scope")
        if _sha(source_sha, "source_sha") != expected["source_sha"]:
            raise ValueError("merge source SHA does not match authorization scope")
        if target_branch != expected["target_branch"]:
            raise ValueError("merge target branch does not match authorization scope")
        issue_id = expected["issue_id"]
        change_request_id = expected["change_request_id"]
        source_sha = expected["source_sha"]
        target_branch = expected["target_branch"]
    else:
        source_sha = _sha(source_sha, "source_sha")

    if change_request.issue_id and issue_id and change_request.issue_id != issue_id:
        raise ValueError("merge Issue does not match Change Request")
    if change_request.change_request_id and change_request_id and change_request.change_request_id != change_request_id:
        raise ValueError("merge Change Request does not match Change Request")
    if not issue_id:
        issue_id = change_request.issue_id
    if not change_request_id:
        change_request_id = change_request.change_request_id
    if require_scope and (not issue_id or not change_request_id):
        raise ValueError("merge scope requires Issue and named Change Request")
    return {
        "issue_id": issue_id,
        "change_request_id": change_request_id,
        "source_sha": source_sha,
        "target_branch": target_branch,
    }


def merge_local(change_request: ChangeRequest, authorization: Any, local_facts: Optional[Mapping[str, Any]] = None) -> LocalMergeEvidence:
    """Create local merge evidence only; this function never invokes Git or push."""
    facts = dict(local_facts or {})
    if change_request.merge_capability == "unknown_remote":
        return LocalMergeEvidence(
            status="blocked_unknown",
            remote_capability="unknown_remote",
            pushed=False,
            remote_mutated=False,
        )
    if change_request.merge_capability == "verified_remote":
        raise PermissionError("verified remote capability does not select local fallback")
    if not is_action_authorized(authorization, "merge_local"):
        raise PermissionError("explicit local merge authorization is required")
    binding = _merge_binding(change_request, authorization, facts, require_scope=True)
    if binding["source_sha"] != change_request.head_sha:
        raise ValueError("source SHA does not match Change Request head")
    target_ref = facts.get("target_ref")
    if not isinstance(target_ref, str) or not target_ref or target_ref != change_request.target:
        raise ValueError("local merge target_ref must exactly match Change Request target")
    tests = _test_evidence(facts.get("tests"))
    return LocalMergeEvidence(
        status="merged_local",
        provider=change_request.provider,
        kind=change_request.kind,
        target_ref=target_ref,
        source_sha=change_request.head_sha,
        merge_base=_sha(facts.get("merge_base"), "merge_base"),
        merge_commit=_sha(facts.get("merge_commit"), "merge_commit"),
        merge_tree=_sha(facts.get("merge_tree"), "merge_tree"),
        tests=tests,
        remote_capability=change_request.merge_capability,
        pushed=False,
        remote_mutated=False,
        issue_id=binding["issue_id"],
        change_request_id=binding["change_request_id"],
    )


def merge_remote(
    change_request: ChangeRequest,
    authorization: Any,
    remote_facts: Optional[Mapping[str, Any]] = None,
) -> RemoteMergeEvidence:
    """Record verified remote merge evidence without invoking a provider.

    The generic ``merge`` action is distinct from ``merge_local``.  A remote
    result is only successful when the provider facts explicitly confirm the
    mutation and every scope component matches the digest-bound authorization.
    """
    facts = dict(remote_facts or {})
    if change_request.merge_capability == "unknown_remote":
        return RemoteMergeEvidence(
            status="blocked_unknown",
            remote_capability="unknown_remote",
            pushed=False,
            remote_mutated=False,
        )
    if change_request.merge_capability != "verified_remote":
        raise PermissionError("verified remote capability is required for remote merge")
    if not is_action_authorized(authorization, "merge"):
        raise PermissionError("explicit remote merge authorization is required")
    nested_target = _nested_binding_value(
        facts, ("target_branch", "target_ref", "target"), "target_branch"
    )
    nested_head = _nested_binding_value(
        facts, ("source_sha", "head_sha"), "source_sha", sha=True
    )
    if nested_target and nested_target != change_request.target:
        raise ValueError("remote merge target does not match Change Request target")
    if nested_head and nested_head != change_request.head_sha:
        raise ValueError("remote merge source SHA does not match Change Request head")
    binding = _merge_binding(change_request, authorization, facts, require_scope=True)
    if binding["target_branch"] != change_request.target:
        raise ValueError("remote merge target does not match Change Request target")
    if binding["source_sha"] != change_request.head_sha:
        raise ValueError("remote merge source SHA does not match Change Request head")
    if _push_contract_conflict(facts):
        return RemoteMergeEvidence(
            status="blocked_unknown",
            provider=change_request.provider,
            kind=change_request.kind,
            target_ref=binding["target_branch"],
            source_sha=binding["source_sha"],
            remote_capability=change_request.merge_capability,
            pushed=True,
            remote_mutated=False,
            issue_id=binding["issue_id"],
            change_request_id=binding["change_request_id"],
        )
    if facts.get("remote_merge_verified") is not True or facts.get("remote_mutated") is not True:
        return RemoteMergeEvidence(
            status="blocked_unknown",
            provider=change_request.provider,
            kind=change_request.kind,
            target_ref=binding["target_branch"],
            source_sha=binding["source_sha"],
            remote_capability=change_request.merge_capability,
            issue_id=binding["issue_id"],
            change_request_id=binding["change_request_id"],
        )
    tests = ()
    if "tests" in facts:
        tests = _test_evidence(facts["tests"])
    return RemoteMergeEvidence(
        status="merged_remote",
        provider=change_request.provider,
        kind=change_request.kind,
        target_ref=binding["target_branch"],
        source_sha=binding["source_sha"],
        tests=tests,
        remote_capability=change_request.merge_capability,
        pushed=False,
        remote_mutated=True,
        issue_id=binding["issue_id"],
        change_request_id=binding["change_request_id"],
    )
