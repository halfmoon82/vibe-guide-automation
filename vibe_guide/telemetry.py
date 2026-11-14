"""Pure system-telemetry contracts; no writes, dispatch, or cursor changes.

A checksum proves integrity, not authorship. New records require an origin
supplied by the protected appender/reader, never reconstructed from inbound
payload. Callers first verify the durable schema/hash chain with load_events
and the effective rules/authorization. Dedicated handlers consume state events.
"""

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .digests import canonical_json_digest


TELEMETRY_ENVELOPE_KEY = "telemetry"
TELEMETRY_SCHEMA = "vibe.system-telemetry"
TELEMETRY_VERSION = 1
TELEMETRY_SOURCE = "internal-observer"
ORIGIN_PRODUCER = "internal-observation-appender"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_LINEAGE = frozenset({"run_id", "authorization_digest", "node_contract_digest"})
_ENVELOPE_KEYS = _LINEAGE | {"schema", "version", "name", "source", "proof"}

# These no-node v5.2.7 observations have complete known production semantics.
# Other old audit records retain their dedicated handler or remain unknown.
LEGACY_TELEMETRY_EVENTS = frozenset({
    "supervisor_beat_rejected", "supervisor_heartbeat",
})
_DEDICATED_EVENTS = frozenset({
    "authorization_reauthorized", "decision_raised", "decision_answer_applied",
    "start_intent", "start_confirmed", "delivered", "accepted", "rework",
    "failed", "stopped", "blocked_unknown", "blocked_design",
    "scope_auto_expanded", "node_repaired", "successor_registration_reconciled",
    "orphan_dispatch_adopted", "self_report_delivery_reconciled",
    "execution_topology_observed", "monitor_entry_evidence_reconciled",
    "initial_create_anchored",
    "capacity_counter_corrected", "capacity_registry_unknown",
    "platform_handoff_disclosed", "provider_observation", "delivery_unreceived",
    "daemon_heal", "daemon_lease_readopted", "run_park_deferred",
    "run_archive_deferred", "run_archived", "run_archive_failed",
    "run_parked", "run_unarchived",
    "remote_delivery_node_appended", "remote_delivery_step_completed",
    "remote_delivery_failed", "remote_delivery_rework",
    "remote_delivery_evidence_invalidated", "remote_delivery_accepted",
    "remote_delivery_retired", "successor_binding_pending", "unknown", "timeout",
    "retry_scheduled", "old_task_reconciled", "terminal_failed", "complete",
    "quarantine_continuation_recovered", "review_finding", "binding_self_healed",
    "binding_degraded", "action_isolated", "scope_correction_registered",
    "consistency_corrected", "review_loop_limit_reached", "blocked_engineering",
    "pair_archived", "acceptance_rejected", "acceptance_recovered",
    "contract_test_execution", "blocked_unknown_disposition_required",
    "blocked_unknown_disposition", "handoff_lease_rebound", "handoff_worker_stopped",
    "handoff_stale_creates_hidden", "handoff_predecessor_reconciled",
    "integration_workspace_quarantined", "contract_error", "worktree_created",
    "worktree_preflight_blocked", "model_fallback_used", "model_unavailable_parked",
    "empty_landing_allowed", "scope_landing_rejected", "worker_question_raised",
    "worker_question_answered",
    "context_exhausted", "context_overflow", "overflow", "failed_retryable",
    "state_unknown", "visibility_unknown",
})


@dataclass(frozen=True)
class TelemetryOrigin:
    """Record-bound evidence supplied out of band by protected appender code.

    Only protected code may construct this after checking source ownership.
    It is not a signature or protection against arbitrary Python execution.
    Inbound dictionaries cannot be origins. A replay caller needs independent
    protected evidence; envelope source/checksum cannot be promoted into it.
    """

    record_digest: str
    producer: str
    evidence_ref: str

    def __post_init__(self):
        if not isinstance(self.record_digest, str) or not _DIGEST.fullmatch(self.record_digest):
            raise ValueError("origin record digest is invalid")
        if self.producer != ORIGIN_PRODUCER:
            raise ValueError("origin producer is not the protected observation appender")
        if (not isinstance(self.evidence_ref, str) or not self.evidence_ref.strip()
                or len(self.evidence_ref) > 256):
            raise ValueError("origin evidence reference is required and bounded")


@dataclass(frozen=True)
class TelemetryClassification:
    category: str
    reason: str
    event: str
    name: Optional[str] = None

    @property
    def consume(self) -> bool:
        return self.category in {"trusted_telemetry", "legacy_telemetry"}

    @property
    def advance_sequence(self) -> bool:
        return self.consume

    @property
    def side_effect(self) -> bool:
        return self.category == "side_effect"

    @property
    def trusted(self) -> bool:
        return self.category == "trusted_telemetry"

    @property
    def pure(self) -> bool:
        return self.category in {"trusted_telemetry", "legacy_telemetry"}

    def to_dict(self) -> Dict[str, Any]:
        return {"category": self.category, "reason": self.reason,
                "event": self.event, "name": self.name,
                "consume": self.consume, "advance_sequence": self.advance_sequence,
                "side_effect": self.side_effect}


def _proof(body: Mapping[str, Any]) -> str:
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(("vibe-guide/system-telemetry/v1\0" + text).encode()).hexdigest()


def _valid_lineage(run_id: Any, authorization_digest: Any, node_contract_digest: Any) -> bool:
    return (isinstance(run_id, str) and bool(_NAME.fullmatch(run_id))
            and all(isinstance(value, str) and _DIGEST.fullmatch(value)
                    for value in (authorization_digest, node_contract_digest)))


def construct_telemetry(name: str, *, run_id: str, authorization_digest: str,
                        node_contract_digest: str) -> Dict[str, Any]:
    """Build the restricted v1 fragment; this does not establish origin."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("telemetry name is invalid")
    if not _valid_lineage(run_id, authorization_digest, node_contract_digest):
        raise ValueError("telemetry run/authorization/contract lineage is invalid")
    body = {
        "schema": TELEMETRY_SCHEMA, "version": TELEMETRY_VERSION,
        "name": name, "source": TELEMETRY_SOURCE, "run_id": run_id,
        "authorization_digest": authorization_digest,
        "node_contract_digest": node_contract_digest,
    }
    return {TELEMETRY_ENVELOPE_KEY: {**body, "proof": _proof(body)}}


def build_telemetry_data(name: str, data: Optional[Mapping[str, Any]] = None,
                         **lineage: Any) -> Dict[str, Any]:
    """Build appender data, rejecting arbitrary payload and state fields."""
    if data is not None and not isinstance(data, Mapping):
        raise ValueError("telemetry data must be a mapping")
    result = dict(data) if data is not None else {}
    if set(result) - {"run_id"}:
        raise ValueError("telemetry data cannot carry state or business payload")
    if "run_id" in result and result["run_id"] != lineage.get("run_id"):
        raise ValueError("telemetry data run lineage differs")
    fragment = construct_telemetry(name, **lineage)
    return {"run_id": lineage["run_id"], **fragment}


def _parts(event: Any, data: Any, provenance: Any) -> Tuple[str, Dict[str, Any], Any, Dict[str, Any]]:
    if isinstance(event, Mapping):
        record = dict(event)
    elif isinstance(event, str):
        record = {"event": event, "data": data, "provenance": provenance}
    elif hasattr(event, "event") and hasattr(event, "data"):
        record = {"event": event.event, "data": event.data, "provenance": provenance}
    else:
        raise ValueError("event must be a record, RunEvent, or event name")
    name, payload = record.get("event"), record.get("data")
    if not isinstance(name, str) or not name or not isinstance(payload, Mapping):
        raise ValueError("event record requires a name and mapping data")
    return name, dict(payload), record.get("provenance"), record


def _lineage_error(record: Mapping[str, Any], data: Mapping[str, Any], provenance: Any,
                   expected: Mapping[str, str]) -> Optional[str]:
    if not _valid_lineage(**expected):
        return "expected run/authorization/contract lineage is invalid"
    if data.get("run_id") != expected["run_id"]:
        return "event data run lineage differs"
    if "run_id" in record and record["run_id"] != expected["run_id"]:
        return "event outer run lineage differs"
    if not isinstance(provenance, Mapping) or provenance.get("role") != "system":
        return "event lacks system provenance"
    for field in ("authorization_digest", "node_contract_digest"):
        if provenance.get(field) != expected[field]:
            return "event {} provenance differs".format(field)
        if field in data and data[field] != expected[field]:
            return "event {} data differs".format(field)
    return None


def _envelope_error(value: Any, expected: Mapping[str, str]) -> Optional[str]:
    if not isinstance(value, Mapping) or set(value) != _ENVELOPE_KEYS:
        return "telemetry envelope fields are not restricted"
    if (value["schema"] != TELEMETRY_SCHEMA or type(value["version"]) is not int
            or value["version"] != TELEMETRY_VERSION
            or value["source"] != TELEMETRY_SOURCE):
        return "telemetry schema/version/source is unsupported"
    if not isinstance(value["name"], str) or not _NAME.fullmatch(value["name"]):
        return "telemetry name is invalid"
    if any(value[field] != expected[field] for field in _LINEAGE):
        return "telemetry envelope lineage differs"
    body = {key: item for key, item in value.items() if key != "proof"}
    if value["proof"] != _proof(body):
        return "telemetry checksum differs (checksum is not source evidence)"
    return None


def classify_event(event: Any, data: Optional[Mapping[str, Any]] = None, *,
                   run_id: Optional[str] = None, authorization_digest: Optional[str] = None,
                   node_contract_digest: Optional[str] = None,
                   provenance: Optional[Mapping[str, Any]] = None,
                   origin: Optional[TelemetryOrigin] = None) -> TelemetryClassification:
    """Validate lineage, then route dedicated events or prove pure observation."""
    try:
        name, payload, prov, record = _parts(event, data, provenance)
    except (TypeError, ValueError) as error:
        return TelemetryClassification("unknown", str(error), "<invalid>")
    unknown = lambda reason: TelemetryClassification("unknown", reason, name)
    expected = dict(run_id=run_id, authorization_digest=authorization_digest,
                    node_contract_digest=node_contract_digest)
    error = _lineage_error(record, payload, prov, expected)
    if error:
        return unknown(error)
    if name in _DEDICATED_EVENTS or name.startswith(("business_", "change_request_", "remote_delivery_")):
        return TelemetryClassification("side_effect", "dedicated handler must validate/apply this record", name)
    if ("node_id" in payload or prov.get("task_id") is not None
            or prov.get("handle_id") is not None
            or type(prov.get("generation", 0)) is not int or prov.get("generation", 0) != 0):
        return unknown("pure system telemetry cannot carry a node/task/generation")
    if TELEMETRY_ENVELOPE_KEY in payload:
        if set(payload) != {"run_id", TELEMETRY_ENVELOPE_KEY}:
            return unknown("telemetry record carries state or business payload")
        envelope = payload[TELEMETRY_ENVELOPE_KEY]
        error = _envelope_error(envelope, expected)
        if error:
            return unknown(error)
        if type(origin) is not TelemetryOrigin:
            return unknown("new telemetry lacks protected out-of-band origin")
        try:
            origin_matches = origin.record_digest == canonical_json_digest(record)
        except (TypeError, ValueError):
            return unknown("event record is not canonical JSON")
        if not origin_matches:
            return unknown("telemetry origin is bound to another record")
        return TelemetryClassification("trusted_telemetry", "protected origin, envelope and lineage verified",
                                       name, envelope["name"])
    if name not in LEGACY_TELEMETRY_EVENTS:
        return unknown("unknown old event has no proven pure observation semantics")
    if name == "supervisor_beat_rejected":
        valid = (set(payload) <= (_LINEAGE | {"reason_code"})
                 and payload.get("reason_code") == "record_not_registered")
    else:
        valid = (set(payload) <= (_LINEAGE | {"status"})
                 and payload.get("status") == "active")
    if not valid:
        return unknown("legacy observation does not match known production semantics")
    return TelemetryClassification("legacy_telemetry", "known v5.2.7 production semantics verified", name, name)


def read_legacy_event(event: Any, data: Optional[Mapping[str, Any]] = None,
                      **kwargs: Any) -> TelemetryClassification:
    """Frozen old-format reader: a v1 envelope is never legacy evidence."""
    try:
        name, payload, _, _ = _parts(event, data, kwargs.get("provenance"))
    except (TypeError, ValueError) as error:
        return TelemetryClassification("unknown", str(error), "<invalid>")
    if TELEMETRY_ENVELOPE_KEY in payload:
        return TelemetryClassification("unknown", "legacy reader cannot consume a v1 envelope", name)
    return classify_event(event, data, **kwargs)


def replay_events(events: Iterable[Any], *, origins: Optional[Mapping[str, TelemetryOrigin]] = None,
                  **lineage: Any) -> Sequence[TelemetryClassification]:
    """Inspect a prefix; never step over unknown or a dedicated handler."""
    results = []
    for event in events:
        origin = None
        if origins is not None:
            try:
                origin = origins.get(canonical_json_digest(event))
            except (TypeError, ValueError):
                pass
        result = classify_event(event, origin=origin, **lineage)
        results.append(result)
        if not result.advance_sequence:
            break
    return results


def build_telemetry_envelope(name: str, **lineage: Any) -> Dict[str, Any]:
    """Return the bare envelope for callers that already own the data map."""
    return construct_telemetry(name, **lineage)[TELEMETRY_ENVELOPE_KEY]


def classify_telemetry(*args: Any, **kwargs: Any) -> TelemetryClassification:
    return classify_event(*args, **kwargs)


def read_event(*args: Any, **kwargs: Any) -> TelemetryClassification:
    return classify_event(*args, **kwargs)


def replay_telemetry(*args: Any, **kwargs: Any) -> Sequence[TelemetryClassification]:
    return replay_events(*args, **kwargs)


__all__ = [
    "LEGACY_TELEMETRY_EVENTS", "ORIGIN_PRODUCER", "TELEMETRY_ENVELOPE_KEY",
    "TELEMETRY_SCHEMA", "TELEMETRY_SOURCE", "TELEMETRY_VERSION",
    "TelemetryClassification", "TelemetryOrigin", "build_telemetry_data",
    "build_telemetry_envelope", "classify_event", "classify_telemetry",
    "construct_telemetry", "read_event", "read_legacy_event", "replay_events",
    "replay_telemetry",
]
