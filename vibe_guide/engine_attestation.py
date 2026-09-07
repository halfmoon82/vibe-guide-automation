"""Provider-neutral, content-addressed evidence for the Monitor execution engine."""

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, Mapping, Optional


SCHEMA_VERSION = 1
EXECUTION_ENGINE = "vibeguide_monitor"
ENGINE_MODE = "dag"
EVIDENCE_PREFIX = "engine-attestation:"
_DIGEST_LENGTH = 64


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("%s must be a non-empty string" % field)
    return value.strip()


def _revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("plan revision must be a positive integer")
    return value


def _timestamp(value: Any) -> str:
    value = _text(value, "now")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("now must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("now must include a timezone")
    return value


def _payload(attestation: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": attestation["schema_version"],
        "plan_id": attestation["plan_id"],
        "plan_revision": attestation["plan_revision"],
        "execution_engine": attestation["execution_engine"],
        "engine_mode": attestation["engine_mode"],
        "provider": attestation["provider"],
        "capability_facts": attestation["capability_facts"],
        "provenance": attestation["provenance"],
        "generated_at": attestation["generated_at"],
    }


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_engine_attestation(
    plan_id: str,
    plan_revision: int,
    execution_engine: str,
    engine_mode: str,
    provider: str,
    capability_facts: Mapping[str, bool],
    provenance: str,
    now: str,
) -> dict:
    """Create deterministic evidence for one plan revision and engine binding."""
    plan_id = _text(plan_id, "plan_id")
    plan_revision = _revision(plan_revision)
    if execution_engine != EXECUTION_ENGINE:
        raise ValueError("execution engine must be vibeguide_monitor")
    if engine_mode != ENGINE_MODE:
        raise ValueError("engine mode must be dag")
    provider = _text(provider, "provider")
    provenance = _text(provenance, "provenance")
    if not isinstance(capability_facts, Mapping) or not capability_facts:
        raise ValueError("capability_facts must be a non-empty mapping")
    facts = {}
    for name, value in capability_facts.items():
        name = _text(name, "capability fact name")
        if not isinstance(value, bool):
            raise ValueError("capability facts must be boolean")
        facts[name] = value
    payload = {
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan_id,
        "plan_revision": plan_revision,
        "execution_engine": execution_engine,
        "engine_mode": engine_mode,
        "provider": provider,
        "capability_facts": facts,
        "provenance": provenance,
        "generated_at": _timestamp(now),
    }
    digest = _digest(payload)
    return dict(payload, digest=digest,
                evidence_ref=EVIDENCE_PREFIX + digest[:16])


def validate_engine_attestation(
    attestation: Mapping[str, Any],
    plan_id: str,
    plan_revision: int,
    expected_digest: Optional[str] = None,
    now: Optional[str] = None,
    max_age_seconds: int = 86400,
) -> None:
    """Raise ValueError unless attestation identity and content are intact."""
    if not isinstance(attestation, Mapping):
        raise ValueError("engine attestation must be an object")
    required = {
        "schema_version", "plan_id", "plan_revision", "execution_engine",
        "engine_mode", "provider", "capability_facts", "provenance",
        "generated_at", "digest", "evidence_ref",
    }
    if set(attestation) != required:
        raise ValueError("engine attestation fields are invalid")
    if attestation["schema_version"] != SCHEMA_VERSION:
        raise ValueError("engine attestation schema is invalid")
    expected_plan = _text(plan_id, "plan_id")
    expected_revision = _revision(plan_revision)
    if attestation["plan_id"] != expected_plan:
        raise ValueError("engine attestation plan mismatch")
    if attestation["plan_revision"] != expected_revision:
        raise ValueError("engine attestation revision mismatch")
    # Reuse creation validation for all signed identity and fact fields.
    candidate = create_engine_attestation(
        attestation["plan_id"], attestation["plan_revision"],
        attestation["execution_engine"], attestation["engine_mode"],
        attestation["provider"], attestation["capability_facts"],
        attestation["provenance"], attestation["generated_at"],
    )
    if attestation["digest"] != candidate["digest"]:
        raise ValueError("engine attestation digest mismatch")
    if attestation["evidence_ref"] != candidate["evidence_ref"]:
        raise ValueError("engine attestation evidence reference mismatch")
    digest = attestation["digest"]
    if not isinstance(digest, str) or len(digest) != _DIGEST_LENGTH:
        raise ValueError("engine attestation digest is invalid")
    if expected_digest is not None and digest != expected_digest:
        raise ValueError("engine attestation digest mismatch")
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or max_age_seconds <= 0:
        raise ValueError("engine attestation max age is invalid")
    reference = datetime.now(timezone.utc) if now is None else datetime.fromisoformat(_timestamp(now).replace("Z", "+00:00"))
    generated = datetime.fromisoformat(_timestamp(attestation["generated_at"]).replace("Z", "+00:00"))
    age = (reference - generated).total_seconds()
    if age < -300:
        raise ValueError("engine attestation timestamp is in the future")
    if age > max_age_seconds:
        raise ValueError("engine attestation is expired")
