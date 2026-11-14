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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

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


class ModelMapConfigError(ValueError):
    """A user ``model_map`` entry that must be rejected as a whole.

    The message always names the offending key path (``model_map.codex.normal``)
    so the user can fix the configuration that was published.
    """


@dataclass(frozen=True)
class ModelChoice:
    platform: str
    tier: str
    model: str
    effort: Optional[str]
    status: str  # "ok" | "unknown"
    source: str
    note: str = ""
    fallbacks: Tuple[str, ...] = ()
    origin: str = "positional"  # "config" (model_map) | "positional" (probe/alias)

    @property
    def reasoning(self) -> str:
        return {"low": "light", "medium": "normal", "high": "deep"}.get(
            self.effort, _PROFILE_REASONING[self.tier]
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "tier": self.tier,
            "model": self.model,
            "effort": self.effort,
            "status": self.status,
            "source": self.source,
            "note": self.note,
            "fallbacks": list(self.fallbacks),
            "origin": self.origin,
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


normalize_platform = _platform_key


def _valid_models(models: Any) -> bool:
    return isinstance(models, list) and bool(models) and all(
        isinstance(item, dict)
        and isinstance(item.get("slug"), str) and bool(item["slug"].strip())
        and isinstance(item.get("efforts", []), list)
        and all(isinstance(level, str) and level for level in item.get("efforts", []))
        and (item.get("default") is None or (
            isinstance(item["default"], str)
            and item["default"] in _EFFORT.values()
            and (not item.get("efforts") or item["default"] in item["efforts"])
        ))
        for item in models
    )


def _read_cache(home: Optional[Path], key: str, now: float) -> Optional[List[Dict[str, Any]]]:
    if home is None:
        return None
    try:
        data = json.loads((home / CACHE_NAME).read_text(encoding="utf-8"))
        entry = data[key]
        if 0 <= now - float(entry["probed_at"]) <= CACHE_TTL_SECONDS and _valid_models(entry["models"]):
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


#: Platforms a ``model_map`` may name (normalized through ``_platform_key``).
_MODEL_MAP_PLATFORMS = frozenset({"codex", "claude-code"})


def load_model_map(vibe_home: Optional[Path]) -> Optional[Dict[str, Dict[str, List[str]]]]:
    """The validated ``model_map`` from ``$VIBE_HOME/config.json``.

    Shape: ``{platform: {tier: [primary, fallback, ...]}}`` -- the first list
    entry is the primary model, the rest its ordered fallback chain.  ``None``
    means unconfigured (no file or no ``model_map`` key), in which case
    :func:`resolve` keeps its positional behavior.  Any malformed entry --
    unknown platform, unknown tier, empty list, non-string member -- rejects
    the *whole* map and names the offending key path; a partially valid map
    never takes effect.
    """
    if vibe_home is None:
        return None
    path = Path(vibe_home) / "config.json"
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as error:
        raise ModelMapConfigError('model_map: config.json is not valid UTF-8') from error
    except OSError as error:
        raise ModelMapConfigError(
            "model_map: cannot read %s (%s)" % (path, type(error).__name__)
        ) from error
    try:
        data = json.loads(raw_text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelMapConfigError(
            "model_map: %s is not valid JSON (%s)" % (path, type(error).__name__)
        ) from error
    if not isinstance(data, dict):
        raise ModelMapConfigError("model_map: %s must contain a JSON object" % path)
    raw_map = data.get("model_map")
    if raw_map is None:
        return None
    if not isinstance(raw_map, dict):
        raise ModelMapConfigError("model_map must be an object mapping a platform to tiers")
    normalized: Dict[str, Dict[str, List[str]]] = {}
    for raw_platform, raw_tiers in raw_map.items():
        platform_path = "model_map.%s" % raw_platform
        key = _platform_key(raw_platform) if isinstance(raw_platform, str) else ""
        if key not in _MODEL_MAP_PLATFORMS:
            raise ModelMapConfigError(
                "%s: unknown platform %r (expected one of %s)"
                % (platform_path, raw_platform, ", ".join(sorted(_MODEL_MAP_PLATFORMS)))
            )
        if key in normalized:
            raise ModelMapConfigError("%s duplicates platform %r" % (platform_path, key))
        if not isinstance(raw_tiers, dict):
            raise ModelMapConfigError(
                "%s must be an object mapping a tier to a model list" % platform_path
            )
        tier_map: Dict[str, List[str]] = {}
        for raw_tier, raw_models in raw_tiers.items():
            tier_path = "%s.%s" % (platform_path, raw_tier)
            if raw_tier not in TIERS:
                raise ModelMapConfigError(
                    "%s: unknown tier %r (expected one of %s)"
                    % (tier_path, raw_tier, ", ".join(TIERS))
                )
            if not isinstance(raw_models, list) or not raw_models:
                raise ModelMapConfigError(
                    "%s must be a non-empty list of model names" % tier_path
                )
            models: List[str] = []
            for item in raw_models:
                if not isinstance(item, str) or not item.strip():
                    raise ModelMapConfigError(
                        "%s must contain only non-empty strings" % tier_path
                    )
                models.append(item)
            tier_map[raw_tier] = models
        normalized[key] = tier_map
    return normalized


def resolve(
    platform: str,
    tier: str,
    *,
    vibe_home: Optional[Path] = None,
    prober: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    now: Optional[float] = None,
    cache_write: bool = True,
) -> ModelChoice:
    """The model and thinking effort ``platform`` should use for ``tier``."""
    _check_tier(tier)
    key = _platform_key(platform)
    effort = _EFFORT[tier]
    home = Path(vibe_home) if vibe_home is not None else None
    model_map = load_model_map(home)
    if model_map is not None:
        tier_map = model_map.get(key)
        if tier_map is not None and tier in tier_map:
            entries = tier_map[tier]
            return ModelChoice(
                key, tier, entries[0], effort, "ok", "config", "",
                fallbacks=tuple(entries[1:]), origin="config",
            )
    if key == "claude-code":
        # Aliases follow the newest model of each family; nothing to probe.
        return ModelChoice(key, tier, _CLAUDE_ALIASES[tier], effort, "ok", "alias")
    probe = prober or _PROBES.get(key)
    if probe is None:
        return ModelChoice(key, tier, "default", None, "unknown", "none", "no model probe for this platform")
    stamp = time.time() if now is None else now
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
        if not _valid_models(models):
            return ModelChoice(key, tier, "default", None, "unknown", "probe", "model probe returned no valid directory; using platform default")
        if cache_write:
            _write_cache(home, key, models, stamp)
            # Dispatch binds source into its pending create identity. Once
            # persisted, use that durable observation from the first attempt;
            # retrying the same cached route must not change the identity.
            if _read_cache(home, key, stamp) == models:
                source = "cache"
    chosen = _pick(models, tier)
    selected_effort = _pick_effort(chosen, tier)
    if selected_effort not in _EFFORT.values():
        return ModelChoice(
            key, tier, "default", None, "unknown", source,
            "directory effort cannot be represented by the worker profile; using platform default",
        )
    return ModelChoice(key, tier, chosen["slug"], selected_effort, "ok", source)


def catalog_snapshot(
    platforms: Sequence[str] = ("codex", "claude-code"), *, vibe_home: Optional[Path] = None,
) -> Dict[str, Any]:
    """Read-only display of the same resolver used by dispatch.

    Each platform probe is observed once, including a failed observation, so
    tiers cannot describe different directory generations. Project tables are
    deliberately never read here and the user cache is never written.
    """
    load_model_map(vibe_home)  # Invalid manual input is not a missing directory.
    rows = []
    for platform in sorted({_platform_key(item) for item in platforms}):
        observed: Dict[str, Any] = {}
        probe = _PROBES.get(platform)

        def once():
            if "models" not in observed and "error" not in observed:
                try:
                    observed["models"] = probe()
                except Exception as error:
                    observed["error"] = error
            if "error" in observed:
                raise observed["error"]
            return observed["models"]

        for tier in TIERS:
            rows.append(resolve(
                platform, tier, vibe_home=vibe_home,
                prober=once if probe else None, cache_write=False,
            ).to_dict())
    return {
        "schema_version": 1,
        "purpose": "generated_display_only",
        "manual_input": "$VIBE_HOME/config.json:model_map",
        "resolutions": rows,
    }


def disclosure_notes(snapshot: Mapping[str, Any]) -> List[str]:
    """Only the unresolved platform/tier combinations use the default."""
    affected: Dict[str, List[str]] = {}
    for row in snapshot["resolutions"]:
        if row["status"] != "ok":
            affected.setdefault(row["platform"], []).append(row["tier"])
    return [
        "%s：%s 未验证，将使用平台默认模型；模型目录缺失本身不阻断授权。"
        % (platform, ", ".join(tiers))
        for platform, tiers in sorted(affected.items())
    ]


def vibe_home_path() -> Optional[Path]:
    """``$VIBE_HOME`` (or its default) when it is a usable absolute path."""
    configured = os.environ.get("VIBE_HOME") or str(Path.home() / ".vibe-guide")
    path = Path(configured)
    return path if path.is_absolute() else None
