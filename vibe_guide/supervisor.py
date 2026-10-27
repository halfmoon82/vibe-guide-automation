"""Durable supervisor lease and one-cycle monitor recovery."""

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

from .authorization import (
    DEFAULT_HEARTBEAT_INTERVAL_MINUTES,
    DEFAULT_ROTATE_HARD_CAP_TOKENS,
    HEARTBEAT_INTERVAL_CHOICES,
    ROTATE_HARD_CAP_CHOICES,
)
from .config import load_supervisor_single_shift
from .contracts import RunEvent
from .decision_boundary import LEGACY_BOUNDARY_CATEGORY, validate_boundary_category
from .providers import CLAUDE_CODE_PROVIDER, CODEX_PROVIDER, WORKBUDDY_VISIBLE_PROVIDER
from .state import append_event, interprocess_lock, run_dir, save_snapshot, load_snapshot


class SupervisorLeaseConflict(ValueError):
    """Lost a lease compare-and-swap, or a stale-epoch supervisor tried to write."""


def _valid_epoch(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _recover_hint(path: Path) -> str:
    return "; confirm no supervisor is running this run, then remove %s to recover" % path


def _read_lease_record(path: Path):
    """Return the lease record, or None when no file exists.

    A file that exists but cannot be parsed is never read as epoch 0: that
    would let a corrupt file reset the counter and readmit a stale writer.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SupervisorLeaseConflict("supervisor lease is unreadable: %s%s" % (error, _recover_hint(path)))
    try:
        record = json.loads(text)
    except ValueError:
        raise SupervisorLeaseConflict("supervisor lease is malformed" + _recover_hint(path))
    if not isinstance(record, dict):
        raise SupervisorLeaseConflict("supervisor lease is malformed" + _recover_hint(path))
    if "lease_epoch" in record and not _valid_epoch(record["lease_epoch"]):
        raise SupervisorLeaseConflict("supervisor lease has an invalid lease_epoch" + _recover_hint(path))
    return record


def _record_epoch(record) -> int:
    # Legacy records carry no epoch and read as 0.
    return 0 if record is None else record.get("lease_epoch", 0)


def read_lease_epoch(paths, run_id: str) -> int:
    """On-disk lease epoch for a run; 0 when there is no lease file."""
    path = run_dir(paths, run_id, create=True) / "supervisor.lease"
    return _record_epoch(_read_lease_record(path))


class SupervisorLease:
    def __init__(self, paths, *, pid=None):
        self.paths = paths
        self.pid = int(pid or os.getpid())
        self.lease_epoch = None
        self._run_id = None

    def path(self, run_id: str) -> Path:
        return run_dir(self.paths, run_id, create=True) / "supervisor.lease"

    def lock(self, run_id: str):
        return interprocess_lock(self.path(run_id).parent / ".supervisor-lease.lock")

    def _write(self, run_id: str, record: Dict[str, Any]) -> None:
        path = self.path(run_id)
        descriptor, temporary = tempfile.mkstemp(prefix=".supervisor.", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, str(path))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _record(self, run_id: str, epoch: int, owner, released=False, started_at=None) -> Dict[str, Any]:
        now = time.time()
        return {
            "pid": self.pid, "run_id": run_id,
            "started_at": started_at or now, "heartbeat": now,
            "lease_epoch": epoch, "owner": owner, "released": released,
        }

    def cas_locked(self, run_id: str, expected_epoch: int, owner=None) -> int:
        """Upgrade the epoch by one if it still equals ``expected_epoch``.

        The caller must hold ``self.lock(run_id)``.  This is the only place
        the epoch changes.
        """
        found = _record_epoch(_read_lease_record(self.path(run_id)))
        if found != expected_epoch:
            raise SupervisorLeaseConflict(
                "supervisor lease takeover lost: expected epoch %s, found %s"
                % (expected_epoch, found)
            )
        new_epoch = expected_epoch + 1
        self._write(run_id, self._record(run_id, new_epoch, owner))
        self._run_id = run_id
        self.lease_epoch = new_epoch
        return new_epoch

    def takeover(self, run_id: str, expected_epoch: int, *, owner=None) -> int:
        if not _valid_epoch(expected_epoch):
            raise ValueError("expected_epoch must be a non-negative integer")
        with self.lock(run_id):
            return self.cas_locked(run_id, expected_epoch, owner)

    def acquire(self, run_id: str) -> bool:
        with self.lock(run_id):
            current = _read_lease_record(self.path(run_id))
            if current is not None and not current.get("released"):
                pid = current.get("pid")
                pid = pid if isinstance(pid, int) and not isinstance(pid, bool) else 0
                if pid == self.pid:
                    if current.get("run_id") == run_id:
                        return False
                elif pid > 0:
                    try:
                        os.kill(pid, 0)
                        return False
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        return False
                    except OSError:
                        pass
            epoch = _record_epoch(current)
            owner = current.get("owner") if current else None
            self._write(run_id, self._record(run_id, epoch, owner))
            self._run_id = run_id
            self.lease_epoch = epoch
            return True

    def ensure_current(self, run_id: str) -> None:
        if self.lease_epoch is None or self._run_id != run_id:
            raise SupervisorLeaseConflict("supervisor lease was never acquired for run %s" % run_id)
        found = read_lease_epoch(self.paths, run_id)
        if found != self.lease_epoch:
            raise SupervisorLeaseConflict(
                "supervisor lease epoch is stale: holding %s, current %s"
                % (self.lease_epoch, found)
            )

    def heartbeat(self, run_id: str) -> None:
        with self.lock(run_id):
            try:
                current = _read_lease_record(self.path(run_id))
            except SupervisorLeaseConflict:
                return
            if (
                current is None or current.get("pid") != self.pid
                or current.get("run_id") != run_id
                or _record_epoch(current) != self.lease_epoch
            ):
                return
            current["heartbeat"] = time.time()
            self._write(run_id, current)

    def release(self, run_id: str) -> None:
        with self.lock(run_id):
            try:
                current = _read_lease_record(self.path(run_id))
            except SupervisorLeaseConflict:
                return
            if (
                current is None or current.get("pid") != self.pid
                or _record_epoch(current) != self.lease_epoch
            ):
                return
            current["released"] = True
            self._write(run_id, current)


class Supervisor:
    def __init__(self, paths, monitor, runner, run_id: str, lease=None):
        self.paths = paths
        self.monitor = monitor
        self.runner = runner
        self.run_id = run_id
        self.lease = lease or SupervisorLease(paths)
        try:
            monitor.lease_guard = lambda: self.lease.ensure_current(self.run_id)
        except AttributeError:
            pass

    def recover_or_start(self, run_id: str = None) -> Dict[str, Any]:
        target = run_id or self.run_id
        ensure_caller_not_superseded(self.paths, target)
        acquired = self.lease.acquire(target)
        return {"stale_pid_recovered": acquired, "active_supervisors": 1 if acquired else 0}

    def run_once(self):
        ensure_caller_not_superseded(self.paths, self.run_id)
        if (
            getattr(self.lease, "_run_id", None) == self.run_id
            and getattr(self.lease, "lease_epoch", None) is not None
        ):
            # Already holding: a newer epoch means this supervisor is stale and
            # must stop before any write.
            self.lease.ensure_current(self.run_id)
        elif not self.lease.acquire(self.run_id):
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
        self.lease.ensure_current(self.run_id)
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
            self.lease.ensure_current(self.run_id)
            save_snapshot(self.paths, snapshot_for_reconcile)
        snapshot = self.monitor.tick(self.run_id, self.runner)
        if hasattr(snapshot, "run_id"):
            self.lease.ensure_current(self.run_id)
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
#: The pre-209 default, and the value an authorization that recorded no choice
#: still runs with.  Defined once in ``authorization`` (the card schema owns
#: which values a card may carry); this alias is the runtime's name for it.
DEFAULT_ROTATE_HARD_CAP = DEFAULT_ROTATE_HARD_CAP_TOKENS
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
        if _usage_present_but_invalid(entry):
            return None  # read as unknown, never as an older line's count
        if _session_tokens(entry) is not None:
            return entry
    return None


def _usage_present_but_invalid(entry):
    """The entry carries a WorkBuddy usage block with no readable input.

    Such a line means "usage was reported but malformed"; skipping it would
    fall back to an older line and report a stale count as current.
    """
    if not isinstance(entry, dict) or entry.get("type") != "function_call":
        return False
    message = entry.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return False
    value = usage.get("input_tokens")
    return not (isinstance(value, int) and not isinstance(value, bool)
                and value >= 0)


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
    message = record.get("message")
    if (not isinstance(usage, dict) and record.get("type") == "function_call"
            and isinstance(message, dict) and isinstance(message.get("usage"), dict)):
        # WorkBuddy function_call usage already counts cached tokens inside
        # input_tokens, whatever its total says; adding the cache fields again
        # double-counts the context.  A non-integer input is unknown, never
        # a reason to fall back to the additive Anthropic reading.
        value = message["usage"].get("input_tokens")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None
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


#: The field a host writes into its session record for the model's window.
_CONTEXT_WINDOW_FIELD = "model_context_window"


def _context_window(record):
    """The context window a session record reports, or None.

    Codex writes ``model_context_window`` on its token-count payload; Claude
    Code writes no window at all.  UNKNOWN stays UNKNOWN: guessing a window is
    how a cap the host can never reach gets offered as usable.
    """
    if not isinstance(record, dict):
        return None
    seen = [record]
    payload = record.get("payload")
    if isinstance(payload, dict):
        seen.append(payload)
        info = payload.get("info")
        if isinstance(info, dict):
            seen.append(info)
    for item in seen:
        value = item.get(_CONTEXT_WINDOW_FIELD)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def context_window_from_record(path):
    """Read a session record and return its reported context window, or None.

    A host appends the window on one line of a JSONL log, not necessarily the
    line that reports usage, so the file is scanned newest-first instead of
    reusing ``_read_json_file`` (which stops at the first usage-bearing line).
    """
    if path is None:
        return None
    try:
        # The protocol writes this flag as ``~/.codex/sessions/...``; without
        # expansion that path silently reads as "window unknown", which is how a
        # product manager who did hand over the record still gets "300k
        # unavailable" with no way to tell why.
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except (OSError, TypeError, UnicodeDecodeError):
        return None
    try:
        whole = json.loads(text)
    except ValueError:
        whole = None
    window = _context_window(whole)
    if window is not None:
        return window
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a line still being written
        window = _context_window(entry)
        if window is not None:
            return window
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


def _with_cap_reason(result, cap_reason):
    """Name a downgraded hard cap on a preflight result.

    The PRD patch requires the fallback to be stated, not silent.  Only ever
    called for a run whose authorization recorded a cap above the default and
    whose session cannot reach it, so a pre-209 run's payload keeps exactly the
    shape it had before the option existed.
    """
    if not cap_reason:
        return result
    result["rotate_hard_cap_downgrade"] = cap_reason
    existing = result.get("reason")
    result["reason"] = (
        "{}; {}".format(existing, cap_reason) if existing else cap_reason
    )
    return result


#: Common cause of a drifted capability contract, named so the supervisor can
#: act instead of guessing (#220): a git write operation (checkout, stash,
#: reset, merge) rewriting tracked ``.vibe`` run-state files mid-run; or a
#: platform handoff, which refreshes the contract on purpose.
CONTRACT_DRIFT_HINT = (
    "常见诱因：git 写操作（checkout/stash/reset/merge）改写了 .vibe 运行态文件，"
    "请检查 .vibe/session-contract.json 的改动来源；"
    "也可能是换平台交接刷新了合同（vibe monitor ... --platform-handoff [--adapter 平台]）"
)


def contract_drift(paths, run_id):
    """Compare the live capability contract with the run snapshot's digest.

    Returns ``None`` when they agree or the run never recorded a contract
    digest (nothing to compare), a ``contract_drift`` payload when they differ,
    and an ``unknown`` payload when the contract cannot be read -- an
    unreadable file is never taken as "consistent".  Read-only (#220).
    """
    try:
        snapshot = load_snapshot(paths, run_id)
    except (OSError, TypeError, ValueError):
        return None
    recorded = getattr(snapshot, "capability_contract_digest", "")
    if not isinstance(recorded, str) or not recorded:
        return None
    from .capability_contract import load_contract

    try:
        current = load_contract(paths).contract_digest
    except (OSError, TypeError, ValueError) as error:
        return {"state": "unknown",
                "reason": "capability contract unreadable: {}".format(str(error)[:160])}
    if current == recorded:
        return None
    return {
        "state": "contract_drift",
        "reason": "capability contract digest differs from the run snapshot",
        "recorded_digest": recorded,
        "current_digest": current,
        "action": (
            "走同一运行的重新授权流程（vibe authorize 同一 plan/run），不要继续推进；"
            "若是换平台交接，用 `vibe monitor --plan <plan> --authorize AUTHORIZE "
            "--platform-handoff` 重新授权：已验收节点保留验收，验收契约有变化时仍按 contract_drift 拒绝"
        ),
        "hint": CONTRACT_DRIFT_HINT,
    }


def supervisor_preflight(
    paths,
    run_id,
    session_record=None,
    *,
    token_threshold=DEFAULT_ROTATE_TOKEN_THRESHOLD,
    context_hard_cap=None,
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
      ``context_hard_cap=None`` means "the cap this run authorized": it is read
      from the run's authorization record (ISSUE-209) and downgraded to the
      default with a reason when this session's reported window cannot reach it.
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
    drift = contract_drift(paths, run_id)
    if drift is not None:
        return drift
    # Resolved before any state branch so a downgrade is named on every exit
    # path, including "unknown": a beat that cannot tell why its cap changed is
    # how a smaller takeover window turns into an unexplained late rotation.
    cap_reason = None
    if context_hard_cap is None:
        context_hard_cap, cap_reason = rotate_hard_cap_for_run(
            paths, run_id, session_record
        )
    tokens = _session_tokens(record)
    if tokens is None:
        return _with_cap_reason(
            {"state": "unknown", "reason": "token usage unknown"}, cap_reason
        )
    baseline = _takeover_baseline(paths, run_id, session_record)
    if token_threshold <= 0:
        return {"state": "unknown", "reason": "growth threshold must be positive"}
    if type(context_hard_cap) is not int or context_hard_cap <= DEFAULT_ROTATE_SOFT_CAP:
        return {"state": "unknown", "reason": "context hard cap must exceed soft cap"}
    if tokens >= context_hard_cap:
        return _with_cap_reason(
            {"state": "rotate", "reason": "context over hard cap",
             "tokens": tokens, "baseline": baseline,
             "context_hard_cap": context_hard_cap, "prepare_handoff": True},
            cap_reason,
        )
    config_warning = None
    if single_shift is None:
        try:
            single_shift, config_warning = load_supervisor_single_shift(paths.root)
        except ValueError:
            return {"state": "unknown", "reason": "supervisor_single_shift invalid"}
    if (not single_shift and tokens >= DEFAULT_ROTATE_SOFT_CAP and baseline is not None
            and tokens - baseline >= token_threshold):
        return _with_cap_reason(
            {"state": "rotate", "reason": "context grew past threshold since takeover",
             "tokens": tokens, "baseline": baseline,
             "context_hard_cap": context_hard_cap, "prepare_handoff": True},
            cap_reason,
        )
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
    if cap_reason:
        result = _with_cap_reason(result, cap_reason)
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


def _delivery_warnings(paths, run_id):
    """Advisory notes attached to this run's already-answered wait requests.

    Anything unreadable is skipped: an observation must never be the reason
    the mailbox state becomes unknown.
    """
    try:
        from .adapters.task_provider import ProviderActionStore

        return ProviderActionStore(paths).answered_wait_warnings(run_id)
    except Exception:
        return []


def _mailbox_state(paths, run_id, holds):
    state = _mailbox_state_inner(paths, run_id, holds)
    if isinstance(state, dict) and state.get("state") == "work":
        warnings = _delivery_warnings(paths, run_id)
        if warnings:
            # #240: the session-root warning is the only observation of the
            # sandbox a worker actually ran in.  Surfacing it here is what
            # makes it visible to the supervisor's next preflight; without it
            # the warning exists on disk and nobody reads it.
            state["warnings"] = warnings
    return state


def _mailbox_state_inner(paths, run_id, holds):
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

        mailbox = DecisionMailbox(paths)
        decisions = mailbox.list_pending(run_id)
        answered = mailbox.list_answered(run_id)
    except (OSError, TypeError, ValueError) as error:
        return {"state": "unknown", "reason": "decision mailbox unreadable: {}".format(error)}
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
    # An answered decision is closed: its hold no longer parks anything, and a
    # node still in blocked_design behind it waits for the supervisor's resume
    # to consume the answer, not for the human.
    answered_ids = {entry["decision_id"] for entry in answered}
    holds = {node: entry for node, entry in holds.items()
             if entry.get("decision_id") not in answered_ids}
    unconsumed_answers = sorted(
        node for node, status in statuses.items()
        if status == "blocked_design" and answered_ids.intersection(
            (snapshot.nodes.get(node) or {}).get("decision_ids") or ())
    )
    if unconsumed_answers:
        return {"state": "work", "reason": "answered decisions pending (resume consumes the answer)",
                "nodes": unconsumed_answers, "decisions": decisions}
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
    return {node: _normalized_hold(entry) for node, entry in holds.items()}


def _normalized_hold(entry):
    """Canonical category on read: aliases map over, a missing one is legacy.

    Any other present value is tampering or corruption and fails closed.
    """
    if entry.get("category") is None:
        entry = dict(entry)
        entry["category"] = LEGACY_BOUNDARY_CATEGORY
        entry["category_source"] = "legacy_default"
        return entry
    try:
        category = validate_boundary_category(entry["category"])
    except ValueError:
        raise ValueError("supervisor holds malformed") from None
    if entry["category"] != category:
        entry = dict(entry)
        entry["category"] = category
    return entry


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


def set_supervisor_hold(paths, run_id, node, reason, boundary_category=None,
                        decision_id=None, authorization_gap=None):
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
    has_decision = isinstance(decision_id, str) and bool(decision_id.strip())
    has_gap = isinstance(authorization_gap, str) and bool(authorization_gap.strip())
    if decision_id is not None and not has_decision or authorization_gap is not None and not has_gap:
        raise ValueError("hold reference must be a non-empty string")
    if has_decision == has_gap:
        raise ValueError(
            "hold must reference a decision id (--decision) or an authorization gap "
            "(--authorization-gap), exactly one of them"
        )
    reference = {}
    if has_decision:
        decision_id = decision_id.strip()
        try:
            from .decisions import DecisionMailbox
            decision = DecisionMailbox(paths)._load_request(decision_id)
        except (OSError, ValueError):
            raise ValueError("decision {} not found".format(decision_id)) from None
        if decision.get("run_id") != run_id or decision.get("node_id") != node:
            raise ValueError("decision {} belongs to another run or node".format(decision_id))
        if decision.get("category") != boundary_category:
            raise ValueError("decision {} has category {}, not {}".format(
                decision_id, decision.get("category"), boundary_category))
        reference["decision_id"] = decision_id
    else:
        gap = authorization_gap.strip()
        if len(gap) > _HOLD_REASON_LIMIT:
            raise ValueError("authorization gap is limited to {} characters".format(_HOLD_REASON_LIMIT))
        reference["authorization_gap"] = gap
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
        **reference,
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


def _same_session(left, right) -> bool:
    """Session identity is provider + session_id; host spelling may vary."""
    return (
        isinstance(left, dict) and isinstance(right, dict)
        and left.get("provider") == right.get("provider")
        and left.get("session_id") == right.get("session_id")
    )


_CALLER_SESSION_ENV = (
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID",
    "CODEBUDDY_SESSION_ID", "CODEX_THREAD_ID",
)


def caller_session_ids(environ=None):
    """All non-empty host session ids in the environment (deduplicated, ordered)."""
    environ = os.environ if environ is None else environ
    found = []
    for name in _CALLER_SESSION_ENV:
        value = environ.get(name)
        if isinstance(value, str) and value and value not in found:
            found.append(value)
    return tuple(found)


def superseded_caller_reason(paths, run_id, session_ids):
    """Reason string when the caller is a superseded supervisor, else None.

    ``session_ids`` are all ids the caller's host exposes; any one matching the
    registry current means it is the current supervisor.

    Missing, unreadable or malformed registry keeps the old behavior (None).
    """
    if not session_ids:
        return None
    try:
        registry = _read_json_file(_registry_path(paths, run_id))
    except (OSError, ValueError):
        return None
    if not isinstance(registry, dict):
        return None
    current = registry.get("current")
    if isinstance(current, dict) and current.get("session_id") in session_ids:
        return None
    history = registry.get("history")
    stale = None
    if isinstance(history, list):
        stale = next((
            item.get("session_id") for item in history
            if isinstance(item, dict) and item.get("session_id") in session_ids
        ), None)
    if stale is not None:
        successor = current.get("session_id") if isinstance(current, dict) else None
        epoch = current.get("lease_epoch") if isinstance(current, dict) else None
        return (
            "supervisor session %s was superseded by %s (lease epoch %s); exit read-only; "
            "run this from the current supervisor session or a new session"
            % (stale, successor, epoch)
        )
    return None


def ensure_caller_not_superseded(paths, run_id, environ=None):
    reason = superseded_caller_reason(paths, run_id, caller_session_ids(environ))
    if reason:
        raise SupervisorLeaseConflict(reason)


def _write_registry(path, registry):
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


def _session_cwd_from_record(session_record):
    """Best-effort extraction of the session's own working directory.

    Codex Desktop writes it once as ``session_meta.payload.cwd``; Claude Code
    repeats a top-level ``cwd`` on its entries.  ``_read_json_file`` is not
    reused here: it returns the newest *usage*-bearing line, which need not
    carry the cwd, while the line that does usually sits at the top.  Advisory
    only -- anything unreadable or missing returns None.
    """
    try:
        text = Path(session_record).expanduser().read_text(encoding="utf-8")
    except (OSError, TypeError, UnicodeDecodeError, ValueError):
        return None
    try:
        entries = [json.loads(text)]
    except ValueError:
        entries = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue  # a line still being written
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        payload = entry.get("payload")
        if entry.get("type") == "session_meta" and isinstance(payload, dict):
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                return cwd.strip()
        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            return cwd.strip()
    return None


def _session_meta(session_record):
    """The Codex ``session_meta`` payload, or None when absent or unreadable."""
    try:
        with Path(session_record).expanduser().open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("type") == "session_meta":
                    payload = entry.get("payload")
                    return payload if isinstance(payload, dict) else None
    except (OSError, TypeError, UnicodeDecodeError, ValueError):
        return None
    return None


def _successor_refusal(session_record):
    """Why a successor shift must not take over, or None.

    run-63e7cfc2 (2026-10-26): the successor was a Codex ``fork_thread`` copy
    of the outgoing supervisor and started at ~297k tokens, so rotating moved
    the whole history along and the next rotation came one growth step later.
    A successor exists to start light; one that does not is refused before the
    lease moves, and the outgoing shift stays in charge.  The session's own
    record is the only evidence of how it was created, so a successor that
    does not supply a readable one is refused too: otherwise leaving the flag
    off would be the way around the fork check.
    """
    if session_record is None:
        return ("successor registration requires --session-record <this session's own log>; "
                "without it a forked session cannot be told apart from a fresh one")
    path = Path(str(session_record)).expanduser()
    if not path.is_absolute() or not path.is_file():
        return ("successor --session-record must be the absolute path of this session's "
                "existing log, got {}".format(session_record))
    meta = _session_meta(session_record)
    forked = meta.get("forked_from_id") if meta else None
    if isinstance(forked, str) and forked:
        return ("successor session is a fork of {} and carries its full history; "
                "create a fresh session in the project instead of fork_thread".format(forked))
    record = _read_json_file(session_record)
    tokens = _session_tokens(record) if record is not None else None
    if tokens is not None and tokens >= DEFAULT_ROTATE_SOFT_CAP:
        return ("successor session already holds {:,} tokens (limit {:,}); "
                "take over from a fresh session that only runs supervisor-handoff".format(
                    tokens, DEFAULT_ROTATE_SOFT_CAP))
    return None


def session_cwd_warning(paths, session_record):
    """Advisory: the registering session lives outside the project root.

    Issue #231: a projectless Codex Desktop session starts in a host-generated
    directory whose creation-time sandbox profile lacks the project root, so
    every read or write of ``.vibe/`` raises its own approval prompt, and
    re-filing the thread into the project afterwards does not backfill the
    profile.  The session record is the only ground truth for the session's
    own cwd -- the CLI process cannot see it, since each command may be
    prefixed with ``cd <project root>``.  Missing or unreadable records stay
    silent: absence of the field is not evidence of a bad session, and this
    warning must never be the reason a registration fails.
    """
    if session_record is None:
        return None
    raw = _session_cwd_from_record(session_record)
    if raw is None:
        return None
    try:
        session = Path(raw).expanduser()
        if not session.is_absolute():
            return None
        session = session.resolve()
        root = Path(paths.root).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if session == root or root in session.parents:
        return None
    # A cwd in another checkout of the same repository (the main checkout of a
    # linked-worktree root, or a sibling worktree) is a project session, not a
    # projectless one: run-63e7cfc2's successor ran from the main checkout and
    # wrote .vibe without a prompt (2026-10-27).
    common = _git_common_dir(root)
    if common is not None and _git_common_dir(session) == common:
        return None
    return (
        "登记会话的 cwd（{}）不在项目根（{}）内：该会话很可能是 projectless 创建，"
        "创建时的沙箱权限档案不含项目根，读写 .vibe/ 的命令会逐条弹审批，"
        "且事后把会话归入项目不回溯（#231 实测）。登记仍然生效；"
        "建议改在本项目下重建会话（Codex 桌面用 create_thread 的 project 目标）再接班。"
        .format(session, root)
    )


def _gitdir_file_target(directory):
    """Where ``<directory>/.git`` points when it is a ``gitdir:`` file, else None."""
    marker = Path(directory) / ".git"
    try:
        if not marker.is_file():
            return None
        text = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:"):].strip())
    return gitdir if gitdir.is_absolute() else Path(directory) / gitdir


def _git_common_dir(path):
    """The repository's shared ``.git`` directory for ``path``, or None.

    Walks up to the nearest ``.git``; a linked worktree's ``gitdir:`` file is
    followed to ``<common>/worktrees/<name>`` and mapped back to ``<common>``.
    """
    current = Path(path)
    for candidate in (current, *current.parents):
        marker = candidate / ".git"
        try:
            if marker.is_dir():
                return marker.resolve()
        except OSError:
            return None
        gitdir = _gitdir_file_target(candidate)
        if gitdir is not None:
            if gitdir.parent.name == "worktrees":
                gitdir = gitdir.parent.parent
            try:
                return gitdir.resolve()
            except (OSError, RuntimeError):
                return None
    return None


def project_root_worktree_warning(paths):
    """Advisory: the project root is a linked git worktree, not the main checkout.

    run-63e7cfc2 (2026-10-26): the run lived in a Codex Desktop host worktree
    (``~/.codex/worktrees/...``) while the host granted new sessions write
    access to the main checkout only, so no successor -- however it was
    created -- could write ``.vibe``.  A ``.git`` *file* pointing into another
    repository's ``worktrees/`` directory is the structural sign; anything
    else stays silent.
    """
    gitdir = _gitdir_file_target(Path(paths.root))
    if gitdir is None or gitdir.parent.name != "worktrees":
        return None
    main = gitdir.parent.parent.parent
    try:
        main = main.resolve()
    except (OSError, RuntimeError):
        pass
    return (
        "项目根 {} 是 git 工作副本，不是主仓库 {}：宿主为新会话授予的可写目录"
        "通常是主仓库，接班会话可能写不了这里的 .vibe/，换班会中断。"
        "建议在主仓库运行 vibe，或确认新会话对本目录有写权限（run-63e7cfc2 实测）。"
        .format(Path(paths.root).resolve(), main)
    )


def register_supervisor_address(paths, run_id, address, *, session_record=None, expected_epoch=None):
    """Atomically record the current supervisor address; keeps history.

    With ``session_record`` (the shift's own session log) the current context
    is stored as the takeover baseline the rotation threshold counts from,
    keyed by a digest of the log path rather than the path itself.  An
    unreadable log still registers -- the wake-up address matters more -- and
    the entry simply carries no baseline.

    A new (successor) session upgrades the lease epoch by compare-and-swap
    against ``expected_epoch`` -- by default the epoch observed before taking
    the lock -- so two candidates racing for the same run cannot both win.
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
    if expected_epoch is None:
        # The lease file is authoritative; the registry can lag it after a
        # crash between the CAS and the registry write.
        expected_epoch = read_lease_epoch(paths, run_id)
    lease = SupervisorLease(paths)
    with lease.lock(run_id):
        registry = _read_json_file(path)
        if not isinstance(registry, dict):
            registry = {}
        history = registry.get("history")
        if not isinstance(history, list):
            history = []
        rejected = registry.get("rejected")
        if not isinstance(rejected, list):
            rejected = []
        current = registry.get("current")
        lease_record = _read_lease_record(lease.path(run_id))
        on_disk = _record_epoch(lease_record)
        disk_owner = lease_record.get("owner") if lease_record else None
        if not isinstance(disk_owner, dict):
            disk_owner = None
        this_session = {"provider": provider, "session_id": session_id}
        owns_disk = _same_session(disk_owner, this_session)

        def reject(reason):
            rejected.append({
                "provider": provider, "session_id": session_id, "host": host,
                "expected_epoch": expected_epoch, "found_epoch": on_disk,
                "reason": reason, "at": time.time(),
            })
            kept = {"history": history, "rejected": rejected[-20:]}
            if isinstance(current, dict):
                kept["current"] = current
            _write_registry(path, kept)
            raise SupervisorLeaseConflict(reason)

        same_shift = _same_session(current, this_session)
        recovered_owner = None
        if owns_disk and not same_shift:
            # This session already won the CAS but died before the registry
            # write: repair the registry without a second epoch bump.
            epoch = on_disk
        elif not same_shift and any(
            isinstance(item, dict) and item.get("session_id") == session_id
            for item in history
        ):
            reject("superseded supervisor %s cannot re-register" % session_id)
        elif same_shift:
            if disk_owner is not None and not owns_disk:
                reject("supervisor lease is owned by another session")
            epoch = on_disk
        else:
            if isinstance(current, dict):
                refusal = _successor_refusal(session_record)
                if refusal:
                    reject(refusal)
            try:
                epoch = lease.cas_locked(
                    run_id, expected_epoch,
                    {"provider": provider, "session_id": session_id, "host": host},
                )
            except SupervisorLeaseConflict as error:
                reject(str(error))
            if disk_owner is not None and not _same_session(disk_owner, current):
                # The previous disk owner won its CAS but never reached the
                # registry; record it as superseded so it cannot re-register.
                recovered_owner = {
                    "provider": disk_owner.get("provider"),
                    "session_id": disk_owner.get("session_id"),
                    "host": disk_owner.get("host"),
                    "lease_epoch": on_disk,
                    "registered_at": time.time(),
                    "recovered_from_lease": True,
                }
        if isinstance(current, dict):
            history.append(current)
        if recovered_owner is not None:
            history.append(recovered_owner)
        entry = {
            "provider": provider,
            "session_id": session_id,
            "host": host,
            "registered_at": time.time(),
            "lease_epoch": epoch,
        }
        # SendMessage reaches a session by its name, not its id (real-machine
        # trial 2026-10-23: the id was "not reachable", the name delivered).
        wake_target = address.get("wake_target")
        if isinstance(wake_target, str) and wake_target.strip():
            entry["wake_target"] = wake_target.strip()
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
        _write_registry(
            path,
            {"current": entry, "history": history, "rejected": rejected[-20:]},
        )
    return entry


#: The push tool a worker uses to wake a supervisor, per host family.  Issue
#: #208: dispatch text used to say "the authorized channel, fixed format"
#: without naming either, so workers could only skip the wake-up.  A host is
#: listed here only with real receipt evidence (Codex: #167); the others say
#: plainly that there is no channel and the heartbeat collects the delivery.
WAKE_CHANNELS = {
    "codex": "send_message_to_thread",
    # Registered as a channel (#220 follow-up, 2026-10-23); it still counts as
    # *verified* only once .vibe/push-wake-verification.json carries both a
    # success and a failure evidence for claude-code.
    "claude": "SendMessage",
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


def wake_channel(provider):
    """Return ``(channel, reason)`` for push wake-ups on ``provider``.

    One definition shared by :func:`wake_instruction` (which also needs the
    target and the signal) and the authorization card's 30-minute availability:
    a card must never offer a cadence the dispatch path could not wake.
    """
    family = _provider_family(provider)
    channel = WAKE_CHANNELS.get(family)
    if channel is None:
        return None, WAKE_NO_CHANNEL_REASON.get(
            family,
            "平台 {} 没有已登记的推送通道，等监工心跳拉取".format(provider or "未知"),
        )
    return channel, ""


def wake_instruction(address, run_id, node_id=None, role=None, generation=None):
    """Return how a worker wakes ``address``: channel, target and signal."""
    channel, reason = wake_channel(address.get("provider"))
    if channel is None:
        return {"channel": None, "reason": reason}
    return {
        "channel": channel,
        "target": address.get("wake_target") or address.get("session_id"),
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
        "lease_epoch": read_lease_epoch(paths, run_id),
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
    "WorkBuddy 两条都得在本会话里手动做（登记地址、建心跳），不会自己发生，"
    "且没有合法的无焦点自动换班入口——rotate 只能由用户在本项目新开会话接班"
    "（Jobs detached 子进程拿不到模型 run identity，凭证边界为宿主设计使然）。"
)


#: One source for the beat cadence.  Default 10 minutes; 30 once a platform's
#: push wake-up is registered as verified (success and failure evidence both);
#: 60 while the only open item is an unanswered human decision.
HEARTBEAT_INTERVAL_DEFAULT_MINUTES = 10
HEARTBEAT_INTERVAL_PUSH_VERIFIED_MINUTES = 30
HEARTBEAT_INTERVAL_DECISION_MINUTES = 60
PUSH_WAKE_VERIFICATION_RELATIVE = ".vibe/push-wake-verification.json"
_HEARTBEAT_INTERVAL_FILE = "heartbeat-interval.json"


#: Provider spellings that name the same push wake-up path.  The control-plane
#: names come from ``providers``, which owns them; the other entries are adapter
#: ids -- a manifest's ``id`` field, which has no constant of its own and is
#: spelled the same way by ``supervisor_wakeup``.  ``-background`` is
#: deliberately absent: it is a different wake path, and substituting its
#: registration would offer 30 minutes on a visible task whose wake-up was never
#: verified.
_WAKE_PROVIDER_GROUPS = {
    "codex": ("codex", "codex-app", CODEX_PROVIDER),
    "claude": ("claude-code", CLAUDE_CODE_PROVIDER),
    "workbuddy": ("workbuddy", WORKBUDDY_VISIBLE_PROVIDER),
}


def _provider_family(provider):
    """The platform family a provider string belongs to, or None.

    Used where the *platform* is what matters (the wake channel is per platform,
    not per dispatch path), so any spelling of a platform resolves the same.
    """
    value = str(provider or "").lower()
    return next(
        (name for name in ("codex", "claude", "workbuddy") if value.startswith(name)),
        None,
    )


def _wake_provider_group(provider):
    """The push wake-up path a provider spelling belongs to, or None.

    Stricter than :func:`_provider_family` on purpose: the push-wake registry is
    keyed per wake path, so ``codex-background`` must never answer for
    ``codex-app-visible`` even though both are the codex platform.
    """
    value = str(provider or "").lower()
    return next(
        (family for family, names in _WAKE_PROVIDER_GROUPS.items() if value in names),
        None,
    )


def _push_wake_entry(platforms, provider):
    """The registry entry for a wake path: exact key first, then its aliases.

    Iterates the declared alias order rather than the registry's key order, so
    which spelling a project happened to register cannot change the answer.
    """
    entry = platforms.get(provider)
    if isinstance(entry, dict):
        return entry
    group = _wake_provider_group(provider)
    if group is None:
        return None
    for name in _WAKE_PROVIDER_GROUPS[group]:
        candidate = platforms.get(name)
        if isinstance(candidate, dict):
            return candidate
    return None


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
    entry = _push_wake_entry(platforms, provider)
    if not isinstance(entry, dict):
        return False, "push wake for {} is not registered".format(provider)
    for kind in ("success", "failure"):
        records = entry.get(kind)
        if not (isinstance(records, list) and any(
                isinstance(item, dict) and isinstance(item.get("evidence_ref"), str)
                and item["evidence_ref"].strip() for item in records)):
            return False, "push wake for {} lacks a {} evidence".format(provider, kind)
    return True, "push wake for {} verified with success and failure evidence".format(provider)


def _push_wake_available(paths, provider):
    """Return ``(available, reason)`` for the 30-minute idle base."""
    if not isinstance(provider, str) or not provider:
        return False, "supervisor provider unknown: the platform's push wake-up cannot be checked"
    verified, reason = _push_wake_verified(paths, provider)
    if not verified:
        return False, reason
    channel, channel_reason = wake_channel(provider)
    if channel is None:
        return False, channel_reason
    return True, reason


#: What choosing 30 minutes costs when push wake-up is not verified.
FORCED_INTERVAL_COST = "worker 完工后监工最长 30 分钟才会发现"


def heartbeat_interval_options(paths, provider):
    """Every idle base a card may offer, with availability and the reason.

    Unavailable tiers are listed, never hidden (product decision 2026-10-13):
    the product manager should see that 30 minutes exists and why it cannot be
    chosen yet, not silently get a two-option card on one platform and a
    one-option card on another.
    """
    entries = []
    for minutes in HEARTBEAT_INTERVAL_CHOICES:
        if minutes == HEARTBEAT_INTERVAL_DEFAULT_MINUTES:
            entries.append({"minutes": minutes, "available": True, "reason": ""})
            continue
        available, reason = _push_wake_available(paths, provider)
        if not available and isinstance(provider, str) and provider:
            # No verified push wake-up: 30 stays selectable as a forced choice
            # (product decision 2026-10-23) with its price on the card.  An
            # unknown provider cannot be judged, so it stays unavailable.
            entries.append({"minutes": minutes, "available": True, "reason": reason,
                            "forced": True, "cost": FORCED_INTERVAL_COST})
            continue
        entries.append({"minutes": minutes, "available": available, "reason": reason})
    return entries


def rotate_hard_cap_options(window):
    """Every rotation hard cap a card may offer, with availability and reason.

    A cap above the default is offered only when the host reports a window at
    least that large.  Both hosts compact before then -- Claude Code's standard
    window is 200k, and Codex compacted at 218,097 of a reported 249,036 -- so a
    cap the host would compact before reaching never fires, and choosing it
    would silently mean "never rotate".
    """
    entries = []
    for tokens in ROTATE_HARD_CAP_CHOICES:
        available, reason = True, ""
        if tokens > DEFAULT_ROTATE_HARD_CAP:
            if window is None:
                available = False
                reason = (
                    "host context window unknown: this host reports no window, so a "
                    "{:,} cap could never be reached before the host compacts".format(tokens)
                )
            elif window < tokens:
                available = False
                reason = (
                    "host reports a {:,}-token context window, so a {:,} cap could "
                    "never be reached before the host compacts".format(window, tokens)
                )
        entries.append({"tokens": tokens, "available": available, "reason": reason})
    return entries


def authorization_choices(paths, run_id):
    """The ISSUE-209 choices this run's authorization recorded.

    Absent keys stay absent rather than being defaulted: a run authorized
    before the option existed keeps exactly the cadence and hard cap it ran
    with before, which is what the acceptance criterion requires.
    """
    try:
        snapshot = load_snapshot(paths, run_id)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}
    # A run whose snapshot cannot name its plan has no authorization to read;
    # tests and recovery paths hand in stand-in snapshots, so this stays a
    # lookup rather than an attribute that must exist.
    plan_id = getattr(snapshot, "plan_id", None)
    if not isinstance(plan_id, str) or not plan_id:
        return {}
    try:
        path = paths.resolve_vibe_path(Path("plans") / plan_id / "authorization.json")
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    choices = {}
    for key, allowed in (
        ("heartbeat_interval_minutes", HEARTBEAT_INTERVAL_CHOICES),
        ("rotate_hard_cap", ROTATE_HARD_CAP_CHOICES),
    ):
        value = data.get(key)
        # The record is signed, but a corrupted or hand-edited file must not be
        # able to set a cap the card vocabulary does not contain: an arbitrary
        # integer there is exactly the "silently never rotate" the option exists
        # to prevent, so anything outside the offered tiers reads as absent.
        if type(value) is int and value in allowed:
            choices[key] = value
    # A forced 30-minute choice is written into the signed authorization record
    # itself (with its stated price); honour it only for that exact tier.
    if (choices.get("heartbeat_interval_minutes") is not None
            and data.get("heartbeat_interval_forced") is True
            and isinstance(data.get("heartbeat_interval_forced_cost"), str)
            and data["heartbeat_interval_forced_cost"].strip()):
        choices["heartbeat_interval_forced"] = True
    return choices


def rotate_hard_cap_for_run(paths, run_id, session_record):
    """Return ``(effective hard cap, downgrade reason or None)`` for this run.

    The authorized cap applies as-is unless it is above the default and this
    session's reported window cannot reach it: a takeover session may be
    smaller than the one the plan was published from, and the PRD patch requires
    falling back to the default *and* naming the reason, never rotating late
    without saying so.
    """
    chosen = authorization_choices(paths, run_id).get("rotate_hard_cap")
    if chosen is None:
        return DEFAULT_ROTATE_HARD_CAP, None
    if chosen <= DEFAULT_ROTATE_HARD_CAP:
        return chosen, None
    window = context_window_from_record(session_record)
    if window is None:
        return DEFAULT_ROTATE_HARD_CAP, (
            "authorization chose a {:,}-token rotation hard cap but this session "
            "reports no context window; using the default {:,}".format(
                chosen, DEFAULT_ROTATE_HARD_CAP)
        )
    if window < chosen:
        return DEFAULT_ROTATE_HARD_CAP, (
            "authorization chose a {:,}-token rotation hard cap but this session "
            "reports a {:,}-token window; using the default {:,}".format(
                chosen, window, DEFAULT_ROTATE_HARD_CAP)
        )
    return chosen, None


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
    verified, reason = _push_wake_available(paths, provider)
    choices = authorization_choices(paths, run_id)
    chosen = choices.get("heartbeat_interval_minutes")
    if chosen == HEARTBEAT_INTERVAL_DEFAULT_MINUTES:
        # The product manager chose the default, so a verified push wake-up does
        # not quietly upgrade the cadence behind their back.
        return {"minutes": HEARTBEAT_INTERVAL_DEFAULT_MINUTES, "mode": "default",
                "reason": "authorization selected the {}-minute base".format(chosen),
                "provider": provider}
    if chosen is not None:
        if verified:
            return {"minutes": chosen, "mode": "push_verified",
                    "reason": reason, "provider": provider}
        if choices.get("heartbeat_interval_forced"):
            # The card marked this 30 as a forced choice and stated its price:
            # honour the authorization instead of silently returning to 10.
            return {"minutes": chosen, "mode": "forced",
                    "reason": "authorization forced {} minutes ({}); {}".format(
                        chosen, reason, FORCED_INTERVAL_COST),
                    "provider": provider}
        return {"minutes": HEARTBEAT_INTERVAL_DEFAULT_MINUTES, "mode": "default",
                "reason": "authorization selected {} minutes but {}".format(chosen, reason),
                "provider": provider}
    # No recorded choice: a run authorized before the option existed keeps the
    # pre-209 rule, where a verified push wake-up is enough on its own.
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
        "idle_streak": interval.get("idle_streak", 0),
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
            if entry.get("decision_id"):
                reference = "决策 " + str(entry["decision_id"])
            elif entry.get("authorization_gap"):
                reference = "授权缺口 " + str(entry["authorization_gap"])
            else:
                reference = "无引用（旧记录，下次刷新须补）"
            lines.append("  - {}（类别 {}；{}）：{}（已挂起 {}）".format(
                node, entry.get("category") or "unknown", reference, entry.get("reason"),
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
            lines.append("  - {}（类别 {}）：{}（选项：{}）".format(
                decision.get("decision_id"), decision.get("category"), decision.get("question"),
                ", ".join(option.get("id", "") for option in decision.get("options", [])),
            ))
    lines += [
        "接班步骤（新会话必须建在本项目里，禁止 projectless：否则 cwd 落在宿主自动生成目录，"
        "创建时的沙箱权限档案不含项目根，每条读写 .vibe/ 的命令都逐条弹审批，"
        "事后把会话归入项目不回溯——#231 实测；"
        "Codex 桌面用 create_thread 的 project 目标，Claude Code 在仓库根目录启动新会话）。"
        "必须是全新会话：不要用 fork_thread 或复制上一班会话——fork 会带上整段历史，"
        "登记会被拒绝（run-63e7cfc2 实测接班即 29.7 万）。"
        "接班会话写不了 .vibe/ 时（session_gate_blocked: host sandbox denied access）不要重试、不要反复申请审批，"
        "停下把目录报给用户：",
        "  1) 登记：vibe supervisor-register --run-id {} --provider <平台> "
        "--session-id <本会话 id> --host <本机标识> --session-record <本会话记录路径>".format(run_id),
        "     " + SESSION_RECORD_HINT,
        "  2) 自建心跳，指令逐字用下面这段，只替换 <本会话记录路径>：",
        heartbeat_command(run_id, command_prefix=command_prefix),
        "  3) 置顶、改标题；删掉上一班的心跳，归档上一班会话"
        "（Claude Code 没有换班原语：上一班结束会话即可；"
        "WorkBuddy 无自动换班入口，新会话由用户在本项目 UI 里手动开启）。",
        "  4) 跑一次心跳指令第 1 步的预检，按输出走。",
        HOST_SHIFT_DIFFERENCES,
        "监工挂起某个节点等用户拍板时，先登记：vibe supervisor-hold --run-id {} "
        "--node <节点> --category <product_acceptance|external_authorization> "
        "--decision <决策ID>|--authorization-gap <缺口> --reason <一句话原因>，"
        "否则每次心跳都会被当成有事做。product_design/deploy 仍可作别名，落盘一律存规范名；"
        "没有 category 的旧挂起按 product_acceptance 读取，下次刷新须补上 category 和引用。"
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
            "按工具清单里的全名重试一次，仍失败即停止服务信箱、向用户报告工具名路由故障，不交班、不开新会话。Codex 的 create 只用 create_thread（project 目标原样传），不得用 fork_thread 代替：fork 会带上本会话整段历史，complete() 查到 forked_from_id 即拒收并抛 ForkedProviderSession；新会话写不了工作副本时停下报告宿主权限，不要改用 fork。工程失败按现有恢复协议继续，"
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
    # ISSUE-280: ``vibe status --json`` readers rely on ``nodes`` being a
    # stable top-level object keyed by node id.  The byte budget may truncate
    # per-node detail, but it must never silently change the top-level shape;
    # the full object is always recoverable through ``output_evidence``.
    nodes = payload.get("nodes")
    if isinstance(nodes, dict):
        for project in (
                lambda node: {
                    key: str(node.get(key, ""))[:200]
                    for key in ("status", "user_status") if node.get(key)},
                lambda node: {},
        ):
            short["nodes"] = {
                str(node_id): project(node) if isinstance(node, dict) else {}
                for node_id, node in nodes.items()
            }
            if len(json.dumps(short, ensure_ascii=False).encode("utf-8")) <= SUPERVISOR_OUTPUT_LIMIT:
                break
    return json.dumps(short, ensure_ascii=False, sort_keys=True)


_CARD_SUMMARY_KEYS = ("plan_id", "plan_version", "digest", "node_contract_digest", "active_pair_limit",
                      "remote_git_actions", "engine_evidence_ref", "execution_engine", "engine_mode")


def authorization_card_summary(paths, plan_id, card=None):
    """Screen-sized card view; the on-disk card stays the full authority.

    ``card`` may be None (authorize has only the plan id).  A card file that
    cannot be read reports ``unknown`` rather than an empty summary.
    """
    path = paths.resolve_vibe_path(Path("plans") / plan_id / "authorization-card.json")
    try:
        raw = path.read_bytes()
        if card is None:
            card = json.loads(raw.decode("utf-8"))
        if not isinstance(card, dict):
            raise ValueError("authorization card must be an object")
    except (OSError, ValueError) as error:
        return {"status": "unknown", "path": str(path), "reason": "authorization card unreadable: {}".format(str(error)[:120])}
    summary = {k: card[k] for k in _CARD_SUMMARY_KEYS if k in card}
    summary.update({
        "status": "ok", "path": str(path.resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
        "node_ids": list(card.get("node_ids") or []),
    })
    return summary


_MAILBOX_KEYS = ("action_id", "operation", "native_tool", "issue_id", "role", "generation", "sequence", "request_digest")


def supervisor_mailbox_view(paths, run_id):
    """Pending provider requests with prompt bodies replaced by {sha256, bytes, path}."""
    from .adapters.task_provider import ProviderActionStore
    try:
        pending = ProviderActionStore(paths).pending(run_id)
    except (OSError, ValueError) as error:
        return {"command": "supervisor-mailbox", "status": "unknown", "run_id": run_id,
                "reason": "provider mailbox unreadable: {}".format(str(error)[:160])}
    items = []
    for action in pending:
        item = {k: action[k] for k in _MAILBOX_KEYS if k in action}
        path = paths.vibe / "provider-actions/requests" / (str(action.get("action_id")) + ".json")
        request = action.get("request")
        if isinstance(request, dict):
            if isinstance(request.get("child_binding"), dict):
                item["child_binding"] = request["child_binding"]
            if "prompt" in request:
                prompt = request["prompt"]
                body = prompt.encode("utf-8") if isinstance(prompt, str) else _json_bytes(prompt)
                item["prompt"] = {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                                  "path": str(path.resolve())}
        items.append(item)
    return {"command": "supervisor-mailbox", "status": "ok", "run_id": run_id,
            "pending_count": len(items), "pending": items}
