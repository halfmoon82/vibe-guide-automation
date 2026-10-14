"""Supervisor runner over Claude Code background sessions.

Completion is decided only by the worker's own delivery report; a session that
reaches ``done`` without one is ``blocked_unknown``, never success.
"""

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..adapters.claude_bg import ClaudeBgLauncher, ClaudeBgUnknown
from ..contracts import RunEvent, RunHandle, Runner
from ..paths import ProjectPaths


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _valid_generation(value):
    return isinstance(value, int) and not isinstance(value, bool)


class ClaudeBgRunner(Runner):
    def __init__(
        self,
        launcher_factory: Callable[[dict, Path], ClaudeBgLauncher],
        delivery_reader: Optional[Callable[[dict], Optional[dict]]] = None,
        paths: Optional[ProjectPaths] = None,
    ):
        self.launcher_factory = launcher_factory
        self._paths = paths
        self.delivery_reader = delivery_reader or self._read_delivery
        self._count = 0
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def _read_delivery(self, claims: dict) -> Optional[dict]:
        paths = self._paths or ProjectPaths.from_cwd(Path.cwd())
        name = "provider-actions/deliveries/{}-{}-{}-g{}.json".format(
            claims.get("run_id"), claims["node_id"], claims["role"], claims.get("generation")
        )
        try:
            path = paths.resolve_vibe_path(name)
            if (paths.vibe_dir / name).is_symlink() or path.is_symlink() or not path.is_file():
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError, UnicodeDecodeError):
            return None
        return value if isinstance(value, dict) and isinstance(value.get("payload"), dict) else None

    def _register(self, launcher, contract: dict, task_id: str) -> RunHandle:
        self._count += 1
        node_id = contract["node_id"]
        handle = RunHandle("claude-bg-{}-{}".format(node_id, self._count))
        self._sessions[handle.run_id] = {
            "launcher": launcher,
            "delivered": False,
            "claims": {
                "node_id": node_id,
                "role": contract.get("role", "developer"),
                "task_id": task_id,
                "handle_id": handle.run_id,
                "generation": contract.get("generation"),
                "run_id": contract.get("run_id"),
            },
        }
        return handle

    def start(self, contract: dict, worktree: Path) -> RunHandle:
        node_id = contract["node_id"]
        role = contract.get("role", "developer")
        resume_id = contract.get("resume_task_id")
        if resume_id and not _valid_generation(contract.get("generation")):
            raise ValueError("resuming a session requires an integer generation")
        launcher = self.launcher_factory(contract, Path(worktree))
        if resume_id:
            launcher.resume(resume_id, str(contract.get("rework_message") or ""))
            task_id = resume_id
        else:
            task_id = launcher.launch(role, node_id, Path(contract["contract_path"]))["task_id"]
        return self._register(launcher, contract, task_id)

    def adopt(self, contract: dict, worktree: Path, task_id: str) -> RunHandle:
        """Rebind an existing session by its persisted full UUID (after a restart)."""
        if not isinstance(task_id, str) or not _UUID.fullmatch(task_id):
            raise ValueError("adopt requires the full lowercase session UUID")
        launcher = self.launcher_factory(contract, Path(worktree))
        return self._register(launcher, contract, task_id)

    def _session(self, handle: RunHandle) -> Dict[str, Any]:
        session = self._sessions.get(handle.run_id)
        if session is None:
            raise ClaudeBgUnknown(
                "run handle %s not known to this runner (supervisor restarted?); "
                "adopt the session to continue" % handle.run_id
            )
        return session

    def poll(self, handle: RunHandle) -> List[RunEvent]:
        try:
            session = self._session(handle)
        except ClaudeBgUnknown:
            return [
                RunEvent(
                    "blocked_unknown",
                    {
                        "handle_id": handle.run_id,
                        "reason": "run handle not known to this runner (supervisor restarted?); adopt the session to continue",
                    },
                )
            ]
        if session["delivered"]:
            return []
        claims = session["claims"]
        report = self.delivery_reader(dict(claims))
        if report:
            session["delivered"] = True
            return [RunEvent("delivered", dict(claims, payload=report["payload"]))]
        try:
            state = session["launcher"].status(claims["task_id"])
        except ClaudeBgUnknown as error:
            return [RunEvent("blocked_unknown", dict(claims, reason=error.reason))]
        if state == "working":
            return []
        reasons = {
            "done": ("blocked_unknown", "session done without worker delivery"),
            "blocked": ("needs_attention", "session blocked, likely waiting for input"),
            "failed": ("failed_retryable", "session failed"),
            "stopped": ("stopped", "session stopped"),
        }
        event, reason = reasons[state]
        return [RunEvent(event, dict(claims, reason=reason))]

    def stop(self, handle: RunHandle) -> None:
        session = self._session(handle)
        session["launcher"].stop(session["claims"]["task_id"])

    def resume(self, handle: RunHandle, message: str, generation: int) -> None:
        session = self._session(handle)
        claims = session["claims"]
        current = claims.get("generation")
        if not _valid_generation(generation) or (
            _valid_generation(current) and generation <= current
        ):
            raise ValueError("resume generation must be an integer greater than %r" % (current,))
        session["launcher"].resume(claims["task_id"], message)
        claims["generation"] = generation
        session["delivered"] = False
