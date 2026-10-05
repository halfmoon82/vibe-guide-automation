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

#: Rotate once the context has grown this much since the shift took over.
#: Run 140 (2026-10-05): with an absolute 60k a fresh shift already sat at
#: 44-70k after its handoff, so late shifts rotated minutes after taking over
#: and each takeover cost more than the reset saved.  A shift registered
#: without its session log has no takeover baseline and falls back to the old
#: absolute reading (baseline 0).
DEFAULT_ROTATE_TOKEN_THRESHOLD = 60000
#: Rotate past this context whatever the baseline (the window is ~250k).
DEFAULT_ROTATE_HARD_CAP = 150000


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
    - ``rotate``: the context grew more than ``token_threshold`` since the
      shift registered its takeover baseline (no baseline: since 0), or
      passed the hard cap.
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
    hard_cap = max(DEFAULT_ROTATE_HARD_CAP, token_threshold)
    if tokens > hard_cap:
        return {"state": "rotate", "reason": "context over hard cap",
                "tokens": tokens, "baseline": baseline}
    if tokens - (baseline or 0) > token_threshold:
        reason = (
            "context grew past threshold since takeover"
            if baseline is not None else "context over threshold"
        )
        return {"state": "rotate", "reason": reason, "tokens": tokens, "baseline": baseline}
    try:
        holds = supervisor_holds(paths, run_id)
    except ValueError:
        return {"state": "unknown", "reason": "supervisor holds unreadable"}
    result = _mailbox_state(paths, run_id, holds)
    result.setdefault("tokens", tokens)
    result.setdefault("baseline", baseline)
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
        # The whole run is exactly as it was when the supervisor parked it,
        # so planned nodes are the ones waiting behind the held decision.
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
    holds[node] = {"reason": reason, "held_at": now, "statuses": statuses}
    for entry in holds.values():
        # One supervisor looked at the whole run just now: every hold it keeps
        # describes this state.
        entry["statuses"] = statuses
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


def heartbeat_prompt(plan_id, run_id):
    """The one heartbeat instruction a supervisor shift may install.

    ISSUE-127 told the shift to make the preflight the first step of a
    heartbeat it wrote itself.  On socialmore (5.0.1, Codex) the shift wrote a
    prompt that ran a full `vibe resume` every beat instead, so the preflight,
    the registration and the rotation never happened and an idle beat cost
    5-15x the design.  vibe now owns the wording; prd-guide §6.0 embeds this
    exact text and a test keeps the two identical.
    """
    return (
        "vibe 监工心跳 · 计划 {plan} · 运行 {run}\n"
        "（本指令由 vibe 生成。建心跳时逐字照抄，只替换 <本会话记录路径>；不得另写、增删或合并步骤。）\n"
        "第 1 步，只跑这一条：\n"
        "  vibe supervisor-preflight --run-id {run} --session-record <本会话记录路径>\n"
        "第 2 步，按输出的 state 走一个分支：\n"
        "  - idle：只回一个字，结束本轮，不再跑任何命令。\n"
        "  - work 或 unknown：按 prd-guide §6.1 服务信箱一轮，"
        "推进用 vibe resume --plan {plan} --run-id {run}；状态只从磁盘读。\n"
        "  - rotate：新开一个会话，让它跑 vibe supervisor-handoff --run-id {run} 并照输出接班；本会话不再做别的。\n"
        "例行轮询不向用户汇报。"
    ).format(plan=plan_id, run=run_id)


def supervisor_handoff(paths, run_id):
    """Everything a new shift needs, in one read-only call.

    Run 140: a takeover re-read several prd-guide sections, the authorization
    card and the full status (11-31 model calls, 0.4-1.7M input tokens) and
    landed at 44-70k context.  The facts live on disk; vibe prints them.
    Raises FileNotFoundError/ValueError when the run cannot be read.
    """
    snapshot = load_snapshot(paths, run_id)
    holds = supervisor_holds(paths, run_id)
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
        "heartbeat_prompt": heartbeat_prompt(plan_id, run_id),
    }
    accepted = sum(1 for status in nodes.values() if status == "accepted")
    others = ["{}（{}）".format(node, status) for node, status in sorted(nodes.items())
              if status != "accepted"]
    lines = [
        "vibe 监工交接 · 计划 {} · 运行 {}".format(plan_id, run_id),
        "（接班只需要本段。进度都在磁盘上，不必再翻 prd-guide、授权卡或完整状态。）",
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
        heartbeat_prompt(plan_id, run_id),
        "  3) 置顶、改标题；删掉上一班的心跳，归档上一班会话"
        "（Claude Code 没有换班原语：上一班结束会话即可）。",
        "  4) 跑一次心跳指令第 1 步的预检，按输出走。",
        "监工挂起某个节点等用户拍板时，先登记：vibe supervisor-hold --run-id {} "
        "--node <节点> --reason <一句话原因>，否则每次心跳都会被当成有事做。"
        "运行状态一变或挂起满 6 小时，预检会报 work（hold needs recheck）：复核后仍在等人就再跑一次同一条命令刷新。".format(run_id),
    ]
    return payload, "\n".join(lines)
