"""Manifest-driven Agent capability adapters.

The adapter layer deliberately observes facts supplied by the host.  It does
not discover or call private desktop-app APIs.  A visible bridge is therefore
usable only when its create/enter/resume/wait probes have been verified by the
host environment.
"""

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .task_provider import BackgroundTaskProvider, VisibleTaskProvider


@dataclass(frozen=True)
class Environment:
    """Observable host facts used by adapters.

    Values are intentionally booleans.  Callers may provide command names,
    paths, permissions and neutral facts from their own probe implementation;
    this module never guesses a platform-specific executable or endpoint.
    """

    commands: Mapping[str, bool] = field(default_factory=dict)
    paths: Mapping[str, bool] = field(default_factory=dict)
    permissions: Mapping[str, bool] = field(default_factory=dict)
    facts: Mapping[str, bool] = field(default_factory=dict)
    available_agents: Sequence[str] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Environment":
        return cls(
            commands=value.get("commands", {}),
            paths=value.get("paths", {}),
            permissions=value.get("permissions", {}),
            facts=value.get("facts", {}),
            available_agents=value.get("available_agents", ()),
        )

    @staticmethod
    def _observed(values: Mapping[str, bool], name: str) -> bool:
        value = values.get(name, False)
        return bool(value)

    def has_command(self, name: str) -> bool:
        return self._observed(self.commands, name)

    def has_path(self, name: str) -> bool:
        return self._observed(self.paths, name)

    def has_permission(self, name: str) -> bool:
        return self._observed(self.permissions, name)

    def has_fact(self, name: str) -> bool:
        return self._observed(self.facts, name)


@dataclass(frozen=True)
class AgentCapabilities:
    agent_id: str
    shell: bool
    subprocess: bool
    worktree: bool
    background: bool
    session_resume: bool
    level: str
    mode: str
    provider: str
    visible_automation: bool
    direct_enter: bool
    create_task: bool
    enter_task: bool
    resume_task: bool
    wait_task: bool
    limitations: Tuple[str, ...] = ()
    evidence: Mapping[str, bool] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["limitations"] = list(self.limitations)
        return result


@dataclass(frozen=True)
class DetectionResult:
    adapter_id: str
    detected: bool
    capabilities: AgentCapabilities
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
    """Adapter whose behavior is limited to data in a manifest."""

    def __init__(self, manifest: Mapping[str, Any]):
        self.manifest = dict(manifest)
        self.id = str(self.manifest["id"])
        self.display_name = str(self.manifest.get("display_name", self.id))
        self.task_provider = VisibleTaskProvider(
            provider=str(self.manifest.get("provider", self.id))
        )

    @classmethod
    def from_path(cls, path: Path) -> "ManifestAdapter":
        # The checked-in manifests are JSON documents with a .yaml suffix. JSON
        # is valid YAML and keeps the adapter usable without an optional parser.
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("invalid adapter manifest: %s" % path) from exc
        return cls(manifest)

    def _probe(self, environment: Environment, probe: Mapping[str, Any]) -> bool:
        kind = probe.get("kind")
        name = str(probe.get("name", ""))
        if kind == "command":
            return environment.has_command(name)
        if kind == "path":
            return environment.has_path(name)
        if kind == "permission":
            return environment.has_permission(name)
        if kind == "fact":
            return environment.has_fact(name)
        raise ValueError("unsupported adapter probe kind: %s" % kind)

    def detect(self, environment: Environment) -> DetectionResult:
        if not isinstance(environment, Environment):
            environment = Environment.from_mapping(environment)  # type: ignore[arg-type]
        evidence = {
            str(probe["name"]): self._probe(environment, probe)
            for probe in self.manifest.get("probes", ())
        }
        command_probe = self.manifest.get("agent_probe")
        command_seen = bool(command_probe and evidence.get(str(command_probe), False))
        agent_seen = self.id in set(environment.available_agents) or environment.has_fact(
            "agent:%s" % self.id
        )
        detected = command_seen or agent_seen

        shell = evidence.get("shell", False)
        subprocess = evidence.get("subprocess", False)
        worktree = evidence.get("worktree", False)
        create = evidence.get("visible_task.create", False)
        enter = evidence.get("visible_task.enter", False)
        resume = evidence.get("visible_task.resume", False)
        wait = evidence.get("visible_task.wait", False)
        visible = shell and subprocess and worktree and create and enter and resume and wait
        if not subprocess:
            level, mode = "guide", "guide"
            limitations = ("无法启动 subprocess；仅保留向导能力",)
        elif visible:
            level, mode = "full", "visible"
            limitations = ()
        elif self.manifest.get("background_fallback", False):
            level, mode = "background", "background"
            limitations = (
                "不可见",
                "不可直接进入",
                "返工续接受限",
            )
        else:
            level, mode = "guide", "guide"
            limitations = ("未验证显式任务桥接",)
        capabilities = AgentCapabilities(
            agent_id=self.id,
            shell=shell,
            subprocess=subprocess,
            worktree=worktree,
            background=mode == "background",
            session_resume=resume,
            level=level,
            mode=mode,
            provider=str(self.manifest.get("provider", self.id)),
            visible_automation=visible,
            direct_enter=enter and visible,
            create_task=create and visible,
            enter_task=enter and visible,
            resume_task=resume and visible,
            wait_task=wait and visible,
            limitations=limitations,
            evidence=evidence,
        )
        reason = None
        if not detected:
            reason = "agent probe not observed"
        return DetectionResult(self.id, detected, capabilities, evidence, reason)

    def capabilities(self, environment: Environment) -> AgentCapabilities:
        return self.detect(environment).capabilities

    def session_prompt(self, trigger: str, plan_id: Optional[str] = None) -> str:
        trigger = str(trigger).strip()
        if not trigger:
            raise ValueError("trigger is required")
        suffix = ""
        if plan_id:
            suffix = "，计划 %s" % str(plan_id).strip()
        return "请%s%s。" % (trigger, suffix)

    def monitor_command(self, plan_id: str, json_output: bool = False) -> list:
        plan_id = str(plan_id).strip()
        if not plan_id:
            raise ValueError("plan_id is required")
        command = ["vibe", "monitor", "--plan", plan_id]
        if json_output:
            command.append("--json")
        return command

    def downgrade_reason(self, capabilities: AgentCapabilities) -> Optional[str]:
        if capabilities.level == "full":
            return None
        return "；".join(capabilities.limitations) or "能力未验证"

    def provider_for(self, capabilities: AgentCapabilities):
        if capabilities.mode == "background":
            return BackgroundTaskProvider(self.id)
        return self.task_provider

    def capability_report(
        self,
        environment: Environment,
        plan_id: Optional[str] = None,
        authorization_card: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Return stable, non-secret data for a desktop session bridge."""
        result = self.detect(environment)
        report = result.to_dict()
        report["capability_level"] = result.capabilities.level
        report["mode"] = result.capabilities.mode
        report["monitor_command"] = self.monitor_command(plan_id, True) if plan_id else None
        report["authorization_card"] = str(authorization_card) if authorization_card else None
        return report


# Public contract name; the implementation remains manifest-driven.
Adapter = ManifestAdapter
