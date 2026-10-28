"""Where a developer delivery actually landed, checked against its file scope.

A node's scope is its allowlist/owned paths, its ``files`` and its earlier
auto-expansions.  At delivery, the files the node's branch changed since its
baseline (``base..HEAD``, minus content merged in unchanged from accepted
upstream branches) are compared with that scope.  A file outside it is pulled
in on the supervisor's own authority only under AGENTS.md §6 -- not held by
another unfinished node, and either the mechanical rule (``tests/`` or
``auto_scope_paths``) or the project-approved acceptance-unchanged rule --
and recorded as ``scope_auto_expanded``.  Anything else refuses the delivery
with each file and its cause, and the node waits for the supervisor.
"""

import fnmatch
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .dag import _write_paths_overlap, _write_scope_paths
from .planner import (
    ScopeExpansionResolution,
    _scope_compare_key,
    auto_scope_rule,
    resolve_scope_expansion,
)
from .worker_questions import KIND_SCOPE_LANDING, park

CAUSE_HELD = "held_by_other_node"
CAUSE_NOT_EXPANDABLE = "not_auto_expandable"
CAUSE_UNREADABLE = "landing_unreadable"
_CAUSE_TEXT = {
    CAUSE_HELD: "已被其他未完成节点的写范围占用",
    CAUSE_NOT_EXPANDABLE: "不在 tests/ 或 auto_scope_paths 内，也不满足项目已批准的验收不变规则",
}
_CLAIM_REASON = "developer delivery landed outside the node files; project approved the acceptance-unchanged rule"
REJECTION_KEY = "scope_landing_rejection"

#: ``git(args) -> CompletedProcess`` run in the node worktree.
GitRunner = Callable[[List[str]], Any]


class ScopeLandingUnknown(Exception):
    """The landed files cannot be read; never treated as "nothing landed"."""


def _names(git: GitRunner, args: List[str]) -> List[str]:
    result = git(["diff", "--name-only", "--no-renames", "-z"] + args)
    if result.returncode != 0:
        raise ScopeLandingUnknown("git diff %s failed: %s"
                                  % (" ".join(args[:2]), (result.stderr or "").strip()[-200:]))
    return [name for name in (result.stdout or "").split("\0") if name]


def landed_files(git: GitRunner, base: str, head: str, upstream: List[str]) -> List[str]:
    """Files ``base..head`` changed, minus upstream content merged in unchanged."""
    changed = _names(git, [base, head])
    for branch in upstream:
        resolved = git(["rev-parse", "--verify", "--quiet", "refs/heads/%s^{commit}" % branch])
        tip = (resolved.stdout or "").strip()
        if resolved.returncode != 0 or not tip:
            continue
        if git(["merge-base", "--is-ancestor", tip, base]).returncode == 0:
            continue
        from_upstream = set(_names(git, [base, tip]))
        candidates = [name for name in changed if name in from_upstream]
        if not candidates:
            continue
        differs = set(_names(git, [tip, head, "--"] + candidates))
        changed = [name for name in changed if name not in from_upstream or name in differs]
    return sorted(changed)


def _git_in(directory: Path) -> GitRunner:
    def run(args: List[str]) -> Any:
        return subprocess.run(["git", "-C", str(directory)] + list(args),
                              capture_output=True, text=True, timeout=60, check=False)
    return run


def _resolve_base(monitor: Any, current: Dict[str, Any], git: GitRunner) -> str:
    recorded = current.get("worktree_base")
    if isinstance(recorded, str) and recorded.strip():
        if git(["rev-parse", "--verify", "--quiet", recorded + "^{commit}"]).returncode == 0:
            return recorded
    project = _git_in(monitor.paths.root)(["rev-parse", "HEAD"])
    if project.returncode != 0 or not project.stdout.strip():
        raise ScopeLandingUnknown("the project HEAD is unreadable, so the node baseline is unknown")
    merged = git(["merge-base", "HEAD", project.stdout.strip()])
    if merged.returncode != 0 or not merged.stdout.strip():
        raise ScopeLandingUnknown("the node branch shares no history with the project HEAD")
    return merged.stdout.strip()


def _inside(path: str, entries: List[str]) -> bool:
    key = _scope_compare_key(path)
    for entry in entries:
        if any(char in entry for char in "*?["):
            if fnmatch.fnmatchcase(path, entry):
                return True
            continue
        scope = _scope_compare_key(entry)
        if scope == "." or key == scope or key.startswith(scope + "/"):
            return True
    return False


def landing_report(monitor: Any, snapshot: Any, node_id: str) -> Optional[Dict[str, Any]]:
    """``{"files", "expanded", "rejected", "base"}``, or None without a node worktree.

    A node whose worktree is not a git worktree root of its own (fake
    providers, guide mode) has no branch claim to check.
    """
    from .worktree_base import upstream_branches

    current = snapshot.nodes[node_id]
    worktree = Path(str(current.get("worktree") or ""))
    if not str(current.get("worktree") or "").strip():
        return None
    if not worktree.is_absolute():
        worktree = monitor.paths.root / worktree
    if not (worktree / ".git").exists():
        return None
    git = _git_in(worktree)
    base = _resolve_base(monitor, current, git)
    files = landed_files(git, base, "HEAD",
                         upstream_branches(monitor.nodes, snapshot.nodes, node_id))
    node = monitor.nodes[node_id]
    scope = list(_write_scope_paths(node) or []) + list(monitor._node_scope_files(node_id, current))
    occupied = [_scope_compare_key(item)
                for item in monitor._files_held_by_other_active_nodes(snapshot, node_id)]
    auto_paths = monitor._auto_scope_paths()
    approved = monitor._acceptance_rule_approved()
    expanded: List[Dict[str, str]] = []
    rejected: List[Dict[str, str]] = []
    for path in files:
        if _inside(path, scope):
            continue
        key = _scope_compare_key(path)
        if any(_write_paths_overlap(key, other) for other in occupied):
            rejected.append({"path": path, "cause": CAUSE_HELD})
            continue
        rule = auto_scope_rule(path, auto_paths)
        if rule is None and approved:
            claim = resolve_scope_expansion(
                {"files": [path], "acceptance_examples_unchanged": True,
                 "reason": _CLAIM_REASON, "adds_third_party_dependency": False,
                 "visibility_downgrade": False},
                authorized_files=(), node_files=(), auto_scope_paths=auto_paths,
                occupied_files=(), acceptance_rule_approved=True)
            if claim is not None and claim.scope_rules:
                rule = claim.scope_rules[0]["rule"]
        if rule is None:
            rejected.append({"path": path, "cause": CAUSE_NOT_EXPANDABLE})
        else:
            expanded.append({"path": path, "rule": rule})
    return {"files": files, "expanded": expanded, "rejected": rejected, "base": base}


def _rejection_text(rejected: List[Dict[str, str]], error: Optional[str] = None) -> str:
    if error:
        return ("交付被拒收：无法核对这次交付实际改了哪些文件（{}）。"
                "确认 worker 分支状态后再续派。".format(error))
    listed = "；".join("{}（{}）".format(item["path"], _CAUSE_TEXT.get(item["cause"], item["cause"]))
                      for item in rejected)
    return ("交付被拒收：以下文件超出本节点的授权文件范围，且不能自动扩范围：{}。"
            "请撤回这些改动后重新交付；确需改动时由监工按授权流程处理。".format(listed))


def check_delivery(monitor: Any, snapshot: Any, node_id: str, active: Dict[str, Any]) -> bool:
    """True when the developer delivery may proceed; False after refusing it."""
    try:
        report = landing_report(monitor, snapshot, node_id)
    except (ScopeLandingUnknown, OSError, subprocess.SubprocessError) as error:
        snapshot.nodes[node_id][REJECTION_KEY] = [{"path": "*", "cause": CAUSE_UNREADABLE}]
        park(monitor, snapshot, node_id, active, _rejection_text([], str(error)[:300]),
             kind=KIND_SCOPE_LANDING, event_name="scope_landing_rejected",
             event_data={"rejection_code": "scope_landing:" + CAUSE_UNREADABLE})
        return False
    if report is None:
        return True
    if report["rejected"]:
        snapshot.nodes[node_id][REJECTION_KEY] = list(report["rejected"])
        park(monitor, snapshot, node_id, active, _rejection_text(report["rejected"]),
             kind=KIND_SCOPE_LANDING, event_name="scope_landing_rejected",
             event_data={
                 "files": [item["path"] for item in report["rejected"]],
                 "rejection_code": "scope_landing:" + ",".join(
                     "{}={}".format(item["path"], item["cause"]) for item in report["rejected"]),
             })
        return False
    snapshot.nodes[node_id].pop(REJECTION_KEY, None)
    if report["expanded"]:
        monitor._apply_scope_expansion(
            snapshot, node_id,
            ScopeExpansionResolution([item["path"] for item in report["expanded"]],
                                     list(report["expanded"])),
            "delivery_landing", active=active)
    return True


def render_status_lines(snapshot: Any) -> str:
    lines = []
    for node_id, current in sorted((getattr(snapshot, "nodes", None) or {}).items()):
        expansions = current.get("scope_expansions") if isinstance(current, dict) else None
        if expansions:
            lines.append("节点 {} 已自动扩范围：{}".format(node_id, "、".join(expansions)))
    return "\n".join(lines)
