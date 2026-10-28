"""The baseline a downstream node's worktree starts from.

A node with ``depends_on`` builds on code its upstream nodes produced; cut
from main alone it cannot see any of it.  The baseline is main with every
accepted upstream branch merged in, in topological order.  The merges are
computed with ``git merge-tree --write-tree`` and recorded with
``git commit-tree``, so nothing touches a working directory: a conflict
leaves no half-built worktree behind and names the conflicting files.
"""

from typing import Any, Callable, Dict, List, Mapping

#: ``git(args) -> CommandResult`` (returncode/stdout/stderr) run in the repo.
GitRunner = Callable[[List[str]], Any]


class UpstreamMergeConflict(Exception):
    def __init__(self, branch: str, files: List[str]):
        super().__init__("upstream branch %s conflicts in: %s" % (branch, ", ".join(files)))
        self.branch = branch
        self.files = files


class UpstreamBaseUnavailable(Exception):
    """The composed baseline cannot be computed; the reason is readable."""


def upstream_branches(nodes: Mapping[str, Any], snapshot_nodes: Mapping[str, Dict[str, Any]],
                      node_id: str) -> List[str]:
    """Branches of the node's accepted ancestors, ancestors before descendants."""
    order: List[str] = []
    seen = set()

    def visit(current: str) -> None:
        node = nodes.get(current)
        for dependency in (getattr(node, "depends_on", None) or ()):
            if dependency in seen:
                continue
            seen.add(dependency)
            visit(dependency)
            order.append(dependency)

    visit(node_id)
    branches = []
    for dependency in order:
        state = snapshot_nodes.get(dependency) or {}
        branch = state.get("branch")
        if state.get("status") == "accepted" and isinstance(branch, str) and branch.strip() \
                and branch not in branches:
            branches.append(branch)
    return branches


def _tail(result: Any) -> str:
    return ((result.stderr or result.stdout or "").strip())[-200:]


def compose_base(git: GitRunner, base: str, branches: List[str]) -> str:
    """Merge each branch into ``base`` in order; returns the resulting commit."""
    for branch in branches:
        resolved = git(["rev-parse", "--verify", "--quiet", "refs/heads/%s^{commit}" % branch])
        tip = (resolved.stdout or "").strip()
        if resolved.returncode != 0 or not tip:
            raise UpstreamBaseUnavailable(
                "accepted upstream branch %s does not exist locally" % branch)
        contained = git(["merge-base", "--is-ancestor", tip, base])
        if contained.returncode == 0:
            continue
        if contained.returncode != 1:
            raise UpstreamBaseUnavailable(
                "cannot tell whether %s is already in the baseline: %s" % (branch, _tail(contained)))
        merged = git(["merge-tree", "--write-tree", "--name-only", base, tip])
        lines = (merged.stdout or "").splitlines()
        if merged.returncode == 1:
            files: List[str] = []
            for line in lines[1:]:
                if not line.strip():
                    break
                if line not in files:
                    files.append(line)
            raise UpstreamMergeConflict(branch, files)
        if merged.returncode != 0 or not lines:
            raise UpstreamBaseUnavailable(
                "git merge-tree for %s failed (exit %s): %s"
                % (branch, merged.returncode, _tail(merged)))
        committed = git(["commit-tree", lines[0].strip(), "-p", base, "-p", tip,
                         "-m", "vibe: merge accepted upstream %s into the node baseline" % branch])
        if committed.returncode != 0 or not (committed.stdout or "").strip():
            raise UpstreamBaseUnavailable(
                "git commit-tree for %s failed (exit %s): %s"
                % (branch, committed.returncode, _tail(committed)))
        base = committed.stdout.strip()
    return base
