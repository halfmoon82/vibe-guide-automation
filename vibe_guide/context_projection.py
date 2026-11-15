"""Role/node context projection: what the model sees, never what is evidence.

Pure helpers.  They shape the *model view* (bounded excerpts, pages, rework
packages, continuation deltas, budget reports) from material the caller already
holds.  They never decide authorization, never execute an action and never
delete evidence: originals stay where they are and every trimmed view carries
its size and sha256 so the source stays reachable.  When a projection cannot be
built reliably the fallback is *more* relevant material, never an empty result.
"""
from typing import Any, Callable, Dict, Iterable, List, Optional

from .digests import sha256_bytes

#: Soft budget of one continuation (design 7.4): reported, never a rejection.
CONTINUATION_SOFT_BUDGET_BYTES = 2048
RESOLVED_STATUSES = frozenset({"fixed", "resolved", "closed", "verified_fixed"})
#: Fields kept for an already-resolved finding: result and source only.
_RESOLVED_KEEP = ("id", "severity", "status", "source", "resolution")


def _fit(text: str, limit: int, *, from_end: bool = False) -> str:
    """Longest prefix (or suffix) of ``text`` within ``limit`` UTF-8 bytes."""
    if limit <= 0:
        return ""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    cut = data[-limit:] if from_end else data[:limit]
    return cut.decode("utf-8", "ignore")


def safe_excerpt(text: Any, limit_bytes: int) -> Dict[str, Any]:
    """Bound ``text`` to ``limit_bytes`` keeping both ends.

    The end usually holds the conclusion (a failure, a negation) so it is kept
    with the head; the cut lands on a character boundary, so Chinese text and
    the JSON built around it stay valid.  Size and sha256 of the whole text are
    returned so the full source can be located and checked.
    """
    if not isinstance(text, str):
        return {"text": "", "truncated": False, "total_bytes": 0, "sha256": sha256_bytes(b"")}
    total = len(text.encode("utf-8"))
    base = {"total_bytes": total, "sha256": sha256_bytes(text.encode("utf-8"))}
    if total <= limit_bytes:
        return {"text": text, "truncated": False, **base}
    marker = "…[已截断，全文见来源]…"
    room = limit_bytes - len(marker.encode("utf-8"))
    if room < 2:
        return {"text": _fit(text, limit_bytes), "truncated": True, **base}
    tail_room = room // 2
    head = _fit(text, room - tail_room)
    tail = _fit(text, tail_room, from_end=True)
    return {"text": head + marker + tail, "truncated": True, **base}


def page_text(text: Any, *, source: str, page_bytes: int) -> List[Dict[str, Any]]:
    """Split ``text`` into ordered pages of at most ``page_bytes`` UTF-8 bytes.

    Every page names its source and position; concatenating the pages restores
    the text exactly, so nothing is dropped and no character is split.
    """
    text = text if isinstance(text, str) else ""
    pieces: List[str] = []
    data = text.encode("utf-8")
    position = 0
    while position < len(data):
        end = min(position + max(page_bytes, 1), len(data))
        piece = data[position:end].decode("utf-8", "ignore")
        if not piece:  # page smaller than one character: take the character whole
            end = position + 1
            while end < len(data) and (data[end] & 0xC0) == 0x80:
                end += 1
            piece = data[position:end].decode("utf-8", "ignore")
        # Back up over a split character at the right edge of the page.
        elif end < len(data):
            consumed = len(piece.encode("utf-8"))
            end = position + consumed
        pieces.append(piece)
        position = end
    if not pieces:
        pieces = [""]
    return [{"source": source, "page": index + 1, "pages": len(pieces), "text": piece}
            for index, piece in enumerate(pieces)]


def project_findings(findings: Iterable[Any], *, resolved_ids: Iterable[str] = (),
                     per_item_bytes: int = 2048) -> Dict[str, Any]:
    """Rework package: open findings in full, resolved ones as result + source.

    A finding is resolved only on explicit evidence (a known status or an id in
    ``resolved_ids``); anything unclear is kept in full.  Oversized string
    fields are bounded per field, so the package stays valid JSON and the
    conclusion at the end of a long log survives.
    """
    resolved = set(resolved_ids or ())
    projected: List[Any] = []
    for item in findings or []:
        if not isinstance(item, dict):
            projected.append(item)
            continue
        if item.get("status") in RESOLVED_STATUSES or item.get("id") in resolved:
            kept = {key: item[key] for key in _RESOLVED_KEEP if key in item}
            kept.setdefault("status", "resolved")
            projected.append(kept)
            continue
        out: Dict[str, Any] = {}
        for key, value in item.items():
            if isinstance(value, str) and len(value.encode("utf-8")) > per_item_bytes:
                bounded = safe_excerpt(value, per_item_bytes)
                out[key] = bounded["text"]
                out[key + "_truncated"] = True
                out[key + "_sha256"] = bounded["sha256"]
                out[key + "_bytes"] = bounded["total_bytes"]
            else:
                out[key] = value
        projected.append(out)
    return {"findings": projected}


def delta_since(items: Iterable[Dict[str, Any]], *, delivered_ids: Any,
                id_key: str = "id") -> Dict[str, Any]:
    """Items not yet delivered to the worker; a lost watermark sends everything.

    ``delivered_ids`` is the delivery watermark.  Missing, malformed or naming
    nothing we hold means the watermark cannot be trusted: the full relevant
    set goes out again (a safe, minimal contract) and the reason is returned.
    Items without an id cannot be proven delivered, so they always go out.
    """
    items = list(items or [])
    if not isinstance(delivered_ids, (list, tuple, set, frozenset)):
        return {"items": items, "fallback": True, "reason": "watermark_missing"}
    known = {item.get(id_key) for item in items if isinstance(item, dict) and item.get(id_key) is not None}
    delivered = set(delivered_ids)
    if delivered and known and not (delivered & known):
        return {"items": items, "fallback": True, "reason": "watermark_unmatched"}
    fresh = [item for item in items
             if not (isinstance(item, dict) and item.get(id_key) in delivered)]
    return {"items": fresh, "fallback": False, "reason": None}


def project_or_full(project: Callable[[], Any], full: Callable[[], Any]) -> Dict[str, Any]:
    """Run ``project``; on failure return ``full()`` (more material), never empty."""
    try:
        return {"value": project(), "fallback": False, "reason": None}
    except Exception as error:  # noqa: BLE001 - any projection failure widens, never narrows
        return {"value": full(), "fallback": True,
                "reason": "{}: {}".format(type(error).__name__, str(error)[:120])}


def budget_report(sections: Dict[str, str], *,
                  soft_budget_bytes: int = CONTINUATION_SOFT_BUDGET_BYTES) -> Dict[str, Any]:
    """Size per source and total.  A soft budget only reports; it drops nothing."""
    sources = {name: len((text or "").encode("utf-8")) for name, text in sections.items()}
    total = sum(sources.values())
    return {"total_bytes": total, "sources": sources, "soft_budget_bytes": soft_budget_bytes,
            "over_budget": total > soft_budget_bytes, "soft": True}


def collapse_repeats(lines: Iterable[str]) -> List[str]:
    """Merge consecutive identical lines into ``line (xN)``; new lines stay."""
    out: List[str] = []
    run_line: Optional[str] = None
    count = 0

    def flush():
        if run_line is not None:
            out.append(run_line if count == 1 else "{} (x{})".format(run_line, count))

    for line in lines or []:
        if line == run_line:
            count += 1
            continue
        flush()
        run_line, count = line, 1
    flush()
    return out


def local_wait_policy(*, supported: Optional[bool], max_wait_seconds: int = 600,
                      max_polls: int = 5) -> Dict[str, Any]:
    """How a worker waits on a failing tool or long test.

    Only a publicly supported bounded local wait may report terminal state
    alone; unknown support counts as unsupported, and then the limit and the
    real cost must be disclosed instead of silently polling.
    """
    if supported is True:
        return {"mode": "bounded_local_wait", "max_wait_seconds": max_wait_seconds,
                "report_terminal_only": True, "disclose_cost": False}
    return {"mode": "limited_polling", "max_polls": max_polls,
            "report_terminal_only": False, "disclose_cost": True}


def compaction_effect(*, before_input_tokens: Any, after_input_tokens: Any,
                      boundary_before: Dict[str, Any], boundary_after: Dict[str, Any],
                      summary_bytes_before: Any = None,
                      summary_bytes_after: Any = None) -> Dict[str, Any]:
    """Judge a native compaction / handoff by the real next-request input.

    A smaller summary alone proves nothing.  Effective needs (a) measured input
    that actually dropped and (b) approval boundary, write permission and
    in-flight actions all still present.  Unmeasured usage is unknown, not ok.
    """
    lost = sorted(key for key, value in (boundary_before or {}).items()
                  if (boundary_after or {}).get(key) != value)
    if not all(type(value) is int for value in (before_input_tokens, after_input_tokens)):
        return {"effective": None, "reason": "usage_unknown", "lost": lost}
    delta = after_input_tokens - before_input_tokens
    result = {"input_delta_tokens": delta, "lost": lost}
    if lost:
        return {"effective": False, "reason": "boundary_lost", **result}
    if delta >= 0:
        return {"effective": False, "reason": "input_not_reduced", **result}
    return {"effective": True, "reason": None, **result}
