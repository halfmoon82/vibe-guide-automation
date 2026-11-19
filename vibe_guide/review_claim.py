"""PR 审阅占位读取（与控制面 multiagent-autoloop 的 ``src/review-claim.mjs`` 同一格式与判定）。

占位写在 PR 评论里：``<!-- review-claim:v1 {"holder":..,"head":..,"state":"active|released"} -->``。
按 GitHub 服务器时间重放：同一持有者的连续 active 记录在有效期内续期（保留起点），
过期后再占重新计起点；released 清除；最早开始且未过期的持有者即当前持有者。
本模块只读，不写占位；格式变更须同步控制面并更新 tests/test_delivery_review_claim.py 的契约用例。
"""
import json
import re
from datetime import datetime
from typing import Any, Iterable, Optional

DEFAULT_TTL_SECONDS = 3 * 3600
_MARK = re.compile(r"<!-- review-claim:v1 (\{[^\n]*?\}) -->")
_HOLDER = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def _parse_time(value: Any) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def active_claim_holder(comments: Iterable[Any], now: datetime, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Optional[str]:
    rows = []
    for comment in comments or []:
        if not isinstance(comment, dict):
            continue
        match = _MARK.search(str(comment.get("body") or ""))
        at = _parse_time(comment.get("createdAt"))
        if not match or at is None:
            continue
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        holder = data.get("holder") if isinstance(data, dict) else None
        if isinstance(holder, str) and _HOLDER.match(holder):
            rows.append((at, holder, data.get("state")))
    holders = {}
    for at, holder, state in sorted(rows, key=lambda row: row[0]):
        if state == "active":
            prev = holders.get(holder)
            since = prev["since"] if prev and at - prev["last"] < ttl_seconds else at
            holders[holder] = {"since": since, "last": at}
        else:
            holders.pop(holder, None)
    current = now.timestamp()
    live = sorted(
        ((info["since"], holder) for holder, info in holders.items() if current - info["last"] < ttl_seconds),
    )
    return live[0][1] if live else None
