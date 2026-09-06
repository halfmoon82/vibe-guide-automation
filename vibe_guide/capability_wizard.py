"""Evidence-bound capability catalog and the two user authorization modes."""

from typing import Any, Iterable, Mapping, Union

from .capability_contract import CapabilityAuthorization, CapabilityItem


_SENSITIVE = {"remote_write", "remote_git_write", "remote_merge", "credentials", "system_permissions", "platform_login", "deploy"}
_REQUIRED = {"filesystem", "local_filesystem", "python_runtime", "git"}


def _layer(raw: Mapping[str, Any], name: str) -> str:
    explicit = raw.get("layer")
    if explicit in {"local_required", "optional_development", "sensitive_external"}:
        return explicit
    lowered = name.lower().replace("-", "_")
    scope = str(raw.get("scope", "")).lower()
    if lowered in _SENSITIVE or scope in {"external", "sensitive"}:
        return "sensitive_external"
    if raw.get("required") is True or lowered in _REQUIRED:
        return "local_required"
    return "optional_development" if scope in {"local", "development", "dev"} else "local_required"


def build_capability_catalog(scan_report: Mapping[str, Any]) -> list[CapabilityItem]:
    """Normalize only capabilities present in *scan_report*; never probe or infer availability."""
    if not isinstance(scan_report, Mapping):
        raise TypeError("scan_report must be an object")
    raw_items = scan_report.get("capabilities", ())
    if isinstance(raw_items, Mapping):
        raw_items = [dict(value, name=name) if isinstance(value, Mapping) else {"name": name, "status": value}
                     for name, value in raw_items.items()]
    if not isinstance(raw_items, Iterable) or isinstance(raw_items, (str, bytes)):
        raise TypeError("scan_report capabilities must be a list or object")
    result = []
    for raw in raw_items:
        if isinstance(raw, CapabilityItem):
            result.append(raw)
            continue
        if not isinstance(raw, Mapping) or not raw.get("name"):
            continue
        name = str(raw["name"])
        status = raw.get("status", "unknown")
        evidence = raw.get("evidence_ref", raw.get("evidence", "scan:" + name))
        result.append(CapabilityItem(
            id=name,
            layer=_layer(raw, name),
            purpose=str(raw.get("purpose", "")),
            action=str(raw.get("action", "")),
            permissions=tuple(raw.get("permissions", ())),
            failure_impact=str(raw.get("failure_impact", "")),
            reversible=bool(raw.get("reversible", True)),
            status=status,
            evidence_ref=str(evidence),
        ))
    return result


def _selected(item: Any) -> bool:
    if isinstance(item, CapabilityItem):
        return True
    if isinstance(item, bool):
        return item
    if isinstance(item, Mapping):
        return bool(item.get("selected", item.get("authorized", False)))
    return False


def authorize_capabilities(mode: str, selections: Union[Mapping[str, Any], Iterable[CapabilityItem]]) -> CapabilityAuthorization:
    """Return a serializable authorization; sensitive capabilities remain pending."""
    if mode not in {"layered", "bundled"}:
        raise ValueError("mode must be layered or bundled")
    item_input = not isinstance(selections, Mapping)
    if isinstance(selections, Mapping):
        chosen = dict(selections)
    else:
        chosen = {item.name: item for item in selections if isinstance(item, CapabilityItem)}

    granted = []
    pending = []
    states = {}
    evidence_refs = {}
    # Layer metadata may be supplied by catalog item dictionaries. Names remain
    # conservative for callers that pass the compact {name: bool} form.
    for name, value in chosen.items():
        lowered = str(name).lower().replace("-", "_")
        sensitive = lowered in _SENSITIVE
        if isinstance(value, CapabilityItem):
            sensitive = value.layer == "sensitive_external"
            states[str(name)] = value.status
            evidence_refs[str(name)] = value.evidence_ref
        elif isinstance(value, Mapping):
            sensitive = value.get("layer") == "sensitive_external" or sensitive
            states[str(name)] = value.get("status", "unknown")
            evidence_refs[str(name)] = str(value.get("evidence_ref", "scan:" + str(name)))
        else:
            # Compact names/bools carry no scan provenance and cannot grant.
            states[str(name)] = "unknown"
        available = states[str(name)] == "verified_available"
        if sensitive or not available:
            pending.append(str(name))
        elif available and (_selected(value) or (mode == "bundled" and isinstance(value, Mapping) and value.get("layer") == "local_required")):
            granted.append(str(name))

    if mode == "bundled":
        # A bundled confirmation does not authorize omitted optional abilities
        # or any sensitive capability. Entries come only from scan metadata.
        pass
    return CapabilityAuthorization(mode=mode, granted=tuple(granted), pending=tuple(pending),
                                   capability_states=states, evidence_refs=evidence_refs)
