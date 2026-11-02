"""The platform capability ledger: one shipped book instead of scattered tables.

``platform_ledger.json`` (shipped in the wheel and sdist) holds one row per
platform; every cell carries ``verified`` / ``pm_ruling`` / ``unverified`` and
its provenance (run id, date, host version).  The dispatch-topology matrix, the
worker wake channels, the rotation rule and the remote-Git enforcement wording
are all derived here and nowhere else.

A platform or cell the ledger lacks reads as ``unverified`` with the most
conservative value: dual-visible dispatch, no wake channel, no claimed
rotation rule, prompt-only push blocking.  A ledger that cannot be read at all
leaves the same conservative values in place so imports keep working, and
:func:`require_ledger` makes plan publication refuse with the reason.

A project's ``.vibe/push-wake-verification.json`` stays that project's own
measured record; the ledger only gives the platform default.
"""

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

LEDGER_PATH = Path(__file__).with_name("platform_ledger.json")
LEDGER_SCHEMA_VERSION = 1

#: Every supported platform; the adapter registry takes its id set from here.
PLATFORMS = (
    "codex", "claude-code", "cursor", "grok", "workbuddy", "kimi-code",
    "deepseek-harness",
)

STATUS_VERIFIED = "verified"
STATUS_PM_RULING = "pm_ruling"
STATUS_UNVERIFIED = "unverified"
STATUS_LABELS = {
    STATUS_VERIFIED: "已验证",
    STATUS_PM_RULING: "产品裁定",
    STATUS_UNVERIFIED: "未验证",
}
#: Statuses whose value code may act on; ``unverified`` values are display only
#: (except dispatch, whose value is itself a probe-gated rule).
_TRUSTED = (STATUS_VERIFIED, STATUS_PM_RULING)

#: The nine cells of a row, in card order, with their card labels.
FIELDS = (
    ("dispatch", "派发方式"),
    ("visibility", "用户能否看到与进入"),
    ("integration_review", "整合评审怎么派"),
    ("worker_wake", "worker 唤醒监工的通道"),
    ("supervisor_notification", "监工侧能收到的完成通知"),
    ("heartbeat", "心跳机制"),
    ("rotation", "能否换班"),
    ("nested_dispatch", "嵌套派发限制"),
    ("push_block", "推送拦截强度"),
)
_FIELD_NAMES = tuple(name for name, _label in FIELDS)

_TOPOLOGY_VALUES = ("in_session_sdd", "dual-visible")
#: Fail-closed dispatch row: UNKNOWN never yields ``in_session_sdd``.
CONSERVATIVE_TOPOLOGY = {"probe_pass": "dual-visible", "probe_unknown": "dual-visible"}
ROTATION_NEVER = "never"


class LedgerError(ValueError):
    """The shipped ledger cannot be read or has the wrong shape."""


def _unverified(note: str = "") -> Dict[str, Any]:
    cell = {"value": None, "status": STATUS_UNVERIFIED, "source": None}
    if note:
        cell["note"] = note
    return cell


def _valid_source(source: Any) -> bool:
    if not isinstance(source, dict):
        return False
    return all(isinstance(source.get(key), str) and source[key].strip() for key in ("date", "ref"))


def _normalize_cell(raw: Any) -> Dict[str, Any]:
    """One cell, or ``unverified`` when malformed: never guess a status."""
    if not isinstance(raw, dict) or "value" not in raw:
        return _unverified()
    status = raw.get("status")
    cell = dict(raw)
    if status not in STATUS_LABELS:
        cell["status"] = STATUS_UNVERIFIED
    elif status in _TRUSTED and not _valid_source(raw.get("source")):
        # A trusted claim without a dated source is not evidence.
        cell["status"] = STATUS_UNVERIFIED
    if cell["status"] == STATUS_UNVERIFIED and not _valid_source(cell.get("source")):
        cell["source"] = None
    return cell


class Ledger:
    """A loaded ledger; every lookup answers, missing cells read unverified."""

    def __init__(self, rows: Mapping[str, Mapping[str, Any]]):
        self._rows = {
            platform: {
                "display_name": (rows.get(platform) or {}).get("display_name") or platform,
                **{
                    name: _normalize_cell((rows.get(platform) or {}).get(name))
                    for name in _FIELD_NAMES
                },
            }
            for platform in PLATFORMS
        }

    def cell(self, platform: Optional[str], field: str) -> Dict[str, Any]:
        if field not in _FIELD_NAMES:
            raise KeyError("unknown ledger field: %s" % field)
        row = self._rows.get(platform or "")
        if row is None:
            return _unverified()
        return dict(row[field])

    def row(self, platform: Optional[str]) -> Dict[str, Any]:
        return {name: self.cell(platform, name) for name in _FIELD_NAMES}

    def display_name(self, platform: str) -> str:
        row = self._rows.get(platform)
        return row["display_name"] if row else platform


def load_ledger(path: Optional[Path] = None) -> Ledger:
    """Read and shape-check the ledger file; raise :class:`LedgerError` if unusable."""
    target = Path(path) if path is not None else LEDGER_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LedgerError("platform ledger unreadable ({}): {}".format(target.name, str(error)[:160]))
    if not isinstance(data, dict) or data.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise LedgerError("platform ledger has an unsupported schema_version")
    rows = data.get("platforms")
    if not isinstance(rows, dict) or not all(isinstance(row, dict) for row in rows.values()):
        raise LedgerError("platform ledger 'platforms' must map platform ids to rows")
    unknown = sorted(set(rows) - set(PLATFORMS))
    if unknown:
        raise LedgerError("platform ledger names unsupported platforms: %s" % ", ".join(unknown))
    return Ledger(rows)


try:
    LEDGER = load_ledger()
    LEDGER_ERROR = ""
except LedgerError as _error:
    # Keep imports alive on the conservative values; publication refuses.
    LEDGER = Ledger({})
    LEDGER_ERROR = str(_error)


def require_ledger() -> Ledger:
    """The ledger, or a refusal naming why it could not be read."""
    if LEDGER_ERROR:
        raise LedgerError("plan publication refused: " + LEDGER_ERROR)
    return LEDGER


def platform_for_provider(provider: Any) -> Optional[str]:
    """The ledger platform a provider spelling belongs to, or None.

    ``claude-code-background``, ``claude-bg`` and ``codex-app-visible`` all
    resolve to their platform; the wake channel and rotation rule are per
    platform, not per dispatch path.
    """
    value = str(provider or "").lower()
    if not value:
        return None
    if value.startswith("claude"):
        return "claude-code"
    for platform in sorted(PLATFORMS, key=len, reverse=True):
        if value.startswith(platform):
            return platform
    return None


# ----------------------------------------------------------------- derivations


def _topology_row(ledger: Ledger, platform: str) -> Dict[str, str]:
    value = ledger.cell(platform, "dispatch").get("value")
    if isinstance(value, dict) and all(value.get(key) in _TOPOLOGY_VALUES for key in CONSERVATIVE_TOPOLOGY):
        # The value is itself a probe-gated rule: ``probe_pass`` still needs the
        # platform's own probe evidence, so it applies whatever the status.
        return {key: value[key] for key in CONSERVATIVE_TOPOLOGY}
    return dict(CONSERVATIVE_TOPOLOGY)


def _wake_channel(ledger: Ledger, platform: str) -> Optional[str]:
    cell = ledger.cell(platform, "worker_wake")
    value = cell.get("value")
    if cell["status"] in _TRUSTED and isinstance(value, str) and value:
        return value
    return None


def _no_channel_reason(ledger: Ledger, platform: str) -> str:
    cell = ledger.cell(platform, "worker_wake")
    note = cell.get("note")
    if cell["status"] in _TRUSTED and isinstance(note, str) and note:
        return note
    if cell["status"] in _TRUSTED:
        return "{} 没有 worker 唤醒通道，等监工心跳拉取".format(ledger.display_name(platform))
    return "{} 的 worker 唤醒通道未验证，等监工心跳拉取".format(ledger.display_name(platform))


def _enforcement(ledger: Ledger, platform: str) -> Tuple[Dict[str, str], ...]:
    cell = ledger.cell(platform, "push_block")
    entries = []
    value = cell.get("value")
    if isinstance(value, list):
        for item in value:
            if (isinstance(item, dict) and all(isinstance(item.get(key), str) and item[key]
                                               for key in ("path", "level", "note"))):
                entries.append({"platform": item["path"], "level": item["level"], "note": item["note"]})
    if not entries:
        entries.append({"platform": platform, "level": "prompt_only",
                        "note": "未验证：仅提示词约束，未强制"})
    return tuple(entries)


#: Dispatch-topology ruling per platform (``probe_pass`` / ``probe_unknown``).
DISPATCH_TOPOLOGY_MATRIX = {platform: _topology_row(LEDGER, platform) for platform in PLATFORMS}
#: Worker -> supervisor push wake-up tool, only where the ledger trusts one.
WAKE_CHANNELS = {
    platform: channel for platform in PLATFORMS
    for channel in (_wake_channel(LEDGER, platform),) if channel
}
#: Why a platform has no wake channel; the heartbeat collects the delivery.
WAKE_NO_CHANNEL_REASON = {
    platform: _no_channel_reason(LEDGER, platform)
    for platform in PLATFORMS if platform not in WAKE_CHANNELS
}
#: Per-platform enforcement under ``deny``, shown on the card and in the README.
REMOTE_GIT_ENFORCEMENT = tuple(
    entry for platform in ("claude-code", "codex", "kimi-code", "workbuddy",
                           "cursor", "grok", "deepseek-harness")
    for entry in _enforcement(LEDGER, platform)
)


def never_rotates(provider: Any) -> bool:
    """True only when the ledger trusts a ``never`` rotation rule for the platform."""
    cell = LEDGER.cell(platform_for_provider(provider), "rotation")
    return cell["status"] in _TRUSTED and cell.get("value") == ROTATION_NEVER


def wake_channel_for(provider: Any) -> Tuple[Optional[str], str]:
    """``(channel, reason)`` for a provider spelling, read from the ledger."""
    platform = platform_for_provider(provider)
    if platform is None:
        return None, "平台 {} 没有已登记的推送通道，等监工心跳拉取".format(provider or "未知")
    channel = WAKE_CHANNELS.get(platform)
    if channel:
        return channel, ""
    return None, WAKE_NO_CHANNEL_REASON[platform]


def _display_value(value: Any) -> str:
    if value is None:
        return "未记录"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def card_capability_rows(platform: Optional[str]) -> Tuple[Dict[str, Any], ...]:
    """The card's view of one platform: every cell with its status label and source."""
    rows = []
    for name, label in FIELDS:
        cell = LEDGER.cell(platform, name)
        source = cell.get("source") or {}
        entry = {
            "field": name,
            "label": label,
            "value": _display_value(cell.get("value")),
            "status": cell["status"],
            "status_label": STATUS_LABELS[cell["status"]],
            "source": "；".join(
                part for part in (
                    source.get("run_id") or "",
                    source.get("date") or "",
                    source.get("host_version") or "",
                    source.get("ref") or "",
                ) if part
            ) or "无出处",
        }
        for extra in ("note", "condition"):
            if isinstance(cell.get(extra), str) and cell[extra]:
                entry[extra] = cell[extra]
        rows.append(entry)
    return tuple(rows)
