"""Read-only Change Request capability classification and merge evidence."""

from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Optional

from .authorization import _scope_from, is_action_authorized

CAPABILITIES = {"verified_remote", "denied_remote", "unsupported_remote", "unknown_remote"}


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

    def __post_init__(self):
        if not all(isinstance(item, str) and item.strip() for item in (self.provider, self.source, self.target)):
            raise ValueError("provider, source and target are required")
        kind = self.kind.strip().casefold() if isinstance(self.kind, str) else ""
        object.__setattr__(self, "kind", {"pr": "PR", "mr": "MR"}.get(kind, "other"))
        object.__setattr__(self, "provider", self.provider.strip())
        object.__setattr__(self, "source", self.source.strip())
        object.__setattr__(self, "target", self.target.strip())
        object.__setattr__(self, "head_sha", _sha(self.head_sha, "head_sha"))
        object.__setattr__(self, "tree_sha", _sha(self.tree_sha, "tree_sha"))
        capability = self.merge_capability.strip().casefold() if isinstance(self.merge_capability, str) else ""
        if capability not in CAPABILITIES:
            raise ValueError("unsupported merge capability")
        object.__setattr__(self, "merge_capability", capability)
        for name in ("status", "issue_id", "change_request_id"):
            value = getattr(self, name)
            if value is None and name == "status":
                value = ""
            if not isinstance(value, str):
                raise ValueError("{} must be a string".format(name))
            object.__setattr__(self, name, value.strip())

    def to_dict(self):
        return asdict(self)

    @property
    def change_request(self):
        return self.change_request_id

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]):
        if not isinstance(data, Mapping):
            raise TypeError("ChangeRequest data must be a mapping")
        required = {"provider", "kind", "source", "target", "head_sha", "tree_sha"}
        if not required.issubset(data):
            raise ValueError("ChangeRequest data is missing required fields")
        return cls(data["provider"], data["kind"], data["source"], data["target"], data["head_sha"], data["tree_sha"], data.get("merge_capability", "unknown_remote"), data.get("status", ""), data.get("issue_id", data.get("issue", "")), data.get("change_request_id", data.get("change_request", data.get("request_id", data.get("mr_id", data.get("pr_id", ""))))))


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

    def to_dict(self):
        result = asdict(self)
        result["tests"] = list(self.tests)
        result["change_request"] = self.change_request_id
        result["target_branch"] = self.target_ref
        return result

    def __getitem__(self, key):
        return self.to_dict()[key]

    @property
    def change_request(self):
        return self.change_request_id

    @property
    def target_branch(self):
        return self.target_ref

    @property
    def merge_scope(self):
        return {"issue_id": self.issue_id, "source_sha": self.source_sha, "target_branch": self.target_ref, "change_request_id": self.change_request_id}


class RemoteMergeEvidence(LocalMergeEvidence):
    pass


MergeEvidence = LocalMergeEvidence


def _nested_values(facts: Mapping[str, Any], key: str):
    if key in facts:
        yield facts[key]
    for value in facts.values():
        if isinstance(value, Mapping):
            yield from _nested_values(value, key)


def _truthy(facts, *keys):
    return any(value is True for key in keys for value in _nested_values(facts, key))


def _text(facts):
    return " ".join(str(value).casefold() for key in ("provider_status", "remote_status", "status", "error", "reason") for value in _nested_values(facts, key))


def _corroborated(facts):
    values = {key: {value.strip() for value in _nested_values(facts, key) if isinstance(value, str) and value.strip()} for key in ("source", "target", "head_sha", "tree_sha")}
    if any(len(values[key]) != 1 for key in values):
        return False
    try:
        _sha(next(iter(values["head_sha"])), "head_sha"); _sha(next(iter(values["tree_sha"])), "tree_sha")
    except (StopIteration, ValueError):
        return False
    return True


def classify_merge_capability(observed_facts: Mapping[str, Any]) -> str:
    if not isinstance(observed_facts, Mapping):
        raise TypeError("observed_facts must be a mapping")
    status = _text(observed_facts)
    if _truthy(observed_facts, "permission_denied", "remote_denied", "denied") or any(marker in status for marker in ("401", "403", "forbidden", "permission denied", "policy denied")):
        return "denied_remote"
    if any(value is False for value in _nested_values(observed_facts, "remote_merge_supported")) or any(value is False for value in _nested_values(observed_facts, "provider_supports_merge")) or "unsupported" in status:
        return "unsupported_remote"
    explicit = _truthy(observed_facts, "remote_merge_supported", "provider_supports_merge")
    verified = _truthy(observed_facts, "remote_merge_verified", "verified_remote")
    provider = observed_facts.get("provider_response")
    if _corroborated(observed_facts) and ((explicit and verified) or (isinstance(provider, Mapping) and _truthy(observed_facts, "merge_allowed"))):
        return "verified_remote"
    return "unknown_remote"


def _fact(facts, *keys):
    values = []
    for key in keys:
        for value in _nested_values(facts, key):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("merge binding fields must be non-empty strings")
            if value.strip() not in values:
                values.append(value.strip())
    if len(values) > 1:
        raise ValueError("merge binding aliases conflict")
    return values[0] if values else ""


def _binding(request, authorization, facts):
    scope = _scope_from(authorization)
    if scope is None:
        raise PermissionError("merge authorization requires an exact merge scope")
    issue = _fact(facts, "issue_id", "issue") or request.issue_id
    change = _fact(facts, "change_request_id", "change_request", "request_id", "mr_id", "pr_id", "name") or request.change_request_id
    source = _fact(facts, "source_sha", "head_sha") or request.head_sha
    target = _fact(facts, "target_branch", "target_ref", "target") or request.target
    source = _sha(source, "source_sha")
    if {"issue_id": issue, "change_request_id": change, "source_sha": source, "target_branch": target} != scope:
        raise ValueError("merge binding does not match authorization scope")
    if source != request.head_sha or target != request.target:
        raise ValueError("merge binding does not match Change Request")
    return scope


def _tests(value):
    if not isinstance(value, (list, tuple)) or not value or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("local merge requires non-empty test evidence")
    return tuple(item.strip() for item in value)


def _push_conflict(facts: Mapping[str, Any]) -> bool:
    for key in ("pushed", "push_attempted", "push_succeeded", "push_status"):
        for value in _nested_values(facts, key):
            if value is True:
                return True
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                return True
            if isinstance(value, str) and value.strip().casefold() not in {"", "false", "none", "no", "not_attempted", "not_attempted_or_unknown"}:
                return True
    return False


def merge_local(change_request: ChangeRequest, authorization: Any, local_facts: Optional[Mapping[str, Any]] = None) -> LocalMergeEvidence:
    if change_request.merge_capability == "unknown_remote":
        return LocalMergeEvidence("blocked_unknown", "unknown_remote", False, False)
    if change_request.merge_capability == "verified_remote":
        raise PermissionError("verified remote capability does not select local fallback")
    if not is_action_authorized(authorization, "merge_local"):
        raise PermissionError("explicit local merge authorization is required")
    facts = dict(local_facts or {})
    scope = _binding(change_request, authorization, facts)
    target = facts.get("target_ref")
    if target != change_request.target:
        raise ValueError("local merge target_ref must exactly match Change Request target")
    return LocalMergeEvidence("merged_local", change_request.merge_capability, False, False, change_request.provider, change_request.kind, target, change_request.head_sha, _sha(facts.get("merge_base"), "merge_base"), _sha(facts.get("merge_commit"), "merge_commit"), _sha(facts.get("merge_tree"), "merge_tree"), _tests(facts.get("tests")), scope["issue_id"], scope["change_request_id"])


def merge_remote(change_request: ChangeRequest, authorization: Any, remote_facts: Optional[Mapping[str, Any]] = None) -> RemoteMergeEvidence:
    if change_request.merge_capability == "unknown_remote":
        return RemoteMergeEvidence("blocked_unknown", "unknown_remote", False, False)
    if change_request.merge_capability != "verified_remote" or not is_action_authorized(authorization, "merge"):
        raise PermissionError("verified remote capability and explicit merge authorization are required")
    facts = dict(remote_facts or {})
    scope = _binding(change_request, authorization, facts)
    if facts.get("remote_merge_verified") is not True or facts.get("remote_mutated") is not True:
        return RemoteMergeEvidence("blocked_unknown", "verified_remote", False, False, change_request.provider, change_request.kind, scope["target_branch"], scope["source_sha"], issue_id=scope["issue_id"], change_request_id=scope["change_request_id"])
    if _push_conflict(facts):
        return RemoteMergeEvidence("blocked_unknown", "verified_remote", True, False, change_request.provider, change_request.kind, scope["target_branch"], scope["source_sha"], issue_id=scope["issue_id"], change_request_id=scope["change_request_id"])
    required = ("merge_base", "merge_commit", "merge_tree", "tests")
    result_shas = ("merge_base", "merge_commit", "merge_tree")
    legacy_v2 = (
        isinstance(authorization, Mapping)
        and "schema_version" not in authorization
        and all(key not in facts or facts[key] in (None, "") for key in result_shas)
        and "tests" in facts
    )
    if any(key not in facts or facts[key] in (None, "", []) for key in required) and not legacy_v2:
        return RemoteMergeEvidence("blocked_unknown", "verified_remote", False, False, change_request.provider, change_request.kind, scope["target_branch"], scope["source_sha"], issue_id=scope["issue_id"], change_request_id=scope["change_request_id"])
    if legacy_v2:
        return RemoteMergeEvidence("merged_remote", "verified_remote", False, True, change_request.provider, change_request.kind, scope["target_branch"], scope["source_sha"], tests=_tests(facts["tests"]), issue_id=scope["issue_id"], change_request_id=scope["change_request_id"])
    return RemoteMergeEvidence("merged_remote", "verified_remote", False, True, change_request.provider, change_request.kind, scope["target_branch"], scope["source_sha"], _sha(facts["merge_base"], "merge_base"), _sha(facts["merge_commit"], "merge_commit"), _sha(facts["merge_tree"], "merge_tree"), _tests(facts["tests"]), scope["issue_id"], scope["change_request_id"])
