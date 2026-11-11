"""Remote integration delivery: the explicit last DAG ring (issue #371).

When a complex run's authorization card carries ``remote_git_actions: allow``
and the integration review is accepted, the monitor appends a
``remote-delivery`` node to the running snapshot as the final ring of the run
DAG.  The node is driven by the monitor itself through a small, resumable
step engine -- never dispatched to a worker:

1. ``prepare``    -- create/refresh the integration source branch from the
                     latest ``origin/main`` and merge every accepted business
                     node's branch into it (target branch is ``main``);
2. ``push``       -- push the source branch, proving the remote head SHA;
3. ``create_pr``  -- open the integration PR (reusing an existing open PR
                     for the same head instead of duplicating one);
4. ``review``     -- require the project's review rule on the PR: at least
                     one approving review and no pending changes-requested
                     (P0-P2 findings block, mirroring the merge gate);
5. ``merge``      -- squash-merge the PR into ``main`` (project rule);
6. ``verify``     -- read remote facts: ``origin/main`` must contain the
                     merge commit and the PR must read as merged.

Evidence is persisted on the snapshot (``remote_delivery_evidence``) and in
the run event log after every step.  A retry resumes from the persisted
evidence and never re-creates a PR or re-merges; every executor step is
individually idempotent.  The evidence is bound to the run's
authorization digest, node-contract digest, topology digest and the
integration-review digest, so a change to the aggregated content or to the
authorization invalidates the old evidence instead of reusing it.

Failure never reports success: a step failure parks the node
``blocked_unknown`` (retryable) or ``rework`` (review findings) and the run
cannot reach ``complete`` until the node is accepted with valid, current,
verified evidence.  Every step is individually idempotent -- merge included:
an already-merged PR with the recorded head reads back as a succeeded merge
step, so a crash between the remote side effect and the snapshot save can
never park the run on a merge that can never succeed.  If a reauthorization
revokes ``remote_git_actions`` the ring is retired (node and evidence
dropped, event recorded) instead of bricking the run.  Neither mode touches
deploy, launchd or production writes -- this engine only ever runs git/gh
commands.
"""
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .digests import canonical_json_digest
from .models import INTEGRATION_REVIEW_NODE_ID, REMOTE_DELIVERY_NODE_ID

REMOTE_DELIVERY_STEPS: Sequence[str] = (
    "prepare",
    "push",
    "create_pr",
    "review",
    "merge",
    "verify",
)

#: Events the delivery engine records.  All are system-provenance audit
#: records: the snapshot (node status + ``remote_delivery_evidence``) is the
#: source of truth, so event replay acknowledges them without re-applying.
REMOTE_DELIVERY_AUDIT_EVENTS = frozenset({
    "remote_delivery_node_appended",
    "remote_delivery_step_completed",
    "remote_delivery_failed",
    "remote_delivery_rework",
    "remote_delivery_evidence_invalidated",
    "remote_delivery_accepted",
    # The ring is retired when a reauthorization revokes
    # remote_git_actions; the node and its evidence are dropped.
    "remote_delivery_retired",
})

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_TARGET_BRANCH = "main"

_EVENT_DATA_WHITELIST = (
    # Run-event ``data`` keys the delivery engine reports with.  Mirrored in
    # state._EVENT_DATA_KEYS; structural identifiers only, never provider
    # free text.
    "step", "pr_url", "pr_number", "head_sha", "merge_commit",
    "source_branch", "target_branch", "remote_verification", "retryable",
    "delivery",
)


def _snapshot_value(snapshot: Any, key: str, default: Any = None) -> Any:
    if isinstance(snapshot, Mapping):
        return snapshot.get(key, default)
    return getattr(snapshot, key, default)


def remote_delivery_required(authorization: Any) -> bool:
    """True only when the run's authorization card allows remote Git actions."""
    if authorization is None:
        return False
    if isinstance(authorization, Mapping):
        switch = authorization.get("remote_git_actions")
    else:
        switch = getattr(authorization, "remote_git_actions", None)
    return switch == "allow"


def integration_evidence_digest(snapshot: Any) -> str:
    """Digest of the current (non-history) integration review evidence.

    Empty when no integration evidence exists yet; the delivery engine must
    not start in that state, and a changed digest invalidates delivery
    evidence so aggregated-content drift can never reuse old remote facts.
    """
    evidence = _snapshot_value(snapshot, "integration_review_evidence", {})
    if not isinstance(evidence, Mapping) or not evidence:
        return ""
    current = {key: value for key, value in evidence.items() if key != "history"}
    return canonical_json_digest(current)


def delivery_binding(snapshot: Any) -> Dict[str, str]:
    """The digest set delivery evidence is bound to."""
    return {
        "authorization_digest": str(_snapshot_value(snapshot, "authorization_digest", "") or ""),
        "node_contract_digest": str(_snapshot_value(snapshot, "node_contract_digest", "") or ""),
        "topology_digest": str(_snapshot_value(snapshot, "topology_digest", "") or ""),
        "integration_digest": integration_evidence_digest(snapshot),
    }


def binding_matches(stored: Any, current: Mapping[str, str]) -> bool:
    if not isinstance(stored, Mapping):
        return False
    return all(stored.get(key) == value for key, value in current.items())


def _check_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.match(value):
        raise ValueError("remote delivery {} must be a 40-character SHA".format(field))
    return value


def _require_hex64(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.match(value):
        raise ValueError("remote delivery {} must be a 64-character digest".format(field))
    return value


def validate_remote_delivery_evidence(snapshot: Any, evidence: Any) -> None:
    """Fail closed unless the delivery evidence is complete and internally sound."""
    if not isinstance(evidence, Mapping):
        raise ValueError("remote delivery evidence must be a mapping")
    if evidence.get("schema_version") != 1:
        raise ValueError("remote delivery evidence schema_version must be 1")
    run_id = _snapshot_value(snapshot, "run_id", "")
    if not isinstance(evidence.get("run_id"), str) or evidence["run_id"] != run_id:
        raise ValueError("remote delivery evidence run lineage is inconsistent")
    binding = evidence.get("binding")
    if not isinstance(binding, Mapping):
        raise ValueError("remote delivery evidence lacks a digest binding")
    _require_hex64(binding.get("authorization_digest"), "authorization_digest")
    _require_hex64(binding.get("node_contract_digest"), "node_contract_digest")
    if not isinstance(binding.get("topology_digest"), str):
        raise ValueError("remote delivery topology digest is invalid")
    _require_hex64(binding.get("integration_digest"), "integration_digest")
    source = evidence.get("source_branch")
    target = evidence.get("target_branch")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("remote delivery source branch is missing")
    if target != DEFAULT_TARGET_BRANCH:
        raise ValueError("remote delivery target branch must be main")
    steps = evidence.get("steps")
    if not isinstance(steps, Mapping):
        raise ValueError("remote delivery steps are missing")
    for step in REMOTE_DELIVERY_STEPS:
        record = steps.get(step)
        if not isinstance(record, Mapping) or record.get("status") != "succeeded":
            raise ValueError("remote delivery step {} did not succeed".format(step))
        if not isinstance(record.get("facts"), Mapping):
            raise ValueError("remote delivery step {} lacks facts".format(step))
    status = evidence.get("status")
    if status not in {"in_progress", "accepted", "rework", "blocked_unknown"}:
        raise ValueError("remote delivery evidence status is invalid")
    if status == "accepted":
        _check_sha(evidence.get("head_sha"), "head_sha")
        pr_url = evidence.get("pr_url")
        if not isinstance(pr_url, str) or not pr_url.startswith("http"):
            raise ValueError("remote delivery PR URL is missing")
        merge_commit = _check_sha(evidence.get("merge_commit"), "merge_commit")
        verification = evidence.get("remote_verification")
        if not isinstance(verification, Mapping) or verification.get("verified") is not True:
            raise ValueError("remote delivery lacks verified remote facts")
        _check_sha(verification.get("main_head_sha"), "main_head_sha")
        if verification.get("merge_commit") != merge_commit:
            raise ValueError("remote verification merge commit mismatch")
        if verification.get("pr_state") != "MERGED":
            raise ValueError("remote verification PR state is not merged")


def delivery_evidence_current(snapshot: Any, evidence: Any) -> bool:
    """Whether persisted evidence may be resumed: schema-valid and digest-bound."""
    try:
        validate_remote_delivery_evidence(snapshot, evidence)
    except (TypeError, ValueError):
        return False
    return binding_matches(evidence.get("binding"), delivery_binding(snapshot))


class RemoteDeliveryFailed(Exception):
    """One delivery step failed.

    ``retryable`` steps park the node ``blocked_unknown`` (a later tick
    retries from the failed step); non-retryable steps carry the same park
    but with ``retryable=False`` so a human sees a conflict class that will
    not heal by itself.  Neither ever fabricates step facts.
    """

    def __init__(self, step: str, reason: str, retryable: bool = True) -> None:
        super().__init__("{}: {}".format(step, reason))
        self.step = step
        self.reason = reason
        self.retryable = retryable


class RemoteDeliveryExecutor:
    """Step-execution boundary.  Implementations must be idempotent per step."""

    def execute(self, step: str, context: Mapping[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


def _run_git(repo: Any, args: Sequence[str], timeout: int = 120) -> str:
    command = ["git", "-C", str(repo)] + list(args)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RemoteDeliveryFailed("git", "git {} unavailable: {}".format(" ".join(args), type(error).__name__), retryable=True)
    if completed.returncode != 0:
        raise RemoteDeliveryFailed(
            "git",
            "git {} exited {}".format(" ".join(args), completed.returncode),
            retryable=True,
        )
    return completed.stdout.strip()


def _run_gh(repo: Any, args: Sequence[str], timeout: int = 120) -> str:
    command = ["gh"] + list(args)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(repo),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RemoteDeliveryFailed("gh", "gh {} unavailable: {}".format(" ".join(args), type(error).__name__), retryable=True)
    if completed.returncode != 0:
        raise RemoteDeliveryFailed(
            "gh",
            "gh {} exited {}".format(" ".join(args), completed.returncode),
            retryable=True,
        )
    return completed.stdout.strip()


NODE_BRANCH_PREFIX = "node/"


def run_node_branches(snapshot: Any) -> List[str]:
    """The run's business node branches (``node/*``), sorted and unique."""
    nodes = _snapshot_value(snapshot, "nodes", {}) or {}
    branches = set()
    if isinstance(nodes, Mapping):
        for node_id, node in nodes.items():
            if node_id in (INTEGRATION_REVIEW_NODE_ID, REMOTE_DELIVERY_NODE_ID):
                continue
            branch = node.get("branch") if isinstance(node, Mapping) else None
            if isinstance(branch, str) and branch.startswith(NODE_BRANCH_PREFIX):
                branches.add(branch)
    return sorted(branches)


def remote_node_branch_deviations(repo: Any, branches: Sequence[str], timeout: int = 30) -> Dict[str, Any]:
    """Which of the run's node branches reached ``origin`` (#379).

    Workers never push; only the remote-delivery ring does, and it pushes
    its integration branch, never a ``node/*`` branch.  A node branch on the
    remote is therefore a boundary deviation.  An unreadable remote is
    ``checked: False`` with a reason -- never "no deviation".
    """
    try:
        listed = _run_git(repo, ["ls-remote", "--heads", "origin", "refs/heads/" + NODE_BRANCH_PREFIX + "*"],
                          timeout=timeout)
    except RemoteDeliveryFailed as error:
        return {"checked": False, "branches": [],
                "reason": "未检查：读不到远端（{}），不能当成没有越界".format(error.reason)}
    remote = set()
    for line in listed.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            remote.add(parts[1][len("refs/heads/"):])
    wanted = {str(item) for item in branches if isinstance(item, str)}
    return {"checked": True, "branches": sorted(remote & wanted)}


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned or "run"


class SubprocessRemoteDeliveryExecutor(RemoteDeliveryExecutor):
    """Real git/gh executor.  Runs only git and gh: no deploy, no launchd,
    no production writes."""

    def __init__(self, repo: Any, plan_id: str = "", target_branch: str = DEFAULT_TARGET_BRANCH, timeout: int = 120) -> None:
        self._repo = repo
        self._plan_id = plan_id
        self._target = target_branch if target_branch == DEFAULT_TARGET_BRANCH else DEFAULT_TARGET_BRANCH
        self._timeout = timeout

    def _source_branch(self, context: Mapping[str, Any]) -> str:
        existing = context.get("source_branch")
        if isinstance(existing, str) and existing.strip():
            return existing
        return "vibe/integration-candidate-{}".format(_slug(self._plan_id or "run"))

    def execute(self, step: str, context: Mapping[str, Any]) -> Dict[str, Any]:
        handler = {
            "prepare": self._prepare,
            "push": self._push,
            "create_pr": self._create_pr,
            "review": self._review,
            "merge": self._merge,
            "verify": self._verify,
        }.get(step)
        if handler is None:
            raise RemoteDeliveryFailed(step, "unknown delivery step", retryable=False)
        return handler(context)

    def _accepted_node_branches(self, context: Mapping[str, Any]) -> List[str]:
        branches = context.get("node_branches")
        if isinstance(branches, list):
            return [str(item) for item in branches if isinstance(item, str) and item.strip()]
        return []

    def _worktree_path(self, source: str) -> Path:
        slug = _slug(self._plan_id or source)
        return Path(str(self._repo)) / ".worktrees" / ("remote-delivery-" + slug)

    def _prepare(self, context: Mapping[str, Any]) -> Dict[str, Any]:
        repo, target, timeout = self._repo, self._target, self._timeout
        source = self._source_branch(context)
        branches = self._accepted_node_branches(context)
        _run_git(repo, ["fetch", "origin", target], timeout=timeout)
        # Merge inside a dedicated temporary worktree checked out on the
        # source branch: ``git merge`` acts on the *checked-out* branch, so
        # merging in the main repo would land the node branches on whatever
        # the user happens to have checked out (and read back the unchanged
        # source ref as the head).
        worktree = self._worktree_path(source)
        if worktree.exists():
            try:
                _run_git(repo, ["worktree", "remove", "--force", str(worktree)], timeout=timeout)
            except RemoteDeliveryFailed:
                pass
        try:
            _run_git(
                repo,
                ["worktree", "add", "--force", str(worktree), "-B", source, "origin/" + target],
                timeout=timeout,
            )
            for branch in branches:
                try:
                    _run_git(worktree, ["merge", "--no-edit", branch], timeout=timeout)
                except RemoteDeliveryFailed as error:
                    # A conflict is deterministic: retrying the same merge
                    # can only fail the same way, so do not burn a tick slot.
                    raise RemoteDeliveryFailed(
                        "prepare",
                        "merge of {} failed (conflict or bad branch): {}".format(branch, error.reason),
                        retryable=False,
                    ) from error
            head = _run_git(worktree, ["rev-parse", "HEAD"], timeout=timeout)
            same_tree = _run_git(worktree, ["rev-parse", "HEAD^{tree}"], timeout=timeout) == \
                _run_git(worktree, ["rev-parse", "origin/" + target + "^{tree}"], timeout=timeout)
        finally:
            try:
                _run_git(repo, ["worktree", "remove", "--force", str(worktree)], timeout=timeout)
            except RemoteDeliveryFailed:
                pass
        if not _SHA_RE.match(head):
            raise RemoteDeliveryFailed("prepare", "source head is not a SHA", retryable=False)
        facts = {
            "source_branch": source,
            "target_branch": target,
            "head_sha": head,
            "merged_node_branches": branches,
        }
        if same_tree:
            # Nothing new to deliver: the target already holds this content
            # (a squash merge landed earlier but the ring never closed).
            # Opening another PR would be empty, so reuse the merged one.
            delivered = self._merged_pr(source, branches)
            if not delivered:
                raise RemoteDeliveryFailed(
                    "prepare",
                    "target already holds this content but no merged PR is provably this delivery",
                    retryable=False)
            facts.update(delivered)
            facts["already_delivered"] = True
        return facts

    def _merged_pr(self, source: str, branches: Sequence[str]) -> Dict[str, Any]:
        """A merged PR that provably delivered *these* node branches.

        Equal trees alone cannot tell this delivery from an unrelated older
        PR on the same source branch, so a candidate is accepted only when
        every accepted node branch tip is an ancestor of the PR head and the
        merge commit is on the target.  No node branches means nothing can be
        proven, so nothing is reused.
        """
        repo, timeout = self._repo, self._timeout
        if not branches:
            return {}
        try:
            entries = json.loads(_run_gh(
                repo,
                ["pr", "list", "--base", self._target, "--head", source, "--state", "merged",
                 "--limit", "100", "--json", "url,number,headRefOid,mergeCommit"],
                timeout=timeout) or "[]")
        except (RemoteDeliveryFailed, ValueError):
            return {}
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, Mapping):
                continue
            oid = str((entry.get("mergeCommit") or {}).get("oid") or "")
            head = str(entry.get("headRefOid") or "")
            number = int(entry.get("number") or 0)
            if not (_SHA_RE.match(oid) and _SHA_RE.match(head) and number > 0):
                continue
            try:
                _run_git(repo, ["fetch", "origin", "refs/pull/{}/head".format(number)], timeout=timeout)
                for branch in branches:
                    _run_git(repo, ["merge-base", "--is-ancestor", branch, head], timeout=timeout)
                _run_git(repo, ["merge-base", "--is-ancestor", oid, "origin/" + self._target],
                         timeout=timeout)
            except RemoteDeliveryFailed:
                continue
            return {"pr_url": str(entry.get("url") or ""), "pr_number": number,
                    "pr_head_sha": head, "merge_commit": oid}
        return {}

    def _push(self, context: Mapping[str, Any]) -> Dict[str, Any]:
        repo, timeout = self._repo, self._timeout
        source = str(context.get("source_branch") or self._source_branch(context))
        local_head = _run_git(repo, ["rev-parse", source], timeout=timeout)
        remote = "refs/heads/" + source
        remote_head = _run_git(
            repo, ["ls-remote", "origin", remote], timeout=timeout
        ).split()
        remote_sha = remote_head[0] if remote_head else ""
        already = remote_sha == local_head
        if not already:
            if remote_sha:
                # Pin the lease to the SHA ls-remote just observed: a bare
                # --force-with-lease fails "stale info" when no local
                # remote-tracking ref exists, which nothing in this engine
                # maintains.
                lease = "--force-with-lease=refs/heads/{}:{}".format(source, remote_sha)
                _run_git(repo, ["push", lease, "origin", "{}:{}".format(source, source)], timeout=timeout)
            else:
                _run_git(repo, ["push", "origin", "{}:{}".format(source, source)], timeout=timeout)
        return {"source_branch": source, "head_sha": local_head, "already_pushed": already}

    def _create_pr(self, context: Mapping[str, Any]) -> Dict[str, Any]:
        repo, target, timeout = self._repo, self._target, self._timeout
        source = str(context.get("source_branch") or self._source_branch(context))
        head_sha = str(context.get("head_sha") or "")
        listing = _run_gh(
            repo,
            ["pr", "list", "--base", target, "--head", source, "--state", "open",
             "--json", "url,number,headRefOid"],
            timeout=timeout,
        )
        try:
            entries = json.loads(listing) if listing else []
        except ValueError:
            entries = []
        for entry in entries:
            if isinstance(entry, Mapping):
                entry_head = str(entry.get("headRefOid") or "")
                if not head_sha or entry_head == head_sha:
                    return {
                        "source_branch": source,
                        "target_branch": target,
                        "pr_url": str(entry.get("url") or ""),
                        "pr_number": int(entry.get("number") or 0),
                        "reused": True,
                    }
        url = _run_gh(
            repo,
            ["pr", "create", "--base", target, "--head", source,
             "--title", "vibe integration delivery: {}".format(self._plan_id or source),
             "--body", "Automated integration delivery for plan {}. Source {} -> {}.".format(self._plan_id or source, source, target)],
            timeout=timeout,
        )
        number = 0
        match = re.search(r"/pull/(\d+)", url)
        if match:
            number = int(match.group(1))
        if not url.startswith("http"):
            raise RemoteDeliveryFailed("create_pr", "gh pr create returned no URL", retryable=True)
        return {
            "source_branch": source,
            "target_branch": target,
            "pr_url": url,
            "pr_number": number,
            "reused": False,
        }

    def _review(self, context: Mapping[str, Any]) -> Dict[str, Any]:
        repo, timeout = self._repo, self._timeout
        pr_url = str(context.get("pr_url") or "")
        if not pr_url.startswith("http"):
            raise RemoteDeliveryFailed("review", "PR URL is missing", retryable=False)
        raw = _run_gh(
            repo,
            ["pr", "view", pr_url, "--json", "reviews,state,reviewRequests"],
            timeout=timeout,
        )
        try:
            payload = json.loads(raw)
        except ValueError as error:
            raise RemoteDeliveryFailed("review", "review evidence is unreadable", retryable=True) from error
        reviews = payload.get("reviews") if isinstance(payload, Mapping) else []
        approved = any(
            isinstance(item, Mapping) and item.get("state") == "APPROVED"
            for item in (reviews if isinstance(reviews, list) else [])
        )
        changes_requested = any(
            isinstance(item, Mapping) and item.get("state") == "CHANGES_REQUESTED"
            for item in (reviews if isinstance(reviews, list) else [])
        )
        requests = payload.get("reviewRequests") if isinstance(payload, Mapping) else []
        reviewers = []
        for item in (requests if isinstance(requests, list) else []):
            if isinstance(item, Mapping):
                name = item.get("login") or item.get("slug") or item.get("name")
                if isinstance(name, str) and name.strip():
                    reviewers.append(name.strip())
        facts = {
            "pr_url": pr_url,
            "approved": approved,
            "changes_requested": changes_requested,
            "pr_state": str(payload.get("state") or "") if isinstance(payload, Mapping) else "",
            "requested_reviewers": reviewers,
        }
        # Only probe protection when no GitHub approval exists and nothing
        # blocks the PR: it decides whether the integration review evidence
        # may stand in for a GitHub review.
        if not approved and not changes_requested:
            facts["review_protection"] = self._review_protection()
        return facts

    def _review_protection(self) -> str:
        """``"none"`` only when both classic protection and rulesets are
        positively confirmed to require no review; every failure, unparseable
        reply or 404 (cannot tell "off" from "no permission") is ``"unknown"``."""
        repo, target, timeout = self._repo, self._target, self._timeout
        try:
            branch = json.loads(_run_gh(
                repo, ["api", "repos/{{owner}}/{{repo}}/branches/{}".format(target)], timeout=timeout))
            if not isinstance(branch, Mapping) or not isinstance(branch.get("protected"), bool):
                return "unknown"
            if branch["protected"]:
                classic = json.loads(_run_gh(
                    repo, ["api", "repos/{{owner}}/{{repo}}/branches/{}/protection".format(target)],
                    timeout=timeout))
                if not isinstance(classic, Mapping):
                    return "unknown"
                reviews = classic.get("required_pull_request_reviews")
                if reviews is not None:
                    if not isinstance(reviews, Mapping):
                        return "unknown"
                    count = reviews.get("required_approving_review_count")
                    if not isinstance(count, int) or isinstance(count, bool) or count > 0 \
                            or reviews.get("require_code_owner_reviews") is not False:
                        return "required"
            rules = json.loads(_run_gh(
                repo, ["api", "repos/{{owner}}/{{repo}}/rules/branches/{}".format(target)],
                timeout=timeout))
            if not isinstance(rules, list):
                return "unknown"
            for rule in rules:
                if not isinstance(rule, Mapping):
                    return "unknown"
                if rule.get("type") == "pull_request":
                    params = rule.get("parameters")
                    if not isinstance(params, Mapping):
                        return "required"
                    count = params.get("required_approving_review_count")
                    if not isinstance(count, int) or isinstance(count, bool) or count > 0 \
                            or params.get("require_code_owner_review") is not False:
                        return "required"
            return "none"
        except (RemoteDeliveryFailed, ValueError, TypeError):
            return "unknown"

    def _merge(self, context: Mapping[str, Any]) -> Dict[str, Any]:
        repo, timeout = self._repo, self._timeout
        pr_url = str(context.get("pr_url") or "")
        head_sha = str(context.get("head_sha") or "")
        if not pr_url.startswith("http"):
            raise RemoteDeliveryFailed("merge", "PR URL is missing", retryable=False)
        view = _run_gh(
            repo, ["pr", "view", pr_url, "--json", "state,headRefOid,mergeCommit"], timeout=timeout
        )
        try:
            payload = json.loads(view)
        except ValueError as error:
            raise RemoteDeliveryFailed(
                "merge", "PR state is unreadable", retryable=True
            ) from error
        if not isinstance(payload, Mapping):
            raise RemoteDeliveryFailed("merge", "PR state is unreadable", retryable=True)
        current_head = str(payload.get("headRefOid") or "")
        merge_oid = str((payload.get("mergeCommit") or {}).get("oid") or "")
        pr_state = str(payload.get("state") or "")
        if pr_state == "MERGED":
            # Idempotent resume: the remote side effect already happened
            # (e.g. a crash between ``gh pr merge`` and the snapshot save).
            # Only accept it when the merged head is the head we reviewed.
            if head_sha and current_head and current_head != head_sha:
                raise RemoteDeliveryFailed(
                    "merge", "merged PR head does not match the recorded head", retryable=False
                )
            if not _SHA_RE.match(merge_oid):
                raise RemoteDeliveryFailed(
                    "merge", "merged PR lacks a merge commit SHA", retryable=True
                )
            return {
                "pr_url": pr_url,
                "merge_commit": merge_oid,
                "merge_method": "squash",
                "already_merged": True,
            }
        if head_sha and current_head and current_head != head_sha:
            raise RemoteDeliveryFailed(
                "merge", "PR head moved after review; delivery must re-verify", retryable=True
            )
        output = _run_gh(repo, ["pr", "merge", pr_url, "--squash"], timeout=timeout)
        match = re.search(r"\b([0-9a-f]{40})\b", output)
        merge_commit = match.group(1) if match else ""
        if not merge_commit:
            if _SHA_RE.match(merge_oid):
                merge_commit = merge_oid
        if not _SHA_RE.match(merge_commit):
            # ``gh pr merge --squash`` prints no SHA and the pre-merge view
            # has no mergeCommit yet: read it back after the merge landed.
            # Without this the merge succeeds remotely but the step fails,
            # and the retry re-runs the whole ring against a merged branch.
            readback_error = ""
            try:
                after = json.loads(_run_gh(
                    repo, ["pr", "view", pr_url, "--json", "state,mergeCommit"], timeout=timeout))
            except RemoteDeliveryFailed as error:
                after, readback_error = {}, error.reason
            except ValueError:
                after, readback_error = {}, "read-back reply is not JSON"
            if isinstance(after, Mapping) and after.get("state") == "MERGED":
                oid = str((after.get("mergeCommit") or {}).get("oid") or "")
                if _SHA_RE.match(oid):
                    merge_commit = oid
        if not _SHA_RE.match(merge_commit):
            reason = "merge commit SHA is unavailable"
            if readback_error:
                reason += " (read-back failed: {})".format(readback_error)
            raise RemoteDeliveryFailed("merge", reason, retryable=True)
        return {"pr_url": pr_url, "merge_commit": merge_commit, "merge_method": "squash"}

    def _verify(self, context: Mapping[str, Any]) -> Dict[str, Any]:
        repo, target, timeout = self._repo, self._target, self._timeout
        pr_url = str(context.get("pr_url") or "")
        merge_commit = str(context.get("merge_commit") or "")
        if not _SHA_RE.match(merge_commit):
            raise RemoteDeliveryFailed("verify", "merge commit is missing", retryable=False)
        state_raw = _run_gh(repo, ["pr", "view", pr_url, "--json", "state"], timeout=timeout)
        try:
            pr_state = str(json.loads(state_raw).get("state") or "")
        except ValueError:
            pr_state = ""
        # Fetch into a private ref: the shared FETCH_HEAD can be clobbered
        # by any concurrent fetch in the main repo.
        verify_ref = "refs/vibe-remote-delivery/verify-main"
        try:
            _run_git(
                repo,
                ["fetch", "origin", "+refs/heads/{}:{}".format(target, verify_ref)],
                timeout=timeout,
            )
            main_head = _run_git(repo, ["rev-parse", verify_ref], timeout=timeout)
            _run_git(
                repo, ["merge-base", "--is-ancestor", merge_commit, verify_ref], timeout=timeout
            )
        finally:
            try:
                _run_git(repo, ["update-ref", "-d", verify_ref], timeout=timeout)
            except RemoteDeliveryFailed:
                pass
        return {
            "pr_url": pr_url,
            "merge_commit": merge_commit,
            "pr_state": pr_state,
            "main_head_sha": main_head,
            "verified": pr_state == "MERGED",
        }


def _context_from_evidence(evidence: Mapping[str, Any], snapshot: Any) -> Dict[str, Any]:
    context: Dict[str, Any] = {
        "run_id": _snapshot_value(snapshot, "run_id", ""),
        "plan_id": _snapshot_value(snapshot, "plan_id", ""),
    }
    for key in ("source_branch", "target_branch", "head_sha", "pr_url", "pr_number", "merge_commit"):
        value = evidence.get(key)
        if value not in (None, ""):
            context[key] = value
    branches = []
    nodes = _snapshot_value(snapshot, "nodes", {}) or {}
    if isinstance(nodes, Mapping):
        for node_id in sorted(nodes):
            if node_id in (INTEGRATION_REVIEW_NODE_ID, REMOTE_DELIVERY_NODE_ID):
                continue
            node = nodes[node_id]
            if isinstance(node, Mapping) and node.get("status") == "accepted":
                branch = node.get("branch")
                if isinstance(branch, str) and branch.strip():
                    branches.append(branch)
    context["node_branches"] = branches
    return context


def _initial_evidence(snapshot: Any, context: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": _snapshot_value(snapshot, "run_id", ""),
        "status": "in_progress",
        "binding": delivery_binding(snapshot),
        "source_branch": str(context.get("source_branch") or ""),
        "target_branch": DEFAULT_TARGET_BRANCH,
        "steps": {},
    }


def _record_event(record_event: Optional[Callable[[str, Dict[str, Any]], None]], name: str, data: Dict[str, Any]) -> None:
    if record_event is not None:
        record_event(name, data)


def _write_delivery_report(paths: Any, snapshot: Any, evidence: Mapping[str, Any]) -> None:
    """Best-effort human-readable delivery report next to the run evidence."""
    if paths is None:
        return
    try:
        from .state import run_dir
        from .fsio import atomic_write_json
    except ImportError:
        return
    try:
        directory = run_dir(paths, str(_snapshot_value(snapshot, "run_id", "")), create=True)
        report = {
            "schema_version": 1,
            "run_id": _snapshot_value(snapshot, "run_id", ""),
            "plan_id": _snapshot_value(snapshot, "plan_id", ""),
            "source_branch": evidence.get("source_branch"),
            "target_branch": evidence.get("target_branch"),
            "head_sha": evidence.get("head_sha"),
            "pr_url": evidence.get("pr_url"),
            "merge_commit": evidence.get("merge_commit"),
            "review_basis": evidence.get("review_basis"),
            "waiting_on": evidence.get("waiting_on"),
            "review_basis_text": {
                "integration_review_evidence": "merged on integration review evidence (no GitHub review required)",
                "github_review": "waited for and merged after a GitHub review",
                "waiting_for_github_review": "waiting for GitHub review",
            }.get(str(evidence.get("review_basis") or ""), ""),
            "remote_verification": evidence.get("remote_verification"),
            "binding": evidence.get("binding"),
            "steps": sorted((evidence.get("steps") or {}).keys()),
            "deviations": {
                "remote_node_branches": remote_node_branch_deviations(
                    paths.root, run_node_branches(snapshot)),
            },
        }
        atomic_write_json(directory / "remote-delivery-report.json", report)
    except (OSError, ValueError, TypeError):
        # The report is presentation; the snapshot evidence and event log
        # remain the durable record, so a report write failure never blocks
        # or fabricates delivery.
        return


def drive_remote_delivery(
    snapshot: Any,
    executor: RemoteDeliveryExecutor,
    record_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    paths: Any = None,
) -> bool:
    """Advance the remote-delivery node by the pending steps.

    Appends the node once the integration review is accepted, resumes from
    persisted evidence (discarding it when the digest binding no longer
    matches), executes steps idempotently, and only accepts the node after
    remote facts confirm ``main`` contains the merge commit.  Returns True
    when the snapshot was changed; the caller persists it.
    """
    authorization = _snapshot_value(snapshot, "authorization", {})
    required = remote_delivery_required(authorization)
    nodes = _snapshot_value(snapshot, "nodes", {}) or {}
    if not isinstance(nodes, dict):
        return False
    run_id = str(_snapshot_value(snapshot, "run_id", ""))
    existing = nodes.get(REMOTE_DELIVERY_NODE_ID)
    if existing is not None and not required:
        # A reauthorization revoked remote_git_actions: retire the ring and
        # its evidence instead of leaving an undrivable node that would also
        # fail snapshot load validation on every subsequent tick.
        nodes.pop(REMOTE_DELIVERY_NODE_ID, None)
        if isinstance(snapshot, dict):
            snapshot["remote_delivery_evidence"] = {}
        else:
            snapshot.remote_delivery_evidence = {}
        _record_event(record_event, "remote_delivery_retired", {
            "run_id": run_id,
            "node_id": REMOTE_DELIVERY_NODE_ID,
            "status": "retired",
            "change_reason": "remote_git_actions_revoked",
            "reason": "remote_git_actions revoked by reauthorization; delivery ring retired",
        })
        return True
    if not required:
        return False
    integration = nodes.get(INTEGRATION_REVIEW_NODE_ID)
    if not isinstance(integration, dict) or integration.get("status") != "accepted":
        return False
    if not integration_evidence_digest(snapshot):
        return False
    node = nodes.get(REMOTE_DELIVERY_NODE_ID)
    if node is None:
        node = {
            "node_id": REMOTE_DELIVERY_NODE_ID,
            "status": "planned",
            "monitor_driven": True,
            "remote_delivery": True,
        }
        nodes[REMOTE_DELIVERY_NODE_ID] = node
        _record_event(record_event, "remote_delivery_node_appended", {
            "run_id": _snapshot_value(snapshot, "run_id", ""),
            "node_id": REMOTE_DELIVERY_NODE_ID,
            "source": "monitor.remote_delivery",
            "status": "planned",
        })
    binding = delivery_binding(snapshot)
    evidence = _snapshot_value(snapshot, "remote_delivery_evidence", {}) or {}
    if evidence and not binding_matches(evidence.get("binding"), binding):
        _record_event(record_event, "remote_delivery_evidence_invalidated", {
            "run_id": _snapshot_value(snapshot, "run_id", ""),
            "node_id": REMOTE_DELIVERY_NODE_ID,
            "reason": "digest binding changed; prior delivery evidence is not reusable",
            "change_reason": "remote_delivery_binding_changed",
        })
        evidence = {"binding_drifted": True}
        node["status"] = "planned"
    elif node.get("status") == "accepted":
        # Only an accepted node with current, digest-bound evidence may stop;
        # staleness is handled above.
        return False
    binding = delivery_binding(snapshot)
    for key in ("authorization_digest", "node_contract_digest", "integration_digest"):
        if not _DIGEST_RE.match(binding.get(key, "")):
            # Refuse to start rather than park on a retryable loop whose
            # closeout reason would misread as digest drift.
            node["status"] = "blocked_unknown"
            node["failure"] = {
                "step": "lineage",
                "reason": "delivery lineage digest {} is incomplete".format(key),
                "retryable": False,
            }
            _record_event(record_event, "remote_delivery_failed", {
                "run_id": run_id,
                "node_id": REMOTE_DELIVERY_NODE_ID,
                "step": "lineage",
                "status": "blocked_unknown",
                "retryable": False,
                "reason_class": "remote_delivery_lineage_incomplete",
                "reason": node["failure"]["reason"],
            })
            return True

    context = _context_from_evidence(evidence, snapshot)
    if not evidence or set(evidence) == {"binding_drifted"}:
        drifted = bool(evidence.get("binding_drifted"))
        evidence = _initial_evidence(snapshot, context)
        if drifted:
            evidence["binding_drifted"] = True
    steps = evidence.setdefault("steps", {})

    def persist() -> None:
        if isinstance(snapshot, dict):
            snapshot["remote_delivery_evidence"] = evidence
        else:
            snapshot.remote_delivery_evidence = evidence
        node["evidence"] = evidence

    try:
        node["status"] = "running"
        for step in REMOTE_DELIVERY_STEPS:
            record = steps.get(step)
            if isinstance(record, Mapping) and record.get("status") == "succeeded":
                continue
            facts = executor.execute(step, context)
            if not isinstance(facts, Mapping):
                raise RemoteDeliveryFailed(step, "executor returned no facts", retryable=False)
            context.update(facts)
            for key in ("source_branch", "target_branch", "head_sha", "pr_url", "pr_number", "merge_commit"):
                value = facts.get(key)
                if value not in (None, ""):
                    evidence[key] = value
            steps[step] = {"status": "succeeded", "facts": dict(facts)}
            already = step == "prepare" and bool(facts.get("already_delivered"))
            if already:
                # Same write as prepare itself: a crash must never leave
                # prepare done with the stubbed steps missing, or a resume
                # would really push, open a PR and merge.
                evidence["head_sha"] = facts["pr_head_sha"]
                evidence["review_basis"] = "already_delivered"
                for done in ("push", "create_pr", "review", "merge"):
                    steps[done] = {"status": "succeeded", "facts": {"already_delivered": True}}
                context["head_sha"] = facts["pr_head_sha"]
            persist()
            step_event = {
                "run_id": run_id,
                "node_id": REMOTE_DELIVERY_NODE_ID,
                "step": step,
                "status": "succeeded",
            }
            for key in ("source_branch", "target_branch", "head_sha", "pr_url", "merge_commit"):
                value = evidence.get(key)
                if value not in (None, ""):
                    step_event[key] = value
            _record_event(record_event, "remote_delivery_step_completed", step_event)
            if already:
                continue
            if step == "review":
                if facts.get("changes_requested"):
                    evidence["status"] = "rework"
                    node["status"] = "rework"
                    persist()
                    _record_event(record_event, "remote_delivery_rework", {
                        "run_id": run_id,
                        "node_id": REMOTE_DELIVERY_NODE_ID,
                        "step": step,
                        "pr_url": evidence.get("pr_url", ""),
                        "status": "rework",
                        "reason": "review requested changes",
                    })
                    return True
                if facts.get("approved"):
                    evidence["review_basis"] = "github_review"
                elif (
                    facts.get("review_protection") == "none"
                    and integration.get("status") == "accepted"
                    and not evidence.get("binding_drifted")
                    and binding_matches(evidence.get("binding"), delivery_binding(snapshot))
                ):
                    # D3-A: no review protection on the target branch, so the
                    # accepted, digest-bound integration review stands in.
                    evidence["review_basis"] = "integration_review_evidence"
                else:
                    waiting = [str(x) for x in (facts.get("requested_reviewers") or []) if x]
                    who = ", ".join(waiting) if waiting else "GitHub reviewer (none assigned)"
                    node["waiting_on"] = who
                    evidence["waiting_on"] = who
                    evidence["review_basis"] = "waiting_for_github_review"
                    # The review gate has not passed: never leave it recorded
                    # as succeeded, or a retry would skip it and merge.
                    steps.pop("review", None)
                    persist()
                    _write_delivery_report(paths, snapshot, evidence)
                    raise RemoteDeliveryFailed(
                        step, "waiting for GitHub review from {}".format(who), retryable=True
                    )
                node.pop("waiting_on", None)
                evidence.pop("waiting_on", None)
            if step == "verify":
                verification = {
                    "verified": facts.get("verified") is True,
                    "main_head_sha": facts.get("main_head_sha", ""),
                    "merge_commit": facts.get("merge_commit", ""),
                    "pr_state": facts.get("pr_state", ""),
                }
                evidence["remote_verification"] = verification
                if not verification["verified"]:
                    raise RemoteDeliveryFailed(
                        step, "remote facts do not confirm the merge on main", retryable=True
                    )
        evidence["status"] = "accepted"
        node["status"] = "accepted"
        node.pop("failure", None)
        persist()
        _write_delivery_report(paths, snapshot, evidence)
        _record_event(record_event, "remote_delivery_accepted", {
            "run_id": run_id,
            "node_id": REMOTE_DELIVERY_NODE_ID,
            "status": "accepted",
            "delivery": {
                "pr_url": evidence.get("pr_url", ""),
                "source_branch": evidence.get("source_branch", ""),
                "target_branch": evidence.get("target_branch", ""),
                "head_sha": evidence.get("head_sha", ""),
                "merge_commit": evidence.get("merge_commit", ""),
                "remote_verification": evidence.get("remote_verification", {}),
            },
        })
        return True
    except RemoteDeliveryFailed as error:
        evidence["status"] = "blocked_unknown"
        node["status"] = "blocked_unknown"
        node["failure"] = {
            "step": error.step,
            "reason": error.reason,
            "retryable": error.retryable,
        }
        persist()
        _record_event(record_event, "remote_delivery_failed", {
            "run_id": run_id,
            "node_id": REMOTE_DELIVERY_NODE_ID,
            "step": error.step,
            "status": "blocked_unknown",
            "retryable": error.retryable,
            "reason_class": "remote_delivery_failed",
            "reason": error.reason,
        })
        return True
