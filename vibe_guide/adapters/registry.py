"""Discovery and strict selection of the seven manifest adapters."""

from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence

from .base import (
    TOPOLOGY_DUAL_VISIBLE,
    TOPOLOGY_IN_SESSION_SDD,
    Environment,
    ManifestAdapter,
    ManifestError,
)
from .task_provider import TaskProviderAdapter


SUPPORTED_ADAPTER_IDS = frozenset({
    "codex", "claude-code", "cursor", "grok", "workbuddy", "kimi-code",
    "deepseek-harness",
})

# Dispatch-topology ruling table (v4.6 ISSUE-07). ``probe_pass`` applies when
# the platform's `<id>.in_session_sdd` fact probe passes with provenance;
# ``probe_unknown`` applies when the evidence is UNKNOWN or unsupported.
# UNKNOWN is fail-closed: it never yields ``in_session_sdd``.
DISPATCH_TOPOLOGY_MATRIX = {
    "codex": {"probe_pass": "in_session_sdd", "probe_unknown": "dual-visible"},
    "claude-code": {"probe_pass": "in_session_sdd", "probe_unknown": "dual-visible"},
    "cursor": {"probe_pass": "in_session_sdd", "probe_unknown": "dual-visible"},
    "kimi-code": {"probe_pass": "in_session_sdd", "probe_unknown": "dual-visible"},
    # DeepSeek Harness dispatches dual-visible until its probe evidence passes.
    "deepseek-harness": {"probe_pass": "in_session_sdd", "probe_unknown": "dual-visible"},
    "workbuddy": {"probe_pass": "dual-visible", "probe_unknown": "dual-visible"},
    "grok": {"probe_pass": "dual-visible", "probe_unknown": "dual-visible"},
}

_TOPOLOGY_ROW_KEYS = {"probe_pass", "probe_unknown"}
_TOPOLOGY_ROW_VALUES = {TOPOLOGY_IN_SESSION_SDD, TOPOLOGY_DUAL_VISIBLE}
if set(DISPATCH_TOPOLOGY_MATRIX) != SUPPORTED_ADAPTER_IDS or any(
    set(row) != _TOPOLOGY_ROW_KEYS
    or any(value not in _TOPOLOGY_ROW_VALUES for value in row.values())
    for row in DISPATCH_TOPOLOGY_MATRIX.values()
):
    raise ManifestError("dispatch topology matrix does not match the supported adapter set and topology values")


class AdapterRegistry:
    def __init__(self, manifest_dir: Optional[Path] = None, background_launchers=None):
        self.manifest_dir = Path(manifest_dir or Path(__file__).parent / "manifests")
        paths = sorted(self.manifest_dir.glob("*.yaml"))
        if not paths:
            raise ManifestError("adapter manifest directory is empty: %s" % self.manifest_dir)
        launchers = background_launchers or {}
        self._adapters: Dict[str, ManifestAdapter] = {}
        for path in paths:
            adapter = ManifestAdapter.from_path(path, background_launcher=launchers.get(path.stem))
            if adapter.id in self._adapters:
                raise ManifestError("duplicate adapter id: %s" % adapter.id)
            adapter.upgrade_adapter = TaskProviderAdapter(adapter.manifest["provider"], adapter.task_provider, mode="visible")
            adapter.topology_spec = DISPATCH_TOPOLOGY_MATRIX.get(adapter.id)
            self._adapters[adapter.id] = adapter
        self._require_complete()

    @classmethod
    def from_manifests(cls, manifests: Sequence[Mapping], background_launchers=None):
        registry = cls.custom_from_manifests(manifests, background_launchers)
        registry._require_complete()
        return registry

    @classmethod
    def custom_from_manifests(cls, manifests: Sequence[Mapping], background_launchers=None):
        """Explicit custom/test registry; production callers use constructor/from_manifests."""
        if not manifests:
            raise ManifestError("adapter manifest set is empty")
        registry = cls.__new__(cls)
        registry.manifest_dir = None
        launchers = background_launchers or {}
        registry._adapters = {}
        for manifest in manifests:
            adapter = ManifestAdapter(manifest, background_launcher=launchers.get(manifest.get("id")))
            if adapter.id in registry._adapters:
                raise ManifestError("duplicate adapter id: %s" % adapter.id)
            adapter.upgrade_adapter = TaskProviderAdapter(adapter.manifest["provider"], adapter.task_provider, mode="visible")
            adapter.topology_spec = DISPATCH_TOPOLOGY_MATRIX.get(adapter.id)
            registry._adapters[adapter.id] = adapter
        return registry

    def _require_complete(self):
        actual = set(self._adapters)
        if actual != SUPPORTED_ADAPTER_IDS:
            missing = sorted(SUPPORTED_ADAPTER_IDS - actual)
            unexpected = sorted(actual - SUPPORTED_ADAPTER_IDS)
            raise ManifestError(
                "production adapter set mismatch; missing=%s unexpected=%s"
                % (missing, unexpected)
            )

    @property
    def ids(self):
        return tuple(self._adapters)

    def get(self, adapter_id: str) -> ManifestAdapter:
        try:
            return self._adapters[adapter_id]
        except KeyError as exc:
            raise KeyError("unknown Agent adapter: %s" % adapter_id) from exc

    def detect_all(self, environment: Environment):
        return [adapter.detect(environment) for adapter in self._adapters.values()]

    def topology_decisions(self, environment: Environment):
        """Per-platform dispatch-topology rulings with evidence references."""
        return {adapter_id: adapter.topology_decision(environment) for adapter_id, adapter in self._adapters.items()}

    def describe_upgrade_entries(self):
        """Return provider-neutral upgrade metadata for all registered adapters."""
        return {adapter_id: adapter.upgrade_adapter.describe_upgrade_entry() for adapter_id, adapter in self._adapters.items()}

    def invoke_upgrade(self, adapter_id: str, request: Mapping):
        """Delegate a session upgrade without duplicating migration logic."""
        return self.get(adapter_id).upgrade_adapter.invoke_upgrade(request)
