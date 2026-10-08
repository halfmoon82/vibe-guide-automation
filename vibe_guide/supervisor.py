"""Durable supervisor lease and one-cycle monitor recovery."""

import hashlib
import json
import os
import re
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

# Absolute context size is primary; growth is an auxiliary rotation condition.
DEFAULT_ROTATE_TOKEN_THRESHOLD = 80000
DEFAULT_ROTATE_SOFT_CAP = 100000
DEFAULT_ROTATE_HARD_CAP = 150000
SUPERVISOR_OUTPUT_POLICY = (
    "监工输出约束：CLI/bridge 的完整输出由运行时落盘，聊天只返回摘要和证据路径。"
    "原生 wait_threads 必须通过工具编排保存完整返回，只向会话输出状态、cursor 和最新一条短消息（最多 200 字）；"
    "遇到 errors、unknown 或需人工处理时保留原因。不得打印完整 reviewer JSON。"
    "接班先读 handoff-summary.json；技能和历史报告仅按需读取，读过的同一 digest 不重复载入。"
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
):
    """Read-only disk check; returns exactly one of idle/work/rotate/unknown.

    - ``unknown``: the session record could not be read or parsed (never
      collapsed into idle).
    - ``rotate``: absolute hard cap, or soft cap plus growth threshold.
      Missing baselines never manufacture growth. Soft warning requests a
      compact artifact from the CLI; this function itself stays read-only.
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
    if tokens >= DEFAULT_ROTATE_HARD_CAP:
        return {"state": "rotate", "reason": "context over hard cap",
                "tokens": tokens, "baseline": baseline, "prepare_handoff": True}
    if (tokens >= DEFAULT_ROTATE_SOFT_CAP and baseline is not None
            and tokens - baseline >= token_threshold):
        return {"state": "rotate", "reason": "context grew past threshold since takeover",
                "tokens": tokens, "baseline": baseline, "prepare_handoff": True}
    try:
        holds = supervisor_holds(paths, run_id)
    except ValueError:
        return {"state": "unknown", "reason": "supervisor holds unreadable"}
    result = _mailbox_state(paths, run_id, holds)
    result.setdefault("tokens", tokens)
    result.setdefault("baseline", baseline)
    result["prepare_handoff"] = tokens >= DEFAULT_ROTATE_SOFT_CAP
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
    statuses = _node_statuses(snapshot)
    holds, stale = _valid_holds(holds, statuses, time.time())
    if stale:
        return {"state": "work", "reason": "hold needs recheck", "nodes": stale}
    # A request the supervisor parked for a human decision is not new work;
    # a delivery or result on that node still is (checked above).
    pending = [action for action in pending if action.get("issue_id") not in holds]
    if pending:
        return {"state": "work", "reason": "pending provider requests", "pending": len(pending)}
    # Idle is only safe when every node is either being worked on, finished,
    # or waiting on a human.  Anything else -- a delivery, a retry, a ready
    # node, an unknown state -- needs the supervisor's next resume.
    needs_service = sorted(
        node for node, status in statuses.items()
        if status not in _IDLE_SAFE_STATUSES and node not in holds
    )
    if needs_service:
        return {"state": "work", "reason": "nodes need servicing", "nodes": needs_service}
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
    """Return ``{node: {"reason", "held_at", "statuses"}}``; ValueError if unreadable."""
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


def set_supervisor_hold(paths, run_id, node, reason):
    """Park ``node`` for a human decision; the node must exist in the run.

    Holding again refreshes the hold to the run's current state.
    """
    if not isinstance(node, str) or not _NODE_ID.fullmatch(node):
        raise ValueError("hold node must be a simple node id")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("hold needs a reason")
    reason = reason.strip()
    if len(reason) > _HOLD_REASON_LIMIT:
        raise ValueError("hold reason is limited to {} characters".format(_HOLD_REASON_LIMIT))
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
    holds[node] = {"reason": reason, "held_at": now, "statuses": statuses}
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


def heartbeat_prompt(plan_id, run_id, *, command_prefix="vibe"):
    """The one heartbeat instruction a supervisor shift may install.

    ISSUE-127 told the shift to make the preflight the first step of a
    heartbeat it wrote itself.  On socialmore (5.0.1, Codex) the shift wrote a
    prompt that ran a full `vibe resume` every beat instead, so the preflight,
    the registration and the rotation never happened and an idle beat cost
    5-15x the design.  vibe now owns the wording; prd-guide §6.0 embeds this
    exact text and a test keeps the two identical.
    """
    prompt = (
        "vibe 监工心跳 · 计划 {plan} · 运行 {run}\n"
        "（本指令由 vibe 生成。建心跳时逐字照抄，只替换 <本会话记录路径>；不得另写、增删或合并步骤。）\n"
        "第 1 步，只跑这一条：\n"
        "  {cli} supervisor-preflight --run-id {run} --session-record <本会话记录路径>\n"
        "第 2 步，按输出的 state 走一个分支：\n"
        "  - idle：只回一个字，结束本轮，不再跑任何命令。\n"
        "  - work 或 unknown：按 prd-guide §6.1 服务信箱一轮，"
        "推进用 {cli} resume --plan {plan} --run-id {run}；状态只从磁盘读。\n"
        "  - rotate：新开一个会话，让它跑 {cli} supervisor-handoff --run-id {run} 并照输出接班；本会话不再做别的。\n"
        "例行轮询不向用户汇报。"
    ).format(plan=plan_id, run=run_id, cli=command_prefix)
    return prompt + ("\n" + SUPERVISOR_OUTPUT_POLICY if command_prefix != "vibe" else "")


def supervisor_handoff(paths, run_id, *, command_prefix="vibe"):
    """Generate a compact runtime artifact and bounded takeover instructions.

    Run 140: a takeover re-read several prd-guide sections, the authorization
    card and the full status (11-31 model calls, 0.4-1.7M input tokens) and
    landed at 44-70k context.  The facts live on disk; vibe prints them.
    Raises FileNotFoundError/ValueError when the run cannot be read.
    """
    state_refs = _snapshot_refs(paths, run_id)
    snapshot = load_snapshot(paths, run_id)
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
    plan_id = snapshot.plan_id
    payload = {
        "status": "ok",
        "plan_id": plan_id,
        "run_id": run_id,
        "run_status": snapshot.status,
        "nodes": nodes,
        "holds": holds,
        "pending": None if pending is None else len(pending),
        "heartbeat_prompt": heartbeat_prompt(plan_id, run_id, command_prefix=command_prefix),
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
            lines.append("  - {}：{}（已挂起 {}）".format(
                node, entry.get("reason"),
                "{:.1f} 小时".format(hours) if hours is not None else "时长未知"))
    lines.append("信箱：待服务请求 {} 项（不含等人拍板的节点）".format(
        "未知" if pending is None else len(pending)))
    lines += [
        "接班步骤：",
        "  1) 登记：vibe supervisor-register --run-id {} --provider <平台> "
        "--session-id <本会话 id> --host <本机标识> --session-record <本会话记录路径>".format(run_id),
        "     <本会话记录路径> 是本会话自己的日志文件，不要另写记录文件："
        "Codex 为 ~/.codex/sessions/<年>/<月>/<日>/rollout-…-<本会话 id>.jsonl，"
        "Claude Code 为 ~/.claude/projects/<项目>/<本会话 id>.jsonl（用 ls 确认）。",
        "  2) 自建心跳，指令逐字用下面这段，只替换 <本会话记录路径>：",
        heartbeat_prompt(plan_id, run_id, command_prefix=command_prefix),
        "  3) 置顶、改标题；删掉上一班的心跳，归档上一班会话"
        "（Claude Code 没有换班原语：上一班结束会话即可）。",
        "  4) 跑一次心跳指令第 1 步的预检，按输出走。",
        "监工挂起某个节点等用户拍板时，先登记：vibe supervisor-hold --run-id {} "
        "--node <节点> --reason <一句话原因>，否则每次心跳都会被当成有事做。"
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
                 "worktree", "branch", "cursor", "generation", "status", "status_file", "handoff_file")
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
    pending_keys = ("action_id", "issue_id", "role", "operation", "generation", "sequence")
    summary = {
        "schema_version": 1, "run_id": run_id, "plan_id": snapshot.plan_id,
        "run_status": snapshot.status,
        "authorization_digest": snapshot.authorization_digest,
        "node_contract_digest": snapshot.node_contract_digest,
        "nodes": nodes, "tasks": [{k: t[k] for k in task_keys if k in t} for t in latest.values()],
        "holds": {n: {"reason": h.get("reason"), "held_at": h.get("held_at")} for n, h in holds.items()},
        "pending_actions": [{k: a[k] for k in pending_keys if k in a} for a in pending],
        "pending_count": len(pending), "evidence": evidence,
        "task_registry_status": "ok" if tasks_path.is_file() else "unknown",
        "output_policy": SUPERVISOR_OUTPUT_POLICY,
        "next_command": "{} supervisor-preflight --run-id {} --session-record <本会话记录路径>".format(command_prefix, run_id),
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
