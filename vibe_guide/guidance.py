"""Canonical, versioned Guidance Contract and cross-agent conformance helpers."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


class GuidanceContractError(RuntimeError):
    """Raised when the canonical Guidance Contract cannot be trusted."""


class GovernancePending(GuidanceContractError):
    """Structured fail-closed state for an unverifiable guidance contract."""

    status = "governance_pending"

    def __init__(self, reason: str, remediation=None):
        super().__init__(reason)
        self.reason = str(reason)
        self.remediation = tuple(remediation or ("restore the versioned canonical Guidance Contract", "rerun conformance"))

    def to_dict(self):
        return {"status": self.status, "reason": self.reason, "remediation": list(self.remediation)}


SUPPORTED_ADAPTERS = (
    "codex", "claude-code", "cursor", "grok", "workbuddy", "kimi-code", "deepseek-harness",
)
REQUIRED_FIELDS = frozenset({
    "version", "stages", "actions", "statuses", "stage_handoff_required",
    "forbidden_automatic_actions", "s1_complex_rules", "authorization_defaults", "contract_hash",
})
_AUTHORIZATION_ACTIONS = ("commit", "push", "create_mr", "merge", "deploy")


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_contract_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return Path(path)
    project_asset = Path(__file__).resolve().parent.parent / "guidance" / "canonical-contract.json"
    if project_asset.is_file():
        return project_asset
    return Path(__file__).resolve().parent / "guidance" / "canonical-contract.json"


def _validated(value: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GuidanceContractError("Guidance Contract must be an object")
    contract = dict(value)
    missing = REQUIRED_FIELDS - set(contract)
    extra = set(contract) - REQUIRED_FIELDS
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing=" + ",".join(sorted(missing)))
        if extra:
            detail.append("unexpected=" + ",".join(sorted(extra)))
        raise GuidanceContractError("Guidance Contract fields are incomplete: " + "; ".join(detail))
    supplied = contract.get("contract_hash")
    if not isinstance(supplied, str) or len(supplied) != 64 or any(ch not in "0123456789abcdef" for ch in supplied):
        raise GuidanceContractError("Guidance Contract hash is missing")
    unsigned = dict(contract)
    unsigned.pop("contract_hash")
    if supplied != _digest(unsigned):
        raise GuidanceContractError("Guidance Contract hash mismatch")
    if contract.get("version") != "v3" or not isinstance(contract.get("stages"), list) or not contract["stages"]:
        raise GuidanceContractError("Guidance Contract version or stages are invalid")
    if not all(isinstance(item, str) and item for item in contract["stages"]):
        raise GuidanceContractError("Guidance Contract stages are invalid")
    for field in ("actions", "statuses", "stage_handoff_required", "forbidden_automatic_actions"):
        if not isinstance(contract.get(field), list) or not all(isinstance(item, str) and item for item in contract[field]):
            raise GuidanceContractError("Guidance Contract %s are invalid" % field)
    defaults = contract.get("authorization_defaults")
    if not isinstance(defaults, Mapping) or set(defaults) != set(_AUTHORIZATION_ACTIONS):
        raise GuidanceContractError("Guidance Contract authorization defaults are incomplete")
    if any(not isinstance(defaults[name], str) or not defaults[name] for name in _AUTHORIZATION_ACTIONS):
        raise GuidanceContractError("Guidance Contract authorization defaults are invalid")
    if defaults.get("deploy") != "excluded":
        raise GuidanceContractError("Guidance Contract must exclude deploy")
    rules = contract.get("s1_complex_rules")
    if not isinstance(rules, Mapping) or rules.get("parallel_ready") is not True or not isinstance(rules.get("requires"), list):
        raise GuidanceContractError("Guidance Contract S1 complex rules are invalid")
    return deepcopy(contract)


def load_guidance_contract(path: Optional[Path] = None) -> Dict[str, Any]:
    target = canonical_contract_path(path)
    if target.is_symlink() or not target.is_file():
        raise GuidanceContractError("Guidance Contract asset is missing: %s" % target)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise GuidanceContractError("Guidance Contract asset is invalid") from exc
    return _validated(value)


load_contract = load_guidance_contract


def contract_digest(contract: Mapping[str, Any]) -> str:
    return _validated(contract)["contract_hash"]


def guidance_for_stage(contract: Mapping[str, Any], stage: str, status: str = "") -> Dict[str, Any]:
    loaded = _validated(contract)
    if stage not in loaded["stages"]:
        raise GuidanceContractError("unknown Guidance Contract stage: %s" % stage)
    if stage == "prd_approved" and status == "approved":
        action = "continue_planning"
    elif stage == "spec_issue_dag":
        action = "continue_planning"
    elif stage == "development_plan_confirmation":
        action = "confirm_plan"
    elif stage == "authorization":
        action = "authorize_execution"
    else:
        action = "none"
    return {
        "stage": stage,
        "status": status,
        "required_user_action": action,
        "stage_handoff_required": list(loaded["stage_handoff_required"]),
        "forbidden_automatic_actions": list(loaded["forbidden_automatic_actions"]),
        "authorization_defaults": deepcopy(loaded["authorization_defaults"]),
    }


def inject_guidance(adapter_id: str, *, stage: str = "prd_approved", status: str = "approved", contract: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    if adapter_id not in SUPPORTED_ADAPTERS:
        raise GuidanceContractError("unsupported adapter: %s" % adapter_id)
    loaded = load_guidance_contract() if contract is None else _validated(contract)
    digest = loaded["contract_hash"]
    return {
        "adapter_id": adapter_id,
        "version": loaded["version"],
        "contract_version": loaded["version"],
        "contract_hash": digest,
        "loaded": True,
        "guidance": guidance_for_stage(loaded, stage, status),
        "injection": {"verified": True, "source": "canonical-contract"},
        "injection_evidence": "canonical-contract:%s" % digest,
    }


build_guidance_context = inject_guidance


def guidance_status(path: Optional[Path] = None) -> Dict[str, Any]:
    try:
        contract = load_guidance_contract(path)
    except GuidanceContractError as exc:
        return GovernancePending(str(exc)).to_dict()
    return {"status": "verified", "version": contract["version"], "contract_hash": contract["contract_hash"]}


def _provider_injection_evidence(selected, contract):
    """Exercise each adapter's create boundary with an in-memory bridge.

    The bridge never talks to a provider; it only proves that the concrete
    VisibleTaskProvider path receives the same structured contract before it
    would issue a create call. Real provider lifecycle remains unverified.
    """
    from .adapters.task_provider import RepositoryTaskRouting, VisibleTaskProvider

    providers = {"codex": "codex-app-visible", **{name: name + "-visible" for name in SUPPORTED_ADAPTERS if name != "codex"}}
    records = {}
    for adapter_id in selected:
        calls = []

        class Bridge:
            def create(self, role, issue_id, contract_path, **kwargs):
                calls.append(kwargs)
                return {"task_id": "conformance-%s" % adapter_id, "host": "local"}

        try:
            provider = VisibleTaskProvider(
                providers[adapter_id], bridge=Bridge(),
                routing=RepositoryTaskRouting("conformance", "local", "worktree", "/tmp/conformance", "codex/v3-8-rev5"),
                guidance_loader=lambda: contract,
            )
            provider.create("developer", "V3-8", Path("contract.md"))
            received = calls[0] if calls else {}
            guidance = received.get("guidance")
            records[adapter_id] = {
                "verified": isinstance(guidance, Mapping) and guidance.get("contract_hash") == contract["contract_hash"],
                "received_structured_guidance": isinstance(guidance, Mapping),
                "contract_hash": guidance.get("contract_hash") if isinstance(guidance, Mapping) else None,
                "evidence": "in-memory VisibleTaskProvider create boundary",
            }
        except Exception as exc:
            records[adapter_id] = {"verified": False, "received_structured_guidance": False, "reason": str(exc)}
    return records


def conformance_report(*, adapters: Optional[Sequence[str]] = None, contract_path: Optional[Path] = None, stage: str = "prd_approved", status: str = "approved") -> Dict[str, Any]:
    try:
        contract = load_guidance_contract(contract_path)
        fixture = guidance_for_stage(contract, stage, status)
    except GuidanceContractError as exc:
        return {"status": "governance_pending", "reason": str(exc), "remediation": ["restore a valid canonical contract", "rerun conformance"], "adapters": {}}
    selected = tuple(adapters or SUPPORTED_ADAPTERS)
    records: Dict[str, Dict[str, Any]] = {}
    for adapter_id in selected:
        try:
            records[adapter_id] = inject_guidance(adapter_id, stage=stage, status=status, contract=contract)
        except GuidanceContractError as exc:
            records[adapter_id] = {"loaded": False, "injection": {"verified": False}, "reason": str(exc)}
    provider_injection = _provider_injection_evidence(selected, contract)
    keys = ("required_user_action", "forbidden_automatic_actions", "authorization_defaults")
    baseline = {key: fixture[key] for key in keys}
    drift = [adapter_id for adapter_id, record in records.items() if any(record.get("guidance", {}).get(key) != value for key, value in baseline.items())]
    passed = (
        not drift and bool(records)
        and all(item.get("injection", {}).get("verified") for item in records.values())
        and all(item.get("verified") for item in provider_injection.values())
    )
    return {
        "status": "passed" if passed else "governance_pending",
        "version": contract["version"],
        "contract_hash": contract["contract_hash"],
        "adapters": records,
        "provider_injection": provider_injection,
        "fixture": fixture,
        "status_semantics": {name: {"status": name, "governance_pending": name == "governance_pending"} for name in contract["statuses"]},
        "drift": drift,
        "remediation": [] if passed else ["inspect adapter injection evidence and contract drift"],
    }


run_conformance = conformance_report

__all__ = ["GuidanceContractError", "GovernancePending", "SUPPORTED_ADAPTERS", "canonical_contract_path", "load_guidance_contract", "load_contract", "contract_digest", "guidance_for_stage", "inject_guidance", "build_guidance_context", "guidance_status", "conformance_report", "run_conformance"]
