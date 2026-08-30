"""Compatibility adapter surface with canonical Guidance Contract injection."""

from pathlib import Path
import re
import string
from typing import Any, Mapping, Optional

from .registry import AdapterCapabilities, DetectionResult, Environment, ManifestAdapter as _RegistryManifestAdapter, ManifestError
from ..guidance import inject_guidance, load_guidance_contract
from .task_provider import VisibleTaskProvider


class ManifestAdapter(_RegistryManifestAdapter):
    """Manifest-backed adapter that cannot override governance semantics."""

    def __init__(self, manifest: Mapping[str, Any], background_launcher=None, path=None):
        self.validate_manifest(manifest)
        super().__init__(manifest, background_launcher=background_launcher, path=path)
        self.task_provider = VisibleTaskProvider(provider=self.provider, guidance_loader=load_guidance_contract)

    @classmethod
    def from_path(cls, path: Path, background_launcher=None) -> "ManifestAdapter":
        from .registry import _load_manifest
        return cls(_load_manifest(Path(path)), background_launcher=background_launcher, path=path)

    @classmethod
    def validate_manifest(cls, manifest: Mapping[str, Any]):
        if not isinstance(manifest, Mapping) or not manifest:
            raise ManifestError("manifest must be a non-empty object")
        for field in ("id", "display_name", "provider"):
            if not isinstance(manifest.get(field), str) or not manifest[field].strip():
                raise ManifestError("manifest %s must be a non-empty string" % field)
        if not re.match(r"^[a-z0-9-]+$", manifest["id"]):
            raise ManifestError("manifest id must be a non-empty string")
        probes = manifest.get("probes", [])
        if not isinstance(probes, list):
            raise ManifestError("manifest probes must be a list")
        seen = set()
        for probe in probes:
            if not isinstance(probe, Mapping) or probe.get("kind") not in {"command", "path", "permission", "fact"}:
                raise ManifestError("manifest probe is invalid")
            name = probe.get("name")
            if not isinstance(name, str) or not name.startswith(manifest["id"] + ".") or name in seen:
                raise ManifestError("manifest probe must be provider-namespaced and unique")
            seen.add(name)
        template = manifest.get("session_prompt", "请{trigger}，计划 {plan_id}。")
        if not isinstance(template, str) or not template:
            raise ManifestError("manifest session_prompt must be a non-empty string")
        fields = [part[1] for part in string.Formatter().parse(template) if part[1]]
        if set(fields) - {"trigger", "plan_id"} or "trigger" not in fields:
            raise ManifestError("session_prompt must use only trigger and plan_id")
        return dict(manifest)

    def guidance_contract(self):
        return load_guidance_contract()

    def guidance_context(self, stage="prd_approved", status="approved"):
        return inject_guidance(self.id, stage=stage, status=status)

    def session_prompt(self, trigger: str, plan_id: Optional[str] = None) -> str:
        prompt = super().session_prompt(trigger, plan_id)
        if not prompt or len(prompt) > 120:
            raise ManifestError("session_prompt must be 1-120 characters")
        return prompt


Adapter = ManifestAdapter

__all__ = ["Environment", "AdapterCapabilities", "DetectionResult", "ManifestError", "ManifestAdapter", "Adapter"]
