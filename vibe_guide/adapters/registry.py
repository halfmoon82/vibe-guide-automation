"""Discovery and selection of the seven manifest-driven adapters."""

from pathlib import Path
from typing import Dict, Optional

from .base import Environment, ManifestAdapter


class AdapterRegistry:
    def __init__(self, manifest_dir: Optional[Path] = None):
        self.manifest_dir = Path(manifest_dir or Path(__file__).parent / "manifests")
        self._adapters: Dict[str, ManifestAdapter] = {}
        for path in sorted(self.manifest_dir.glob("*.yaml")):
            adapter = ManifestAdapter.from_path(path)
            self._adapters[adapter.id] = adapter

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
