"""Shared human-decision boundary categories."""

BOUNDARY_CATEGORIES = (
    "product_design",
    "external_authorization",
    "deploy",
)


def validate_boundary_category(category):
    """Return a supported boundary category or fail before any state write."""
    value = category.strip() if isinstance(category, str) else ""
    if value not in BOUNDARY_CATEGORIES:
        raise ValueError(
            "hold category must be one of: {}; engineering changes that do not "
            "change PRD acceptance must be decided and recorded by the supervisor"
            .format(", ".join(BOUNDARY_CATEGORIES))
        )
    return value

import json
from pathlib import Path
import subprocess
from typing import Any, Mapping, Optional

from .decisions import DecisionMailbox, canonical_digest, normalize_options


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
        project = self._read_json(self.paths.resolve_vibe_path("config.json"))
        project_bridge = project.get("notify_bridge")
        if not isinstance(project_bridge, dict):
            return {}
        if set(project_bridge) - {"enabled"}:
            return {"notify_bridge": {"enabled": True, "project_command": True}}
        user = self._read_json(self.paths.resolve_vibe_home_path("config.json"))
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
        command = bridge.get("command")
        if not isinstance(command, str) or not command.strip():
            return {"status": "unknown", "reason": "notify bridge command is missing"}
        decision_id = decision.get("decision_id") if isinstance(decision, Mapping) else None
        if not isinstance(decision_id, str) or not decision_id:
            return {"status": "unknown", "reason": "decision id is missing"}
        request_path = self.paths.resolve_vibe_path("decisions/requests/" + decision_id + ".json")
        if request_path.is_symlink() or not request_path.is_file():
            return {"status": "unknown", "reason": "decision request is unreadable"}
        try:
            timeout = float(bridge.get("timeout_seconds", 10))
            if timeout <= 0:
                raise ValueError("notify bridge timeout must be positive")
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


def raise_decision(paths, **kwargs):
    mailbox = DecisionMailbox(paths)
    record = mailbox.raise_decision(**kwargs)
    notification = NotificationBridge(paths).notify(record)
    return {"request": record, "notification": notification}


def ingest_worker_decisions(paths, run_id, node_id, requests):
    mailbox = DecisionMailbox(paths)
    records = mailbox.ingest_worker_requests(run_id, node_id, requests)
    bridge = NotificationBridge(paths)
    notifications = [bridge.notify(record) for record in records]
    return records, notifications


def decision_list(paths, run_id=None):
    return DecisionMailbox(paths).list_pending(run_id)


def answer_decision(paths, decision_id, choice, **kwargs):
    return DecisionMailbox(paths).answer(decision_id, choice, **kwargs)


__all__ = [
    "DecisionMailbox",
    "NotificationBridge",
    "answer_decision",
    "decision_list",
    "ingest_worker_decisions",
    "normalize_options",
    "raise_decision",
]
