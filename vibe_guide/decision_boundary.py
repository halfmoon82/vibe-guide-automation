"""Shared human-decision boundary categories.

Holds and decisions use two canonical categories: ``product_acceptance`` and
``external_authorization``.  ``product_design`` and ``deploy`` stay accepted
as aliases but are always stored canonical; records without a category are
read as ``product_acceptance``.  The definitions live in ``decisions`` so the
mailbox can validate without a circular import.
"""

import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping, Optional

from .decisions import (
    BOUNDARY_CATEGORIES,
    BOUNDARY_CATEGORY_ALIASES,
    LEGACY_BOUNDARY_CATEGORY,
    DecisionMailbox,
    canonical_digest,
    normalize_options,
    validate_boundary_category,
)


#: Upper bound for the user-configured bridge timeout, in seconds.
_MAX_BRIDGE_TIMEOUT = 300.0


class NotificationBridge:
    def __init__(self, paths, config: Optional[Mapping[str, Any]] = None):
        self.paths = paths
        self.config = dict(config) if config is not None else self._load_config()

    @staticmethod
    def _read_json(path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _load_config(self) -> dict[str, Any]:
        """Project config switches the bridge on; only user config names it.

        The project ``.vibe/config.json`` travels with the repository, so it
        may set ``notify_bridge.enabled`` and nothing else.  The command and
        timeout come from the user-level ``$VIBE_HOME/config.json`` so a cloned
        repository can never make this machine run a program (integrated PRD
        N8).  A project config that names a command is refused, not ignored.
        """
        try:
            project_path = self.paths.resolve_vibe_path("config.json")
        except ValueError:
            # A project config escaping .vibe never switches the bridge on.
            return {}
        project = self._read_json(project_path)
        project_bridge = project.get("notify_bridge")
        if not isinstance(project_bridge, dict):
            return {}
        if set(project_bridge) - {"enabled"}:
            return {"notify_bridge": {"enabled": True, "project_command": True}}
        if project_bridge.get("enabled") is not True:
            return {}
        try:
            user_path = self.paths.resolve_vibe_home_path("config.json")
        except ValueError:
            # A user config escaping VIBE_HOME (e.g. a dotfiles symlink) never
            # breaks decision ingestion; the enabled bridge is simply unknown.
            return {"notify_bridge": {"enabled": True, "unreadable": True}}
        user = self._read_json(user_path)
        user_bridge = user.get("notify_bridge")
        merged = dict(user_bridge) if isinstance(user_bridge, dict) else {}
        merged.pop("enabled", None)
        merged["enabled"] = project_bridge.get("enabled")
        return {"notify_bridge": merged}

    def notify(self, decision: Mapping[str, Any]) -> dict[str, Any]:
        bridge = self.config.get("notify_bridge") if isinstance(self.config, Mapping) else None
        if bridge is None and isinstance(self.config, Mapping) and "enabled" in self.config:
            bridge = self.config
        if not isinstance(bridge, Mapping) or bridge.get("enabled") is not True:
            return {"status": "disabled", "evidence": "notify_bridge:disabled"}
        if bridge.get("project_command"):
            return {"status": "unknown",
                    "reason": "notify_bridge command must live in user-level config"}
        if bridge.get("unreadable"):
            return {"status": "unknown", "reason": "notify bridge config is unreadable"}
        command = bridge.get("command")
        if not isinstance(command, str) or not command.strip():
            return {"status": "unknown", "reason": "notify bridge command is missing"}
        if not os.path.isabs(command):
            # The bridge runs with the project root as cwd; a relative command
            # would execute a file from the repository.
            return {"status": "unknown", "reason": "notify bridge command must be an absolute path"}
        decision_id = decision.get("decision_id") if isinstance(decision, Mapping) else None
        if not isinstance(decision_id, str) or not decision_id:
            return {"status": "unknown", "reason": "decision id is missing"}
        request_path = self.paths.resolve_vibe_path("decisions/requests/" + decision_id + ".json")
        if request_path.is_symlink() or not request_path.is_file():
            return {"status": "unknown", "reason": "decision request is unreadable"}
        try:
            raw_timeout = bridge.get("timeout_seconds", 10)
            if isinstance(raw_timeout, bool):
                raise ValueError("notify bridge timeout must be a number")
            timeout = float(raw_timeout)
            if not 0 < timeout <= _MAX_BRIDGE_TIMEOUT:
                raise ValueError("notify bridge timeout out of range")
            completed = subprocess.run(
                [command, "push", str(request_path.resolve())],
                cwd=str(self.paths.root),
                timeout=timeout,
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            return {"status": "unknown", "reason": type(error).__name__}
        if completed.returncode != 0:
            return {"status": "unknown", "reason": "bridge exited {}".format(completed.returncode)}
        return {"status": "accepted", "evidence": "notify_bridge:exit0"}


#: Reuse scopes for decision records.  ``run`` decisions are answered once per
#: run and reused by every node; ``node`` decisions belong to the raising node.
DECISION_SCOPES = ("run", "node")
#: Records written without an explicit scope default to run scope.
DEFAULT_DECISION_SCOPE = "run"
#: Records written before scopes existed read as node scope, so they are never
#: silently promoted to run-level conclusions other nodes could reuse.
LEGACY_DECISION_SCOPE = "node"


def decision_fingerprint(question) -> Optional[str]:
    """Canonical fingerprint of a question for cross-node reuse matching.

    Whitespace is normalized so superficial formatting differences do not
    defeat reuse.  ``None`` means "treat as a different question": callers
    skip the reuse lookup and raise a fresh request as usual.
    """
    if not isinstance(question, str):
        return None
    normalized = " ".join(question.split())
    if not normalized:
        return None
    return canonical_digest({"question": normalized})


class ScopedDecisionMailbox(DecisionMailbox):
    """Decision mailbox whose records carry a reuse scope and a fingerprint.

    ``decisions.DecisionMailbox`` stays untouched; this subclass only injects
    the two extra fields into built records (the stored digest naturally
    covers them).  Worker-ingested requests are node-scoped; directly raised
    ones default to run scope.
    """

    def __init__(self, paths):
        super().__init__(paths)
        self._build_scope_default = DEFAULT_DECISION_SCOPE

    def _build_record(self, *, scope: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        chosen = self._build_scope_default if scope is None else scope
        if chosen not in DECISION_SCOPES:
            raise ValueError("scope must be one of: {}".format(", ".join(DECISION_SCOPES)))
        record = super()._build_record(**kwargs)
        record["scope"] = chosen
        record["fingerprint"] = decision_fingerprint(kwargs.get("question"))
        return record

    def ingest_worker_requests(self, run_id: str, node_id: str, requests):
        """Worker-originated requests are node-scoped; the supervisor's own
        raises default to run scope.  The all-or-nothing batch logic stays in
        the base class; only the build default is switched here."""
        previous = self._build_scope_default
        self._build_scope_default = "node"
        try:
            return super().ingest_worker_requests(run_id, node_id, requests)
        finally:
            self._build_scope_default = previous

    def list_answered_scoped(self, run_id: Optional[str] = None) -> list:
        """Answered requests with question and effective scope attached.

        Records written before scopes existed read as ``node`` scope, so a
        damaged or legacy ledger fails closed toward "not reusable".
        """
        if self.requests.is_symlink() or self.results.is_symlink():
            raise ValueError("decision mailbox path may not be a symlink")
        if not self.requests.is_dir():
            return []
        answered = []
        for path in sorted(self.requests.glob("dec-*.json")):
            record = self._load_request(path.stem)
            if run_id is not None and record.get("run_id") != run_id:
                continue
            result_path = self._result_path(path.stem)
            if result_path.is_symlink():
                raise ValueError("decision result must be a regular file")
            if result_path.exists():
                result = self._load_result(path.stem, record)
                answered.append({
                    "decision_id": path.stem,
                    "node_id": record.get("node_id"),
                    "question": record.get("question"),
                    "choice": result["choice"],
                    "scope": _record_scope(record),
                })
        return answered


def _record_scope(record: Mapping[str, Any]) -> str:
    scope = record.get("scope")
    return scope if scope in DECISION_SCOPES else LEGACY_DECISION_SCOPE


def find_reusable_decision(paths, run_id: str, node_id: str, question, options,
                           requested_scope=DEFAULT_DECISION_SCOPE):
    """Look up an answered decision this raise may reuse.

    A match requires, besides the scope rule, that the earlier conclusion's
    ``choice`` is one of the option ids in this request — an old answer whose
    choice is not among the new options does not apply and the raise proceeds
    as if the questions differed (a fresh request is created).

    Returns ``(entry, status)`` where ``entry`` is the answered record (or
    ``None``) and ``status`` is ``"ok"`` or ``"unknown"`` — unknown means the
    ledger could not be read, so the caller must raise fresh without claiming
    there was no earlier answer.
    """
    fingerprint = decision_fingerprint(question)
    if fingerprint is None:
        return None, "ok"
    option_ids = {item["id"] for item in normalize_options(options)}
    try:
        answered = ScopedDecisionMailbox(paths).list_answered_scoped(run_id)
    except (OSError, ValueError):
        return None, "unknown"
    for entry in answered:
        if decision_fingerprint(entry.get("question")) != fingerprint:
            continue
        if entry["choice"] not in option_ids:
            continue
        if entry["scope"] == "run" or (
                requested_scope != "run"
                and entry["scope"] == "node" and entry.get("node_id") == node_id):
            return entry, "ok"
    return None, "ok"


def raise_decision(paths, **kwargs):
    mailbox = ScopedDecisionMailbox(paths)
    record = mailbox.raise_decision(**kwargs)
    notification = NotificationBridge(paths).notify(record)
    return {"request": record, "notification": notification}


def raise_reusable_decision(paths, *, run_id, node_id, question, options,
                            recommendation=None, evidence=None, category=None,
                            scope=DEFAULT_DECISION_SCOPE):
    """Raise a decision, reusing an earlier answered one when it applies.

    A raise hits reuse when the same run already has an answered decision
    whose question fingerprint matches, whose scope is ``run`` (any node may
    reuse) or ``node`` owned by this same node, and whose answered ``choice``
    is one of this request's option ids.  Reuse returns the existing request
    and choice without writing a new record; an unreadable ledger is reported
    as ``reuse_status="unknown"`` and a fresh request is raised anyway.

    The reuse lookup and the fresh raise are not atomic: within a concurrent
    window two raises of the same question can both miss the ledger and both
    create pending requests.  That duplication is accepted — the answers stay
    option-bound and the supervisor answers one and voids the other.
    """
    matched, status = find_reusable_decision(
        paths, run_id, node_id, question, options, requested_scope=scope)
    if matched is not None:
        request = ScopedDecisionMailbox(paths)._load_request(matched["decision_id"])
        return {
            "request": request,
            "notification": {"status": "reused", "evidence": "decision_reuse"},
            "reused": True,
            "reuse_status": "ok",
            "choice": matched["choice"],
        }
    payload = raise_decision(
        paths,
        run_id=run_id,
        node_id=node_id,
        question=question,
        options=options,
        recommendation=recommendation,
        evidence=evidence,
        category=category,
        scope=scope,
    )
    payload["reused"] = False
    payload["reuse_status"] = status
    payload["choice"] = None
    return payload


def ingest_worker_decisions(paths, run_id, node_id, requests):
    mailbox = ScopedDecisionMailbox(paths)
    records = mailbox.ingest_worker_requests(run_id, node_id, requests)

    bridge = NotificationBridge(paths)
    notifications = [bridge.notify(record) for record in records]
    return records, notifications


def decision_list(paths, run_id=None):
    return DecisionMailbox(paths).list_pending(run_id)


def answer_decision(paths, decision_id, choice, **kwargs):
    return DecisionMailbox(paths).answer(decision_id, choice, **kwargs)


__all__ = [
    "BOUNDARY_CATEGORIES",
    "DECISION_SCOPES",
    "DEFAULT_DECISION_SCOPE",
    "LEGACY_DECISION_SCOPE",
    "ScopedDecisionMailbox",
    "decision_fingerprint",
    "find_reusable_decision",
    "raise_reusable_decision",
    "BOUNDARY_CATEGORY_ALIASES",
    "LEGACY_BOUNDARY_CATEGORY",
    "validate_boundary_category",
    "DecisionMailbox",
    "NotificationBridge",
    "answer_decision",
    "decision_list",
    "ingest_worker_decisions",
    "normalize_options",
    "raise_decision",
]
