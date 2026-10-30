"""Local daemon and three-level self-heal for one authorized run.

Level 0 is mechanical: a detached local process holds the supervisor lease and
ticks ``Supervisor.run_once`` (resume, collect deliveries, dispatch, accept).
No model is involved.  Level 1 re-runs a tick for a node that went idle.  Level 2
starts one short agent session with the heal pack; the session must end with
``vibe heal-verify`` and the daemon re-checks it itself.  Level 3 hands the node
to the product manager: a decision card plus a push to the registered session.

The daemon never acts as a second writer: it only runs while it holds the lease
and stops the moment the lease is lost.  What it cannot verify (a push channel,
a platform's one-shot command) is reported as unverified, never as success.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Mapping, Optional

from . import fsio, short_session, stall
from .contracts import RunEvent
from .state import append_event, load_events, load_snapshot, run_dir

DAEMON_FILE = "daemon.json"
HEAL_STATE_FILE = "heal-state.json"
PM_ADDRESS_FILE = "pm-address.json"
PM_INBOX_FILE = "pm-inbox.jsonl"
DAEMON_LOG_FILE = "daemon.log"

TERMINAL_STATUSES = frozenset({"complete", "failed", "blocked_design", "stopped"})
#: closed out only once parked: no node is moving and every blocked node reached
#: the product manager (or the grace period passed)
PARKED_STATUS = "blocked_unknown"
#: node statuses that mean the next tick may still dispatch, poll or accept work
ACTIVE_NODE_STATUSES = frozenset({
    "ready", "start_pending", "running", "review", "rework", "delivered", "retry_pending", "brief_pending",
})
LEASE_HELD_MESSAGE = "supervisor lease is held by another process"
_ERROR_ESCALATION_STREAK = 5
_ESCALATE_MARK = "VIBE_ESCALATE:"
_REPAIRS_BEFORE_ESCALATION = 2
_ARCHIVE_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# configuration

@dataclass
class DaemonConfig:
    interval: float = 20.0
    #: seconds between self-heal passes (stall assessment counts one beat per pass)
    heal_interval: float = 120.0
    #: a heartbeat older than this reads as inactive
    stale_seconds: float = 120.0
    platform: str = short_session.PLATFORM_CLAUDE_CODE
    model: Optional[str] = None
    session_timeout: float = short_session.DEFAULT_TIMEOUT_SECONDS
    max_sessions: int = 6
    max_tokens: int = 500000
    cache_zero_limit: int = 3
    #: seconds a run may sit parked in blocked_unknown (no node moving) before the
    #: daemon closes it out even if the stall ladder has not escalated every node
    blocked_grace: float = 1800.0
    executable: Optional[str] = None
    extra_args: List[str] = field(default_factory=list)

    def validate(self) -> "DaemonConfig":
        for name in ("interval", "heal_interval", "stale_seconds", "session_timeout", "blocked_grace"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError("daemon config %s must be a non-negative number" % name)
        for name in ("max_sessions", "max_tokens", "cache_zero_limit"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("daemon config %s must be a non-negative integer" % name)
        if self.platform not in short_session.SUPPORTED_PLATFORMS:
            raise ValueError("daemon config platform is not supported: %s" % self.platform)
        if not isinstance(self.extra_args, list) or not all(isinstance(item, str) for item in self.extra_args):
            raise ValueError("daemon config extra_args must be a list of strings")
        return self


#: fields that choose which program this machine runs; only the user-level
#: ``$VIBE_HOME/config.json`` may set them, never a project file.
_MACHINE_ONLY_FIELDS = ("executable", "extra_args")


def _daemon_section(read: Callable[[], str]) -> Dict[str, Any]:
    try:
        data = json.loads(read())
    except (OSError, ValueError):
        return {}
    section = data.get("daemon") if isinstance(data, dict) else None
    return dict(section) if isinstance(section, dict) else {}


def load_config(paths, overrides: Optional[Mapping[str, Any]] = None) -> DaemonConfig:
    """Defaults, then ``$VIBE_HOME`` and project ``daemon`` objects, then overrides.

    ``executable`` and ``extra_args`` are accepted from ``$VIBE_HOME/config.json``
    only: a project file that names them is refused, matching ``PmNotifier``.
    """
    values: Dict[str, Any] = {}
    home = _daemon_section(lambda: paths.resolve_vibe_home_path("config.json").read_text(encoding="utf-8"))
    project = _daemon_section(lambda: paths.resolve_vibe_path("config.json").read_text(encoding="utf-8"))
    forbidden = [name for name in _MACHINE_ONLY_FIELDS if name in project]
    if forbidden:
        raise ValueError("daemon config %s may only be set in the user-level VIBE_HOME config"
                         % ", ".join(forbidden))
    values.update(home)
    values.update(project)
    values.update({key: value for key, value in (overrides or {}).items() if value is not None})
    known = set(DaemonConfig.__dataclass_fields__)
    unknown = sorted(set(values) - known)
    if unknown:
        raise ValueError("unknown daemon config field: %s" % ", ".join(unknown))
    return DaemonConfig(**values).validate()


# ---------------------------------------------------------------------------
# run-directory records

def _file(paths, run_id: str, name: str, create: bool = False) -> Path:
    return run_dir(paths, run_id, create=create) / name


def _run_record(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = fsio.read_json(path)
    except (fsio.JsonMissingError, fsio.JsonCorruptError):
        return None
    return value if isinstance(value, dict) else None


def _save_run_record(path: Path, value: Mapping[str, Any]) -> None:
    fsio.atomic_write_bytes(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def pid_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lease_view(paths, run_id: str, pid: Any) -> Dict[str, Any]:
    from .supervisor import SupervisorLeaseConflict, _read_lease_record, _record_epoch
    try:
        record = _read_lease_record(_file(paths, run_id, "supervisor.lease"))
    except SupervisorLeaseConflict as error:
        return {"state": "unreadable", "reason": str(error)[:160]}
    if record is None:
        return {"state": "none"}
    held = not record.get("released") and record.get("pid") == pid
    return {"state": "held" if held else "not_held", "epoch": _record_epoch(record),
            "holder_pid": record.get("pid")}


def daemon_status(paths, run_id: str, *, now: Optional[float] = None,
                  alive: Callable[[Any], bool] = pid_alive) -> Dict[str, Any]:
    """Read-only: is the daemon of this run up, and if not, why."""
    now = time.time() if now is None else now
    base = {"command": "daemon", "action": "status", "run_id": run_id}
    record = _run_record(_file(paths, run_id, DAEMON_FILE))
    if record is None:
        return {**base, "status": "not_started", "reason": "no daemon record for this run"}
    pid = record.get("pid")
    heartbeat = record.get("heartbeat")
    age = round(now - heartbeat, 1) if isinstance(heartbeat, (int, float)) else None
    view = {**base, "pid": pid, "heartbeat_age": age, "cycles": record.get("cycles", 0),
            "run_status": record.get("run_status"), "last_error": record.get("last_error"),
            "lease": _lease_view(paths, run_id, pid)}
    heal = _run_record(_file(paths, run_id, HEAL_STATE_FILE)) or {}
    view["heal"] = {key: heal.get(key) for key in ("sessions", "tokens", "cache_zero_streak")}
    state = record.get("state")
    if state != "running":
        return {**view, "status": "inactive", "reason": record.get("reason") or str(state)}
    if not alive(pid):
        return {**view, "status": "inactive", "reason": "process_not_alive"}
    if age is None or age > float(record.get("stale_seconds") or 120.0):
        return {**view, "status": "inactive", "reason": "heartbeat_stale"}
    if view["lease"]["state"] == "not_held":
        return {**view, "status": "inactive", "reason": "lease_lost"}
    return {**view, "status": "running", "reason": ""}


def register_pm_address(paths, run_id: str, provider: str, session_name: str) -> Dict[str, Any]:
    if not (isinstance(provider, str) and provider.strip() and isinstance(session_name, str) and session_name.strip()):
        raise ValueError("provider and session name are required")
    record = {"provider": provider.strip(), "session_name": session_name.strip(), "registered_at": time.time()}
    _save_run_record(_file(paths, run_id, PM_ADDRESS_FILE, create=True), record)
    return record


def start_daemon(paths, run_id: str, plan_id: str, *, interval: Optional[float] = None,
                 model: Optional[str] = None, provider: Optional[str] = None,
                 spawn: Optional[Callable[..., Any]] = None, wait: float = 5.0) -> Dict[str, Any]:
    """Start the detached daemon unless one is already up for this run."""
    current = daemon_status(paths, run_id)
    if current["status"] == "running":
        return {**current, "status": "already_running"}
    directory = run_dir(paths, run_id, create=True)
    command = [sys.executable, "-m", "vibe_guide", "daemon", "run", "--plan", plan_id, "--run-id", run_id]
    if interval is not None:
        command += ["--interval", str(interval)]
    if model:
        command += ["--model", model]
    if provider:
        command += ["--provider", provider]
    spawn = spawn or subprocess.Popen
    with open(str(directory / DAEMON_LOG_FILE), "ab") as log:
        process = spawn(command, cwd=str(paths.root), stdin=subprocess.DEVNULL,
                        stdout=log, stderr=log, start_new_session=True)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        status = daemon_status(paths, run_id)
        if status["status"] == "running" and status.get("pid") == getattr(process, "pid", None):
            return {**status, "status": "started"}
        if getattr(process, "poll", None) and process.poll() is not None:
            break
        time.sleep(0.1)
    status = daemon_status(paths, run_id)
    if status["status"] == "running":
        return {**status, "status": "started"}
    return {**status, "status": "start_unknown",
            "reason": status.get("reason") or "daemon did not report running yet; see %s" % DAEMON_LOG_FILE}


def stop_daemon(paths, run_id: str, *, wait: float = 10.0,
                alive: Callable[[Any], bool] = pid_alive,
                kill: Callable[[int, int], None] = os.kill) -> Dict[str, Any]:
    base = {"command": "daemon", "action": "stop", "run_id": run_id}
    record = _run_record(_file(paths, run_id, DAEMON_FILE))
    if record is None or record.get("state") != "running" or not alive(record.get("pid")):
        return {**base, "status": "not_running"}
    pid = record["pid"]
    try:
        kill(pid, signal.SIGTERM)
    except OSError as error:
        return {**base, "status": "stop_failed", "reason": type(error).__name__}
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not alive(pid):
            return {**base, "status": "stopped", "pid": pid}
        time.sleep(0.1)
    return {**base, "status": "stop_pending", "pid": pid, "reason": "process still alive after SIGTERM"}


# ---------------------------------------------------------------------------
# push to the product manager

class PmNotifier:
    """Deliver a message to the product manager's registered session.

    The message is always appended to ``pm-inbox.jsonl`` (what a heartbeat reads
    later).  A push is attempted only through a user-level command named in
    ``$VIBE_HOME/config.json`` (``daemon_push_command``: argv with ``{target}``
    and ``{message}``); a project file can never make this machine run one.
    Without it the answer is ``queued_inbox``: not pushed, and said so.
    """

    def __init__(self, paths, run_id: str, runner: Callable[..., Any] = subprocess.run):
        self.paths = paths
        self.run_id = run_id
        self.runner = runner

    def _command(self):
        try:
            data = json.loads(self.paths.resolve_vibe_home_path("config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        command = data.get("daemon_push_command") if isinstance(data, dict) else None
        if isinstance(command, list) and command and all(isinstance(item, str) for item in command):
            return command
        return None

    def __call__(self, message: str, kind: str, node_id: str = "") -> Dict[str, Any]:
        address = _run_record(_file(self.paths, self.run_id, PM_ADDRESS_FILE, create=True))
        entry = {"at": time.time(), "kind": kind, "node_id": node_id, "message": message,
                 "target": (address or {}).get("session_name")}
        inbox = _file(self.paths, self.run_id, PM_INBOX_FILE, create=True)
        try:
            with open(str(inbox), "a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as error:
            return {"status": "inbox_failed", "reason": type(error).__name__}
        if address is None:
            return {"status": "no_address", "reason": "no product-manager session registered"}
        command = self._command()
        if command is None:
            return {"status": "queued_inbox", "target": address["session_name"],
                    "reason": "no verified push channel configured; message waits in the inbox"}
        argv = [item.replace("{target}", address["session_name"]).replace("{message}", message) for item in command]
        try:
            result = self.runner(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"status": "unknown", "target": address["session_name"], "reason": type(error).__name__}
        return {"status": "sent" if result.returncode == 0 else "unknown",
                "target": address["session_name"], "returncode": result.returncode}


# ---------------------------------------------------------------------------
# daemon

class Daemon:
    def __init__(self, paths, run_id: str, supervisor, config: Optional[DaemonConfig] = None, *,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 launcher: Callable[..., Any] = short_session.launch,
                 notifier: Optional[Callable[..., Any]] = None,
                 assess: Optional[Callable[[], Dict[str, Any]]] = None,
                 verify: Optional[Callable[[str, str], Dict[str, Any]]] = None,
                 retry: Optional[Callable[[str], Any]] = None,
                 gate: Optional[Callable[[], Optional[Mapping[str, Any]]]] = None,
                 archive: Optional[Callable[[], Any]] = None,
                 sessions: Optional[Callable[[Any], Dict[str, Any]]] = None,
                 command_prefix: str = "vibe"):
        self.paths = paths
        self.run_id = run_id
        self.supervisor = supervisor
        self.config = (config or DaemonConfig()).validate()
        self.clock = clock
        self.sleep = sleep
        self.launcher = launcher
        self.notifier = notifier or PmNotifier(paths, run_id)
        self.command_prefix = command_prefix
        self._assess = assess or self._default_assess
        self._verify = verify or self._default_verify
        self._retry = retry or self._default_retry
        self._archive = archive or self._default_archive
        self._sessions = sessions or self._default_sessions
        self._snapshot = None
        #: run status being closed out; None while the run is still advancing
        self._closing: Optional[str] = None
        self._deferred_key: Optional[str] = None
        self._deferred_since: Optional[float] = None
        self._deferred_escalated = False
        self._archive_failures = 0
        self._parked_since: Optional[float] = None
        #: the same pre-resume checks the public ``resume`` runs; returns None
        #: when the run may advance, else ``{"status", "reason"}`` to stop on.
        self._gate = gate
        self._stop = False
        self._cycles = 0
        self._started_at = clock()
        self._last_error: Optional[Dict[str, Any]] = None
        self._error_streak = 0
        self._last_heal = None
        self._run_status: Optional[str] = None

    # -- defaults wired to the real supervisor -----------------------------
    def _default_assess(self):
        from .supervisor import beat_stall_report
        return beat_stall_report(self.paths, self.run_id)

    def _default_verify(self, node_id, role):
        from .supervisor import heal_verify
        return heal_verify(self.paths, self.run_id, node_id, role=role)

    def _default_archive(self):
        return self.supervisor.monitor.archive_run(self.run_id, sessions_verified=True)

    def _default_sessions(self, snapshot):
        from .live_sessions import probe_live_sessions
        return probe_live_sessions(self.paths, snapshot)

    def _default_retry(self, node_id):
        # The mechanical retry: one more resume/tick re-attaches bindings and
        # re-reads deliveries.  Provider-specific restarts are an injected hook.
        return self.supervisor.run_once()

    # -- records -----------------------------------------------------------
    def _write_state(self, state: str, reason: str = "") -> None:
        record = {"run_id": self.run_id, "pid": os.getpid(), "state": state, "reason": reason,
                  "started_at": self._started_at, "heartbeat": self.clock(), "cycles": self._cycles,
                  "run_status": self._run_status, "last_error": self._last_error,
                  "stale_seconds": max(self.config.stale_seconds, 4 * self.config.interval)}
        _save_run_record(_file(self.paths, self.run_id, DAEMON_FILE, create=True), record)

    def _heal_state(self) -> Dict[str, Any]:
        state = _run_record(_file(self.paths, self.run_id, HEAL_STATE_FILE)) or {}
        state.setdefault("sessions", 0)
        state.setdefault("tokens", 0)
        state.setdefault("cache_zero_streak", 0)
        state.setdefault("nodes", {})
        state.setdefault("pushed", [])
        return state

    def _save_heal_state(self, state: Mapping[str, Any]) -> None:
        _save_run_record(_file(self.paths, self.run_id, HEAL_STATE_FILE, create=True), state)

    def _event(self, name: str, **data: Any) -> None:
        provenance = None
        try:
            current = load_snapshot(self.paths, self.run_id)
            provenance = {"role": "system", "task_id": None, "handle_id": None, "generation": 0,
                          "authorization_digest": current.authorization_digest,
                          "node_contract_digest": current.node_contract_digest}
        except (OSError, ValueError, TypeError, KeyError):
            pass
        try:
            append_event(self.paths, RunEvent(name, {"run_id": self.run_id, **data}), provenance)
        except (OSError, ValueError) as error:
            self._last_error = {"type": type(error).__name__, "reason": "event write failed", "at": self.clock()}

    # -- the loop ----------------------------------------------------------
    def request_stop(self, *_ignored) -> None:
        self._stop = True

    def run(self, max_cycles: Optional[int] = None) -> Dict[str, Any]:
        from .supervisor import SupervisorLeaseConflict
        try:
            lease = self.supervisor.recover_or_start()
        except SupervisorLeaseConflict as error:
            self._write_state("inactive", "lease_conflict: %s" % str(error)[:120])
            return {"status": "lease_lost", "reason": str(error)[:160]}
        if not lease.get("active_supervisors"):
            self._write_state("inactive", "lease_held_by_other_process")
            return {"status": "lease_held", "reason": LEASE_HELD_MESSAGE}
        previous = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, self.request_stop)
        self._write_state("running")
        outcome = {"status": "stopped", "reason": "stop requested"}
        lost = False
        try:
            while not self._stop:
                if not self._closing:
                    blocked = self._check_gate()
                    if blocked is not None:
                        outcome = {"status": "blocked", "reason": blocked["reason"],
                                   "run_status": blocked.get("status")}
                        self._run_status = blocked.get("status") or self._run_status
                        self._escalate("", "daemon_gate_blocked",
                                       "守护程序已暂停推进：%s" % blocked["reason"], "product_acceptance")
                        break
                    self.tick()
                    self._closing = self._closeout_status()
                if self._closing:
                    # No more ticks: a finished or parked run must not be advanced
                    # again while its sessions and archive are being settled.
                    closed = self._close_out(self._closing)
                    if closed is not None:
                        outcome = closed
                        break
                    self._cycles += 1
                    self._write_state("running")
                if max_cycles is not None and self._cycles >= max_cycles:
                    outcome = {"status": "max_cycles", "cycles": self._cycles}
                    break
                self._wait(self.config.interval)
        except SupervisorLeaseConflict as error:
            lost = True
            outcome = {"status": "lease_lost", "reason": str(error)[:160]}
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        if lost:
            self._write_state("lost_lease", outcome["reason"])
        else:
            try:
                self.supervisor.lease.release(self.run_id)
            except (OSError, ValueError):
                pass
            self._write_state("exited", outcome.get("reason") or outcome["status"])
        return outcome

    # -- close-out ---------------------------------------------------------
    def _log(self, message: str) -> None:
        print("%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.clock())), message),
              file=sys.stderr, flush=True)

    def _blocked_nodes(self) -> List[str]:
        nodes = getattr(self._snapshot, "nodes", None) or {}
        return sorted(node_id for node_id, node in nodes.items()
                      if isinstance(node, dict) and node.get("status") == "blocked_unknown")

    def _parked(self) -> bool:
        """blocked_unknown with nothing left to advance, and every blocked node
        handed to the product manager (or the grace period spent)."""
        nodes = getattr(self._snapshot, "nodes", None) or {}
        blocked = self._blocked_nodes()
        if not blocked or any(isinstance(node, dict) and node.get("status") in ACTIVE_NODE_STATUSES
                              for node in nodes.values()):
            self._parked_since = None
            return False
        now = self.clock()
        if self._parked_since is None:
            self._parked_since = now
        recorded = self._heal_state()["nodes"]
        handed_over = all(recorded.get(node_id, {}).get("escalated") for node_id in blocked)
        return handed_over or now - self._parked_since >= self.config.blocked_grace

    def _closeout_status(self) -> Optional[str]:
        status = self._run_status
        if status in TERMINAL_STATUSES:
            return status
        if status == PARKED_STATUS and self._parked():
            return status
        return None

    def _announce(self, status: str, blocked: List[str]) -> bool:
        """The completion / decision message, in pm-inbox before anything is archived."""
        state = self._heal_state()
        key = "run:" + status
        if key in state["pushed"]:
            return True
        if status == "complete":
            text = "运行 %s 已结束：complete" % self.run_id
        elif blocked:
            text = "运行 %s 停在 %s，节点 %s 无法继续，需要你拍板或处理" % (self.run_id, status, "、".join(blocked))
        else:
            text = "运行 %s 需要你拍板或处理：%s" % (self.run_id, status)
        result = self._push(text, "run_" + status)
        if result.get("status") == "inbox_failed":
            self._log("pm-inbox write failed (%s); archive postponed" % result.get("reason"))
            return False
        state["pushed"].append(key)
        self._save_heal_state(state)
        return True

    def _defer(self, status: str, probe: Mapping[str, Any]) -> None:
        live = list(probe.get("live") or [])
        unknown = probe.get("unknown")
        signature = json.dumps([live, bool(unknown)])
        now = self.clock()
        if self._deferred_since is None:
            self._deferred_since = now
        if signature != self._deferred_key:
            self._deferred_key = signature
            self._event("run_archive_deferred", run_status=status, live_sessions=live,
                        reason_code="sessions_unknown" if unknown else "live_session")
            self._log("archive deferred (%s): live=%s unknown=%s" % (status, live, unknown))
        if not self._deferred_escalated and now - self._deferred_since >= self.config.blocked_grace:
            self._deferred_escalated = True
            detail = "仍有活会话 %s" % "、".join(live) if live else "无法确认会话是否已停止"
            self._escalate("", "archive_deferred", "运行 %s 已收口但%s，暂不归档" % (self.run_id, detail),
                           "product_acceptance")

    def _close_out(self, status: str) -> Optional[Dict[str, Any]]:
        """Settle a finished or parked run: message, session check, lease, archive.

        Returns the daemon outcome once the run is archived (or archiving gave
        up), ``None`` while it must be retried on the next cycle.
        """
        blocked = self._blocked_nodes() if status == PARKED_STATUS else []
        if blocked:
            state = self._heal_state()
            for node_id in blocked:
                self._escalate(node_id, "run_parked", "节点 %s 停在 blocked_unknown，无法继续" % node_id,
                               "product_acceptance", state=state)
            self._save_heal_state(state)
        if not self._announce(status, blocked):
            return None
        try:
            probe = self._sessions(self._snapshot)
        except Exception as error:  # an unverifiable session list is "unknown", never "none"
            probe = {"live": [], "unknown": "%s: %s" % (type(error).__name__, str(error)[:120])}
        if probe.get("unknown") or probe.get("live"):
            self._defer(status, probe)
            return None
        self._deferred_since = None
        try:
            self.supervisor.lease.release(self.run_id)
        except (OSError, ValueError):
            pass
        try:
            self._archive()
        except (OSError, ValueError, TypeError) as error:
            # archive_run has already left run_archive_failed on disk.
            self._archive_failures += 1
            self._log("archive failed (%d/%d): %s: %s" % (
                self._archive_failures, _ARCHIVE_ATTEMPTS, type(error).__name__, str(error)[:200]))
            if self._archive_failures >= _ARCHIVE_ATTEMPTS:
                return {"status": "archive_failed", "run_status": status, "reason": type(error).__name__}
            return None
        return {"status": "terminal", "run_status": status}

    def _check_gate(self) -> Optional[Dict[str, Any]]:
        if self._gate is None:
            return None
        try:
            result = self._gate()
        except SystemExit:
            raise
        except Exception as error:  # an unverifiable gate is a block, never a pass
            return {"status": "blocked_unknown", "reason": "gate_error: %s: %s" % (type(error).__name__, str(error)[:160])}
        return dict(result) if result else None

    def _wait(self, seconds: float) -> None:
        remaining = seconds
        while remaining > 0 and not self._stop:
            slice_ = min(1.0, remaining)
            self.sleep(slice_)
            remaining -= slice_

    def tick(self) -> None:
        from .supervisor import SupervisorLeaseConflict
        snapshot = None
        try:
            snapshot = self._run_once()
            self._snapshot = snapshot
            self._error_streak = 0
        except SupervisorLeaseConflict:
            raise
        except RuntimeError as error:
            if LEASE_HELD_MESSAGE in str(error):
                raise SupervisorLeaseConflict(str(error))
            self._record_error(error)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, PermissionError) as error:
            self._record_error(error)
        self._cycles += 1
        status = getattr(snapshot, "status", None) if snapshot is not None else None
        if isinstance(snapshot, dict):
            status = snapshot.get("status")
        if status:
            self._run_status = status
        self._write_state("running")
        if snapshot is not None:
            now = self.clock()
            if self._last_heal is None or now - self._last_heal >= self.config.heal_interval:
                self._last_heal = now
                self.heal_pass(snapshot)
        self._push_pending(snapshot)

    def _run_once(self):
        """One tick; a newer epoch still recorded under this pid is adopted once."""
        from .supervisor import SupervisorLeaseConflict
        try:
            return self.supervisor.run_once()
        except SupervisorLeaseConflict as original:
            readopt = getattr(self.supervisor.lease, "readopt", None)
            if readopt is None:
                raise
            try:
                epoch = readopt(self.run_id)
            except SupervisorLeaseConflict:
                raise original
            self._event("daemon_lease_readopted", lease_epoch=epoch)
            return self.supervisor.run_once()

    def _record_error(self, error: BaseException) -> None:
        self._error_streak += 1
        self._last_error = {"type": type(error).__name__, "reason": str(error)[:200], "at": self.clock()}
        if self._error_streak == _ERROR_ESCALATION_STREAK:
            self._escalate("", "daemon_errors",
                           "守护程序连续 %d 次推进失败：%s" % (self._error_streak, self._last_error["reason"]),
                           "product_acceptance")

    # -- self-heal ---------------------------------------------------------
    def _fingerprint(self, snapshot, node_id: str) -> str:
        try:
            return stall.node_fingerprint(snapshot.nodes[node_id], snapshot.tasks, node_id)
        except (AttributeError, KeyError, TypeError):
            return ""

    def heal_pass(self, snapshot) -> None:
        try:
            report = self._assess()
        except (OSError, ValueError, TypeError, KeyError) as error:
            self._record_error(error)
            return
        if not isinstance(report, dict) or report.get("status") != "ok":
            return
        state = self._heal_state()
        self._heal_ended_reviewers(snapshot, state)
        ran_session = False
        for node_id, entry in sorted((report.get("nodes") or {}).items()):
            fingerprint = self._fingerprint(snapshot, node_id)
            node_state = state["nodes"].get(node_id)
            if not node_state or node_state.get("fingerprint") != fingerprint:
                node_state = {"fingerprint": fingerprint, "retried": False, "repairs": 0, "escalated": False}
                state["nodes"][node_id] = node_state
            if node_state["escalated"]:
                continue
            if entry.get("tier") == stall.TIER_ESCALATE or node_state["repairs"] >= _REPAIRS_BEFORE_ESCALATION:
                self._escalate(node_id, "self_heal_no_progress",
                               "节点 %s 空转，修复 %d 次仍未恢复" % (node_id, node_state["repairs"]),
                               "product_acceptance", state=state, entry=entry)
                continue
            if not node_state["retried"]:
                node_state["retried"] = True
                self._event("daemon_heal", node_id=node_id, kind="auto_retry", phase="level1")
                try:
                    self._retry(node_id)
                except Exception as error:  # a retry is best effort; the next pass decides
                    self._record_error(error)
                continue
            if ran_session:
                continue  # at most one short session per run at a time
            ran_session = True
            self._level_two(state, node_state, node_id, entry, snapshot)
        self._save_heal_state(state)

    def _rejected_and_waiting(self, node_id: str) -> bool:
        """True when the node's latest event is an ``acceptance_rejected``.

        The reviewer notice that follows a rejection is bookkeeping, not
        progress, so it is skipped.
        """
        try:
            events = load_events(self.paths, self.run_id)
        except (OSError, ValueError, TypeError):
            return False
        for record in reversed(events):
            data = record.get("data") or {}
            if data.get("node_id") != node_id or record.get("event") == "acceptance_correction_notified":
                continue
            return record.get("event") == "acceptance_rejected"
        return False

    def _reviewer_session_state(self, snapshot, node_id: str):
        """``("gone" | "live" | "unknown", reason)`` for this node's reviewer only."""
        tasks = getattr(snapshot, "tasks", None)
        binding = tasks.get(node_id + ":reviewer") if isinstance(tasks, dict) else None
        if not isinstance(binding, dict):
            return "unknown", "reviewer binding is not readable"
        scoped = SimpleNamespace(tasks={node_id + ":reviewer": binding},
                                 nodes={node_id: snapshot.nodes[node_id]})
        try:
            probe = self._sessions(scoped)
        except Exception as error:  # a failed probe is "unknown", never a dispatch
            return "unknown", type(error).__name__
        if not isinstance(probe, dict) or probe.get("unknown") or not isinstance(probe.get("live"), list):
            return "unknown", str((probe or {}).get("unknown") or "session state unreadable")[:160] if isinstance(probe, dict) else "session state unreadable"
        return ("live", "") if probe["live"] else ("gone", "")

    def _heal_ended_reviewers(self, snapshot, state) -> None:
        """Replace a rejected reviewer whose session is gone, once per node.

        Only a node parked in ``review`` whose last event is an
        ``acceptance_rejected`` qualifies.  A live or unverifiable session is
        never replaced.  A reviewer that was already replaced and was rejected
        again goes to the product manager instead of a second replacement.
        """
        nodes = getattr(snapshot, "nodes", None)
        if not isinstance(nodes, dict):
            return
        seen = state.setdefault("reviewer_heal", {})
        for node_id, node in sorted(nodes.items()):
            if not isinstance(node, dict) or node.get("status") != "review":
                continue
            if node.get("reviewer_replacement_required") or not self._rejected_and_waiting(node_id):
                continue
            mark = seen.setdefault(node_id, {})
            if mark.get("escalated"):
                continue
            verdict, reason = self._reviewer_session_state(snapshot, node_id)
            if verdict == "unknown":
                if mark.get("unknown") != reason:
                    mark["unknown"] = reason
                    self._event("daemon_heal", node_id=node_id, kind="reviewer_replace_unknown",
                                phase="level1", reason=reason)
                continue
            mark.pop("unknown", None)
            if verdict == "live":
                continue
            if node.get("reviewer_auto_replaced") is True:
                mark["escalated"] = True
                self._escalate(node_id, "reviewer_rejected_again",
                               "节点 %s 的替换审查员再次被退回且会话已退出" % node_id,
                               "product_acceptance", state=state)
                continue
            try:
                self.supervisor.monitor.replace_ended_reviewer(node_id, self.run_id)
            except (OSError, ValueError, TypeError, KeyError) as error:
                self._record_error(error)
                continue
            self._event("daemon_heal", node_id=node_id, kind="reviewer_replace", phase="level1")

    def _prompt(self, node_id: str, entry: Mapping[str, Any], snapshot) -> str:
        from .protocols import load_protocol
        pack = json.dumps(entry.get("pack"), ensure_ascii=False, sort_keys=True)
        authorization = json.dumps(getattr(snapshot, "authorization", None), ensure_ascii=False,
                                   sort_keys=True, default=str)[:4000]
        role = (entry.get("pack") or {}).get("role") or "developer"
        role_flag = "" if role == "developer" else " --role " + role
        return "\n\n".join([
            load_protocol("heal-session"),
            "## 本次输入\n- run-id: %s\n- node-id: %s\n- 项目目录: %s" % (self.run_id, node_id, self.paths.root),
            "### 排查包\n" + pack,
            "### 监工卡与授权范围（已授权项目、已授权 DAG、非 deploy）\n" + authorization,
            "### 收尾命令\n%s heal-verify --run-id %s --node %s%s" % (self.command_prefix, self.run_id, node_id, role_flag),
        ])

    def _level_two(self, state, node_state, node_id, entry, snapshot) -> None:
        config = self.config
        if state["sessions"] >= config.max_sessions or state["tokens"] >= config.max_tokens:
            self._escalate(node_id, "budget_exhausted",
                           "节点 %s 空转，短会话预算已用尽（%d 次 / %d token）" % (node_id, state["sessions"], state["tokens"]),
                           "product_acceptance", state=state, entry=entry)
            return
        marker = state.get("active")
        if isinstance(marker, dict) and marker.get("pid") != os.getpid() and _pid_marker_alive(marker):
            return
        state["active"] = {"node_id": node_id, "pid": os.getpid(), "started_at": self.clock()}
        self._save_heal_state(state)
        before = len(load_events(self.paths, self.run_id))
        result = self.launcher(
            config.platform, self._prompt(node_id, entry, snapshot), model=config.model, cwd=str(self.paths.root),
            timeout=config.session_timeout, executable=config.executable, extra_args=config.extra_args,
            mailbox_dir=str(_file(self.paths, self.run_id, "short-session-mailbox", create=True)),
            run_id=self.run_id, node_id=node_id, poll=lambda: self._write_state("running"))
        state["active"] = None
        state["sessions"] += 1
        node_state["repairs"] += 1
        self._account_usage(state, result)
        self._event("daemon_heal", node_id=node_id, kind="short_session", phase="level2",
                    reason_code=result.status, reason=result.reason)
        if result.status in (short_session.STATUS_UNKNOWN, short_session.STATUS_QUEUED):
            # Not a success and not a verified failure: the outcome is unknown.
            self._escalate(node_id, "short_session_unknown",
                           "节点 %s 的修复短会话结果未知（%s）" % (node_id, result.reason or result.status),
                           "product_acceptance", state=state, entry=entry)
            return
        text = result.text or ""
        mark = next((line for line in reversed(text.splitlines()) if _ESCALATE_MARK in line), None)
        if mark is not None:
            reason = mark.split(_ESCALATE_MARK, 1)[1].strip()[:300]
            category = "external_authorization" if ("授权" in reason or "authoriz" in reason.lower()) else "product_acceptance"
            self._escalate(node_id, "needs_decision", "节点 %s 的修复涉及产品决策或授权：%s" % (node_id, reason),
                           category, state=state, entry=entry)
            return
        self._recheck(node_id, entry, before)

    def _recheck(self, node_id: str, entry: Mapping[str, Any], events_before: int) -> str:
        """The daemon's own verdict: the session's heal-verify if it ran one, else ours."""
        events = load_events(self.paths, self.run_id)[events_before:]
        own = [item for item in events if item.get("event") == "self_heal_attempt"
               and (item.get("data") or {}).get("node_id") == node_id]
        if own:
            outcome = (own[-1].get("data") or {}).get("outcome")
        else:
            role = (entry.get("pack") or {}).get("role") or "developer"
            outcome = "passed" if self._verify(node_id, role).get("status") == "verify_passed" else "failed"
        self._event("daemon_heal", node_id=node_id, kind="recheck", phase="level2", reason_code=str(outcome))
        return str(outcome)

    def _account_usage(self, state, result) -> None:
        usage = result.usage
        if usage is None:
            return
        state["tokens"] += usage["input"] + usage["output"] + usage["cache_creation"]
        if usage["cache_read"] == 0:
            state["cache_zero_streak"] += 1
        else:
            state["cache_zero_streak"] = 0
        limit = self.config.cache_zero_limit
        if limit and state["cache_zero_streak"] >= limit:
            self._event("daemon_heal", kind="cache_health", phase="level2", source="short_session",
                        reason_code="cache_read_zero_streak",
                        reason="cache_read=0 on %d consecutive short sessions" % state["cache_zero_streak"])

    # -- level three -------------------------------------------------------
    def _escalate(self, node_id: str, code: str, summary: str, category: str, *,
                  state: Optional[Dict[str, Any]] = None, entry: Optional[Mapping[str, Any]] = None,
                  ) -> None:
        own_state = state is None
        state = state if state is not None else self._heal_state()
        if node_id and state["nodes"].get(node_id, {}).get("escalated"):
            return
        if node_id:
            state["nodes"].setdefault(node_id, {})["escalated"] = True
        card = None
        try:
            from .decision_boundary import raise_decision
            card = raise_decision(
                self.paths, run_id=self.run_id, node_id=node_id or "daemon", category=category,
                question="%s。怎么处理？" % summary,
                options=[{"id": "keep_trying", "label": "继续让监工自愈", "impact": "不改范围，再给一轮机会"},
                         {"id": "pm_takes_over", "label": "我来处理", "impact": "守护程序不再动这个节点"}],
                recommendation="pm_takes_over",
                evidence={"reason_code": code, "pack": (entry or {}).get("pack") if entry else None})
        except (OSError, ValueError, TypeError) as error:
            self._last_error = {"type": type(error).__name__, "reason": "decision card failed: %s" % str(error)[:120],
                                "at": self.clock()}
        decision_id = ((card or {}).get("request") or {}).get("decision_id")
        if decision_id:
            state["pushed"].append("decision:" + decision_id)  # this push already names the card
        message = "【需要你处理】%s（运行 %s）。决策卡：%s" % (
            summary, self.run_id, decision_id or "未能建立，见 pm-inbox")
        push = self._push(message, "escalation", node_id)
        self._event("daemon_heal", node_id=node_id or "daemon", kind="escalated", phase="level3",
                    reason_code=code, reason=push.get("status", "unknown"))
        if own_state:
            self._save_heal_state(state)

    def _push(self, message: str, kind: str, node_id: str = "") -> Dict[str, Any]:
        try:
            result = self.notifier(message, kind, node_id)
        except Exception as error:  # a failed push is reported, never fatal
            return {"status": "unknown", "reason": type(error).__name__}
        return result if isinstance(result, dict) else {"status": "unknown"}

    def _push_pending(self, snapshot) -> None:
        """Report unanswered decisions once each (the run's own end is ``_announce``)."""
        state = self._heal_state()
        changed = False
        try:
            from .decision_boundary import decision_list
            pending = decision_list(self.paths, self.run_id)
        except (OSError, ValueError, TypeError):
            pending = []
        for item in pending:
            key = "decision:" + str(item.get("decision_id"))
            if key not in state["pushed"]:
                self._push("运行 %s 有待你拍板的决策：%s" % (self.run_id, item.get("question", "")), "decision",
                           str(item.get("node_id") or ""))
                state["pushed"].append(key)
                changed = True
        if changed:
            self._save_heal_state(state)


def _pid_marker_alive(marker: Mapping[str, Any]) -> bool:
    return pid_alive(marker.get("pid"))
