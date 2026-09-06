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
        append_event(self.paths, RunEvent("supervisor_heartbeat", {"run_id": self.run_id, "status": "active"}))
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
