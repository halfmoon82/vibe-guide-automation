"""Evidence-bounded Change Request capability and local merge fallback.

This module deliberately treats PR/MR labels as presentation metadata.  Merge
capability comes only from explicit, corroborated provider facts; a local merge
record is evidence, not a claim about remote platform state.
"""

from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


_CAPABILITIES = frozenset(
    {"verified_remote", "denied_remote", "unsupported_remote", "unknown_remote"}
)
_KINDS = {"pr": "PR", "mr": "MR"}
_SHA_LENGTH = 40


def _text(value: Any, field: str, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError("{} must be a non-empty string".format(field))
    return value.strip()


def _sha(value: Any, field: str) -> str:
    value = _text(value, field).casefold()
    if len(value) != _SHA_LENGTH or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("{} must be a 40-character SHA".format(field))
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _text(self.provider, "provider"))
        raw_kind = _text(self.kind, "kind").casefold()
        object.__setattr__(self, "kind", _KINDS.get(raw_kind, "other"))
        object.__setattr__(self, "source", _text(self.source, "source"))
        object.__setattr__(self, "target", _text(self.target, "target"))
        object.__setattr__(self, "head_sha", _sha(self.head_sha, "head_sha"))
        object.__setattr__(self, "tree_sha", _sha(self.tree_sha, "tree_sha"))
        capability = _text(self.merge_capability, "merge_capability").casefold()
        if capability not in _CAPABILITIES:
            raise ValueError("unsupported merge capability")
        object.__setattr__(self, "merge_capability", capability)
        object.__setattr__(self, "status", self.status.strip() if isinstance(self.status, str) else "")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

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
            data.get("status", ""),
        )


def _status_text(facts: Mapping[str, Any]) -> str:
    values = []
    for key in ("provider_status", "remote_status", "status", "error", "reason"):
        for value in _nested_values(facts, key):
            if isinstance(value, str):
                values.append(value.casefold())
            elif isinstance(value, int):
                values.append(str(value))
    return " ".join(values)


def _nested_values(facts: Mapping[str, Any], key: str) -> Iterable[Any]:
    """Yield values for a key in bounded provider response objects."""
    if key in facts:
        yield facts[key]
    for value in facts.values():
        if isinstance(value, Mapping):
            yield from _nested_values(value, key)


def _nested_truthy(facts: Mapping[str, Any], *keys: str) -> bool:
    """Return true only for an explicit boolean marker at any response level."""
    return any(value is True for key in keys for value in _nested_values(facts, key))


def _fact_values(facts: Mapping[str, Any], key: str) -> Tuple[str, ...]:
    """Collect non-empty string fact values from bounded response mappings."""
    values = []
    for value in _nested_values(facts, key):
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return tuple(dict.fromkeys(values))


def _facts_are_corroborated(facts: Mapping[str, Any]) -> bool:
    """Require one consistent source/target/head/tree set across responses."""
    required = ("source", "target", "head_sha", "tree_sha")
    values = {key: _fact_values(facts, key) for key in required}
    if any(not values[key] for key in required):
        return False
    # Refs are case-sensitive; hexadecimal SHAs are normalized for comparison.
    for key in ("source", "target"):
        if len(set(values[key])) != 1:
            return False
    for key in ("head_sha", "tree_sha"):
        if len({item.casefold() for item in values[key]}) != 1:
            return False
    return True


def classify_merge_capability(observed_facts: Mapping[str, Any]) -> str:
    """Classify remote merge capability conservatively from provider evidence.

    Names, command presence, worker claims, and standalone ``CANMERGE``/``PASS``
    fields are intentionally ignored.  Verification requires explicit provider
    support plus a positive verification flag and matching source/target facts.
    """

    if not isinstance(observed_facts, Mapping):
        raise TypeError("observed_facts must be a mapping")
    status = _status_text(observed_facts)
    if _nested_truthy(observed_facts, "permission_denied", "remote_denied", "denied") or any(
        marker in status for marker in ("401", "403", "permission denied", "forbidden", "policy denied")
    ) or any(
        isinstance(value, str) and value.casefold() in {"denied", "forbidden", "permission_denied"}
        for value in _nested_values(observed_facts, "remote_merge_status")
    ):
        return "denied_remote"
    if (
        any(value is False for value in _nested_values(observed_facts, "remote_merge_supported"))
        or any(value is False for value in _nested_values(observed_facts, "provider_supports_merge"))
        or any(value is False for value in _nested_values(observed_facts, "adapter_supports_merge"))
        or _nested_truthy(observed_facts, "remote_unsupported", "unsupported_remote")
        or "unsupported" in status
    ):
        return "unsupported_remote"

    corroborated = _facts_are_corroborated(observed_facts)
    provider_response = observed_facts.get("provider_response")
    nested_allowed = _nested_truthy(observed_facts, "merge_allowed")
    verified = _nested_truthy(observed_facts, "remote_merge_verified", "verified_remote")
    explicitly_supported = _nested_truthy(
        observed_facts,
        "remote_merge_supported",
        "provider_supports_merge",
    )
    if corroborated and ((explicitly_supported and verified) or (isinstance(provider_response, Mapping) and nested_allowed)):
        return "verified_remote"
    return "unknown_remote"


@dataclass(frozen=True)
class LocalMergeEvidence:
    status: str
    provider: str
    kind: str
    target_ref: str
    source_sha: str
    merge_base: str
    merge_commit: str
    merge_tree: str
    tests: Tuple[str, ...]
    remote_capability: str
    pushed: bool = False
    remote_mutated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _has_local_merge_authorization(authorization: Any) -> bool:
    actions = getattr(authorization, "allowed_actions", None)
    if not isinstance(actions, (tuple, list, set, frozenset)):
        return False
    return any(str(action).strip().casefold() in {"merge_local", "local_merge"} for action in actions)


def _safe_tests(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (tuple, list)) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("tests must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def merge_local(
    change_request: ChangeRequest,
    authorization: Any,
    local_facts: Optional[Mapping[str, Any]] = None,
) -> LocalMergeEvidence:
    """Return a local-only merge evidence record without touching Git/remotes."""

    if not isinstance(change_request, ChangeRequest):
        raise TypeError("change_request must be a ChangeRequest")
    facts = dict(local_facts or {})
    if change_request.merge_capability == "unknown_remote" and not _has_local_merge_authorization(authorization):
        return LocalMergeEvidence(
            "blocked_unknown", change_request.provider, change_request.kind,
            change_request.target, change_request.head_sha, "", "", "", (),
            change_request.merge_capability,
        )
    if change_request.merge_capability == "verified_remote":
        raise PermissionError("local fallback is not selected for verified remote capability")
    if not _has_local_merge_authorization(authorization):
        raise PermissionError("explicit merge_local authorization is required")
    target = _text(facts.get("target_ref", change_request.target), "target_ref")
    if target != change_request.target:
        raise ValueError("local merge target does not match Change Request target")
    merge_base = _sha(facts.get("merge_base"), "merge_base")
    merge_commit = _sha(facts.get("merge_commit"), "merge_commit")
    merge_tree = _sha(facts.get("merge_tree"), "merge_tree")
    source_sha = _sha(facts.get("source_sha", change_request.head_sha), "source_sha")
    if source_sha != change_request.head_sha:
        raise ValueError("local merge source SHA does not match Change Request head")
    tests = _safe_tests(facts.get("tests"))
    if not tests:
        raise ValueError("local merge requires test evidence")
    return LocalMergeEvidence(
        "merged_local", change_request.provider, change_request.kind, target,
        change_request.head_sha, merge_base, merge_commit, merge_tree,
        tests, change_request.merge_capability,
        pushed=False, remote_mutated=False,
    )
