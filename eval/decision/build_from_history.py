#!/usr/bin/env python3
"""从本机真实会话历史构建入口路由评测集（issue #128）。

## expected 的定义（真实判定行）

`vibe-entry` 协议要求每次入口评分后输出一行可验证结果，形如
`S1：11→轻规划（不触碰 vibe）`（`protocols/vibe-entry.md` §2）。这行就是**生产
实际做的判定**，本脚本把它抽出来，与它被打分的那条请求配对。

不采用「拿请求文本重跑 `default_s1_context`」的口径：那条路径是**没有显式
`--s1` 时的兜底**，不是生产实际决策（生产传显式五维向量）。实测两种口径的
分布差别极大——兜底版 265 条里 243 条是 simple，评测无意义。

## 档位词表

用**代码实际档位名** `simple` / `light_plan` / `complex`，与
`planner.ROUTE_SIMPLE_MAX_SCORE`（8）/ `ROUTE_LIGHT_PLAN_MAX_SCORE`（15）
及协议 §2 的阈值表同一套。

## 标签校验

每行 S1 必须**自洽**：`<=8 → simple`、`9-15 → light_plan`、`>15 → complex`。
不自洽的行会被丢弃并计数——它们要么是文档里的示例被引用了，要么是判定本身有
问题，两种都不该进评测集。

## 数据不入库

仓库是公开的，而这些是真实用户请求（含项目名、issue 号、业务上下文）。
数据集默认写到仓库内路径但**必须保持未跟踪**（见 `.gitignore`）。

用法：
    PYTHONPATH=. python3 eval/decision/build_from_history.py            # 写 eval/decision/route.jsonl
    PYTHONPATH=. python3 eval/decision/build_from_history.py --dry-run   # 只看分布
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from pathlib import Path

#: 协议 §2 的判定行。只认这四种档位说法，避免把普通叙述里的"S1"当成判定。
_S1_LINE = re.compile(
    r"S1[：:]\s*(\d{1,2})\s*→\s*(直接执行|轻规划|vibe\s*正式路由|正式路由|进入复杂流程)"
)

#: 判定行里的档位说法 → 代码档位名。
_BAND_WORDS = (
    ("直接执行", "simple"),
    ("轻规划", "light_plan"),
    ("vibe 正式路由", "complex"),
    ("正式路由", "complex"),
    ("进入复杂流程", "complex"),
)

ROUTE_SIMPLE_MAX_SCORE = 8
ROUTE_LIGHT_PLAN_MAX_SCORE = 15

#: 机器生成、并非用户入口请求的消息形态。每条对应一个可验证的来源——用户请求
#: 和这些注入消息在记录里长得一样，只能按来源特征分，不能按"看起来像不像"分。
#: 「抽样后补」那几条是实际看到混进来的形态（各附一句它是什么），不是猜的。
_NOT_A_USER_REQUEST = tuple(re.compile(pattern) for pattern in (
    r"^Another Claude session sent a message:",
    r"^\[Subagent hand-back\]",
    r"^<agent-message",
    r"^<cross-session-message",
    r"^<user-prompt-submit-hook>",
    r"^Stop hook feedback:",
    r"^Base directory for this skill:",
    r"^This session is being continued from a previous conversation",
    r"^vibe 监工心跳",
    r"^监工巡检[：:]",
    r"^cd /",
    r"^env -u PYTHONPATH",
    r"权限探针",
    r"par-b\.txt",
    # --- 抽样后补：vibe 自己生成并回投的指令 ---
    r"^你是 vibe ",               # 接班监工提示词（运行/计划两种句式都命中）
    r"^接班监工[：:]",            # supervisor-handoff 的接班提示词
    r"^读取并逐字执行 ",           # 指向 .vibe/runs/... 的执行指令
    r"^vibe [a-z-]+ --",         # 直接粘进对话的 vibe 子命令（心跳/派发）
    # --- 抽样后补：宿主注入的通知 ---
    r"^\[Cross-session",
    r"^\[vibe",
    r"^\[Agent",
))

#: 判据冻结在这一行：issue #128 要求「问法逐字定稿后改动必须重新评测」。
CANONICAL_QUESTION = {
    "key": "route",
    "type": "choice",
    "instructions": "这个请求应该走哪条处理路径？",
    "criteria": {
        "simple": "直接执行（S1 总分 <= 8，不触碰 vibe，不需要规划）",
        "light_plan": "轻规划（S1 总分 9-15，列出步骤要点后执行，不触碰 vibe）",
        "complex": "vibe 正式路由（S1 总分 > 15，或带强制升级标记，走完整规划流程）",
    },
}

#: 默认的会话记录根。取**全部项目**而不是只取 vibeguide：vibe 的入口路由器是
#: 仓库无关的通用库，别的项目里真实的入口评分同样是它的合法输入（实测不加宽
#: 只有 73 条，加宽后 92 条）。本机 Claude Code 把每个项目的历史放在这里。
DEFAULT_SOURCE = "~/.claude/projects/*/*.jsonl"


def band_for_score(score: int) -> str:
    if score <= ROUTE_SIMPLE_MAX_SCORE:
        return "simple"
    if score <= ROUTE_LIGHT_PLAN_MAX_SCORE:
        return "light_plan"
    return "complex"


def band_word(text: str) -> str:
    for word, band in _BAND_WORDS:
        if word in text:
            return band
    raise ValueError(text)


def _text_of(entry):
    """The assistant's own prose, never a tool result or tool input.

    Reading `vibe-entry.md` puts the protocol's own example lines into the
    transcript as a tool result; scanning those would mint labels out of
    documentation.  Only `text` blocks of an assistant message count.
    """
    if entry.get("type") != "assistant":
        return ""
    content = entry.get("message", {}).get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


def _user_text(entry):
    if entry.get("type") != "user":
        return ""
    content = entry.get("message", {}).get("content")
    if isinstance(content, list):
        content = " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return content if isinstance(content, str) else ""


def pairs(source_glob: str, min_chars: int):
    """Yield ``(request, score, band)`` from the real decisions in the records.

    Pairing rule: the nearest preceding user message.  The protocol has the
    agent print the line immediately after scoring, so the message it scored is
    the last one before it.  Failure mode to keep in mind: a decision whose
    request never appears as a user message (a scheduled wake-up, a resumed
    session) pairs with whatever the user said last, which is wrong.  Such
    rows are indistinguishable from good ones downstream, so the count of
    decisions is reported separately from the count of usable pairs.
    """
    found, used, inconsistent, orphans = [], 0, 0, 0
    for path in sorted(glob.glob(os.path.expanduser(source_glob))):
        try:
            stream = open(path, errors="ignore")
        except OSError:
            continue
        last_request = ""
        with stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                text = _user_text(entry)
                if text:
                    candidate = " ".join(text.strip().split())
                    if (
                        len(candidate) >= min_chars
                        and not candidate.startswith("<")
                        and not any(p.search(candidate) for p in _NOT_A_USER_REQUEST)
                    ):
                        last_request = candidate
                    continue
                prose = _text_of(entry)
                if not prose:
                    continue
                for match in _S1_LINE.finditer(prose):
                    score, band = int(match.group(1)), band_word(match.group(2))
                    if band_for_score(score) != band:
                        inconsistent += 1
                        continue
                    if not last_request:
                        orphans += 1
                        continue
                    found.append((last_request, score, band))
                    used += 1
    return found, inconsistent, orphans


def build(source_glob: str, min_chars: int):
    found, inconsistent, orphans = pairs(source_glob, min_chars)
    seen, rows = set(), []
    for request, score, band in found:
        key = (request, band)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "id": "vr-%03d" % (len(rows) + 1),
            "state": request,
            "question": CANONICAL_QUESTION,
            "expected": band,
        })
    # The same request scored differently at two points in time leaves rows a
    # model cannot both get right, which caps accuracy for no reason the eval is
    # meant to measure.  Drop both sides of the conflict rather than keep the
    # first verdict by luck of ordering.
    labels = {}
    for row in rows:
        labels.setdefault(row["state"], set()).add(row["expected"])
    conflicting = {state for state, bands in labels.items() if len(bands) > 1}
    kept = [row for row in rows if row["state"] not in conflicting]
    for index, row in enumerate(kept, 1):
        row["id"] = "vr-%03d" % index
    return kept, {
        "decisions": len(found),
        "inconsistent": inconsistent,
        "orphans": orphans,
        "duplicate_pairs": len(found) - len(rows),
        "conflicting_labels": len(conflicting),
        "rows_dropped_conflict": len(rows) - len(kept),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--out", default="eval/decision/route.jsonl")
    parser.add_argument("--min-chars", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    rows, stats = build(args.source, args.min_chars)
    if not rows:
        print("没有可用的判定行：检查 --source 是否指向存在的会话记录", file=sys.stderr)
        return 2
    counts = {}
    for row in rows:
        counts[row["expected"]] = counts.get(row["expected"], 0) + 1
    print(json.dumps({
        "source": args.source,
        "rows": len(rows),
        "distribution": counts,
        **stats,
    }, ensure_ascii=False, indent=2))

    if args.dry_run:
        return 0
    out = Path(args.out)
    if out.exists() and not args.force:
        print("数据集已存在：%s（加 --force 覆盖；issue #128 要求问法冻结后不轻改）" % out, file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("已写入 %s（%d 条；请确认它未被 git 跟踪）" % (out, len(rows)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
