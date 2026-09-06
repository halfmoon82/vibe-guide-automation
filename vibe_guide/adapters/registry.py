"""Discovery and strict selection of the seven manifest adapters."""

from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence

from .base import Environment, ManifestAdapter, ManifestError
from .task_provider import TaskProviderAdapter


SUPPORTED_ADAPTER_IDS = frozenset({
    "codex", "claude-code", "cursor", "grok", "workbuddy", "kimi-code",
    "deepseek-harness",
})


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

    def describe_upgrade_entries(self):
        """Return provider-neutral upgrade metadata for all registered adapters."""
        return {adapter_id: adapter.upgrade_adapter.describe_upgrade_entry() for adapter_id, adapter in self._adapters.items()}

    def invoke_upgrade(self, adapter_id: str, request: Mapping):
        """Delegate a session upgrade without duplicating migration logic."""
        return self.get(adapter_id).upgrade_adapter.invoke_upgrade(request)
