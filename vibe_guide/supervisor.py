"""Durable supervisor lease and one-cycle monitor recovery."""

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

from .config import load_supervisor_single_shift
from .contracts import RunEvent
from .decision_boundary import validate_boundary_category
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

# Absolute context size is primary; growth is an auxiliary rotation condition.
DEFAULT_ROTATE_TOKEN_THRESHOLD = 80000
DEFAULT_ROTATE_SOFT_CAP = 100000
DEFAULT_ROTATE_HARD_CAP = 200000
SUPERVISOR_OUTPUT_POLICY = (
    "监工整批输出约束：每次工具编排合并后的返回最多 8192 UTF-8 字节，不能按每个子调用各算一份预算。"
    "所有 shell、源码读取和原生工具完整结果先保存为 name/output 列表，再调用 "
    "supervisor-output --run-id <run-id> --payload <批次 JSON 路径> --json；"
    "只把该入口摘要返回聊天，完整结果仍用于真实 provider 回写，摘要不能替代结果。"
    "相同批次 SHA 已读时只返回证据引用；需要下钻时只读相关片段，不重复载入完整协议或报告。"
    "原生 wait_threads 仅保留状态、cursor、错误和最新消息（最多 200 字），不得打印完整 reviewer JSON。"
    "接班先读 handoff-summary.json；已解决故障不重新调查。"
)

SUPERVISOR_OUTPUT_LIMIT = 8192
HANDOFF_SUMMARY_LIMIT = 24576


def _read_json_file(path):
    """Read a JSON record, or the newest usage-bearing line of a JSONL log.

    Claude Code and Codex Desktop both keep their session record as JSONL;
    the last line that reports usage holds the session's current context size.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, TypeError, UnicodeDecodeError):
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a line still being written
        if _session_tokens(entry) is not None:
            return entry
    return None


def _session_tokens(record):
    """Best-effort token usage extraction; None when unreadable/unknown."""
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if isinstance(payload, dict) and payload.get("type") == "token_count":
        # Codex Desktop rollout: ``last_token_usage`` is the latest call, whose
        # input (cached included) is the context, short only by that call's
        # own output; ``total_token_usage`` is a
        # running sum and must never be read as the context.
        info = payload.get("info")
        last = info.get("last_token_usage") if isinstance(info, dict) else None
        value = last.get("input_tokens") if isinstance(last, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
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
    context_hard_cap=DEFAULT_ROTATE_HARD_CAP,
    single_shift=None,
    accept_decision=False,
):
    """Read-only disk check; returns exactly one of idle/work/decision/rotate/unknown.

    - ``unknown``: the session record could not be read or parsed (never
      collapsed into idle).
    - ``rotate``: absolute hard cap, or soft cap plus growth threshold.
      Missing baselines never manufacture growth. Soft warning requests a
      compact artifact from the CLI; this function itself stays read-only.
      With ``single_shift`` (``supervisor_single_shift`` in
      ``.vibe/config.json`` when not passed) only the hard cap rotates.
    - ``decision``: only pending human decisions remain and the caller passed
      ``accept_decision`` (the current heartbeat command does).  A legacy
      heartbeat that does not know this state gets ``work`` for the same
      situation, resumes, and ``resume`` renders the decision cards.  Both
      forms carry ``decision_only`` so the beat can pick the 60-minute interval.
    - ``work``: pending provider requests exist, or a bound worker has
      delivered/finished something not yet consumed.
    - ``idle``: workers are active, or the remaining nodes wait on a human
      (``supervisor-hold``), and nothing new needs servicing.
    """
    if session_record is None:
        return {"state": "unknown", "reason": "session record path is missing"}
    record = _read_json_file(session_record)
    if record is None:
        return {"state": "unknown", "reason": "session record unreadable"}
    tokens = _session_tokens(record)
    if tokens is None:
        return {"state": "unknown", "reason": "token usage unknown"}
    baseline = _takeover_baseline(paths, run_id, session_record)
    if token_threshold <= 0:
        return {"state": "unknown", "reason": "growth threshold must be positive"}
    if type(context_hard_cap) is not int or context_hard_cap <= DEFAULT_ROTATE_SOFT_CAP:
        return {"state": "unknown", "reason": "context hard cap must exceed soft cap"}
    if tokens >= context_hard_cap:
        return {"state": "rotate", "reason": "context over hard cap",
                "tokens": tokens, "baseline": baseline, "context_hard_cap": context_hard_cap, "prepare_handoff": True}
    config_warning = None
    if single_shift is None:
        try:
            single_shift, config_warning = load_supervisor_single_shift(paths.root)
        except ValueError:
            return {"state": "unknown", "reason": "supervisor_single_shift invalid"}
    if (not single_shift and tokens >= DEFAULT_ROTATE_SOFT_CAP and baseline is not None
            and tokens - baseline >= token_threshold):
        return {"state": "rotate", "reason": "context grew past threshold since takeover",
                "tokens": tokens, "baseline": baseline, "context_hard_cap": context_hard_cap, "prepare_handoff": True}
    try:
        holds = supervisor_holds(paths, run_id)
    except ValueError:
        return {"state": "unknown", "reason": "supervisor holds unreadable"}
    result = _mailbox_state(paths, run_id, holds)
    if result.get("state") == "decision":
        result["decision_only"] = True
        if not accept_decision:
            result = {key: value for key, value in result.items() if key != "heartbeat"}
            result["state"] = "work"
            result["reason"] = "pending decisions (legacy heartbeat: resume renders the cards)"
    result.setdefault("tokens", tokens)
    result.setdefault("baseline", baseline)
    result["context_hard_cap"] = context_hard_cap
    result["prepare_handoff"] = tokens >= DEFAULT_ROTATE_SOFT_CAP
    result["single_shift"] = bool(single_shift)
    if config_warning:
        result["config_warning"] = config_warning
    return result


def _valid_holds(holds, statuses, now):
    """Holds still describing the run the supervisor looked at when parking.

    A hold carries every node's status at hold time.  Any change since --
    a downstream node, a review, a quarantine -- or a hold older than
    ``HOLD_RECHECK_SECONDS`` voids it, so the supervisor looks again and
    re-holds if the run still waits on the human.
    """
    stale = []
    valid = {}
    for node, entry in holds.items():
        held_at = entry.get("held_at")
        if (
            entry.get("statuses") != statuses
            or not isinstance(held_at, (int, float))
            or now - held_at > HOLD_RECHECK_SECONDS
            or held_at > now + 60  # clock moved back: recheck rather than trust it
        ):
            stale.append(node)
        else:
            valid[node] = entry
    return valid, sorted(stale)


def _mailbox_state(paths, run_id, holds):
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
    try:
        from .decisions import DecisionMailbox

        decisions = DecisionMailbox(paths).list_pending(run_id)
    except (OSError, TypeError, ValueError):
        return {"state": "unknown", "reason": "decision mailbox unreadable"}
    if unconsumed:
        return {"state": "work", "reason": "worker delivery pending", "nodes": unconsumed, "decisions": decisions}
    if unpolled:
        return {"state": "work", "reason": "provider results not yet polled", "nodes": unpolled, "decisions": decisions}
    decision_state = {
            "state": "decision",
            "reason": "pending decisions",
            "decisions": decisions,
            "heartbeat": {"mode": "degraded_frequency", "paused": False},
        }
    decision_nodes = {entry.get("node_id") for entry in decisions}
    independent_pending = [action for action in pending
                           if action.get("issue_id") not in decision_nodes
                           and action.get("issue_id") not in holds]
    if independent_pending:
        return {"state": "work", "reason": "pending provider requests",
                "pending": len(independent_pending), "decisions": decisions}

    try:
        snapshot = load_snapshot(paths, run_id)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        snapshot = None
    if snapshot is None:
        if decisions:
            return decision_state
        return {"state": "unknown", "reason": "run snapshot unavailable"}
    statuses = _node_statuses(snapshot)
    holds, stale = _valid_holds(holds, statuses, time.time())
    if stale:
        return {"state": "work", "reason": "hold needs recheck", "nodes": stale, "decisions": decisions}
    # A request the supervisor parked for a human decision is not new work;
    # a delivery or result on that node still is (checked above).
    pending = [action for action in pending if action.get("issue_id") not in holds
               and action.get("issue_id") not in decision_nodes]
    if pending:
        return {"state": "work", "reason": "pending provider requests", "pending": len(pending), "decisions": decisions}
    # Idle is only safe when every node is either being worked on, finished,
    # or waiting on a human.  Anything else -- a delivery, a retry, a ready
    # node, an unknown state -- needs the supervisor's next resume.
    needs_service = sorted(
        node for node, status in statuses.items()
        if status not in _IDLE_SAFE_STATUSES and node not in holds
    )
    if needs_service:
        return {"state": "work", "reason": "nodes need servicing", "nodes": needs_service, "decisions": decisions}
    if decisions:
        dispatchable = sorted(node for node in (getattr(snapshot, "ready_set", None) or [])
                              if node not in holds and node not in decision_nodes)
        if dispatchable:
            return {"state": "work", "reason": "dispatchable nodes",
                    "nodes": dispatchable, "decisions": decisions}
        return decision_state
    if holds:
        running = sorted(node for node, status in statuses.items() if status == "running")
        if running:
            # A delivery from a running worker wakes the supervisor anyway,
            # and that resume dispatches whatever capacity frees up.
            return {"state": "idle", "reason": "workers active", "nodes": running,
                    "held": sorted(holds)}
        # Nothing runs: a node the DAG marks dispatchable is not waiting behind
        # the held decision, even if it was already dispatchable at hold time.
        dispatchable = sorted(
            node for node in (getattr(snapshot, "ready_set", None) or [])
            if node not in holds
        )
        if dispatchable:
            return {"state": "work", "reason": "dispatchable nodes", "nodes": dispatchable}
        # The whole run is exactly as it was when the supervisor parked it and
        # nothing is dispatchable, so planned nodes wait behind the decision.
        return {"state": "idle", "reason": "waiting on a human decision", "held": sorted(holds)}
    running = sorted(
        node for node, status in statuses.items()
        if status == "running"
    )
    if running:
        return {"state": "idle", "reason": "workers active", "nodes": running}
    if any(status == "planned" for status in statuses.values()):
        return {"state": "work", "reason": "planned nodes with nothing running"}
    return {"state": "idle", "reason": "nothing pending"}


def _node_statuses(snapshot):
    return {
        node: (data.get("status") if isinstance(data, dict) else None)
        for node, data in (snapshot.nodes or {}).items()
    }


# ---------------------------------------------------------------------------
# Takeover baseline: rotation measures growth since the shift took over.

def _record_digest(session_record):
    """Identify a session log without storing a user path on disk."""
    try:
        resolved = str(Path(session_record).expanduser().resolve())
    except (OSError, TypeError, ValueError, RuntimeError):
        return None
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()


def _takeover_baseline(paths, run_id, session_record):
    try:
        registry = _read_json_file(run_dir(paths, run_id) / _REGISTRY_NAME)
    except (OSError, ValueError):
        return None
    current = registry.get("current") if isinstance(registry, dict) else None
    if not isinstance(current, dict):
        return None
    baseline = current.get("baseline_context")
    digest = current.get("session_record_digest")
    if (
        isinstance(baseline, int) and not isinstance(baseline, bool) and baseline >= 0
        and isinstance(digest, str) and digest == _record_digest(session_record)
    ):
        return baseline
    return None


# ---------------------------------------------------------------------------
# Holds: nodes the supervisor parked for a human decision.  Before this the
# decision lived only in the supervisor's context, so the preflight saw a
# pending request every beat and the idle fast path never ran (run 140:
# 46 of 60 preflights said work while the run waited on one A/B answer).

_HOLDS_NAME = "supervisor-holds.json"
#: A hold older than this wakes the supervisor once to recheck it, so a hold
#: nobody released after the human answered cannot stall a run silently.
HOLD_RECHECK_SECONDS = 6 * 3600
_HOLD_REASON_LIMIT = 200
#: Reviewers have no self-report channel and reworks advance only on resume
#: (see ``_IDLE_SAFE_STATUSES``); parking them would hide their completion.
_UNHOLDABLE_STATUSES = frozenset({"review", "rework"})
_NODE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _holds_path(paths, run_id, create=False):
    return run_dir(paths, run_id, create=create) / _HOLDS_NAME


def supervisor_holds(paths, run_id):
    """Return ``{node: {"category", "reason", "held_at", "statuses"}}``."""
    path = _holds_path(paths, run_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as error:
        raise ValueError("supervisor holds unreadable") from error
    holds = data.get("holds") if isinstance(data, dict) else None
    if not isinstance(holds, dict) or not all(
        isinstance(node, str) and isinstance(entry, dict) for node, entry in holds.items()
    ):
        raise ValueError("supervisor holds malformed")
    return holds


def _write_holds(paths, run_id, holds):
    path = _holds_path(paths, run_id, create=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".supervisor-holds-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"holds": holds}, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def set_supervisor_hold(paths, run_id, node, reason, boundary_category=None):
    """Park ``node`` for a human decision; the node must exist in the run.

    Holding again refreshes the hold to the run's current state.  New and
    refreshed holds must name one approved human-decision boundary.
    """
    if not isinstance(node, str) or not _NODE_ID.fullmatch(node):
        raise ValueError("hold node must be a simple node id")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("hold needs a reason")
    reason = reason.strip()
    if len(reason) > _HOLD_REASON_LIMIT:
        raise ValueError("hold reason is limited to {} characters".format(_HOLD_REASON_LIMIT))
    boundary_category = validate_boundary_category(boundary_category)
    snapshot = load_snapshot(paths, run_id)
    if node not in (snapshot.nodes or {}):
        raise ValueError("node {} is not in this run".format(node))
    status = _node_statuses(snapshot)[node]
    if status in _UNHOLDABLE_STATUSES:
        raise ValueError(
            "node {} is in {}; it advances only on the supervisor's resume".format(node, status)
        )
    holds = supervisor_holds(paths, run_id)
    statuses = _node_statuses(snapshot)
    now = time.time()
    # Only this hold is refreshed; another hold voided by a change stays void
    # until the supervisor rechecks and re-holds that node too.
    holds[node] = {
        "category": boundary_category,
        "reason": reason,
        "held_at": now,
        "statuses": statuses,
    }
    _write_holds(paths, run_id, holds)
    return holds


def release_supervisor_hold(paths, run_id, node):
    """Release a hold; returns False when the node was not held."""
    holds = supervisor_holds(paths, run_id)
    if node not in holds:
        return False
    del holds[node]
    _write_holds(paths, run_id, holds)
    return True


_REGISTRY_NAME = "supervisor-registry.json"


def _registry_path(paths, run_id):
    return run_dir(paths, run_id, create=True) / _REGISTRY_NAME


def register_supervisor_address(paths, run_id, address, *, session_record=None):
    """Atomically record the current supervisor address; keeps history.

    With ``session_record`` (the shift's own session log) the current context
    is stored as the takeover baseline the rotation threshold counts from,
    keyed by a digest of the log path rather than the path itself.  An
    unreadable log still registers -- the wake-up address matters more -- and
    the entry simply carries no baseline.
    """
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
    if session_record is not None and Path(str(session_record)).expanduser().is_absolute():
        record = _read_json_file(session_record)
        baseline = _session_tokens(record) if record is not None else None
        digest = _record_digest(session_record)
        if (
            isinstance(current, dict)
            and current.get("session_id") == session_id
            and current.get("session_record_digest") == digest
            and isinstance(current.get("baseline_context"), int)
        ):
            # Registering the same shift again must not push its rotation out.
            baseline = current["baseline_context"]
        if baseline is not None and digest is not None:
            entry["baseline_context"] = baseline
            entry["session_record_digest"] = digest
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


#: The push tool a worker uses to wake a supervisor, per host family.  Issue
#: #208: dispatch text used to say "the authorized channel, fixed format"
#: without naming either, so workers could only skip the wake-up.  A host is
#: listed here only with real receipt evidence (Codex: #167); the others say
#: plainly that there is no channel and the heartbeat collects the delivery.
WAKE_CHANNELS = {
    "codex": "send_message_to_thread",
}
WAKE_NO_CHANNEL_REASON = {
    "claude": "Claude Code 没有已验证的推送通道（#154 实测 unverified），等监工心跳拉取",
    "workbuddy": "WorkBuddy 没有推送通道，等监工心跳拉取",
}


def wake_signal(run_id, node_id=None, role=None, generation=None):
    """Return the fixed one-line wake-up text.

    It only tells the supervisor where to look; the supervisor never trusts
    it and re-checks the self-report and native wait as before.
    """
    return "vibe-wake run={} node={} role={} generation={}".format(
        run_id,
        node_id or "<node>",
        role or "<role>",
        generation if generation is not None else "<generation>",
    )


def wake_instruction(address, run_id, node_id=None, role=None, generation=None):
    """Return how a worker wakes ``address``: channel, target and signal."""
    provider = str(address.get("provider") or "").lower()
    family = next((name for name in ("codex", "claude", "workbuddy") if provider.startswith(name)), None)
    channel = WAKE_CHANNELS.get(family)
    if channel is None:
        reason = WAKE_NO_CHANNEL_REASON.get(
            family, "平台 {} 没有已登记的推送通道，等监工心跳拉取".format(provider or "未知")
        )
        return {"channel": None, "reason": reason}
    return {
        "channel": channel,
        "target": address.get("session_id"),
        "signal": wake_signal(run_id, node_id, role, generation),
    }


def current_supervisor_address(paths, run_id, *, node_id=None, role=None, generation=None):
    """Return the registered supervisor address or an explicit unknown.

    An ``ok`` answer also carries ``wake``: the push channel, its target and
    the filled-in signal, or ``channel: null`` with the reason there is none.
    """
    registry = _read_json_file(_registry_path(paths, run_id))
    if not isinstance(registry, dict) or not isinstance(registry.get("current"), dict):
        return {"status": "unknown", "reason": "no supervisor registered"}
    return {
        "status": "ok",
        "current": registry["current"],
        "history": registry.get("history", []),
        "wake": wake_instruction(registry["current"], run_id, node_id, role, generation),
    }


#: Where each host writes the session log a shift hands over.  One definition,
#: printed by both the first-shift hint and the handoff steps: two copies of
#: this list is how a host silently goes missing from one of them, which is
#: exactly what had happened to WorkBuddy.
SESSION_RECORD_HINT = (
    "<本会话记录路径> 是本会话自己的日志文件，不要另写记录文件："
    "Codex 为 ~/.codex/sessions/<年>/<月>/<日>/rollout-…-<本会话 id>.jsonl，"
    "Claude Code 为 ~/.claude/projects/<项目>/<本会话 id>.jsonl，"
    "WorkBuddy 为 ~/.workbuddy-ai/projects/<项目路径把 / 换成 ->/<本会话 id>.jsonl"
    "（<本会话 id> 就是 $CODEBUDDY_SESSION_ID）；用 ls 确认。"
)

#: What each host automates about a shift and what it cannot.  Issue #127 asks
#: the first-shift hint to explain the difference instead of only listing two
#: commands, and both texts that print it now read this one string.
HOST_SHIFT_DIFFERENCES = (
    "宿主差异：Codex 桌面两条都能自动建；"
    "Claude Code 没有换班原语，心跳降级为兜底机制；"
    "WorkBuddy 两条都得在本会话里手动做（登记地址、建心跳），不会自己发生。"
)


#: One source for the beat cadence.  Default 10 minutes; 30 once a platform's
#: push wake-up is registered as verified (success and failure evidence both);
#: 60 while the only open item is an unanswered human decision.
HEARTBEAT_INTERVAL_DEFAULT_MINUTES = 10
HEARTBEAT_INTERVAL_PUSH_VERIFIED_MINUTES = 30
HEARTBEAT_INTERVAL_DECISION_MINUTES = 60
PUSH_WAKE_VERIFICATION_RELATIVE = ".vibe/push-wake-verification.json"
_HEARTBEAT_INTERVAL_FILE = "heartbeat-interval.json"


def _push_wake_verified(paths, provider):
    """Return (verified, reason); an unreadable registry is named, never silent."""
    path = Path(paths.root) / PUSH_WAKE_VERIFICATION_RELATIVE
    if not path.exists():
        return False, "no push wake verification registered"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        platforms = data["platforms"]
        if not isinstance(platforms, dict):
            raise ValueError("platforms must be an object")
    except (OSError, ValueError, KeyError, TypeError) as error:
        return False, "push wake verification unreadable: {}".format(str(error)[:120])
    entry = platforms.get(provider)
    if not isinstance(entry, dict):
        return False, "push wake for {} is not registered".format(provider)
    for kind in ("success", "failure"):
        records = entry.get(kind)
        if not (isinstance(records, list) and any(
                isinstance(item, dict) and isinstance(item.get("evidence_ref"), str)
                and item["evidence_ref"].strip() for item in records)):
            return False, "push wake for {} lacks a {} evidence".format(provider, kind)
    return True, "push wake for {} verified with success and failure evidence".format(provider)


def heartbeat_interval(paths, run_id, *, decision_only):
    """Pick the next beat interval in minutes with the reason it was chosen."""
    if decision_only:
        return {"minutes": HEARTBEAT_INTERVAL_DECISION_MINUTES, "mode": "decision",
                "reason": "only unanswered decisions remain", "provider": None}
    try:
        address = current_supervisor_address(paths, run_id)
    except (ValueError, OSError) as error:
        # A damaged lease must not stop the beat: fall back to the default cadence.
        return {"minutes": HEARTBEAT_INTERVAL_DEFAULT_MINUTES, "mode": "default",
                "reason": "supervisor address unreadable: " + str(error)[:200], "provider": None}
    provider = None
    if address.get("status") == "ok":
        provider = address["current"].get("provider")
    if not isinstance(provider, str) or not provider:
        return {"minutes": HEARTBEAT_INTERVAL_DEFAULT_MINUTES, "mode": "default",
                "reason": "supervisor provider unknown: " + str(address.get("reason", "no provider")),
                "provider": None}
    verified, reason = _push_wake_verified(paths, provider)
    if verified:
        return {"minutes": HEARTBEAT_INTERVAL_PUSH_VERIFIED_MINUTES, "mode": "push_verified",
                "reason": reason, "provider": provider}
    return {"minutes": HEARTBEAT_INTERVAL_DEFAULT_MINUTES, "mode": "default",
            "reason": reason, "provider": provider}


def read_heartbeat_interval(paths, run_id):
    """Read the persisted interval state; None when absent or unreadable."""
    data = _read_json_file(run_dir(paths, run_id) / _HEARTBEAT_INTERVAL_FILE)
    return data if isinstance(data, dict) and isinstance(data.get("minutes"), int) else None


def record_heartbeat_interval(paths, run_id, interval, *, clock=None):
    """Persist the interval so a takeover supervisor restores it from disk."""
    from .state import _atomic_bytes

    now = (clock or time.time)()
    previous = read_heartbeat_interval(paths, run_id)
    since = previous.get("since") if previous and previous.get("mode") == interval["mode"] else None
    entry = {
        "minutes": interval["minutes"], "mode": interval["mode"],
        "reason": interval["reason"], "provider": interval.get("provider"),
        "since": since if since is not None else now, "updated_at": now,
    }
    _atomic_bytes(run_dir(paths, run_id) / _HEARTBEAT_INTERVAL_FILE, _json_bytes(entry))
    return entry


def heartbeat_command(run_id, *, command_prefix="vibe"):
    """Return the complete one-line command a host may install as a beat."""
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id is required")
    if not isinstance(command_prefix, str) or not command_prefix.strip():
        raise ValueError("command_prefix is required")
    return "{} supervisor-beat --run-id {} --accept-decision --session-record <本会话记录路径>".format(
        command_prefix.strip(), run_id
    )


def heartbeat_prompt(plan_id, run_id, *, command_prefix="vibe"):
    """Compatibility alias for the only heartbeat installation command."""
    return heartbeat_command(run_id, command_prefix=command_prefix)


def supervisor_handoff(paths, run_id, *, command_prefix="vibe"):
    """Generate a compact runtime artifact and bounded takeover instructions.

    Run 140: a takeover re-read several prd-guide sections, the authorization
    card and the full status (11-31 model calls, 0.4-1.7M input tokens) and
    landed at 44-70k context.  The facts live on disk; vibe prints them.
    Raises FileNotFoundError/ValueError when the run cannot be read.
    """
    state_refs = _snapshot_refs(paths, run_id)
    snapshot = load_snapshot(paths, run_id)
    recovered_refs = _snapshot_refs(paths, run_id)
    if recovered_refs != state_refs:
        # load_snapshot may atomically restore the verified previous snapshot.
        # Re-read after restoration; later concurrent drift still fails closed.
        snapshot = load_snapshot(paths, run_id)
        if _snapshot_refs(paths, run_id) != recovered_refs:
            raise ValueError("handoff snapshot changed; retry at next safe point")
        state_refs = recovered_refs
    holds = supervisor_holds(paths, run_id)
    summary_ref = _write_handoff_summary(paths, run_id, snapshot, holds, state_refs, command_prefix)
    nodes = {
        node: (data.get("status") if isinstance(data, dict) else None)
        for node, data in (snapshot.nodes or {}).items()
    }
    try:
        from .adapters.task_provider import ProviderActionStore

        pending = [
            action for action in ProviderActionStore(paths).pending(run_id)
            if action.get("issue_id") not in holds
        ]
    except Exception:
        pending = None
    try:
        from .decisions import DecisionMailbox

        decisions = DecisionMailbox(paths).list_pending(run_id)
    except Exception:
        decisions = None
    plan_id = snapshot.plan_id
    payload = {
        "status": "ok",
        "plan_id": plan_id,
        "run_id": run_id,
        "run_status": snapshot.status,
        "nodes": nodes,
        "holds": holds,
        "pending": None if pending is None else len(pending),
        "decisions": decisions,
        "heartbeat_interval": read_heartbeat_interval(paths, run_id),
        "heartbeat_command": heartbeat_command(run_id, command_prefix=command_prefix),
        "heartbeat_prompt": heartbeat_command(run_id, command_prefix=command_prefix),
        "handoff_summary": summary_ref,
    }
    accepted = sum(1 for status in nodes.values() if status == "accepted")
    others = ["{}（{}）".format(node, status) for node, status in sorted(nodes.items())
              if status != "accepted"]
    lines = [
        "vibe 监工交接 · 计划 {} · 运行 {}".format(plan_id, run_id),
        "紧凑交接：{}（SHA-256 {}）".format(summary_ref["path"], summary_ref["sha256"]),
        "接班先读 handoff-summary.json；摘要不是授权或验收证据，写操作前仍由运行时核对绑定。"
        "技能和完整报告按需读取，不重复打印；完整证据见摘要中的路径与 digest。",
        "进度：运行 {}；已验收 {}/{}{}".format(
            snapshot.status, accepted, len(nodes),
            "；其余：" + "、".join(others) if others else ""),
    ]
    if holds:
        lines.append("等人拍板（{} 项）：在本会话向用户复述一次即可，不要重新调查；"
                     "用户回复后先 vibe supervisor-hold --run-id {} --node <节点> --release，再推进。".format(
                         len(holds), run_id))
        for node, entry in sorted(holds.items()):
            held_at = entry.get("held_at")
            hours = (time.time() - held_at) / 3600 if isinstance(held_at, (int, float)) else None
            lines.append("  - {}（类别 {}）：{}（已挂起 {}）".format(
                node, entry.get("category") or "unknown", entry.get("reason"),
                "{:.1f} 小时".format(hours) if hours is not None else "时长未知"))
    lines.append("信箱：待服务请求 {} 项（不含等人拍板的节点）".format(
        "未知" if pending is None else len(pending)))
    lines.append("决策信箱：待回答 {} 项；心跳降频但不暂停".format(
        "未知" if decisions is None else len(decisions)))
    interval = payload["heartbeat_interval"]
    lines.append("心跳间隔：当前 {} 分钟（{}，{}）".format(interval["minutes"], interval.get("mode"), interval.get("reason"))
                 if interval else "心跳间隔：未记录，按默认 {} 分钟建".format(HEARTBEAT_INTERVAL_DEFAULT_MINUTES))
    if decisions:
        for decision in decisions:
            lines.append("  - {}：{}（选项：{}）".format(
                decision.get("decision_id"), decision.get("question"),
                ", ".join(option.get("id", "") for option in decision.get("options", [])),
            ))
    lines += [
        "接班步骤：",
        "  1) 登记：vibe supervisor-register --run-id {} --provider <平台> "
        "--session-id <本会话 id> --host <本机标识> --session-record <本会话记录路径>".format(run_id),
        "     " + SESSION_RECORD_HINT,
        "  2) 自建心跳，指令逐字用下面这段，只替换 <本会话记录路径>：",
        heartbeat_command(run_id, command_prefix=command_prefix),
        "  3) 置顶、改标题；删掉上一班的心跳，归档上一班会话"
        "（Claude Code 没有换班原语：上一班结束会话即可）。",
        "  4) 跑一次心跳指令第 1 步的预检，按输出走。",
        HOST_SHIFT_DIFFERENCES,
        "监工挂起某个节点等用户拍板时，先登记：vibe supervisor-hold --run-id {} "
        "--node <节点> --category <product_design|external_authorization|deploy> "
        "--reason <一句话原因>，否则每次心跳都会被当成有事做。"
        "运行状态一变或挂起满 6 小时，预检会报 work（hold needs recheck）：复核后仍在等人就再跑一次同一条命令刷新。".format(run_id),
    ]
    return payload, "\n".join(lines).replace("vibe supervisor-", command_prefix + " supervisor-")


def _artifact_ref(path):
    raw = path.read_bytes()
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw)}


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _snapshot_refs(paths, run_id):
    directory = run_dir(paths, run_id)
    return [_artifact_ref(directory / name)
            for name in ("state.json", "state.previous.json", "events.jsonl")
            if (directory / name).is_file()]


def _write_handoff_summary(paths, run_id, snapshot, holds, state_refs, command_prefix):
    from .state import _atomic_bytes
    from .adapters.task_provider import ProviderActionStore

    directory = run_dir(paths, run_id)
    evidence = list(state_refs)
    for name in ("tasks.json", "supervisor-registry.json", "supervisor-holds.json"):
        path = directory / name
        if path.is_file():
            evidence.append(_artifact_ref(path))
    if _snapshot_refs(paths, run_id) != state_refs:
        raise ValueError("handoff snapshot changed; retry at next safe point")
    plan_root = paths.vibe / "plans" / snapshot.plan_id
    for path in (plan_root / "authorization.json", plan_root / "authorization-card.json",
                 plan_root / "plan.json", paths.vibe / "proposals/skills/prd-guide/SKILL.md",
                 paths.vibe / "proposals/skills/vibe-entry/SKILL.md"):
        if path.is_file():
            evidence.append(_artifact_ref(path))
    tasks_path = directory / "tasks.json"
    tasks_record = _read_json_file(tasks_path) if tasks_path.is_file() else {"bindings": []}
    if not isinstance(tasks_record, dict) or not isinstance(tasks_record.get("bindings"), list):
        raise ValueError("task registry unreadable")
    # Retain the most recent generation for each role, never the whole history.
    latest = {}
    for task in tasks_record["bindings"]:
        if not isinstance(task, dict):
            raise ValueError("task binding invalid")
        key = (task.get("issue_id"), task.get("role"))
        previous = latest.get(key)
        if previous is None or task.get("generation", 0) >= previous.get("generation", 0):
            latest[key] = task
    task_keys = ("issue_id", "role", "task_id", "threadId", "hostId", "provider", "mode",
                 "worktree", "branch", "cursor", "generation", "status", "status_file", "handoff_file",
                 "model", "reasoning", "allowlist", "route_digest")
    node_keys = ("status", "worktree", "branch", "active_role", "active_task", "retryable_action")
    nodes = {}
    for node, data in (snapshot.nodes or {}).items():
        if not isinstance(data, dict):
            raise ValueError("node state invalid")
        nodes[node] = {k: data[k] for k in node_keys if k in data}
        reason = data.get("reason") or (data.get("quarantine") or {}).get("reason")
        if reason:
            nodes[node]["blocked_reason"] = str(reason)[:200]
    pending = ProviderActionStore(paths).pending(run_id)
    pending_keys = ("action_id", "issue_id", "role", "operation", "generation", "sequence",
                    "request_digest", "native_tool")
    source_root = Path(__file__).resolve().parent.parent
    from .runners import provider_action
    module_paths = {"supervisor": str(Path(__file__).resolve()),
                    "task_provider": str(Path(__import__(ProviderActionStore.__module__,
                        fromlist=["__file__"]).__file__).resolve()),
                    "provider_action": str(Path(provider_action.__file__).resolve())}
    runtime_evidence = [_artifact_ref(Path(path)) for path in module_paths.values()]
    evidence.extend(runtime_evidence)
    core_evidence = list(evidence)
    pending_summary = []
    for action in pending:
        ref = _artifact_ref(paths.vibe / "provider-actions/requests" / (action["action_id"] + ".json"))
        evidence.append(ref)
        pending_summary.append(dict({k: action[k] for k in pending_keys if k in action}, evidence=ref))
    summary = {
        "schema_version": 1, "run_id": run_id, "plan_id": snapshot.plan_id,
        "run_status": snapshot.status,
        "authorization_digest": snapshot.authorization_digest,
        "node_contract_digest": snapshot.node_contract_digest,
        "nodes": nodes, "tasks": [{k: t[k] for k in task_keys if k in t} for t in latest.values()],
        "holds": {n: {"reason": h.get("reason"), "held_at": h.get("held_at")} for n, h in holds.items()},
        "pending_actions": pending_summary,
        "runtime": {"source_root": str(source_root), "command_prefix": command_prefix,
                    "module_paths": module_paths, "evidence": runtime_evidence,
                    "evidence_scope": "current_disk_sources_at_loaded_module_paths",
                    "loaded_code_attested": False},
        "service_protocol": (
            "核对当前 run/授权、任务身份、writer、generation 和 cursor；读取动作 evidence，"
            "按 native_tool 执行真实调用，保留完整原始结果；调用 ProviderActionStore.complete"
            "(action_id, payload, request_digest=原请求摘要)，再执行 resume 并核验状态/cursor/交付变化。"
            "已完成动作不重复调用；新观测使用新 sequence。宿主报 No such tool available 时 complete() 拒收并抛 UnresolvedNativeTool："
            "按工具清单里的全名重试一次，仍失败即停止服务信箱、向用户报告工具名路由故障，不交班、不开新会话。工程失败按现有恢复协议继续，"
            "不得用重复 preflight/resume 代替 native 调用；产品变化、外部授权或 deploy 才请求用户。"
        ),
        "pending_count": len(pending), "evidence": evidence,
        "task_registry_status": "ok" if tasks_path.is_file() else "unknown",
        "output_policy": SUPERVISOR_OUTPUT_POLICY,
        "next_command": heartbeat_command(run_id, command_prefix=command_prefix),
        "resume_command": "{} resume --plan {} --run-id {}".format(command_prefix, snapshot.plan_id, run_id),
        "instructions": "摘要仅用于定位。运行时继续核对授权、writer、generation 与 cursor。按需读证据，禁止将原始报告重复打印。",
    }
    raw = _json_bytes(summary)
    if len(raw) > HANDOFF_SUMMARY_LIMIT:
        # Never silently clip opaque cursors or identities. Keep the complete
        # selected state in an immutable companion and make omissions explicit.
        manifest = directory / ("handoff-details-" + hashlib.sha256(raw).hexdigest() + ".json")
        _atomic_bytes(manifest, raw)
        summary["details"] = _artifact_ref(manifest)
        summary["details_omitted"] = True
        summary["nodes"] = {n: {"status": d.get("status")} for n, d in nodes.items()}
        summary["tasks"] = []
        summary["pending_actions"] = []
        summary["evidence"] = core_evidence
        raw = _json_bytes(summary)
        if len(raw) > HANDOFF_SUMMARY_LIMIT:
            summary["node_count"] = len(nodes)
            summary["nodes"] = {}
            summary["holds"] = {}
            raw = _json_bytes(summary)
        if len(raw) > HANDOFF_SUMMARY_LIMIT:
            raise ValueError("handoff metadata exceeds byte budget")
    # Detect state updates during artifact generation instead of certifying a
    # digest from a different generation of the source files.
    for ref in evidence:
        if _artifact_ref(Path(ref["path"])) != ref:
            raise ValueError("handoff source changed; retry at next safe point")
    if _snapshot_refs(paths, run_id) != state_refs:
        raise ValueError("handoff source changed; retry at next safe point")
    path = directory / "handoff-summary.json"
    _atomic_bytes(path, raw)
    return _artifact_ref(path)


def bounded_supervisor_output(paths, run_id, payload, rendered):
    """Preserve full output on disk; limit UTF-8 bytes returned to chat."""
    raw = rendered.encode("utf-8")
    if len(raw) <= SUPERVISOR_OUTPUT_LIMIT:
        return rendered
    from .state import _atomic_bytes
    directory = run_dir(paths, run_id, create=True) / "tool-output"
    path = directory / (hashlib.sha256(raw).hexdigest() + ".txt")
    _atomic_bytes(path, raw)
    short = {k: payload[k] for k in ("command", "state", "status", "run_id", "plan_id",
                                    "tokens", "baseline", "prepare_handoff", "pending", "handoff_summary", "handoff_status", "handoff_error")
             if k in payload}
    if "reason" in payload:
        short["reason"] = str(payload["reason"])[:200]
    short.update({"truncated": True, "output_evidence": _artifact_ref(path)})
    return json.dumps(short, ensure_ascii=False, sort_keys=True)
