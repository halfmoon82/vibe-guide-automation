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
CAUSE_UNCOMMITTED = "uncommitted"
_CAUSE_TEXT = {
    CAUSE_HELD: "已被其他未完成节点的写范围占用",
    CAUSE_NOT_EXPANDABLE: "不在 tests/ 或 auto_scope_paths 内，也不满足项目已批准的验收不变规则",
    CAUSE_UNCOMMITTED: "只在工作区或暂存区，没有 commit",
}
_CLAIM_REASON = "developer delivery landed outside the node files; project approved the acceptance-unchanged rule"
REJECTION_KEY = "scope_landing_rejection"
#: A refused delivery still arrived: the worker finished its turn, exactly the
#: binding state an ordinary delivery awaiting rework carries.  No stop is
#: claimed for a session vibe never stopped.
_DELIVERED = "delivered"

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


def landed_files(git: GitRunner, base: str, head: str, merged_in: List[str]) -> List[str]:
    """Files ``base..head`` changed, minus content merged in unchanged.

    ``merged_in`` are revisions the node branch may have merged after its
    base: accepted upstream branches and the main commit it last took in.
    A file one of them changed, that the node left exactly as merged, is not
    the node's own landing.
    """
    changed = _names(git, [base, head])
    for revision in merged_in:
        resolved = git(["rev-parse", "--verify", "--quiet", "%s^{commit}" % revision])
        tip = (resolved.stdout or "").strip()
        if resolved.returncode != 0 or not tip:
            continue
        if git(["merge-base", "--is-ancestor", tip, base]).returncode == 0:
            continue
        from_merged = set(_names(git, [base, tip]))
        candidates = [name for name in changed if name in from_merged]
        if not candidates:
            continue
        differs = set(_names(git, [tip, head, "--"] + candidates))
        changed = [name for name in changed if name not in from_merged or name in differs]
    return sorted(changed)


def _git_in(directory: Path) -> GitRunner:
    def run(args: List[str]) -> Any:
        return subprocess.run(["git", "-C", str(directory)] + list(args),
                              capture_output=True, text=True, timeout=60, check=False)
    return run


def _project_git_available(monitor: Any) -> bool:
    """True when the project root is a readable git checkout.

    Scope landing is a git discipline: in a project that is not a git
    checkout (fake providers, fixtures, guide mode) there is no base..HEAD
    claim to enforce, so the compatibility paths stay open.
    """
    try:
        resolved = _git_in(monitor.paths.root)(["rev-parse", "--verify", "--quiet", "HEAD"])
    except (OSError, subprocess.SubprocessError) as error:
        raise ScopeLandingUnknown("the project Git state is unreadable") from error
    if resolved.returncode == 0 and bool((resolved.stdout or "").strip()):
        return True
    # A non-checkout is a legitimate fake/fixture compatibility path.  A
    # checkout whose Git metadata cannot be read is not: treating the two
    # alike would bypass both landing gates exactly when verification is
    # unavailable (permissions, safe.directory, corruption, unsafe HEAD).
    stderr = str(resolved.stderr or "").casefold()
    if not (monitor.paths.root / ".git").exists() and "not a git repository" in stderr:
        return False
    raise ScopeLandingUnknown("the project Git state is unreadable")


def _project_head(monitor: Any) -> str:
    project = _git_in(monitor.paths.root)(["rev-parse", "HEAD"])
    if project.returncode != 0 or not project.stdout.strip():
        raise ScopeLandingUnknown("the project HEAD is unreadable, so the node baseline is unknown")
    return project.stdout.strip()


def _resolve_base(monitor: Any, current: Dict[str, Any], git: GitRunner, project_head: str) -> str:
    recorded = current.get("worktree_base")
    if isinstance(recorded, str) and recorded.strip():
        if git(["rev-parse", "--verify", "--quiet", recorded + "^{commit}"]).returncode == 0:
            return recorded
    merged = git(["merge-base", "HEAD", project_head])
    if merged.returncode != 0 or not merged.stdout.strip():
        raise ScopeLandingUnknown("the node branch shares no history with the project HEAD")
    return merged.stdout.strip()


def uncommitted_files(git: GitRunner) -> List[str]:
    """Tracked or staged paths whose changes are not committed on the node branch.

    Such a delivery never reaches the composed baseline of the nodes that
    depend on it.  Untracked files are left alone: a worker's delivery marker
    file is legitimately untracked, so they cannot be told apart from lost work.
    """
    result = git(["status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=no"])
    if result.returncode != 0:
        raise ScopeLandingUnknown("git status failed: %s" % (result.stderr or "").strip()[-200:])
    return sorted({entry[3:] for entry in (result.stdout or "").split("\0") if len(entry) > 3})


def _glob_match(path: List[str], pattern: List[str]) -> bool:
    """Segment-wise glob: ``*`` stays inside one directory, ``**`` spans any."""
    if not pattern:
        return not path
    if pattern[0] == "**":
        return any(_glob_match(path[index:], pattern[1:]) for index in range(len(path) + 1))
    return bool(path) and fnmatch.fnmatchcase(path[0], pattern[0]) \
        and _glob_match(path[1:], pattern[1:])


def _inside(path: str, entries: List[str]) -> bool:
    key = _scope_compare_key(path)
    for entry in entries:
        if any(char in entry for char in "*?["):
            if _glob_match(path.split("/"), entry.strip("/").split("/")):
                return True
            continue
        scope = _scope_compare_key(entry)
        if scope == "." or key == scope or key.startswith(scope + "/"):
            return True
    return False


def landing_report(monitor: Any, snapshot: Any, node_id: str) -> Optional[Dict[str, Any]]:
    """``{"files", "expanded", "rejected", "base"}``, or None without a node worktree.

    A node whose worktree is not a git worktree root of its own (fake
    providers, guide mode) has no branch claim to check -- but only when
    there is no claim at all: no worktree configured, no monitor project
    paths, or the directory does not exist yet.  A configured worktree that
    *exists* without a ``.git`` (a plain directory) claimed to be an
    independent git worktree and cannot vouch for anything: per #336 that is
    "cannot confirm the landing", never "nothing landed", so it raises
    ``ScopeLandingUnknown`` and the caller's fail-closed path parks the node.
    """
    from .worktree_base import upstream_branches

    current = snapshot.nodes[node_id]
    raw_worktree = str(current.get("worktree") or "")
    if not raw_worktree.strip() or not hasattr(monitor, "paths"):
        return None
    configured_absolute = Path(raw_worktree).is_absolute()
    worktree = Path(raw_worktree)
    if not worktree.is_absolute():
        worktree = monitor.paths.root / worktree
    if worktree == Path(monitor.paths.root).resolve() or worktree == monitor.paths.root:
        return None  # the node writes the main tree by contract (worker-deliver does the same)
    if not (worktree / ".git").exists():
        if worktree.exists():
            # An existing directory that claims to be a node worktree but has
            # no .git can vouch for nothing -- "cannot confirm the landing"
            # is never "nothing landed".  An absolute claim is foreign by
            # definition (vibe node worktrees are always repo-relative, see
            # AGENTS.md §10); a repo-relative claim fails closed inside a
            # real git project, while a non-git project is a fake/fixture
            # with no landing discipline to enforce (IR-5 compatibility).
            if configured_absolute or _project_git_available(monitor):
                raise ScopeLandingUnknown(
                    "worktree {} exists but is not an independent git worktree "
                    "(no .git)".format(worktree)
                )
            return None
        # Missing directory: "cannot confirm the landing" is never "nothing
        # landed" (#336).  Branch existence is never a pass condition (IR-6,
        # integration review g9): inside a git project the landing
        # discipline exists, so a configured writer worktree that is gone at
        # delivery time -- with or without its branch -- fails closed
        # through the caller's park path.  Outside a git project (fake
        # providers, fixtures, the launcher has not built it yet) there is
        # no claim to check and the compatibility path stays open.
        if _project_git_available(monitor):
            raise ScopeLandingUnknown(
                "worktree {} is missing at delivery time; the landing "
                "cannot be confirmed".format(worktree)
            )
        return None
    git = _git_in(worktree)
    project_head = _project_head(monitor)
    base = _resolve_base(monitor, current, git, project_head)
    merged_in = ["refs/heads/" + branch
                 for branch in upstream_branches(monitor.nodes, snapshot.nodes, node_id)]
    # The main commit the branch last took in (a merge of or rebase onto a
    # newer main); its content is main's, not the node's.
    taken = git(["merge-base", "HEAD", project_head])
    if taken.returncode == 0 and taken.stdout.strip():
        merged_in.append(taken.stdout.strip())
    files = landed_files(git, base, "HEAD", merged_in)
    uncommitted = uncommitted_files(git)
    node = monitor.nodes[node_id]
    scope = list(_write_scope_paths(node) or []) + list(monitor._node_scope_files(node_id, current))
    occupied = [_scope_compare_key(item)
                for item in monitor._files_held_by_other_active_nodes(snapshot, node_id)]
    auto_paths = monitor._auto_scope_paths()
    approved = monitor._acceptance_rule_approved()
    expanded: List[Dict[str, str]] = []
    rejected: List[Dict[str, str]] = [
        {"path": path, "cause": CAUSE_UNCOMMITTED} for path in uncommitted
    ]
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
    def listed(items: List[Dict[str, str]]) -> str:
        return "；".join("{}（{}）".format(item["path"], _CAUSE_TEXT.get(item["cause"], item["cause"]))
                        for item in items)

    parts = []
    loose = [item for item in rejected if item["cause"] == CAUSE_UNCOMMITTED]
    outside = [item for item in rejected if item["cause"] != CAUSE_UNCOMMITTED]
    if loose:
        parts.append("以下改动没有 commit 到节点分支，不会进入依赖它的节点和整合基线：{}。"
                     "请 git add 并 git commit 后重新交付。".format(listed(loose)))
    if outside:
        parts.append("以下文件超出本节点的授权文件范围，且不能自动扩范围：{}。"
                     "请撤回这些改动后重新交付；确需改动时由监工按授权流程处理。".format(listed(outside)))
    return "交付被拒收：" + "".join(parts)


#: Fixed classification of an unconfirmable-landing block (#336 / IR-6):
#: the delivery gate could not read where the work landed, so the node is
#: blocked_unknown with this structural code -- never a silent pass, and not
#: the supervisor-question park used for rejected file lists.
LANDING_UNREADABLE_CLASS = "landing_unreadable"
LANDING_UNREADABLE_CODE = "scope_landing:landing_unreadable"


def _park_landing_unreadable(monitor: Any, snapshot: Any, node_id: str,
                             active: Dict[str, Any], error: Any,
                             event_data: Dict[str, Any]) -> None:
    """Park the node blocked_unknown: the delivery landing cannot be confirmed.

    Distinct from the rejected-file-list park (``park`` with
    ``KIND_SCOPE_LANDING``), which leaves a worker question a supervisor can
    answer: here there is nothing to answer until the worktree state is
    repaired, so #336 records blocked_unknown plus a structural code.
    Everything stamped is a program constant; the worker's free text never
    reaches the record.  ``worker_questions`` is deliberately not involved.
    """
    current = snapshot.nodes[node_id]
    reason = "delivery landing cannot be confirmed ({})".format(str(error)[:200])
    current["status"] = "blocked_unknown"
    current["reason"] = reason
    current["reason_class"] = LANDING_UNREADABLE_CLASS
    current["reason_code"] = LANDING_UNREADABLE_CODE
    current.pop("recovery_action", None)
    current.pop("error_type", None)
    current["retryable_action"] = None
    current["active_role"] = None
    current["active_task"] = None
    snapshot.handles.pop(node_id, None)
    current["quarantine"] = {
        "run_id": snapshot.run_id,
        "handle_id": active.get("handle_id") if isinstance(active, dict) else None,
        "reason": reason,
    }
    monitor._release_node_lease(snapshot, node_id)
    if isinstance(active, dict) and active.get("role") in {"developer", "reviewer"}:
        try:
            monitor._set_binding_status(snapshot, node_id, str(active["role"]), "blocked_unknown")
        except (FileNotFoundError, OSError, TypeError, ValueError):
            pass
    monitor._record(snapshot, "scope_landing_rejected", event_data, active)


def check_delivery(monitor: Any, snapshot: Any, node_id: str, active: Dict[str, Any]) -> bool:
    """True when the developer delivery may proceed; False after refusing it."""
    try:
        report = landing_report(monitor, snapshot, node_id)
    except (ScopeLandingUnknown, OSError, subprocess.SubprocessError) as error:
        snapshot.nodes[node_id][REJECTION_KEY] = [{"path": "*", "cause": CAUSE_UNREADABLE}]
        _park_landing_unreadable(
            monitor, snapshot, node_id, active, error,
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "role": active.get("role") if isinstance(active, dict) else None,
                "generation": active.get("generation") if isinstance(active, dict) else None,
                "handle_id": active.get("handle_id") if isinstance(active, dict) else None,
                "reason_class": LANDING_UNREADABLE_CLASS,
                "reason_code": LANDING_UNREADABLE_CODE,
                "rejection_code": LANDING_UNREADABLE_CODE,
            },
        )
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
             },
             binding_status=_DELIVERED)
        return False
    snapshot.nodes[node_id].pop(REJECTION_KEY, None)
    if report["expanded"]:
        monitor._apply_scope_expansion(
            snapshot, node_id,
            ScopeExpansionResolution([item["path"] for item in report["expanded"]],
                                     list(report["expanded"])),
            "delivery_landing", active=active)
    return True


#: Structural code of the empty-landing refusal (#336): the node worktree is
#: an independent git worktree but ``base..HEAD`` (minus content merged in
#: unchanged) changed nothing.  A report without a landing is not a delivery;
#: the same session can commit real work and re-report.
EMPTY_LANDING_CODE = "empty_landing"
#: Event recorded when the contract opted in (``allow_empty_landing: true``)
#: and an empty diff is allowed through.
EMPTY_LANDING_ALLOWED_EVENT = "empty_landing_allowed"


def check_delivery_content(monitor: Any, snapshot: Any, node_id: str, active: Dict[str, Any],
                           runner: Any = None, handle_id: Optional[str] = None) -> bool:
    """True when the developer delivery actually landed something (#336).

    Runs after ``check_delivery`` accepted the file scope: the same landing
    report is reused, so a refusal here means the diff was readable and in
    scope but *empty*.  That is a format-class error, not a supervisor
    question: the worker is still live, so the refusal keeps the handle and
    binding (``acceptance_rejected`` with the ``empty_landing`` structural
    code) and the same session re-reports after a real commit.

    A node without a git-worktree landing (no worktree configured, or the
    guide/fake-provider modes where there is no branch claim to check) keeps
    today's pass-through: this gate only judges landings that can be read.
    When the contract carries ``allow_empty_landing: true`` an empty diff is
    allowed through and recorded as ``empty_landing_allowed``.
    """
    try:
        report = landing_report(monitor, snapshot, node_id)
    except (ScopeLandingUnknown, OSError, subprocess.SubprocessError) as error:
        # Unreadable diffs stay fail-closed: the node is parked
        # blocked_unknown by _park_landing_unreadable, so there is nothing
        # left to judge here.
        snapshot.nodes[node_id][REJECTION_KEY] = [{"path": "*", "cause": CAUSE_UNREADABLE}]
        _park_landing_unreadable(
            monitor, snapshot, node_id, active, error,
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "role": active.get("role") if isinstance(active, dict) else None,
                "generation": active.get("generation") if isinstance(active, dict) else None,
                "handle_id": active.get("handle_id") if isinstance(active, dict) else None,
                "reason_class": LANDING_UNREADABLE_CLASS,
                "reason_code": LANDING_UNREADABLE_CODE,
                "rejection_code": LANDING_UNREADABLE_CODE,
            },
        )
        return False
    if report is None or report["files"]:
        return True
    contract = getattr(getattr(monitor, "nodes", {}).get(node_id), "contract", None)
    if isinstance(contract, dict) and contract.get("allow_empty_landing") is True:
        monitor._record(
            snapshot,
            EMPTY_LANDING_ALLOWED_EVENT,
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "role": str(active.get("role")) if isinstance(active, dict) else None,
                "generation": active.get("generation") if isinstance(active, dict) else None,
                "reason": "contract allow_empty_landing: true; empty diff allowed",
            },
            active,
        )
        return True
    monitor._reject_acceptance_format(
        snapshot,
        node_id,
        active,
        "empty landing: the node worktree has no committed change since its base",
        EMPTY_LANDING_CODE,
        {"disposition": "acceptance_rejected",
         "finding": ["empty landing: base..HEAD changed nothing"]},
        runner=runner,
        handle_id=handle_id,
    )
    return False


def render_status_lines(snapshot: Any) -> str:
    lines = []
    for node_id, current in sorted((getattr(snapshot, "nodes", None) or {}).items()):
        expansions = current.get("scope_expansions") if isinstance(current, dict) else None
        if expansions:
            lines.append("节点 {} 已自动扩范围：{}".format(node_id, "、".join(expansions)))
    return "\n".join(lines)
