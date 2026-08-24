"""Manifest-driven Agent capability adapters."""

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import string
import re
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from vibe_guide.models import AgentCapabilities as SharedAgentCapabilities

from .task_provider import BackgroundTaskProvider, VisibleTaskProvider


class ManifestError(ValueError):
    """A manifest set is empty, malformed, duplicated or unsupported."""


@dataclass(frozen=True)
class Environment:
    """Strict, provider-bound facts supplied by the host probe layer."""

    commands: Mapping[str, Any] = field(default_factory=dict)
    paths: Mapping[str, Any] = field(default_factory=dict)
    permissions: Mapping[str, Any] = field(default_factory=dict)
    facts: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, str] = field(default_factory=dict)
    available_agents: Sequence[str] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Environment":
        return cls(
            commands=value.get("commands", {}),
            paths=value.get("paths", {}),
            permissions=value.get("permissions", {}),
            facts=value.get("facts", {}),
            provenance=value.get("provenance", {}),
            available_agents=value.get("available_agents", ()),
        )

    @staticmethod
    def _bool(values: Mapping[str, Any], name: str) -> bool:
        if name not in values:
            return False
        value = values[name]
        if not isinstance(value, bool):
            raise ValueError("capability evidence must be boolean: %s" % name)
        return value

    def has_command(self, name: str) -> bool:
        return self._bool(self.commands, name)

    def has_path(self, name: str) -> bool:
        return self._bool(self.paths, name)

    def has_permission(self, name: str) -> bool:
        return self._bool(self.permissions, name)

    def has_fact(self, name: str, provider: Optional[str] = None) -> bool:
        if provider and not name.startswith(provider + "."):
            return False
        return self._bool(self.facts, name)

    def fact_source(self, name: str) -> Optional[str]:
        source = self.provenance.get(name)
        if source is not None and not isinstance(source, str):
            raise ValueError("capability evidence provenance must be a string: %s" % name)
        return source


@dataclass
class AdapterCapabilities(SharedAgentCapabilities):
    """N0 capability model plus selected provider identity and mode."""

    provider: str = ""
    mode: str = "guide"
    visible_automation: bool = False
    direct_enter: bool = False
    create_task: bool = False
    enter_task: bool = False
    resume_task: bool = False
    wait_task: bool = False
    limitations: Tuple[str, ...] = ()
    evidence: Mapping[str, bool] = field(default_factory=dict)
    provenance: Mapping[str, Optional[str]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["limitations"] = list(self.limitations)
        result["provenance"] = dict(self.provenance)
        return result


@dataclass(frozen=True)
class DetectionResult:
    adapter_id: str
    detected: bool
    capabilities: AdapterCapabilities
    evidence: Mapping[str, bool]
    reason: Optional[str] = None

    @property
    def level(self):
        return self.capabilities.level

    @property
    def mode(self):
        return self.capabilities.mode

    @property
    def visible_automation(self):
        return self.capabilities.visible_automation

    def to_dict(self) -> Dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "detected": self.detected,
            "capabilities": self.capabilities.to_dict(),
            "evidence": dict(self.evidence),
            "reason": self.reason,
        }


class ManifestAdapter:
    """Adapter with no platform-specific behavior outside its manifest."""

    _allowed_fields = {
        "id", "display_name", "agent_probe", "provider", "background_provider",
        "background_fallback", "session_prompt", "probes",
    }

    def __init__(self, manifest: Mapping[str, Any], background_launcher=None):
        self.manifest = self.validate_manifest(manifest)
        self.id = self.manifest["id"]
        self.display_name = self.manifest["display_name"]
        self.background_launcher = background_launcher
        self.task_provider = VisibleTaskProvider(
            provider=self.manifest["provider"],
            prompt_factory=lambda role, issue, path: self.session_prompt(
                "执行 %s 任务" % role, issue
            ),
        )

    @classmethod
    def from_path(cls, path: Path, background_launcher=None) -> "ManifestAdapter":
        try:
            manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ManifestError("invalid adapter manifest: %s" % path) from exc
        return cls(manifest, background_launcher=background_launcher)

    @classmethod
    def validate_manifest(cls, manifest: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(manifest, Mapping) or not manifest:
            raise ManifestError("manifest must be a non-empty object")
        unknown = set(manifest) - cls._allowed_fields
        if unknown:
            raise ManifestError("unsupported manifest fields: %s" % ", ".join(sorted(unknown)))
        missing = cls._allowed_fields - set(manifest)
        if missing:
            raise ManifestError("manifest missing fields: %s" % ", ".join(sorted(missing)))
        if not isinstance(manifest["id"], str) or not re.match(r"^[a-z0-9-]+$", manifest["id"]):
            raise ManifestError("manifest id must be a non-empty string")
        for field_name in ("display_name", "agent_probe", "provider", "background_provider", "session_prompt"):
            if not isinstance(manifest[field_name], str) or not manifest[field_name]:
                raise ManifestError("manifest %s must be a non-empty string" % field_name)
        if not isinstance(manifest["background_fallback"], bool):
            raise ManifestError("manifest background_fallback must be boolean")
        if not isinstance(manifest["probes"], list) or not manifest["probes"]:
            raise ManifestError("manifest probes must be a non-empty list")
        seen = set()
        for probe in manifest["probes"]:
            if not isinstance(probe, Mapping) or set(probe) - {"kind", "name"}:
                raise ManifestError("manifest probe has invalid fields")
            if probe.get("kind") not in {"command", "path", "permission", "fact"}:
                raise ManifestError("manifest probe kind is unsupported")
            name = probe.get("name")
            if not isinstance(name, str) or not name.startswith(manifest["id"] + "."):
                raise ManifestError("manifest probe must be provider-namespaced")
            if name in seen:
                raise ManifestError("duplicate manifest probe: %s" % name)
            seen.add(name)
        fields = [item[1] for item in string.Formatter().parse(manifest["session_prompt"]) if item[1]]
        if set(fields) - {"trigger", "plan_id"} or "trigger" not in fields:
            raise ManifestError("session_prompt must use only trigger and plan_id")
        return dict(manifest)

    def _probe(self, environment: Environment, probe: Mapping[str, Any]) -> bool:
        kind, name = probe["kind"], probe["name"]
        if kind == "command":
            return environment.has_command(name)
        if kind == "path":
            return environment.has_path(name)
        if kind == "permission":
            return environment.has_permission(name)
        return environment.has_fact(name, self.id)

    def detect(self, environment: Environment) -> DetectionResult:
        if not isinstance(environment, Environment):
            environment = Environment.from_mapping(environment)
        evidence = {probe["name"]: self._probe(environment, probe) for probe in self.manifest["probes"]}
        provenance = {name: environment.fact_source(name) for name in evidence}
        command_seen = environment.has_command(self.manifest["agent_probe"])
        agent_seen = self.id in set(environment.available_agents) or environment.has_fact(self.id + ".agent", self.id)
        detected = command_seen or agent_seen
        def fact(suffix):
            return evidence.get(self.id + "." + suffix, False)
        shell, subprocess, worktree = fact("shell"), fact("subprocess"), fact("worktree")
        create, enter = fact("visible_task.create"), fact("visible_task.enter")
        resume, wait = fact("visible_task.resume"), fact("visible_task.wait")
        visible = shell and subprocess and worktree and create and enter and resume and wait
        if not subprocess:
            level, mode, provider = "guide", "guide", ""
            limitations = ("无法启动 subprocess；仅保留向导能力",)
        elif visible:
            level, mode, provider = "full", "visible", self.manifest["provider"]
            limitations = ()
        elif self.manifest["background_fallback"]:
            level, mode, provider = "background", "background", self.manifest["background_provider"]
            limitations = ("不可见", "不可直接进入", "返工续接受限")
        else:
            level, mode, provider = "guide", "guide", ""
            limitations = ("未验证显式任务桥接",)
        capabilities = AdapterCapabilities(
            agent_id=self.id, shell=shell, subprocess=subprocess, worktree=worktree,
            background=mode == "background", session_resume=resume, level=level,
            provider=provider, mode=mode, visible_automation=visible,
            direct_enter=enter and visible, create_task=create and visible,
            enter_task=enter and visible, resume_task=resume and visible,
            wait_task=wait and visible, limitations=limitations,
            evidence=evidence, provenance=provenance,
        )
        return DetectionResult(self.id, detected, capabilities, evidence,
                               None if detected else "agent probe not observed")

    def capabilities(self, environment: Environment) -> AdapterCapabilities:
        return self.detect(environment).capabilities

    def session_prompt(self, trigger: str, plan_id: Optional[str] = None) -> str:
        try:
            prompt = self.manifest["session_prompt"].format(trigger=str(trigger).strip(), plan_id=str(plan_id or "").strip())
        except (KeyError, ValueError) as exc:
            raise ManifestError("invalid session_prompt template") from exc
        if not prompt or len(prompt) > 120:
            raise ManifestError("session_prompt must be 1-120 characters")
        return prompt

    def monitor_command(self, plan_id: str, json_output: bool = False) -> list:
        if not str(plan_id).strip():
            raise ValueError("plan_id is required")
        command = ["vibe", "monitor", "--plan", str(plan_id).strip()]
        if json_output:
            command.append("--json")
        return command

    def downgrade_reason(self, capabilities: AdapterCapabilities) -> Optional[str]:
        return None if capabilities.level == "full" else "；".join(capabilities.limitations)

    def provider_for(self, capabilities: AdapterCapabilities):
        if capabilities.mode == "guide":
            return None
        expected = self.manifest["provider"] if capabilities.mode == "visible" else self.manifest["background_provider"]
        if capabilities.provider != expected:
            raise ManifestError("selected provider does not match capability report")
        if capabilities.mode == "background":
            return BackgroundTaskProvider(capabilities.provider, self.background_launcher)
        return self.task_provider

    def capability_report(self, environment: Environment, plan_id: Optional[str] = None, authorization_card: Optional[Path] = None):
        result = self.detect(environment)
        report = result.to_dict()
        report["capability_level"] = result.capabilities.level
        report["mode"] = result.capabilities.mode
        report["provider"] = result.capabilities.provider
        report["monitor_command"] = self.monitor_command(plan_id, True) if plan_id else None
        report["authorization_card"] = str(authorization_card) if authorization_card else None
        return report


Adapter = ManifestAdapter
