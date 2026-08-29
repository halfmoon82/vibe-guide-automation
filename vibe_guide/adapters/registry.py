"""Evidence-bound discovery and stable routing for Agent adapters."""

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence
from urllib.parse import urlparse


SUPPORTED_ADAPTER_IDS = frozenset({
    "codex", "claude-code", "cursor", "grok", "workbuddy", "kimi-code",
    "deepseek-harness",
})
ADAPTER_PROVIDERS = {
    "codex": "codex-app-visible", "claude-code": "claude-code-visible",
    "cursor": "cursor-visible", "grok": "grok-visible",
    "workbuddy": "workbuddy-visible", "kimi-code": "kimi-code-visible",
    "deepseek-harness": "deepseek-harness-visible",
}
DEFAULT_SOURCE_REF = "https://github.com/halfmoon82/vibe-guide-automation"
DEFAULT_SOURCE_COMMIT = "0c4bb712f7b344f1996de1c760388bcfe7b03d4d"


class ManifestError(ValueError):
    """Manifest data is incomplete, duplicated, or not source-verifiable."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def inspect_manifest_source(source_ref: str, source_commit: str) -> Dict[str, Any]:
    """Validate a reproducible HTTPS source without treating local paths as proof."""
    if not isinstance(source_ref, str) or not source_ref.strip():
        raise ValueError("stable source reference is required")
    if not isinstance(source_commit, str) or len(source_commit) != 40 or any(ch not in "0123456789abcdefABCDEF" for ch in source_commit):
        raise ValueError("stable source commit must be a 40-character SHA")
    ref = source_ref.strip()
    parsed = urlparse(ref)
    verified = parsed.scheme == "https" and bool(parsed.netloc)
    return {
        "status": "verified" if verified else "blocked_unknown",
        "source_ref": ref,
        "source_commit": source_commit.lower(),
        "reason": None if verified else "temporary source is not a recoverable upgrade origin",
    }


def _builtin_manifests():
    return [
        {
            "id": adapter_id,
            "display_name": adapter_id,
            "provider": ADAPTER_PROVIDERS[adapter_id],
            "source": {"ref": DEFAULT_SOURCE_REF, "commit": DEFAULT_SOURCE_COMMIT},
            "agent_probe": adapter_id + ".agent",
            "probes": [{"kind": "command", "name": adapter_id + ".agent"}] + [
                {"kind": "fact", "name": adapter_id + "." + suffix}
                for suffix in ("shell", "subprocess", "worktree", "visible_task.create", "visible_task.enter", "visible_task.resume", "visible_task.wait")
            ],
            "session_prompt": "请{trigger}，计划 {plan_id}。",
        }
        for adapter_id in sorted(SUPPORTED_ADAPTER_IDS)
    ]


@dataclass(frozen=True)
class Environment:
    commands: Mapping[str, Any] = field(default_factory=dict)
    paths: Mapping[str, Any] = field(default_factory=dict)
    permissions: Mapping[str, Any] = field(default_factory=dict)
    facts: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, str] = field(default_factory=dict)
    available_agents: Sequence[str] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping):
            raise TypeError("environment must be an object")
        return cls(**{key: value.get(key, {}) for key in ("commands", "paths", "permissions", "facts", "provenance")}, available_agents=value.get("available_agents", ()))

    @staticmethod
    def _bool(values, name):
        if name not in values:
            return False
        if type(values[name]) is not bool:
            raise ValueError("capability evidence must be boolean: %s" % name)
        return values[name]

    def has_command(self, name): return self._bool(self.commands, name)
    def has_path(self, name): return self._bool(self.paths, name)
    def has_permission(self, name): return self._bool(self.permissions, name)
    def has_fact(self, name, provider=None):
        return self._bool(self.facts, name) if not provider or name.startswith(provider + ".") else False
    def fact_source(self, name): return self.provenance.get(name)


@dataclass
class AdapterCapabilities:
    agent_id: str
    shell: bool
    subprocess: bool
    worktree: bool
    background: bool
    session_resume: bool
    level: str
    provider: str = ""
    mode: str = "guide"
    visible_automation: bool = False
    direct_enter: bool = False
    create_task: bool = False
    enter_task: bool = False
    resume_task: bool = False
    wait_task: bool = False
    limitations: Sequence[str] = ()
    evidence: Mapping[str, bool] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self): return asdict(self)


@dataclass(frozen=True)
class DetectionResult:
    adapter_id: str
    detected: bool
    capabilities: AdapterCapabilities
    evidence: Mapping[str, bool]
    reason: Optional[str] = None

    @property
    def level(self): return self.capabilities.level
    @property
    def mode(self): return self.capabilities.mode
    def to_dict(self):
        return {"adapter_id": self.adapter_id, "detected": self.detected, "capabilities": self.capabilities.to_dict(), "evidence": dict(self.evidence), "reason": self.reason}


@dataclass
class ManifestRecord:
    id: str
    display_name: str
    provider: str
    raw: Mapping[str, Any]
    path: Optional[str] = None

    def to_dict(self):
        value = dict(self.raw)
        value.setdefault("id", self.id)
        value.setdefault("display_name", self.display_name)
        value.setdefault("provider", self.provider)
        if self.path:
            value["path"] = self.path
        return value


class ManifestAdapter(ManifestRecord):
    def __init__(self, manifest: Mapping[str, Any], background_launcher=None, path=None):
        if not isinstance(manifest, Mapping):
            raise ManifestError("manifest must be an object")
        required = {"id", "display_name", "provider"}
        if required - set(manifest):
            raise ManifestError("manifest missing fields: %s" % ", ".join(sorted(required - set(manifest))))
        adapter_id = manifest["id"]
        if adapter_id not in SUPPORTED_ADAPTER_IDS:
            raise ManifestError("unsupported adapter id: %s" % adapter_id)
        if not all(isinstance(manifest[name], str) and manifest[name].strip() for name in ("display_name", "provider")):
            raise ManifestError("manifest display_name/provider must be non-empty strings")
        if manifest["provider"] != ADAPTER_PROVIDERS[adapter_id]:
            raise ManifestError("manifest provider does not match adapter route: %s" % adapter_id)
        source = manifest.get("source")
        if source is not None:
            if not isinstance(source, Mapping) or set(source) != {"ref", "commit"}:
                raise ManifestError("manifest source must contain ref and commit")
            try:
                source_check = inspect_manifest_source(source["ref"], source["commit"])
            except ValueError as exc:
                raise ManifestError("manifest source is invalid") from exc
            if source_check["status"] != "verified":
                raise ManifestError("manifest source is not stable")
        super().__init__(adapter_id, manifest["display_name"], manifest["provider"], dict(manifest), str(path) if path else None)
        self.manifest = dict(manifest)
        self.background_launcher = background_launcher
        self.task_provider = None
        try:
            from .task_provider import BackgroundTaskProvider, VisibleTaskProvider
            self.task_provider = VisibleTaskProvider(provider=self.provider)
            self._background_provider_type = BackgroundTaskProvider
        except ImportError:  # pragma: no cover
            self._background_provider_type = None

    @classmethod
    def from_path(cls, path: Path):
        return cls(_load_manifest(Path(path)), path=Path(path))

    def to_dict(self):
        value = dict(self.raw)
        if self.path:
            value["path"] = self.path
        return value

    def detect(self, environment):
        if not isinstance(environment, Environment):
            environment = Environment.from_mapping(environment)
        evidence = {}
        for probe in self.manifest.get("probes", []):
            if not isinstance(probe, Mapping) or not isinstance(probe.get("name"), str):
                raise ManifestError("manifest probe is invalid")
            name, kind = probe["name"], probe.get("kind", "fact")
            probe_fn = {
                "command": environment.has_command,
                "path": environment.has_path,
                "permission": environment.has_permission,
                "fact": lambda n: environment.has_fact(n, self.id) if "." in n else environment.has_fact(n),
            }.get(kind)
            if probe_fn is None:
                raise ManifestError("unsupported adapter probe kind: %s" % kind)
            evidence[name] = probe_fn(name)
        def fact(s): return evidence.get(self.id + "." + s, False)
        shell, subprocess, worktree = fact("shell"), fact("subprocess"), fact("worktree")
        create, enter, resume, wait = (fact("visible_task." + name) for name in ("create", "enter", "resume", "wait"))
        visible = all((shell, subprocess, worktree, create, enter, resume, wait))
        background = bool(subprocess and self.manifest.get("background_fallback", False) and not visible)
        capabilities = AdapterCapabilities(
            self.id, shell, subprocess, worktree, background, resume,
            "full" if visible else ("background" if background else "guide"), self.provider if visible else "",
            "visible" if visible else ("background" if background else "guide"), visible, visible, visible, visible,
            visible, visible,
            ("不可见", "不可直接进入", "返工续接受限") if background else (("未验证显式任务桥接",) if not visible else ()), evidence,
            {name: environment.fact_source(name) for name in evidence},
        )
        detected = self.id in set(environment.available_agents) or environment.has_command(self.manifest.get("agent_probe", self.id + ".agent"))
        return DetectionResult(self.id, detected, capabilities, evidence, None if detected else "agent probe not observed")

    def capabilities(self, environment): return self.detect(environment).capabilities
    def session_prompt(self, trigger, plan_id=None):
        trigger = str(trigger).strip()
        if not trigger:
            raise ValueError("trigger is required")
        return self.manifest.get("session_prompt", "请{trigger}，计划 {plan_id}。").format(trigger=trigger, plan_id=str(plan_id or "").strip())
    def monitor_command(self, plan_id, json_output=False):
        if not str(plan_id).strip():
            raise ValueError("plan_id is required")
        command = ["vibe", "monitor", "--plan", str(plan_id)]
        if json_output: command.append("--json")
        return command
    def provider_for(self, capabilities):
        if capabilities.mode == "background" and self._background_provider_type:
            return self._background_provider_type(self.provider, self.background_launcher)
        if capabilities.mode == "visible":
            return self.task_provider
        return None
    def downgrade_reason(self, capabilities):
        return None if capabilities.level == "full" else "；".join(capabilities.limitations) or "能力未验证"
    def capability_report(self, environment, plan_id=None, authorization_card=None):
        result = self.detect(environment).to_dict()
        result.update({"capability_level": result["capabilities"]["level"], "mode": result["capabilities"]["mode"], "provider": result["capabilities"]["provider"], "monitor_command": self.monitor_command(plan_id, True) if plan_id else None, "authorization_card": str(authorization_card) if authorization_card else None})
        return result
    def guidance_contract(self):
        return load_guidance_contract()


Adapter = ManifestAdapter


def _load_manifest(path: Path):
    if path.is_symlink() or not path.is_file():
        raise ManifestError("adapter manifest must be a regular file: %s" % path)
    try:
        text = path.read_text(encoding="utf-8")
        try:
            value = json.loads(text)
        except ValueError:
            import yaml
            value = yaml.safe_load(text)
    except Exception as exc:
        raise ManifestError("adapter manifest is not machine-readable: %s" % path) from exc
    if not isinstance(value, Mapping):
        raise ManifestError("adapter manifest must be an object: %s" % path)
    return value


def _source_from_adapters(adapters):
    sources = [adapter.raw.get("source") for adapter in adapters]
    if not sources or any(not isinstance(source, Mapping) for source in sources):
        return {"status": "retry_pending", "reason": "manifest source metadata is missing"}
    first = sources[0]
    if any(source != first for source in sources[1:]):
        return {"status": "blocked_unknown", "reason": "manifest source metadata is inconsistent"}
    try:
        return inspect_manifest_source(first["ref"], first["commit"])
    except (KeyError, TypeError, ValueError):
        return {"status": "blocked_unknown", "reason": "manifest source metadata is invalid"}


def load_guidance_contract(path: Optional[Path] = None):
    target = Path(path or Path(__file__).parent.parent / "guidance" / "canonical-contract.json")
    if target.is_symlink() or not target.is_file():
        raise ManifestError("Guidance Contract asset is missing: %s" % target)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ManifestError("Guidance Contract asset is invalid") from exc
    if not isinstance(value, dict) or not isinstance(value.get("contract_hash"), str):
        raise ManifestError("Guidance Contract hash is missing")
    supplied = value.pop("contract_hash")
    expected = _digest(value)
    value["contract_hash"] = supplied
    if supplied != expected:
        raise ManifestError("Guidance Contract hash mismatch")
    return value


class AdapterRegistry:
    def __init__(self, manifest_dir: Optional[Path] = None, background_launchers=None, *, source_ref=None, source_commit=None, require_complete=True):
        self.manifest_dir = Path(manifest_dir or Path(__file__).parent / "manifests")
        self._adapters = {}
        if not self.manifest_dir.exists() or not self.manifest_dir.is_dir():
            if manifest_dir is not None:
                raise ManifestError("adapter manifest directory is missing: %s" % self.manifest_dir)
            self._adapters = {item["id"]: ManifestAdapter(item) for item in _builtin_manifests()}
            self._source = inspect_manifest_source(DEFAULT_SOURCE_REF, DEFAULT_SOURCE_COMMIT)
        else:
            paths = sorted(self.manifest_dir.glob("*.yaml"))
            if not paths:
                raise ManifestError("adapter manifest directory is empty: %s" % self.manifest_dir)
            launchers = background_launchers or {}
            for path in paths:
                adapter = ManifestAdapter(_load_manifest(path), background_launcher=launchers.get(path.stem), path=path)
                if adapter.id in self._adapters:
                    raise ManifestError("duplicate adapter id: %s" % adapter.id)
                self._adapters[adapter.id] = adapter
            if require_complete:
                self._require_supported_set()
            self._source = inspect_manifest_source(source_ref, source_commit) if source_ref is not None or source_commit is not None else _source_from_adapters(self._adapters.values())
        self._require_complete = require_complete
        if require_complete and self._adapters and set(self._adapters) != SUPPORTED_ADAPTER_IDS:
            self._require_supported_set()

    @classmethod
    def from_manifests(cls, manifests: Sequence[Mapping[str, Any]], background_launchers=None, *, source_ref=None, source_commit=None, require_complete=True):
        if not manifests:
            raise ManifestError("adapter manifest set is empty")
        registry = cls.__new__(cls)
        registry.manifest_dir = None
        registry._adapters = {}
        launchers = background_launchers or {}
        for manifest in manifests:
            adapter = ManifestAdapter(manifest, background_launcher=launchers.get(manifest.get("id")))
            if adapter.id in registry._adapters:
                raise ManifestError("duplicate adapter id: %s" % adapter.id)
            registry._adapters[adapter.id] = adapter
        registry._require_complete = require_complete
        if require_complete:
            registry._require_supported_set()
        registry._source = inspect_manifest_source(source_ref, source_commit) if source_ref is not None or source_commit is not None else _source_from_adapters(registry._adapters.values())
        return registry

    @classmethod
    def custom_from_manifests(cls, manifests, background_launchers=None):
        return cls.from_manifests(manifests, background_launchers, require_complete=False)

    def _require_supported_set(self):
        actual = set(self._adapters)
        if actual != SUPPORTED_ADAPTER_IDS:
            raise ManifestError("production adapter set mismatch; missing=%s unexpected=%s" % (sorted(SUPPORTED_ADAPTER_IDS - actual), sorted(actual - SUPPORTED_ADAPTER_IDS)))

    @property
    def ids(self): return tuple(self._adapters)
    def get(self, adapter_id):
        try: return self._adapters[adapter_id]
        except KeyError as exc: raise KeyError("unknown Agent adapter: %s" % adapter_id) from exc
    def detect_all(self, environment): return [adapter.detect(environment) for adapter in self._adapters.values()]
    def integrity(self):
        records = [self._adapters[key].to_dict() for key in sorted(self._adapters)]
        source = self._source
        if source.get("status") == "unknown":
            source = {"status": "retry_pending", "reason": source.get("reason", "stable source reference not supplied")}
        return {**source, "manifest_count": len(records), "manifest_digest": _digest(records)}
