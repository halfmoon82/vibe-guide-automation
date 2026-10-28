"""Platform-neutral capability tiers and their per-platform model resolution.

A node carries a *tier* (``light`` / ``standard`` / ``deep``) derived from its
own contract.  The tier is platform independent and is what the authorization
digest sees; the concrete model is looked up at dispatch time by
:func:`resolve` and never enters a digest, so moving a run between platforms or
changing a configured model never invalidates an authorization.

A failed or empty probe is ``unknown``: the caller falls back to the platform
default model and records that fact; it is never read as "no model".
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

TIERS = ("light", "standard", "deep")
HIGH_RISK_TAGS = frozenset({"migration", "security", "cross_module"})
DEEP_FILE_COUNT = 8
DEEP_MODULE_COUNT = 4  # "more than 3 modules"
CACHE_NAME = "model_catalog.json"
CACHE_TTL_SECONDS = 24 * 3600

_EFFORT = {"light": "low", "standard": "medium", "deep": "high"}
# What WorkerProfile.reasoning carries (see model_router.provider_thinking_for).
_PROFILE_REASONING = {"light": "light", "standard": "normal", "deep": "deep"}
_CLAUDE_ALIASES = {"light": "haiku", "standard": "sonnet", "deep": "opus"}
_DOC_SUFFIXES = (".md", ".rst", ".txt")


class TierError(ValueError):
    """A tier or tier adjustment that is not allowed."""


@dataclass(frozen=True)
class ModelChoice:
    platform: str
    tier: str
    model: str
    effort: Optional[str]
    status: str  # "ok" | "unknown"
    source: str
    note: str = ""

    @property
    def reasoning(self) -> str:
        return _PROFILE_REASONING[self.tier]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "tier": self.tier,
            "model": self.model,
            "effort": self.effort,
            "status": self.status,
            "source": self.source,
            "note": self.note,
        }


def _check_tier(tier: Any) -> str:
    if not isinstance(tier, str) or tier not in TIERS:
        raise TierError("unknown tier: %r (expected one of %s)" % (tier, ", ".join(TIERS)))
    return tier


def max_tier(*tiers: str) -> str:
    return max((_check_tier(item) for item in tiers), key=TIERS.index)


def _files(contract: Mapping[str, Any]) -> List[str]:
    value = contract.get("files")
    if not value:
        profile = contract.get("worker_profile")
        value = profile.get("allowlist") if isinstance(profile, dict) else None
    return [item for item in (value or []) if isinstance(item, str) and item.strip() and item != "."]


def _is_doc(path: str) -> bool:
    posix = PurePosixPath(path.replace("\\", "/"))
    return posix.parts[:1] == ("docs",) or posix.suffix.lower() in _DOC_SUFFIXES


def _module(path: str) -> str:
    parts = PurePosixPath(path.replace("\\", "/")).parts
    return parts[0] if len(parts) > 1 else "."


def derive_tier(contract: Mapping[str, Any], *, role: str = "developer", integration: bool = False) -> str:
    """The tier a node contract implies (reviewer: at least ``standard``)."""
    tags = {str(tag) for tag in (contract.get("risk_tags") or [])}
    files = _files(contract)
    if integration or "integration" in tags:
        tier = "deep"
    elif tags & HIGH_RISK_TAGS:
        tier = "deep"
    elif len(files) >= DEEP_FILE_COUNT or len({_module(item) for item in files}) >= DEEP_MODULE_COUNT:
        tier = "deep"
    elif files and all(_is_doc(item) for item in files):
        tier = "light"
    else:
        tier = "standard"
    if role == "reviewer":
        tier = max_tier(tier, "standard")
    return tier


def node_tier(node: Any, *, role: str = "developer") -> str:
    """The tier of a DAGNode (integration review nodes are always ``deep``)."""
    from .dag import is_integration_review_node

    return derive_tier(
        node.contract or {}, role=role, integration=is_integration_review_node(node)
    )


def adjust_tier(tier: str, delta: int, reason: str, *, minimum: str = "light") -> str:
    """Move ``tier`` by ``delta`` levels; out-of-range or reason-less moves are refused."""
    _check_tier(tier)
    if not isinstance(delta, int) or isinstance(delta, bool) or delta not in (-1, 1):
        raise TierError("a tier adjustment is exactly +1 or -1, got %r" % (delta,))
    if not isinstance(reason, str) or not reason.strip():
        raise TierError("a tier adjustment needs a reason")
    index = TIERS.index(tier) + delta
    if index < TIERS.index(_check_tier(minimum)) or index >= len(TIERS):
        raise TierError(
            "cannot move tier %s by %+d: allowed range is %s..%s"
            % (tier, delta, minimum, TIERS[-1])
        )
    return TIERS[index]


# -- platform probes -----------------------------------------------------------
def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def probe_codex_models() -> List[Dict[str, Any]]:
    """Locally available Codex models, best first: ``[{slug, efforts, default}]``."""
    for name in ("models_cache.json", "models.json"):
        path = _codex_home() / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        models = data.get("models") if isinstance(data, dict) else None
        found = []
        for item in models if isinstance(models, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("slug"), str) or not item["slug"].strip():
                continue
            if item.get("visibility", "list") != "list":
                continue
            efforts = [
                level.get("effort")
                for level in item.get("supported_reasoning_levels") or []
                if isinstance(level, dict) and isinstance(level.get("effort"), str)
            ]
            priority = item.get("priority")
            found.append(
                {
                    "slug": item["slug"],
                    "efforts": efforts,
                    "default": item.get("default_reasoning_level"),
                    "priority": priority if isinstance(priority, (int, float)) and not isinstance(priority, bool) else 1_000,
                }
            )
        if found:
            found.sort(key=lambda entry: (entry["priority"], entry["slug"]))
            return [{k: v for k, v in entry.items() if k != "priority"} for entry in found]
    raise OSError("no Codex model list found under %s" % _codex_home())


_PROBES: Dict[str, Callable[[], List[Dict[str, Any]]]] = {"codex": probe_codex_models}


def _platform_key(platform: str) -> str:
    text = (platform or "").strip().lower()
    if text.startswith("claude"):
        return "claude-code"
    if text.startswith("codex"):
        return "codex"
    return text


def _read_cache(home: Optional[Path], key: str, now: float) -> Optional[List[Dict[str, Any]]]:
    if home is None:
        return None
    try:
        data = json.loads((home / CACHE_NAME).read_text(encoding="utf-8"))
        entry = data[key]
        if now - float(entry["probed_at"]) <= CACHE_TTL_SECONDS and entry["models"]:
            return entry["models"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return None


def _write_cache(home: Optional[Path], key: str, models: List[Dict[str, Any]], now: float) -> None:
    if home is None:
        return
    try:
        path = home / CACHE_NAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        data[key] = {"probed_at": now, "models": models}
        home.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp%d" % os.getpid())
        tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # a cache that cannot be written only costs a re-probe


def _pick(models: Sequence[Dict[str, Any]], tier: str) -> Dict[str, Any]:
    if tier == "deep":
        return models[0]
    if tier == "light":
        return models[-1]
    return models[len(models) // 2]


def _pick_effort(model: Mapping[str, Any], tier: str) -> Optional[str]:
    wanted = _EFFORT[tier]
    efforts = model.get("efforts") or []
    if not efforts or wanted in efforts:
        return wanted
    default = model.get("default")
    return default if isinstance(default, str) and default else None


def resolve(
    platform: str,
    tier: str,
    *,
    vibe_home: Optional[Path] = None,
    prober: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    now: Optional[float] = None,
) -> ModelChoice:
    """The model and thinking effort ``platform`` should use for ``tier``."""
    _check_tier(tier)
    key = _platform_key(platform)
    effort = _EFFORT[tier]
    if key == "claude-code":
        # Aliases follow the newest model of each family; nothing to probe.
        return ModelChoice(key, tier, _CLAUDE_ALIASES[tier], effort, "ok", "alias")
    probe = prober or _PROBES.get(key)
    if probe is None:
        return ModelChoice(key, tier, "default", None, "unknown", "none", "no model probe for this platform")
    stamp = time.time() if now is None else now
    home = Path(vibe_home) if vibe_home is not None else None
    models = _read_cache(home, key, stamp)
    source = "cache"
    if models is None:
        source = "probe"
        try:
            models = probe()
        except Exception as error:  # noqa: BLE001 - any probe failure is "unknown", never "unavailable"
            return ModelChoice(
                key, tier, "default", None, "unknown", "probe",
                "model probe failed (%s); using platform default" % type(error).__name__,
            )
        if not models:
            return ModelChoice(key, tier, "default", None, "unknown", "probe", "model probe returned nothing; using platform default")
        _write_cache(home, key, models, stamp)
    chosen = _pick(models, tier)
    return ModelChoice(key, tier, chosen["slug"], _pick_effort(chosen, tier), "ok", source)


def vibe_home_path() -> Optional[Path]:
    """``$VIBE_HOME`` (or its default) when it is a usable absolute path."""
    configured = os.environ.get("VIBE_HOME") or str(Path.home() / ".vibe-guide")
    path = Path(configured)
    return path if path.is_absolute() else None
