"""Small, fail-closed contracts for the optional Deploy stage.

Deploy is deliberately independent from the normal executable-plan
authorization.  This module only plans and verifies a manifest; it never
executes a command or contacts a target environment.
"""

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
import secrets
from typing import Any, Dict, List, Optional, Tuple


_HEX = re.compile(r"^[0-9a-fA-F]{7,128}$")
_SENSITIVE = ("password", "secret", "token", "credential", "private_key", "api_key")


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("{} is required".format(field_name))
    return value.strip()


def _safe_list(value: Any, field_name: str) -> List[Any]:
    if not isinstance(value, list) or not value or len(value) > 64:
        raise ValueError("{} must be a non-empty bounded list".format(field_name))
    return list(value)


def _reject_secrets(value: Any, path: str = "manifest") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE) and not normalized.endswith("_ref"):
                raise ValueError("raw secret fields are forbidden at " + path)
            _reject_secrets(item, path + "." + str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secrets(item, "{}[{}]".format(path, index))


@dataclass(frozen=True)
class DeployManifest:
    target: str
    commit: str
    command_allowlist: List[str]
    health_checks: List[Dict[str, Any]]
    rollback: Dict[str, Any]
    tree: Optional[str] = None
    config_refs: List[str] = field(default_factory=list)
    migrations: List[Dict[str, Any]] = field(default_factory=list)
    observation_window: Optional[int] = None
    stop_conditions: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        target = _safe_text(self.target, "target")
        commit = _safe_text(self.commit, "commit")
        if not _HEX.fullmatch(commit):
            raise ValueError("commit must be a hexadecimal release identifier")
        commands = _safe_list(self.command_allowlist, "command_allowlist")
        if any(not isinstance(item, str) or not item.strip() for item in commands):
            raise ValueError("command_allowlist must contain command names")
        checks = _safe_list(self.health_checks, "health_checks")
        if any(not isinstance(item, dict) or not item.get("name") for item in checks):
            raise ValueError("health_checks must contain named checks")
        if not isinstance(self.rollback, dict) or not self.rollback.get("version") or not self.rollback.get("command"):
            raise ValueError("rollback version and command are required")
        stops = _safe_list(self.stop_conditions, "stop_conditions")
        if any(not isinstance(item, str) or not item.strip() for item in stops):
            raise ValueError("stop_conditions must contain descriptions")
        if self.tree is not None:
            _safe_text(self.tree, "tree")
        if not isinstance(self.config_refs, list) or any(not isinstance(item, str) or not item.strip() for item in self.config_refs):
            raise ValueError("config_refs must be a list of references")
        if not isinstance(self.migrations, list) or any(not isinstance(item, dict) for item in self.migrations):
            raise ValueError("migrations must be a list of objects")
        if self.observation_window is not None and (isinstance(self.observation_window, bool) or not isinstance(self.observation_window, int) or self.observation_window < 0):
            raise ValueError("observation_window must be a non-negative integer")
        _reject_secrets(asdict(self))
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "commit", commit)
        object.__setattr__(self, "command_allowlist", [item.strip() for item in commands])
        object.__setattr__(self, "health_checks", [dict(item) for item in checks])
        object.__setattr__(self, "stop_conditions", [item.strip() for item in stops])

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeployManifest":
        if not isinstance(data, dict):
            raise TypeError("DeployManifest data must be an object")
        values = dict(data)
        aliases = {
            "target_environment": "target",
            "release_commit": "commit",
            "release_tree": "tree",
            "rollback_version": "rollback_version",
            "rollback_commands": "rollback_commands",
        }
        for source, target in aliases.items():
            if source in values and target not in values:
                values[target] = values.pop(source)
        if "rollback_version" in values or "rollback_commands" in values:
            rollback = dict(values.get("rollback", {}))
            if "rollback_version" in values:
                rollback.setdefault("version", values.pop("rollback_version"))
            if "rollback_commands" in values:
                commands = values.pop("rollback_commands")
                rollback.setdefault("command", commands[0] if isinstance(commands, list) and commands else commands)
            values["rollback"] = rollback
        return cls(**values)


@dataclass(frozen=True)
class DeployState:
    status: str
    manifest: DeployManifest
    reason: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "manifest": self.manifest.to_dict(), "reason": self.reason, "evidence": self.evidence}


@dataclass(frozen=True)
class DeployAuthorizationRecord:
    manifest_digest: str
    allowed_actions: Tuple[str, ...]
    digest: str
    schema_version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {"manifest_digest": self.manifest_digest, "allowed_actions": list(self.allowed_actions), "digest": self.digest, "schema_version": self.schema_version}


def deploy_manifest_digest(manifest: DeployManifest) -> str:
    if not isinstance(manifest, DeployManifest):
        raise TypeError("manifest must be a DeployManifest")
    return _digest(manifest.to_dict())


def plan_deploy(manifest: Optional[DeployManifest], acceptance_state: str) -> Optional[DeployState]:
    """Create the planned state only after independent acceptance."""
    if manifest is None:
        return None
    if not isinstance(manifest, DeployManifest):
        raise TypeError("manifest must be a DeployManifest")
    if isinstance(acceptance_state, dict):
        acceptance_state = acceptance_state.get("status", "")
    if acceptance_state not in {"accepted", "complete", "independent_acceptance"}:
        raise ValueError("Deploy requires independent acceptance")
    return DeployState("deploy_planned", manifest)


def authorize_deploy(manifest: DeployManifest, confirmation: str) -> DeployAuthorizationRecord:
    if confirmation != "AUTHORIZE_DEPLOY":
        raise ValueError("Deploy authorization requires exact AUTHORIZE_DEPLOY confirmation")
    # The manifest, rather than a global default, is the source of the exact
    # action scope.  ``deploy`` is the required primary action; auxiliary
    # commands (restart/migrate/rollback/publish or a project-specific name)
    # remain individually bound to this manifest digest.
    actions = tuple(sorted(set(manifest.command_allowlist + ["deploy"])))
    payload = {"manifest_digest": deploy_manifest_digest(manifest), "allowed_actions": actions, "schema_version": 1}
    return DeployAuthorizationRecord(payload["manifest_digest"], actions, _digest(payload))


def is_deploy_authorization_valid(record: DeployAuthorizationRecord, manifest: DeployManifest) -> bool:
    if not isinstance(record, DeployAuthorizationRecord) or not isinstance(manifest, DeployManifest):
        return False
    payload = {"manifest_digest": deploy_manifest_digest(manifest), "allowed_actions": tuple(record.allowed_actions), "schema_version": record.schema_version}
    return record.schema_version == 1 and secrets.compare_digest(record.manifest_digest, payload["manifest_digest"]) and secrets.compare_digest(record.digest, _digest(payload))


def verify_deploy(manifest: DeployManifest, observations: Dict[str, Any]) -> DeployState:
    if not isinstance(observations, dict):
        return DeployState("blocked_unknown", manifest, "observations are unavailable")
    version = observations.get("version", observations.get("release_commit"))
    health = observations.get("health")
    if health is None and "health_checks" in observations:
        checks = observations.get("health_checks")
        if isinstance(checks, dict):
            health = all(value is True for value in checks.values()) if checks else None
        elif isinstance(checks, list):
            health = all(item is True or (isinstance(item, dict) and item.get("ok") is True) for item in checks) if checks else None
    if version is None or health is None:
        return DeployState("blocked_unknown", manifest, "health or version is unknown", dict(observations))
    # A rollback can only be considered after the failed observation itself is
    # bound to this manifest.  Otherwise a healthy rollback observation could
    # mask an unrelated/mismatched release failure.
    expected = manifest.tree or manifest.commit
    if version != expected:
        return DeployState("blocked_deploy", manifest, "observed version does not match manifest", dict(observations))
    rollback = observations.get("rollback")
    if health is not True and isinstance(rollback, dict):
        rollback_version = rollback.get("version", rollback.get("release_commit"))
        rollback_health = rollback.get("health")
        if rollback_health is True and rollback_version == manifest.rollback.get("version"):
            return DeployState("rolled_back", manifest, "rollback health and version verified", dict(observations))
    if health is not True:
        return DeployState("blocked_deploy", manifest, "health check failed", dict(observations))
    return DeployState("deployed", manifest, evidence=dict(observations))


def _authorized_transition(
    manifest: DeployManifest,
    state: DeployState,
    authorization: DeployAuthorizationRecord,
    expected_status: str,
    next_status: str,
    command: Optional[str] = None,
) -> DeployState:
    if not isinstance(manifest, DeployManifest):
        raise TypeError("manifest must be a DeployManifest")
    if not isinstance(state, DeployState):
        raise TypeError("state must be a DeployState")
    if not is_deploy_authorization_valid(authorization, manifest):
        raise PermissionError("a valid separate Deploy authorization is required")
    if state.manifest.to_dict() != manifest.to_dict() or state.status != expected_status:
        raise ValueError("Deploy state is not ready for " + next_status)
    if command is not None and command not in manifest.command_allowlist:
        raise ValueError("Deploy command is outside the manifest allowlist")
    evidence = dict(state.evidence)
    evidence["authorization_digest"] = authorization.digest
    return DeployState(next_status, manifest, evidence=evidence)


def prepare_deploy(
    manifest: DeployManifest,
    state: DeployState,
    authorization: DeployAuthorizationRecord,
) -> DeployState:
    """Advance a planned Deploy to ready under its separate authorization."""

    return _authorized_transition(
        manifest, state, authorization, "deploy_planned", "deploy_ready"
    )


def start_deploy(
    manifest: DeployManifest,
    state: DeployState,
    authorization: DeployAuthorizationRecord,
    command: Optional[str] = None,
) -> DeployState:
    """Advance a ready Deploy to running without executing external actions."""

    return _authorized_transition(
        manifest, state, authorization, "deploy_ready", "deploy_running", command
    )


__all__ = [
    "DeployAuthorizationRecord",
    "DeployManifest",
    "DeployState",
    "authorize_deploy",
    "deploy_manifest_digest",
    "is_deploy_authorization_valid",
    "plan_deploy",
    "prepare_deploy",
    "start_deploy",
    "verify_deploy",
]
