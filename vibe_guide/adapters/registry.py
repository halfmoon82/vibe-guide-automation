"""Discovery and strict selection of the seven manifest adapters."""

from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence

from .base import Environment, ManifestAdapter, ManifestError


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
            self._adapters[adapter.id] = adapter

    @classmethod
    def from_manifests(cls, manifests: Sequence[Mapping], background_launchers=None):
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
            registry._adapters[adapter.id] = adapter
        return registry

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
