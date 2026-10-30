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
LEASE_HELD_MESSAGE = "supervisor lease is held by another process"
_ERROR_ESCALATION_STREAK = 5
_ESCALATE_MARK = "VIBE_ESCALATE:"
_REPAIRS_BEFORE_ESCALATION = 2
#: Longest silence before the product manager gets a plain-language summary of
#: what the system could not fix; a config may only shorten it.
MAX_UNRESOLVED_REPORT_SECONDS = 6 * 3600
UNRESOLVED_REPORT_KIND = "unresolved_report"


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
    #: seconds an unresolved engineering item may wait before the product
    #: manager is told in plain language; never longer than six hours
    unresolved_report_seconds: float = MAX_UNRESOLVED_REPORT_SECONDS
    executable: Optional[str] = None
    extra_args: List[str] = field(default_factory=list)

    def validate(self) -> "DaemonConfig":
        for name in ("interval", "heal_interval", "stale_seconds", "session_timeout"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError("daemon config %s must be a non-negative number" % name)
        value = self.unresolved_report_seconds
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not 0 < value <= MAX_UNRESOLVED_REPORT_SECONDS:
            raise ValueError("daemon config unresolved_report_seconds must be in (0, %d]"
                             % MAX_UNRESOLVED_REPORT_SECONDS)
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
        with open(str(inbox), "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
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
        self._clear_unresolved("daemon:daemon_errors", "daemon:daemon_gate_blocked")  # left by an earlier process
        outcome = {"status": "stopped", "reason": "stop requested"}
        lost = False
        try:
            while not self._stop:
                blocked = self._check_gate()
                if blocked is not None:
                    outcome = {"status": "blocked", "reason": blocked["reason"],
                               "run_status": blocked.get("status")}
                    self._run_status = blocked.get("status") or self._run_status
                    summary = "守护程序已暂停推进：%s" % blocked["reason"]
                    if blocked.get("status") == "blocked_design":
                        # An invalidated authorization only the product manager can renew.
                        self._escalate("", "daemon_gate_blocked", summary, "external_authorization")
                    else:
                        self._unresolved("", "daemon_gate_blocked", summary)
                        state = self._heal_state()
                        if self._report_unresolved(None, state, final=True):
                            self._save_heal_state(state)
                    break
                self.tick()
                if self._run_status in TERMINAL_STATUSES:
                    outcome = {"status": "terminal", "run_status": self._run_status}
                    break
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
            snapshot = self.supervisor.run_once()
            if self._error_streak >= _ERROR_ESCALATION_STREAK:
                self._clear_unresolved("daemon:daemon_errors")  # the daemon recovered
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

    def _record_error(self, error: BaseException, state: Optional[Dict[str, Any]] = None) -> None:
        self._error_streak += 1
        self._last_error = {"type": type(error).__name__, "reason": str(error)[:200], "at": self.clock()}
        if self._error_streak == _ERROR_ESCALATION_STREAK:
            self._unresolved("", "daemon_errors",
                             "守护程序连续 %d 次推进失败：%s" % (self._error_streak, self._last_error["reason"]),
                             state=state)

    def _clear_unresolved(self, *keys: str) -> None:
        state = self._heal_state()
        record = state.get("unresolved") or {}
        if any(key in record for key in keys):
            for key in keys:
                record.pop(key, None)
            self._save_heal_state(state)

    # -- self-heal ---------------------------------------------------------
    def _fingerprint(self, snapshot, node_id: str) -> str:
        return _node_fingerprint(snapshot, node_id)

    def heal_pass(self, snapshot) -> None:
        try:
            report = self._assess()
        except (OSError, ValueError, TypeError, KeyError) as error:
            self._record_error(error)
            return
        if not isinstance(report, dict) or report.get("status") != "ok":
            return
        state = self._heal_state()
        ran_session = False
        if state.get("unresolved"):
            state["unresolved"] = live_unresolved(state, snapshot)
        for node_id, entry in sorted((report.get("nodes") or {}).items()):
            fingerprint = self._fingerprint(snapshot, node_id)
            node_state = state["nodes"].get(node_id)
            if not node_state or node_state.get("fingerprint") != fingerprint:
                node_state = {"fingerprint": fingerprint, "retried": False, "repairs": 0, "escalated": False}
                state["nodes"][node_id] = node_state
                (state.get("unresolved") or {}).pop(node_id, None)  # the node moved: no longer stuck
            if node_state["escalated"]:
                continue
            if entry.get("tier") == stall.TIER_ESCALATE or node_state["repairs"] >= _REPAIRS_BEFORE_ESCALATION:
                self._unresolved(node_id, "self_heal_no_progress",
                                 "节点 %s 空转，修复 %d 次仍未恢复" % (node_id, node_state["repairs"]),
                                 state=state, entry=entry)
                continue
            if not node_state["retried"]:
                node_state["retried"] = True
                self._event("daemon_heal", node_id=node_id, kind="auto_retry", phase="level1")
                try:
                    self._retry(node_id)
                except Exception as error:  # a retry is best effort; the next pass decides
                    self._record_error(error, state)
                continue
            if ran_session:
                continue  # at most one short session per run at a time
            ran_session = True
            self._level_two(state, node_state, node_id, entry, snapshot)
        self._save_heal_state(state)

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
            self._unresolved(node_id, "budget_exhausted",
                             "节点 %s 空转，短会话预算已用尽（%d 次 / %d token）" % (node_id, state["sessions"], state["tokens"]),
                             state=state, entry=entry)
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
            self._unresolved(node_id, "short_session_unknown",
                             "节点 %s 的修复短会话结果未知（%s）" % (node_id, result.reason or result.status),
                             state=state, entry=entry)
            return
        text = result.text or ""
        mark = next((line for line in reversed(text.splitlines()) if _ESCALATE_MARK in line), None)
        if mark is not None:
            reason = mark.split(_ESCALATE_MARK, 1)[1].strip()[:300]
            category = _escalate_category(reason)
            if category:
                self._escalate(node_id, "needs_decision", "节点 %s 的修复需要你来定：%s" % (node_id, reason),
                               category, state=state, entry=entry)
            else:
                self._unresolved(node_id, "needs_decision", "节点 %s 的修复会话无法判定：%s" % (node_id, reason),
                                 state=state, entry=entry)
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
    def _unresolved(self, node_id: str, code: str, summary: str, *,
                    state: Optional[Dict[str, Any]] = None, entry: Optional[Mapping[str, Any]] = None,
                    ) -> None:
        """An engineering failure the system could not fix: record it, never ask.

        No decision card and no push: the product manager cannot answer an
        engineering question.  The item waits in the heal state; the final report
        and the periodic plain-language summary list it, and the node is not
        given another repair session.
        """
        own_state = state is None
        state = state if state is not None else self._heal_state()
        key = node_id or "daemon:" + code
        record = state.setdefault("unresolved", {})
        if key in record:
            return
        monitor_nodes = getattr(getattr(self.supervisor, "monitor", None), "nodes", None) or {}
        contract = getattr(monitor_nodes.get(node_id), "contract", None) if node_id else None
        example = str(contract.get("acceptance_example") or "").strip() if isinstance(contract, Mapping) else ""
        record[key] = {"code": code, "summary": summary[:300], "at": self.clock(), "example": example,
                       "repairs": (state["nodes"].get(node_id) or {}).get("repairs", 0) if node_id else 0}
        if node_id:
            state["nodes"].setdefault(node_id, {})["escalated"] = True
        self._event("daemon_heal", node_id=node_id or "daemon", kind="engineering_unresolved", phase="level3",
                    reason_code=code, reason=summary[:200])
        if own_state:
            self._save_heal_state(state)

    def _escalate(self, node_id: str, code: str, summary: str, category: str, *,
                  state: Optional[Dict[str, Any]] = None, entry: Optional[Mapping[str, Any]] = None,
                  ) -> None:
        """A product or authorization boundary the system must not cross: ask once."""
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
                options=[{"id": "keep_trying", "label": "再给系统一轮", "impact": "范围不变，系统再试一次"},
                         {"id": "pm_takes_over", "label": "我来处理", "impact": "守护程序不再动这个节点，由你决定下一步"}],
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

    def _report_unresolved(self, snapshot, state: Dict[str, Any], *, final: bool) -> bool:
        """Tell the product manager once per set of unresolved items; reads disk only."""
        items = live_unresolved(state, snapshot)
        if not items:
            return False
        signature = "unresolved:" + ",".join(
            sorted("%s=%s@%s" % (key, item.get("code"), item.get("at")) for key, item in items.items()))
        if signature in state["pushed"]:
            return False
        if not final:
            oldest = min(item.get("at", self.clock()) for item in items.values())
            if self.clock() - oldest < self.config.unresolved_report_seconds:
                return False
        text = "运行 %s 有 %d 项系统没能自动解决：\n\n%s" % (
            self.run_id, len(items), "\n\n".join(unresolved_lines(items)))
        self._push(text, UNRESOLVED_REPORT_KIND)
        state["pushed"].append(signature)
        return True

    def _push_pending(self, snapshot) -> None:
        """Report completion and unanswered decisions once each."""
        state = self._heal_state()
        changed = self._report_unresolved(snapshot, state, final=self._run_status in TERMINAL_STATUSES)
        status = self._run_status
        if status in TERMINAL_STATUSES and ("run:" + status) not in state["pushed"]:
            text = "运行 %s 已结束：%s" % (self.run_id, status)
            if status != "complete":
                text = "运行 %s 需要你拍板或处理：%s" % (self.run_id, status)
            self._push(text, "run_" + status)
            state["pushed"].append("run:" + status)
            changed = True
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


UNRESOLVED_OPTIONS = ("调整这条验收（改变产品范围，需要你确认）",
                      "接受缺口后继续（该功能不随本次上线）",
                      "再给系统一轮")


def _escalate_category(reason: str) -> Optional[str]:
    """Decision category named by the reason's fixed prefix; None means engineering."""
    head = reason.lstrip()
    if head.startswith("产品决策"):
        return "product_acceptance"
    if head.startswith(("授权", "外部权限")):
        return "external_authorization"
    return None


def _node_fingerprint(snapshot, node_id: str) -> str:
    try:
        return stall.node_fingerprint(snapshot.nodes[node_id], snapshot.tasks, node_id)
    except (AttributeError, KeyError, TypeError):
        return ""


def live_unresolved(state: Mapping[str, Any], snapshot=None) -> Dict[str, Any]:
    """Items still stuck: a node that was accepted or moved on is no longer one."""
    nodes = getattr(snapshot, "nodes", None) or {}
    live = {}
    for key, item in (state.get("unresolved") or {}).items():
        if snapshot is not None and not key.startswith("daemon:"):
            node = nodes.get(key)
            if isinstance(node, Mapping) and node.get("status") == "accepted":
                continue
            stored = ((state.get("nodes") or {}).get(key) or {}).get("fingerprint")
            current = _node_fingerprint(snapshot, key)
            if stored and current and stored != current:
                continue
        live[key] = item
    return live


def unresolved_lines(items: Mapping[str, Any]) -> List[str]:
    """Product-language items for every engineering failure the system could not fix."""
    lines = []
    for key, item in sorted(items.items()):
        run_level = key.startswith("daemon:")
        lines.append("\n".join([
            "受影响的验收例子：%s" % ("整个运行的推进（守护程序层面，不对应某一条验收例子）" if run_level
                              else item.get("example") or "（该节点未写验收例子）"),
            "现在的情况：已多次尝试仍未达成",
            "你可以选择：",
            *["  - " + option for option in UNRESOLVED_OPTIONS],
            "（展开：%s，原因码 %s）" % (
                "守护程序" if run_level else "节点 %s，已试 %s 次" % (key, item.get("repairs", 0)), item.get("code"))]))
    return lines


def unresolved_report(paths, run_id: str, snapshot=None) -> List[str]:
    """Read-only view of the same list the push carries, for ``vibe status``."""
    state = _run_record(_file(paths, run_id, HEAL_STATE_FILE)) or {}
    return unresolved_lines(live_unresolved(state, snapshot))


def _pid_marker_alive(marker: Mapping[str, Any]) -> bool:
    return pid_alive(marker.get("pid"))
