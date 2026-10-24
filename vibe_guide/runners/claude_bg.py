"""Supervisor runner over Claude Code background sessions.

Completion is decided only by the worker's own delivery report; a session that
reaches ``done`` without one is ``blocked_unknown``, never success.
"""

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..adapters.claude_bg import ClaudeBgLauncher, ClaudeBgUnknown
from ..contracts import RunEvent, RunHandle, Runner
from ..paths import ProjectPaths
from ..state import validate_run_id
from .provider_action import RESULT_EVENT_KEYS


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HANDLE = re.compile(r"claude-bg-[A-Za-z0-9._-]+")
# What a restarted runner needs to rebuild the launcher for a session.
_LAUNCHER_KEYS = ("node_id", "role", "run_id", "generation", "host", "branch",
                  "status_file", "handoff_file", "child_binding")
#: States a superseded session may already rest in; none of them writes.
#: ``blocked`` is alive but waiting, so it cannot be writing either -- the
#: same set ``ClaudeBgLauncher.stop`` accepts as proof the session ended.
_RETIRED_STATES = frozenset({"stopped", "done", "failed", "blocked"})
#: ``claude stop`` returns before ``claude agents`` shows the session down;
#: resuming or launching inside that window starts a copy (live).  Wait
#: through these delays, then refuse to add a second writer.
_RETIRE_SETTLE_SECONDS = (1, 2, 3, 5, 8)


def _valid_generation(value):
    return isinstance(value, int) and not isinstance(value, bool)


class ClaudeBgRunner(Runner):
    def __init__(
        self,
        launcher_factory: Callable[[dict, Path], ClaudeBgLauncher],
        delivery_reader: Optional[Callable[[dict], Optional[dict]]] = None,
        paths: Optional[ProjectPaths] = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.launcher_factory = launcher_factory
        self._paths = paths
        self._sleeper = sleeper
        # An injected reader is a test seam with no archive behind it.
        self._consumes_archive = delivery_reader is None
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
        if not isinstance(value, dict) or not isinstance(value.get("payload"), dict) or any(
            value.get(field) != claims.get(key)
            for field, key in (("run_id", "run_id"), ("issue_id", "node_id"),
                               ("role", "role"), ("generation", "generation"))
        ):
            return None
        return value

    def _take_delivery(self, claims: dict) -> bool:
        """Mark the archive handed to the monitor, as a visible ``wait`` does.

        A later identical ``worker-deliver`` is then a duplicate and the
        heartbeat stops counting the report; an archive another consumer
        already took is not this session's to deliver.
        """
        if not self._consumes_archive:
            return True
        from ..adapters.task_provider import ProviderActionStore

        try:
            return ProviderActionStore(self._paths or ProjectPaths.from_cwd(Path.cwd())) \
                .consume_background_delivery(claims.get("run_id"), claims["node_id"],
                                             claims["role"], claims.get("generation"),
                                             claims.get("task_id"))
        except (OSError, ValueError):
            return False

    def _register(self, launcher, contract: dict, session_id: str,
                  worktree: Path, handle_id: Optional[str] = None,
                  stale: bool = False) -> RunHandle:
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
            # Set when a newer dispatch for the same node replaced this
            # session: a superseded handle must never emit an event again
            # (the monitor would read it as an unregistered handle).
            "stale": stale,
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
        record = {key: session[key] for key in ("contract", "worktree", "claims", "stale")}
        # .vibe never stores absolute user paths: keep project worktrees relative.
        root = (self._paths or ProjectPaths.from_cwd(Path.cwd())).root
        try:
            record["worktree"] = Path(session["worktree"]).relative_to(root).as_posix()
        except ValueError:
            pass
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
            stored = record["contract"]
            # The record must describe the session its name and its own
            # contract say it does; the handle name embeds the node.
            if (claims.get("role") not in ("developer", "reviewer")
                    or not _valid_generation(claims.get("generation"))
                    or not isinstance(claims.get("node_id"), str)
                    or not handle_id.startswith("claude-bg-{}-".format(claims["node_id"]))
                    or any(stored.get(key) != claims.get(key)
                           for key in ("node_id", "role", "run_id"))):
                return None
            validate_run_id(claims.get("run_id"))
            contract = dict(stored, task_id=claims["task_id"],
                            generation=claims.get("generation"))
            worktree = Path(record["worktree"])
            if not worktree.is_absolute():
                worktree = (self._paths or ProjectPaths.from_cwd(Path.cwd())).root / worktree
        except (ValueError, OSError, UnicodeDecodeError, KeyError, TypeError):
            return None
        launcher = self.launcher_factory(contract, worktree)
        self._register(launcher, contract, session_id, worktree,
                       handle_id=handle_id, stale=bool(record.get("stale")))
        return self._sessions[handle_id]

    def _prior_session_handles(self, node_id: str) -> List[str]:
        """Persisted session records that claim this node.

        A restarted runner's memory is empty, so the durable records under
        ``claude-bg-sessions/`` are the only list of sessions a previous
        dispatch created; the in-process case scans the same place.
        """
        paths = self._paths or ProjectPaths.from_cwd(Path.cwd())
        directory = paths.resolve_vibe_path("provider-actions/claude-bg-sessions")
        try:
            names = sorted(item.name for item in directory.iterdir())
        except OSError:
            return []
        prefix = "claude-bg-{}-".format(node_id)
        found = []
        for name in names:
            if not name.endswith(".json"):
                continue
            handle_id = name[:-len(".json")]
            if not _HANDLE.fullmatch(handle_id):
                continue
            if handle_id.startswith(prefix):
                found.append(handle_id)
                continue
            # A record whose handle embeds another node belongs to us only if
            # its claims say so; adoption re-validates either way.
            try:
                record = json.loads((directory / name).read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            claims = record.get("claims") if isinstance(record, dict) else None
            if isinstance(claims, dict) and claims.get("node_id") == node_id:
                found.append(handle_id)
        return found

    def _retire_session(self, session: Dict[str, Any], handle_id: str) -> None:
        """Prove one prior session can no longer write before it is replaced.

        A session already resting needs no repeated stop; a ``working`` one
        is stopped and re-observed until it lands in a resting state.  A stop
        that fails, a session that cannot be reached, or one that is still
        ``working`` after the settle window raises ``ClaudeBgUnknown`` so the
        dispatch falls back to a retry instead of adding a second writer to
        the same worktree (run-64c826b5 ended with three).
        """
        session_id = session["claims"]["session_id"]
        launcher = session["launcher"]
        if launcher.status(session_id) in _RETIRED_STATES:
            return
        launcher.stop(session_id)
        for delay in _RETIRE_SETTLE_SECONDS:
            self._sleeper(delay)
            state = launcher.status(session_id)
            if state in _RETIRED_STATES:
                return
        raise ClaudeBgUnknown(
            "prior session %s did not reach a resting state after stop "
            "(last seen %s); not dispatching a second writer beside it"
            % (session_id, state)
        )

    def _retire_prior_sessions(self, contract: dict,
                               keep_session_id: Optional[str] = None) -> None:
        """Retire every earlier session of this node before a new dispatch.

        The session being resumed (``keep_session_id``) is not stopped here --
        ``launcher.resume`` runs its own verified stop -- but its old handle
        is still marked stale so it is never polled or reported again.
        """
        node_id = contract["node_id"]
        retired = []
        for handle_id in self._prior_session_handles(node_id):
            session = self._adopt_persisted(handle_id)
            if session is None or session["claims"]["node_id"] != node_id:
                # A record that names this node but cannot be validated
                # cannot prove its session ended; dispatching beside it may
                # add a second writer, so the start must wait and retry.
                raise ClaudeBgUnknown(
                    "prior session record %s for node %s cannot be verified; "
                    "not dispatching another writer" % (handle_id, node_id)
                )
            if session["stale"]:
                continue
            if session["claims"]["session_id"] != keep_session_id:
                self._retire_session(session, handle_id)
            retired.append(handle_id)
        for handle_id in retired:
            self._sessions[handle_id]["stale"] = True
            self._persist(handle_id)

    def start(self, contract: dict, worktree: Path) -> RunHandle:
        node_id = contract["node_id"]
        role = contract.get("role", "developer")
        resume_id = contract.get("resume_task_id")
        if resume_id and not _valid_generation(contract.get("generation")):
            raise ValueError("resuming a session requires an integer generation")
        launcher = self.launcher_factory(contract, Path(worktree))
        if resume_id:
            # Resume runs its own verified stop on the target session; every
            # other session this node left behind must be proven retired
            # first so the continuation never writes beside it.
            self._retire_prior_sessions(contract, keep_session_id=resume_id)
            launcher.resume(resume_id, str(contract.get("rework_message") or ""))
            task_id = resume_id
        else:
            if contract.get("continuation") or str(contract.get("phase")) == "rework":
                # A rework/continuation dispatch starts a fresh session in
                # the same node worktree; any earlier session still working
                # there would be a second live writer.
                self._retire_prior_sessions(contract)
            contract_path = contract.get("contract_path") or self._write_prompt(contract)
            task_id = launcher.launch(role, node_id, Path(contract_path))["task_id"]
            try:
                return self._register(launcher, contract, task_id, Path(worktree))
            except (OSError, ValueError, TypeError) as error:
                # No record means no restart can find this session, and the
                # monitor retries a failed start: stop it so a retry never
                # runs a second writer beside it.
                try:
                    launcher.stop(task_id)
                except Exception as stop_error:
                    # A ValueError is a contract error to the monitor, never
                    # retried; the session may still be writing.
                    raise ValueError(
                        "background session {} started but its record could not be "
                        "written and stopping it failed ({}); stop it before "
                        "dispatching this node again".format(task_id, type(stop_error).__name__)
                    ) from error
                raise
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
        claims = session["claims"]
        if session["stale"]:
            # A superseded handle is never polled for events again: whatever
            # it could still report belongs to a session a newer dispatch
            # already replaced.
            return []
        report = self.delivery_reader(dict(claims))
        if session["delivered"] and not (
            # A corrected re-report rewrites the archive unconsumed; take it
            # like the next visible wait would, never the one already handed.
            self._consumes_archive and report and report.get("consumed_by") is None
        ):
            return []
        if report:
            if not self._take_delivery(claims):
                return [RunEvent("blocked_unknown", dict(
                    claims, reason="worker delivery archive was taken by another consumer"))]
            session["delivered"] = True
            # Same projection as a visible wait result: the monitor reads the
            # report's fields at the top level, and the runner's claims always
            # win over anything the worker wrote.
            payload = report["payload"]
            data = {key: payload[key] for key in RESULT_EVENT_KEYS if key in payload}
            data.update(claims)
            return [RunEvent(str(payload.get("event") or "delivered"), data)]
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
