"""Project-contained adoption receipts; recording is never reauthorization.

The execution projection must bind a receipt through its verified authorization.
Installing a package, matching an old shipped copy, or accepting only part of
the ruleset does not enable the interdependent responsibility rules.
"""
import hashlib
import json
import re

from .protocols import published_protocol_digest
from .state import _atomic_bytes, interprocess_lock
from .digests import canonical_json_digest


_NAMES = ("prd-guide", "vibe-entry", "supervisor-card", "heal-session", "visible-sdd-worker")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _receipt_path(paths, digest):
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError("invalid adoption digest")
    return paths.resolve_vibe_path("protocol-adoptions/" + digest + ".json")


def responsibility_manifest():
    protocols = {"vibe_guide/protocols/" + name + ".md": published_protocol_digest(name)
                 for name in _NAMES}
    body = {"schema": 1, "version": "r4", "protocols": protocols}
    return dict(body, digest=canonical_json_digest(body))


def record_adoption(paths, *, plan_id, run_id, manifest, accepted_protocols,
                    evidence_ref, evidence_sha256, authorization_digest, contract_digest):
    """Retain an immutable selection receipt without changing a live run."""
    from .state import validate_run_id
    validate_run_id(run_id)
    validate_run_id(plan_id)
    if manifest != responsibility_manifest():
        raise ValueError("manifest does not match the published responsibility rules")
    if not isinstance(accepted_protocols, dict) or any(
        key not in manifest["protocols"] or value != manifest["protocols"][key]
        for key, value in accepted_protocols.items()
    ):
        raise ValueError("accepted protocols do not match manifest")
    for name, value in (("evidence", evidence_sha256), ("authorization", authorization_digest),
                        ("contract", contract_digest)):
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ValueError(name + " digest is invalid")
    evidence = paths.resolve_relative(evidence_ref)
    if not evidence.is_file() or hashlib.sha256(evidence.read_bytes()).hexdigest() != evidence_sha256:
        raise ValueError("adoption evidence digest mismatch")
    status = "pending_reauthorization" if accepted_protocols == manifest["protocols"] else (
        "partial" if accepted_protocols else "rejected")
    body = dict(schema=1, project_digest=canonical_json_digest({"root": str(paths.root.resolve())}), plan_id=plan_id, run_id=run_id,
                manifest=manifest, accepted_protocols=accepted_protocols, evidence_ref=evidence_ref,
                evidence_sha256=evidence_sha256, authorization_digest=authorization_digest,
                contract_digest=contract_digest, status=status)
    digest = canonical_json_digest(body)
    receipt = dict(body, adoption_digest=digest)
    target = _receipt_path(paths, digest)
    lock = paths.resolve_vibe_path("protocol-adoptions/.lock")
    with interprocess_lock(lock):
        target = _receipt_path(paths, digest)
        if target.exists():
            if json.loads(target.read_text(encoding="utf-8")) != receipt:
                raise ValueError("existing adoption receipt was modified")
        else:
            _atomic_bytes(target, (json.dumps(receipt, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
    return receipt


def effective_responsibility(paths, snapshot):
    """Fail closed until a verified same-run authorization binds a receipt.

    This projection intentionally does not treat a caller supplied ``effective``
    boolean or unsigned snapshot metadata as proof of reauthorization.
    """
    if hasattr(snapshot, "to_dict"):
        snapshot = snapshot.to_dict()
    if not isinstance(snapshot, dict):
        return {"status": "unknown", "reason": "invalid snapshot"}
    authorization = snapshot.get("authorization") or {}
    signed_digest = authorization.get("adoption_digest") if isinstance(authorization, dict) else None
    if signed_digest:
        return _effective_signed_adoption(paths, snapshot, authorization)
    adoption = None
    if adoption is None:
        # A receipt is an immutable fact, but it is not itself a live-state
        # mutation.  Locate a matching full receipt so callers can distinguish
        # pending reauthorization from an entirely unknown run.
        directory = paths.resolve_vibe_path("protocol-adoptions")
        if directory.is_dir() and not directory.is_symlink():
            for candidate in sorted(directory.glob("*.json")):
                try:
                    item = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if (item.get("status") == "pending_reauthorization" and
                        item.get("plan_id") == snapshot.get("plan_id") and
                        item.get("run_id") == snapshot.get("run_id") and
                        item.get("authorization_digest") == snapshot.get("authorization_digest") and
                        item.get("contract_digest") == snapshot.get("node_contract_digest")):
                    adoption = item
                    break
    if not isinstance(adoption, dict):
        return {"status": "unknown", "reason": "no verified ruleset adoption"}
    body = {key: value for key, value in adoption.items() if key != "adoption_digest"}
    if (canonical_json_digest(body) != adoption.get("adoption_digest") or
            adoption.get("project_digest") != canonical_json_digest({"root": str(paths.root.resolve())}) or
            adoption.get("manifest") != responsibility_manifest()):
        return {"status": "unknown", "reason": "adoption integrity mismatch"}
    return {"status": "pending_reauthorization", "reason": "same-run reauthorization required"}


def adoption_card_binding(paths, snapshot, digest):
    """Select one explicit full receipt for a same-run authorization card."""
    current = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
    try:
        receipt = json.loads(_receipt_path(paths, digest).read_text(encoding="utf-8"))
        body = {key: value for key, value in receipt.items() if key != "adoption_digest"}
        manifest = responsibility_manifest()
        if (canonical_json_digest(body) != digest or receipt.get("adoption_digest") != digest or
                receipt.get("project_digest") != canonical_json_digest({"root": str(paths.root.resolve())}) or
                receipt.get("plan_id") != current.get("plan_id") or
                receipt.get("run_id") != current.get("run_id") or
                receipt.get("authorization_digest") != current.get("authorization_digest") or
                receipt.get("contract_digest") != current.get("node_contract_digest") or
                receipt.get("manifest") != manifest or
                receipt.get("accepted_protocols") != manifest["protocols"] or
                receipt.get("status") != "pending_reauthorization"):
            raise ValueError("adoption selection binding mismatch")
        evidence = paths.resolve_relative(receipt["evidence_ref"])
        if hashlib.sha256(evidence.read_bytes()).hexdigest() != receipt["evidence_sha256"]:
            raise ValueError("adoption approval evidence changed")
    except (OSError, TypeError, KeyError) as error:
        raise ValueError("adoption selection unavailable: " + str(error)) from error
    return {"ruleset_digest": manifest["digest"], "adoption_digest": digest}


def validate_adoption_authorization(paths, snapshot, record):
    """Validate the proposed pair before the authorization transaction writes."""
    if not record.adoption_digest:
        return
    proposed = dict(snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot)
    proposed.update(authorization_digest=record.digest, authorization=record.to_dict(),
                    node_contract_digest=record.node_contract_digest)
    result = _effective_signed_adoption(paths, proposed, record.to_dict(), transition_required=False)
    if result.get("status") != "validated":
        raise PermissionError("responsibility adoption invalid: " + result.get("reason", "unknown"))
    receipt = json.loads(_receipt_path(paths, record.adoption_digest).read_text(encoding="utf-8"))
    previous_digest = snapshot.authorization_digest if hasattr(snapshot, "authorization_digest") else snapshot.get("authorization_digest")
    if receipt["authorization_digest"] != previous_digest:
        raise PermissionError("responsibility adoption previous authorization mismatch")


def _effective_signed_adoption(paths, snapshot, raw_authorization, *, transition_required=True):
    from .authorization import AuthorizationRecord, is_authorization_integrity_valid
    from .state import load_events
    try:
        authorization = AuthorizationRecord.from_dict(raw_authorization)
        if not is_authorization_integrity_valid(authorization):
            raise ValueError("authorization integrity mismatch")
        if (authorization.digest != snapshot.get("authorization_digest") or
                authorization.plan_id != snapshot.get("plan_id") or
                authorization.plan_version != snapshot.get("plan_version") or
                authorization.node_contract_digest != snapshot.get("node_contract_digest")):
            raise ValueError("snapshot authorization lineage mismatch")
        digest = raw_authorization["adoption_digest"]
        receipt = json.loads(_receipt_path(paths, digest).read_text(encoding="utf-8"))
        body = {key: value for key, value in receipt.items() if key != "adoption_digest"}
        manifest = responsibility_manifest()
        if (canonical_json_digest(body) != digest or receipt.get("adoption_digest") != digest or
                receipt.get("project_digest") != canonical_json_digest({"root": str(paths.root.resolve())}) or
                receipt.get("plan_id") != snapshot.get("plan_id") or
                receipt.get("run_id") != snapshot.get("run_id") or
                receipt.get("contract_digest") != snapshot.get("node_contract_digest") or
                receipt.get("manifest") != manifest or
                raw_authorization.get("ruleset_digest") != manifest["digest"] or
                receipt.get("accepted_protocols") != manifest["protocols"] or
                receipt.get("status") != "pending_reauthorization"):
            raise ValueError("adoption binding mismatch")
        evidence = paths.resolve_relative(receipt["evidence_ref"])
        if hashlib.sha256(evidence.read_bytes()).hexdigest() != receipt["evidence_sha256"]:
            raise ValueError("approval evidence changed")
        if not transition_required:
            return {"status": "validated"}
        records = load_events(paths, snapshot["run_id"])
        for record in reversed(records):
            data = record.get("data", {})
            if record.get("event") != "authorization_reauthorized" or data.get("authorization_digest") != authorization.digest:
                continue
            if (data.get("previous_authorization_digest") == receipt["authorization_digest"] and
                    canonical_json_digest(data.get("new_authorization")) == canonical_json_digest(raw_authorization)):
                return {"status": "effective", "adoption_digest": digest,
                        "ruleset_digest": manifest["digest"], "version": manifest["version"]}
        return {"status": "pending_reauthorization", "reason": "same-run transition missing"}
    except (OSError, ValueError, TypeError, KeyError) as error:
        return {"status": "unknown", "reason": str(error)}
