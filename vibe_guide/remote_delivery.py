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
verified evidence.  Neither mode touches deploy, launchd or production
writes -- this engine only ever runs git/gh commands.
"""
from dataclasses import dataclass
import json
import re
import subprocess
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

    def _prepare(self, context: Mapping[str, Any]) -> Dict[str, Any]:
        repo, target, timeout = self._repo, self._target, self._timeout
        source = self._source_branch(context)
        _run_git(repo, ["fetch", "origin", target], timeout=timeout)
        _run_git(repo, ["branch", "-f", source, "origin/" + target], timeout=timeout)
        for branch in self._accepted_node_branches(context):
            _run_git(repo, ["merge", "--no-edit", branch], timeout=timeout)
        head = _run_git(repo, ["rev-parse", source], timeout=timeout)
        if not _SHA_RE.match(head):
            raise RemoteDeliveryFailed("prepare", "source head is not a SHA", retryable=False)
        return {
            "source_branch": source,
            "target_branch": target,
            "head_sha": head,
            "merged_node_branches": self._accepted_node_branches(context),
        }

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
            _run_git(repo, ["push", "--force-with-lease", "origin", "{}:{}".format(source, source)], timeout=timeout)
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
            ["pr", "view", pr_url, "--json", "reviews,state"],
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
        return {
            "pr_url": pr_url,
            "approved": approved,
            "changes_requested": changes_requested,
            "pr_state": str(payload.get("state") or "") if isinstance(payload, Mapping) else "",
        }

    def _merge(self, context: Mapping[str, Any]) -> Dict[str, Any]:
        repo, timeout = self._repo, self._timeout
        pr_url = str(context.get("pr_url") or "")
        head_sha = str(context.get("head_sha") or "")
        if not pr_url.startswith("http"):
            raise RemoteDeliveryFailed("merge", "PR URL is missing", retryable=False)
        if head_sha:
            current = _run_gh(
                repo, ["pr", "view", pr_url, "--json", "headRefOid"], timeout=timeout
            )
            try:
                current_head = str(json.loads(current).get("headRefOid") or "")
            except ValueError:
                current_head = ""
            if current_head and current_head != head_sha:
                raise RemoteDeliveryFailed(
                    "merge", "PR head moved after review; delivery must re-verify", retryable=True
                )
        output = _run_gh(repo, ["pr", "merge", pr_url, "--squash"], timeout=timeout)
        match = re.search(r"\b([0-9a-f]{40})\b", output)
        merge_commit = match.group(1) if match else ""
        if not merge_commit:
            view = _run_gh(
                repo, ["pr", "view", pr_url, "--json", "mergeCommit"], timeout=timeout
            )
            try:
                merge_commit = str((json.loads(view).get("mergeCommit") or {}).get("oid") or "")
            except ValueError:
                merge_commit = ""
        if not _SHA_RE.match(merge_commit):
            raise RemoteDeliveryFailed("merge", "merge commit SHA is unavailable", retryable=True)
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
        _run_git(repo, ["fetch", "origin", target], timeout=timeout)
        main_head = _run_git(repo, ["rev-parse", "FETCH_HEAD"], timeout=timeout)
        _run_git(repo, ["merge-base", "--is-ancestor", merge_commit, "FETCH_HEAD"], timeout=timeout)
        return {
            "pr_url": pr_url,
            "merge_commit": merge_commit,
            "pr_state": pr_state,
            "main_head_sha": main_head,
            "verified": pr_state == "MERGED",
        }


@dataclass
class _DriveResult:
    changed: bool = False


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
            "remote_verification": evidence.get("remote_verification"),
            "binding": evidence.get("binding"),
            "steps": sorted((evidence.get("steps") or {}).keys()),
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
    if not remote_delivery_required(authorization):
        return False
    nodes = _snapshot_value(snapshot, "nodes", {}) or {}
    if not isinstance(nodes, dict):
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
    evidence = _snapshot_value(snapshot, "remote_delivery_evidence", {}) or {}
    binding = delivery_binding(snapshot)
    if evidence and not binding_matches(evidence.get("binding"), binding):
        _record_event(record_event, "remote_delivery_evidence_invalidated", {
            "run_id": _snapshot_value(snapshot, "run_id", ""),
            "node_id": REMOTE_DELIVERY_NODE_ID,
            "reason": "digest binding changed; prior delivery evidence is not reusable",
            "change_reason": "remote_delivery_binding_changed",
        })
        evidence = {}
        node["status"] = "planned"
    elif node.get("status") == "accepted":
        # Only an accepted node with current, digest-bound evidence may stop;
        # staleness is handled above.
        return False

    context = _context_from_evidence(evidence, snapshot)
    if not evidence:
        evidence = _initial_evidence(snapshot, context)
    steps = evidence.setdefault("steps", {})

    run_id = str(_snapshot_value(snapshot, "run_id", ""))

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
                if not facts.get("approved"):
                    raise RemoteDeliveryFailed(
                        step, "integration PR has no approving review", retryable=True
                    )
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
