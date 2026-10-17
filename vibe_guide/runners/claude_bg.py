"""Supervisor runner over Claude Code background sessions.

Completion is decided only by the worker's own delivery report; a session that
reaches ``done`` without one is ``blocked_unknown``, never success.
"""

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..adapters.claude_bg import ClaudeBgLauncher, ClaudeBgUnknown
from ..contracts import RunEvent, RunHandle, Runner
from ..paths import ProjectPaths


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HANDLE = re.compile(r"claude-bg-[A-Za-z0-9._-]+")
# What a restarted runner needs to rebuild the launcher for a session.
_LAUNCHER_KEYS = ("node_id", "role", "run_id", "generation", "host", "branch",
                  "status_file", "handoff_file", "child_binding")


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

    def _register(self, launcher, contract: dict, session_id: str,
                  worktree: Path, handle_id: Optional[str] = None) -> RunHandle:
        if handle_id is None:
            # Unique across processes: a restarted runner must never reissue
            # a handle a persisted session record already owns.
            self._count += 1
            handle_id = "claude-bg-{}-{}-{}".format(
                contract["node_id"], self._count, uuid.uuid4().hex[:8]
            )
        handle = RunHandle(handle_id)
        self._sessions[handle.run_id] = {
            "launcher": launcher,
            # In memory only: the monitor drops a handle once it applies
            # ``delivered``, so a handle still polled after a restart means
            # that event was lost and must be emitted again.
            "delivered": False,
            "contract": {key: contract[key] for key in _LAUNCHER_KEYS if key in contract},
            "worktree": str(worktree),
            "claims": {
                "node_id": contract["node_id"],
                "role": contract.get("role", "developer"),
                # The identity the monitor registered; the real session UUID
                # travels separately so event provenance matches tasks.json.
                "task_id": contract.get("task_id") or session_id,
                "session_id": session_id,
                "handle_id": handle.run_id,
                "generation": contract.get("generation"),
                "run_id": contract.get("run_id"),
            },
        }
        self._persist(handle.run_id)
        return handle

    def _record_path(self, handle_id: str) -> Optional[Path]:
        if not _HANDLE.fullmatch(handle_id):
            return None
        paths = self._paths or ProjectPaths.from_cwd(Path.cwd())
        return paths.resolve_vibe_path("provider-actions/claude-bg-sessions/{}.json".format(handle_id))

    def _persist(self, handle_id: str) -> None:
        """Write the session record a restarted runner adopts from."""
        session = self._sessions[handle_id]
        path = self._record_path(handle_id)
        record = {key: session[key] for key in ("contract", "worktree", "claims")}
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

    def _adopt_persisted(self, handle_id: str) -> Optional[Dict[str, Any]]:
        try:
            path = self._record_path(handle_id)
            if path is None or path.is_symlink() or not path.is_file():
                return None
            record = json.loads(path.read_text(encoding="utf-8"))
            claims = record["claims"]
            session_id = claims["session_id"]
            if claims.get("handle_id") != handle_id or not _UUID.fullmatch(session_id):
                return None
            contract = dict(record["contract"], task_id=claims["task_id"],
                            generation=claims.get("generation"))
            worktree = Path(record["worktree"])
        except (ValueError, OSError, UnicodeDecodeError, KeyError, TypeError):
            return None
        launcher = self.launcher_factory(contract, worktree)
        self._register(launcher, contract, session_id, worktree, handle_id=handle_id)
        return self._sessions[handle_id]

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
            contract_path = contract.get("contract_path") or self._write_prompt(contract)
            task_id = launcher.launch(role, node_id, Path(contract_path))["task_id"]
        return self._register(launcher, contract, task_id, Path(worktree))

    def _write_prompt(self, contract: dict) -> Path:
        """Materialize the worker prompt for a monitor payload.

        The monitor hands the runner its dispatch contract (spec_path,
        worktree, files, ...), never a ``contract_path``; the launcher reads
        its prompt from a file, so write the same continuation prompt the
        visible providers send under the project's ``.vibe``.
        """
        from .provider_action import ProviderActionRunner

        paths = self._paths or ProjectPaths.from_cwd(Path.cwd())
        name = "provider-actions/prompts/{}-{}-{}-g{}.md".format(
            contract.get("run_id"), contract["node_id"],
            contract.get("role", "developer"), contract.get("generation"),
        )
        path = paths.resolve_vibe_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ProviderActionRunner._continuation_prompt(contract), encoding="utf-8")
        return path

    def adopt(self, contract: dict, worktree: Path, task_id: str) -> RunHandle:
        """Rebind an existing session by its persisted full UUID (after a restart)."""
        if not isinstance(task_id, str) or not _UUID.fullmatch(task_id):
            raise ValueError("adopt requires the full lowercase session UUID")
        launcher = self.launcher_factory(contract, Path(worktree))
        return self._register(launcher, contract, task_id, Path(worktree))

    def _session(self, handle: RunHandle) -> Dict[str, Any]:
        session = self._sessions.get(handle.run_id) or self._adopt_persisted(handle.run_id)
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
            state = session["launcher"].status(claims["session_id"])
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
        session["launcher"].stop(session["claims"]["session_id"])

    def resume(self, handle: RunHandle, message: str, generation: int) -> None:
        session = self._session(handle)
        claims = session["claims"]
        current = claims.get("generation")
        if not _valid_generation(generation) or (
            _valid_generation(current) and generation <= current
        ):
            raise ValueError("resume generation must be an integer greater than %r" % (current,))
        session["launcher"].resume(claims["session_id"], message)
        claims["generation"] = generation
        session["contract"]["generation"] = generation
        session["delivered"] = False
        self._persist(handle.run_id)
