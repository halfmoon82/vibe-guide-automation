"""`vibe attest`: record the capability facts a host session actually observed.

vibe does not probe or infer what a desktop platform can do.  The session
that sees the native tools states every manifest fact as ``true``/``false``;
this module validates the shape against the adapter manifest, writes the
bridge file through ``ProviderActionStore`` (the only reader), and reports
what adapter detection makes of the record.  Nothing here upgrades a fact
the session did not state.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from .adapters.registry import AdapterRegistry
from .adapters.task_provider import ProviderActionStore
from .node_spec import observe_capabilities

CAPABILITIES_RELATIVE_PATH = ".vibe/provider-actions/capabilities.json"


def expected_fact_names(adapter_id: str) -> List[str]:
    """The exact fact names the adapter manifest probes for."""
    try:
        adapter = AdapterRegistry().get(adapter_id)
    except KeyError as error:
        raise ValueError("unknown adapter: {}".format(adapter_id)) from error
    return [probe["name"] for probe in adapter.manifest["probes"]]


def validate_session_facts(adapter_id: str, facts: Any) -> Dict[str, bool]:
    """Fail closed unless every manifest fact is stated exactly once as a bool."""
    if not isinstance(facts, Mapping):
        raise TypeError("facts must be a JSON object mapping fact name to true/false")
    expected = expected_fact_names(adapter_id)
    unknown = sorted(str(key) for key in facts if key not in expected)
    if unknown:
        raise ValueError("facts contain names the {} manifest does not probe: {}".format(adapter_id, ", ".join(unknown)))
    missing = [name for name in expected if name not in facts]
    if missing:
        raise ValueError("facts must state every {} probe; missing: {}".format(adapter_id, ", ".join(missing)))
    non_bool = [name for name in expected if type(facts[name]) is not bool]
    if non_bool:
        raise ValueError("facts must be true or false, not strings or unknown: {}".format(", ".join(non_bool)))
    return {name: facts[name] for name in expected}


def record_session_capabilities(paths: Any, adapter_id: str, facts: Any, provenance: Any, project_id: Optional[str] = None) -> Dict[str, Any]:
    """Validate, write the bridge file, and return the detection summary."""
    if not paths.vibe.is_dir():
        raise PermissionError("init_required: 请先运行 vibe init --confirm，再登记会话能力")
    if not isinstance(adapter_id, str) or not adapter_id.strip():
        raise ValueError("adapter is required")
    adapter_id = adapter_id.strip()
    if not isinstance(provenance, str) or not provenance.strip():
        raise ValueError("provenance is required: state where the facts were observed")
    if project_id is not None and (not isinstance(project_id, str) or not project_id.strip()):
        raise ValueError("project-id must be a non-empty string when given")
    validated = validate_session_facts(adapter_id, facts)
    ProviderActionStore(paths).publish_capabilities(
        adapter_id, validated, provenance.strip(), project_id.strip() if project_id else None,
    )
    observed = observe_capabilities(paths)
    caps = observed.detection.capabilities
    return {
        "adapter_id": observed.adapter_id,
        "level": caps.level,
        "mode": caps.mode,
        "visible_automation": bool(caps.visible_automation),
        "project_id": observed.project_id,
        "facts": validated,
        "capabilities_path": CAPABILITIES_RELATIVE_PATH,
    }
