"""Optional, explicitly authorized Deploy stage.

Deploy is deliberately separate from the normal plan authorization.  These
helpers only classify and verify observable state; they do not execute shell
commands or mutate a remote environment.
"""

from typing import Any, Dict, Optional

from .authorization import (
    DeployAuthorizationRecord,
    build_deploy_authorization,
    is_deploy_authorization_valid,
)
from .models import DeployManifest, DeployState


_DEPLOY_CONFIRMATION = "AUTHORIZE DEPLOY"


def _safe_evidence(observations: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only bounded, non-provider-text observations in durable state."""

    if not isinstance(observations, dict):
        raise TypeError("deploy observations must be a dictionary")
    evidence: Dict[str, Any] = {}
    for key in ("health", "health_ok", "version", "commit", "rollback"):
        if key not in observations:
            continue
        value = observations[key]
        if key in {"health", "health_ok"}:
            if isinstance(value, bool):
                evidence[key] = value
            elif isinstance(value, str) and value.casefold() in {"pass", "passed", "ok", "healthy", "fail", "failed", "unhealthy"}:
                evidence[key] = value.casefold()
        elif key == "rollback":
            if isinstance(value, dict):
                nested: Dict[str, Any] = {}
                for nested_key in ("health", "health_ok", "version", "commit"):
                    nested_value = value.get(nested_key)
                    if nested_key in {"health", "health_ok"} and isinstance(nested_value, bool):
                        nested[nested_key] = nested_value
                    elif nested_key in {"version", "commit"} and isinstance(nested_value, str) and len(nested_value) <= 128:
                        nested[nested_key] = nested_value
                if nested:
                    evidence[key] = nested
        elif isinstance(value, str) and value.strip() and len(value) <= 128:
            evidence[key] = value.strip()
    return evidence


def plan_deploy(manifest: DeployManifest, acceptance_state: str) -> DeployState:
    """Create the first Deploy state only after independent acceptance."""

    if not isinstance(manifest, DeployManifest):
        raise TypeError("manifest must be a DeployManifest")
    if not isinstance(acceptance_state, str):
        raise TypeError("acceptance_state must be a string")
    normalized = acceptance_state.strip().casefold()
    if not manifest.stop_conditions:
        return DeployState(
            "blocked_deploy",
            manifest.digest,
            manifest.target,
            reason="Deploy stop conditions are required",
        )
    if normalized not in {"accepted", "independently_accepted", "independent_acceptance"}:
        return DeployState(
            "blocked_deploy",
            manifest.digest,
            manifest.target,
            reason="independent acceptance is required before Deploy",
        )
    return DeployState("deploy_planned", manifest.digest, manifest.target)


def authorize_deploy(manifest: DeployManifest, confirmation: str) -> DeployAuthorizationRecord:
    """Issue a manifest-bound authorization using a distinct confirmation."""

    if not isinstance(manifest, DeployManifest):
        raise TypeError("manifest must be a DeployManifest")
    if confirmation != _DEPLOY_CONFIRMATION:
        raise PermissionError("Deploy requires exact AUTHORIZE DEPLOY confirmation")
    return build_deploy_authorization(manifest.digest, manifest.target)


def _health_value(evidence: Dict[str, Any]) -> Optional[bool]:
    value = evidence.get("health_ok", evidence.get("health"))
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value in {"pass", "passed", "ok", "healthy"}:
            return True
        if value in {"fail", "failed", "unhealthy"}:
            return False
    return None


def verify_deploy(manifest: DeployManifest, observations: Dict[str, Any]) -> DeployState:
    """Classify Deploy from independently observable health and version facts."""

    if not isinstance(manifest, DeployManifest):
        raise TypeError("manifest must be a DeployManifest")
    evidence = _safe_evidence(observations)
    health = _health_value(evidence)
    version = evidence.get("version", evidence.get("commit"))
    if health is None or not isinstance(version, str) or not version:
        return DeployState(
            "blocked_unknown",
            manifest.digest,
            manifest.target,
            evidence=evidence,
            reason="health or deployed version is not observable",
        )
    if health and version == manifest.commit:
        return DeployState("deployed", manifest.digest, manifest.target, evidence=evidence)

    rollback = evidence.get("rollback")
    if not health and isinstance(rollback, dict):
        rollback_health = _health_value(rollback)
        rollback_version = rollback.get("version", rollback.get("commit"))
        rollback_commit = manifest.rollback.get("commit")
        if rollback_health is True and isinstance(rollback_version, str) and rollback_version == rollback_commit:
            return DeployState("rolled_back", manifest.digest, manifest.target, evidence=evidence)
    return DeployState(
        "blocked_deploy",
        manifest.digest,
        manifest.target,
        evidence=evidence,
        reason="health or deployed version failed the manifest checks",
    )


def assert_deploy_action_allowed(manifest: DeployManifest, command: str) -> None:
    """Require an exact command from the manifest allowlist before execution."""

    if not isinstance(manifest, DeployManifest):
        raise TypeError("manifest must be a DeployManifest")
    if not isinstance(command, str) or command.strip() not in manifest.command_allowlist:
        raise PermissionError("Deploy command is outside the manifest allowlist")


def start_deploy(
    manifest: DeployManifest,
    state: DeployState,
    authorization: DeployAuthorizationRecord,
    command: Optional[str] = None,
) -> DeployState:
    """Bind a start action to both the manifest and its separate authorization."""

    if not is_deploy_authorization_valid(authorization, manifest.digest, manifest.target):
        raise PermissionError("Deploy authorization does not match the manifest")
    if state.manifest_digest != manifest.digest or state.status not in {"deploy_planned", "deploy_ready"}:
        raise ValueError("Deploy state is not ready for execution")
    if command is not None:
        assert_deploy_action_allowed(manifest, command)
    return DeployState(
        "deploy_running",
        manifest.digest,
        manifest.target,
        evidence=state.evidence,
        authorization_digest=authorization.digest,
    )


__all__ = [
    "DeployManifest",
    "DeployState",
    "DeployAuthorizationRecord",
    "plan_deploy",
    "authorize_deploy",
    "verify_deploy",
    "assert_deploy_action_allowed",
    "start_deploy",
]
