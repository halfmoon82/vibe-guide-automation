"""Durable supervisor lease and one-cycle monitor recovery."""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

from .contracts import RunEvent
from .state import append_event, run_dir, save_snapshot, load_snapshot


class SupervisorLease:
    def __init__(self, paths, *, pid=None):
        self.paths = paths
        self.pid = int(pid or os.getpid())

    def path(self, run_id: str) -> Path:
        return run_dir(self.paths, run_id, create=True) / "supervisor.lease"

    def _payload(self, run_id: str) -> Dict[str, Any]:
        now = time.time()
        return {"pid": self.pid, "run_id": run_id, "started_at": now, "heartbeat": now}

    def acquire(self, run_id: str) -> bool:
        path = self.path(run_id)
        payload = self._payload(run_id)
        try:
            descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                pid = int(current.get("pid", 0))
                same_process = pid == self.pid
                if not same_process and pid > 0:
                    os.kill(pid, 0)
                    return False
                if same_process and current.get("run_id") == run_id:
                    return False
                path.unlink()
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                try:
                    path.unlink()
                except OSError:
                    return False
            return self.acquire(run_id)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        self._run_id = run_id
        return True

    def heartbeat(self, run_id: str) -> None:
        path = self.path(run_id)
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return
        if current.get("pid") != self.pid or current.get("run_id") != run_id:
            return
        current["heartbeat"] = time.time()
        descriptor, temporary = tempfile.mkstemp(prefix=".supervisor.", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(current, stream, sort_keys=True)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, str(path))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def release(self, run_id: str) -> None:
        path = self.path(run_id)
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if current.get("pid") == self.pid:
                path.unlink()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return


class Supervisor:
    def __init__(self, paths, monitor, runner, run_id: str, lease=None):
        self.paths = paths
        self.monitor = monitor
        self.runner = runner
        self.run_id = run_id
        self.lease = lease or SupervisorLease(paths)

    def recover_or_start(self, run_id: str = None) -> Dict[str, Any]:
        target = run_id or self.run_id
        acquired = self.lease.acquire(target)
        return {"stale_pid_recovered": acquired, "active_supervisors": 1 if acquired else 0}

    def run_once(self):
        if not self.lease.acquire(self.run_id) and not getattr(self.lease, "_run_id", None):
            raise RuntimeError("supervisor lease is held by another process")
        heartbeat_data = {"run_id": self.run_id, "status": "active"}
        heartbeat_provenance = None
        try:
            current = load_snapshot(self.paths, self.run_id)
            heartbeat_data.update({
                "authorization_digest": current.authorization_digest,
                "node_contract_digest": current.node_contract_digest,
            })
            heartbeat_provenance = {
                "role": "system",
                "task_id": None,
                "handle_id": None,
                "generation": 0,
                "authorization_digest": current.authorization_digest,
                "node_contract_digest": current.node_contract_digest,
            }
        except (FileNotFoundError, OSError, ValueError, TypeError):
            # A first-cycle heartbeat has no snapshot lineage yet; Monitor
            # start/resume will establish it before any provider write.
            pass
        append_event(self.paths, RunEvent("supervisor_heartbeat", heartbeat_data), heartbeat_provenance)
        self.lease.heartbeat(self.run_id)
        self.monitor.resume(self.run_id, self.runner, poll_handles=False)
        from .monitor import reconcile_pending_binding
        try:
            snapshot_for_reconcile = load_snapshot(self.paths, self.run_id)
        except (FileNotFoundError, ValueError, OSError):
            snapshot_for_reconcile = None
        if snapshot_for_reconcile is not None:
            for node_id in snapshot_for_reconcile.nodes:
                reconcile_pending_binding(snapshot_for_reconcile, node_id, self.runner)
            save_snapshot(self.paths, snapshot_for_reconcile)
        snapshot = self.monitor.tick(self.run_id, self.runner)
        if hasattr(snapshot, "run_id"):
            save_snapshot(self.paths, snapshot)
        return snapshot

    def run_until_terminal(self, *, interval: float = 1.0, max_cycles=None):
        """Keep supervising this run until it reaches a terminal run status.

        ``run_once`` is deliberately a single recovery cycle for callers that
        already own a scheduler.  The CLI ``--watch`` path must not return
        after that first cycle: this loop retains the lease, heartbeats it on
        every cycle, and only exits for a real terminal outcome (or an
        explicit test/integration ``max_cycles`` bound).
        """
        if interval < 0:
            raise ValueError("interval must be non-negative")
        cycles = 0
        while True:
            snapshot = self.run_once()
            cycles += 1
            status = getattr(snapshot, "status", None)
            if isinstance(snapshot, dict):
                status = snapshot.get("status")
            if status in {"complete", "failed", "blocked_design", "stopped"}:
                return snapshot
            if max_cycles is not None and cycles >= max_cycles:
                return snapshot
            if interval:
                time.sleep(interval)

    def watch(self, *, interval=20.0, max_cycles=None):
        """Keep the lease alive until a terminal outcome is confirmed.

        ``blocked_unknown`` is intentionally not terminal: it means the
        provider has not yet returned enough evidence and the next cycle must
        continue polling/recovery rather than letting the parent exit.
        """
        return self.run_until_terminal(interval=interval, max_cycles=max_cycles)


# ---------------------------------------------------------------------------
# ISSUE-91: local supervisor preflight, address registry, and rotation.

DEFAULT_ROTATE_TOKEN_THRESHOLD = 60000


def _read_json_file(path):
    """Read a JSON record, or the newest usage-bearing line of a JSONL log.

    Claude Code keeps its session record as JSONL; the last line that
    reports ``usage`` holds the session's current context size.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, TypeError, UnicodeDecodeError):
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    latest = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a line still being written
        if _session_tokens(entry) is not None:
            latest = entry
    return latest


def _session_tokens(record):
    """Best-effort token usage extraction; None when unreadable/unknown."""
    if not isinstance(record, dict):
        return None
    for key in ("token_count", "tokens", "context_tokens", "context_used"):
        value = record.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    usage = record.get("usage")
    if isinstance(usage, dict):
        nested = _session_tokens(usage)
        if nested is not None:
            return nested
    message = record.get("message")
    if not isinstance(usage, dict) and isinstance(message, dict):
        usage = message.get("usage")
    if isinstance(usage, dict):
        # Anthropic usage: the context in play is the prompt side of the turn.
        parts = [
            usage.get(key)
            for key in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        ]
        numbers = [
            value for value in parts
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        ]
        if numbers:
            return sum(numbers)
    return None


# Nodes a developer is actively on, finished nodes and nodes waiting on a
# human or an upstream node need no supervisor resume.  ``review`` and
# ``rework`` are deliberately excluded: reviewers have no self-report channel
# and an integration-review rework only advances on the next resume, so both
# must keep the heartbeat pulling.
_IDLE_SAFE_STATUSES = frozenset(
    {
        "running", "planned", "accepted", "failed",
        "stopped", "skipped_by_user", "blocked_design", "blocked_by_required_node",
    }
)


def supervisor_preflight(
    paths,
    run_id,
    session_record=None,
    *,
    token_threshold=DEFAULT_ROTATE_TOKEN_THRESHOLD,
):
    """Read-only disk check; returns exactly one of idle/work/rotate/unknown.

    - ``unknown``: the session record could not be read or parsed (never
      collapsed into idle).
    - ``rotate``: the session's own context/token usage exceeds the
      threshold (default ~60k tokens).
    - ``work``: pending provider requests exist, or a bound worker has
      delivered/finished something not yet consumed.
    - ``idle``: workers are active and nothing new needs servicing.
    """
    if session_record is None:
        return {"state": "unknown", "reason": "session record path is missing"}
    record = _read_json_file(session_record)
    if record is None:
        return {"state": "unknown", "reason": "session record unreadable"}
    tokens = _session_tokens(record)
    if tokens is None:
        return {"state": "unknown", "reason": "token usage unknown"}
    if tokens > token_threshold:
        return {"state": "rotate", "reason": "context over threshold", "tokens": tokens}

    try:
        from .adapters.task_provider import ProviderActionStore

        store = ProviderActionStore(paths)
        pending = store.pending(run_id)
        unconsumed = store.unconsumed_deliveries(run_id)
        unpolled = store.unpolled_results(run_id)
    except Exception:
        pending = None
    if pending is None:
        return {"state": "unknown", "reason": "provider mailbox unreadable"}
    if pending:
        return {"state": "work", "reason": "pending provider requests", "pending": len(pending)}
    if unconsumed:
        return {"state": "work", "reason": "worker delivery pending", "nodes": unconsumed}
    if unpolled:
        return {"state": "work", "reason": "provider results not yet polled", "nodes": unpolled}

    try:
        snapshot = load_snapshot(paths, run_id)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        snapshot = None
    if snapshot is None:
        return {"state": "unknown", "reason": "run snapshot unavailable"}
    # Idle is only safe when every node is either being worked on, finished,
    # or waiting on a human.  Anything else -- a delivery, a retry, a ready
    # node, an unknown state -- needs the supervisor's next resume.
    statuses = {
        node: (data.get("status") if isinstance(data, dict) else None)
        for node, data in (snapshot.nodes or {}).items()
    }
    needs_service = sorted(
        node for node, status in statuses.items() if status not in _IDLE_SAFE_STATUSES
    )
    if needs_service:
        return {"state": "work", "reason": "nodes need servicing", "nodes": needs_service}
    running = sorted(
        node for node, status in statuses.items()
        if status == "running"
    )
    if running:
        return {"state": "idle", "reason": "workers active", "nodes": running}
    if any(status == "planned" for status in statuses.values()):
        return {"state": "work", "reason": "planned nodes with nothing running"}
    return {"state": "idle", "reason": "nothing pending"}


_REGISTRY_NAME = "supervisor-registry.json"


def _registry_path(paths, run_id):
    return run_dir(paths, run_id, create=True) / _REGISTRY_NAME


def register_supervisor_address(paths, run_id, address):
    """Atomically record the current supervisor address; keeps history."""
    if not isinstance(address, dict):
        raise TypeError("address must be a mapping")
    provider = address.get("provider")
    session_id = address.get("session_id") or address.get("task_id")
    host = address.get("host") or address.get("hostId")
    if not (
        isinstance(provider, str) and provider
        and isinstance(session_id, str) and session_id
        and isinstance(host, str) and host
    ):
        raise ValueError("supervisor address requires provider, session_id and host")
    for forbidden in ("token", "password", "secret", "credential"):
        for key in address:
            if forbidden in str(key).lower():
                raise ValueError("supervisor address must not carry credentials")
    path = _registry_path(paths, run_id)
    registry = _read_json_file(path)
    if not isinstance(registry, dict):
        registry = {}
    history = registry.get("history")
    if not isinstance(history, list):
        history = []
    current = registry.get("current")
    if isinstance(current, dict):
        history.append(current)
    entry = {
        "provider": provider,
        "session_id": session_id,
        "host": host,
        "registered_at": time.time(),
    }
    registry = {"current": entry, "history": history}
    descriptor, temporary = tempfile.mkstemp(
        prefix=".supervisor-registry-", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(registry, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return entry


def current_supervisor_address(paths, run_id):
    """Return the registered supervisor address or an explicit unknown."""
    registry = _read_json_file(_registry_path(paths, run_id))
    if not isinstance(registry, dict) or not isinstance(registry.get("current"), dict):
        return {"status": "unknown", "reason": "no supervisor registered"}
    return {
        "status": "ok",
        "current": registry["current"],
        "history": registry.get("history", []),
    }
