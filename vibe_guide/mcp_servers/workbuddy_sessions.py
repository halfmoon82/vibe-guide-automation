"""Window-session dispatch for WorkBuddy, plus the mailbox servicer.

Why this module exists
----------------------
``POST /api/v1/jobs`` spawns a *fresh* CLI process and that process never
obtains model credentials.  Measured on WorkBuddy AI 5.6.2 / CLI 2.147.0
(macOS, 2026-10-08): 48 jobs on disk, 42 agent jobs stuck at
``detail="starting…"`` with 0-byte logs, the single non-empty one reading
``Authentication required. Please use /login command to sign in to your
account``.  The six that finished were all ``bash: true`` echo probes.

The mechanism is in ``cli/dist/codebuddy-lite-wb.mjs`` (module 5159): the CLI
reads ``CODEBUDDY_SIDECAR_CREDENTIAL_BOOTSTRAP_SOCKET`` and three siblings at
startup and immediately ``delete``\\ s them from ``process.env``, so no child
process ever inherits them.  The bootstrap socket is also one-shot — it only
answers inside the launch window of the process the sidecar itself spawned —
so setting those variables by hand afterwards yields ``receiver-timeout``,
not credentials.  Injecting them is not a fix.

What does work is dispatching into a window session that is *already running*:
it holds credentials obtained at its own startup.  Measured::

    POST {endpoint}/api/v1/sessions/{sid}/reply   {"text": "…"}
      -> {"data": {"delivered": true}}
    GET  {endpoint}/api/v1/sessions/{sid}/history
      -> {"data": {"requests": [{"userInput": …, "finalReply": …}]}}

A delivered prompt there produced a **real upstream model response** (a 429
quota error), categorically different from a background job's silent hang.

Consequences that shape this module:

* Concurrency is bounded by the number of open windows.
* A window handles one turn at a time (``writerOccupied``).
* Windows can only be created by the sidecar (the user opening a conversation);
  ``POST /api/v1/sessions`` answers 404, so no API can mint a credentialed one.

Stdlib only, like its sibling ``workbuddy_jobs``: it must start from a bare
interpreter with no install step.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

try:  # POSIX only; on other platforms the lock degrades to a no-op.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

#: Shared with ``workbuddy_jobs`` on purpose: one override name for the
#: gateway password, so a session that already knows it does not need two.
TOKEN_ENV = "WORKBUDDY_JOB_TOKEN"
SESSIONS_ROOT_ENV = "WORKBUDDY_SESSIONS_DIR"
EXCLUDE_SIDS_ENV = "WORKBUDDY_DISPATCH_EXCLUDE_SIDS"
HANDLE_ROOT_ENV = "WORKBUDDY_HANDLE_ROOT"
PROJECT_DIR_ENVS = ("CODEBUDDY_PROJECT_DIR", "CLAUDE_PROJECT_DIR")

DEFAULT_SESSIONS_ROOT = Path.home() / ".workbuddy-ai" / "sessions"
DEFAULT_HANDLE_ROOT = Path.home() / ".workbuddy-ai" / "session-handles"

_REQUEST_HEADERS = {"X-CodeBuddy-Request": "1"}
_MAX_BODY = 512 * 1024

_PASSWORD_RE = re.compile(r"CODEBUDDY_GATEWAY_PASSWORD=(\S+)")

#: Real window sessions carry a UUID.  The host-CLI session file uses a
#: placeholder id such as ``interactive-9851``: it still claims
#: ``kind: interactive`` and still gets an endpoint, but ``/api/v1/sessions/
#: <that id>`` answers ``SESSION_NOT_FOUND``.  Since those files sort first by
#: pid, without this filter they are exactly what auto-pick would choose.
_SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class SessionError(ValueError):
    """A session-side dispatch or mailbox operation was rejected."""


class DeliveryUnknown(SessionError):
    """A delivery may or may not have been queued, so it must never be retried.

    Deliberately distinct from a plain :class:`SessionError`.  A plain error
    means the turn never left us -- no window, an explicit refusal, a rejected
    request -- so the mailbox request may safely stay pending for the next
    sweep.  This one means the gateway answered in a shape we cannot read
    (empty body, missing or non-boolean ``delivered``) or died mid-request: the
    turn may already be queued, so retrying it runs the task twice.  The
    servicer answers it with a terminal negative result instead of leaving the
    request pending, which is the only way to stop the retry without lying.
    """


class TransportError(SessionError):
    """A request never got an answer from the gateway.

    ``reached`` records whether the bytes could have arrived.  A refused
    connection or a host that does not resolve provably never got as far as the
    gateway, so the caller may retry safely; a timeout or a reset connection
    may have been served before its answer was lost, so it must not be retried.
    """

    def __init__(self, message: str, reached: bool = True) -> None:
        super().__init__(message)
        self.reached = reached


def _may_have_reached(error: BaseException) -> bool:
    """Whether a failed request could still have arrived at the gateway.

    Only the two provable cases count as "never sent": the connection was
    refused, or the host did not resolve.  Everything else -- a timeout, a
    reset, a truncated read -- may have been served before its answer was lost,
    so it has to be treated as ambiguous.
    """
    reason = getattr(error, "reason", error)
    return not isinstance(reason, (ConnectionRefusedError, socket.gaierror))


#: The host's own session id.  The supervisor normally runs *inside* the very
#: conversation it dispatches for, and that window is a live window on disk, so
#: auto-pick will happily choose it.  A task delivered back into the supervisor's
#: own thread collapses the two roles into one conversation, so the host session
#: is excluded by default rather than left to an operator to remember.
HOST_SESSION_ENV = "CODEBUDDY_SESSION_ID"


def host_session_id() -> Optional[str]:
    """The host's own session id, or ``None`` when it is absent or malformed."""
    value = os.environ.get(HOST_SESSION_ENV)
    if isinstance(value, str):
        value = value.strip()
        if _SESSION_ID_RE.match(value):
            return value
    return None


# ---------------------------------------------------------------------------
# small JSON helpers
# ---------------------------------------------------------------------------
def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    with open(str(tmp), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
    os.replace(str(tmp), str(path))


def _unlink(path: Path) -> None:
    """Remove a bookkeeping file, tolerating the race where it is already gone."""
    try:
        path.unlink()
    except OSError:
        pass


def _exclusive_lock(path: Path) -> Optional[Any]:
    """Best-effort exclusive lock so two ``serve`` loops cannot race.

    Returns an opaque handle for :func:`_release_lock`, or ``None`` when the
    lock file could not be opened (the loop then runs unlocked, as before).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(path), "a+")
    except OSError:
        return None
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            pass
    return handle


def _release_lock(handle: Optional[Any]) -> None:
    if handle is None:
        return
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
    try:
        handle.close()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# handles: which window holds which dispatched turn
# ---------------------------------------------------------------------------
def handle_root() -> Path:
    override = os.environ.get(HANDLE_ROOT_ENV)
    return Path(override) if override else DEFAULT_HANDLE_ROOT


def load_handle(handle_id: str, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Read a handle without needing a gateway token.

    Kept module level so callers can tell "this id is a window handle" apart
    from "this id is a background job" before any credential work happens.
    """
    if not isinstance(handle_id, str) or not handle_id:
        return None
    directory = Path(root) if root is not None else handle_root()
    return _load_json(directory / (handle_id + ".json"))


def save_handle(record: Mapping[str, Any], root: Optional[Path] = None) -> None:
    directory = Path(root) if root is not None else handle_root()
    _write_json(directory / (str(record["id"]) + ".json"), record)


# ---------------------------------------------------------------------------
# credential discovery
# ---------------------------------------------------------------------------
def gateway_token(scanner: Optional[Callable[[], str]] = None) -> str:
    """The App-level gateway password, shared by every session.

    It is not written to disk anywhere.  The host injects it into the
    processes it spawns, so when we are one of them it is simply in our
    environment; otherwise we read it back out of a running ``codebuddy``
    process, which is what a human would do with ``ps eww``.

    ``ps -axeww`` is *not* usable for this: on macOS it prints the command
    column but not the environment block, so a scan of its output matches
    only processes whose command line happens to contain the variable name —
    including the scanner itself.  ``ps eww -p <pid>`` does print the
    environment, so the scan is two steps: list candidate pids, then read
    each one's environment.
    """
    for name in (TOKEN_ENV, "CODEBUDDY_GATEWAY_PASSWORD"):
        value = os.environ.get(name)
        if value:
            return value
    text = scanner() if scanner is not None else _scan_process_env()
    match = _PASSWORD_RE.search(text or "")
    if not match:
        raise SessionError(
            "gateway password not found; set %s or start a WorkBuddy session" % TOKEN_ENV
        )
    return match.group(1)


def _scan_process_env() -> str:
    """Concatenate the environments of running ``codebuddy`` processes."""
    try:
        listing = subprocess.run(
            ["ps", "-ax", "-o", "pid=", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    chunks: List[str] = []
    seen = 0
    for line in (listing.stdout or "").splitlines():
        fields = line.split(None, 1)
        if not fields or not fields[0].isdigit():
            continue
        if "cli/bin/codebuddy" not in fields[-1]:
            continue
        seen += 1
        if seen > 40:  # bounded: enough to find one live session
            break
        try:
            detail = subprocess.run(
                ["ps", "eww", "-p", fields[0]],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        chunks.append(detail.stdout or "")
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# window discovery
# ---------------------------------------------------------------------------
def sessions_root() -> Path:
    override = os.environ.get(SESSIONS_ROOT_ENV)
    return Path(override) if override else DEFAULT_SESSIONS_ROOT


def excluded_session_ids() -> frozenset:
    raw = os.environ.get(EXCLUDE_SIDS_ENV) or ""
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def discover_sessions(
    root: Optional[Path] = None,
    exclude: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Every live window session that can be dispatched into.

    Only ``kind == "interactive"`` sessions carry an ``endpoint``; prewarm and
    background sessions have nothing to talk to.  A dead pid means the file is
    stale, so it is dropped rather than offered as a target that will 404.
    """
    directory = Path(root) if root is not None else sessions_root()
    if not directory.is_dir():
        return []
    skip = set(exclude) if exclude else set(excluded_session_ids())
    found: List[Dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        payload = _load_json(path)
        if payload is None:
            continue
        if payload.get("kind") != "interactive":
            continue
        session_id = payload.get("sessionId")
        endpoint = payload.get("endpoint") or payload.get("url")
        if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
            continue
        if not isinstance(endpoint, str) or not endpoint:
            continue
        if session_id in skip:
            continue
        pid = payload.get("pid")
        if not _pid_alive(pid):
            # Stale file: the window is gone, so there is nothing to deliver
            # into.  Offering it would turn a dispatch into a 404.
            continue
        found.append(
            {
                "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
                "session_id": session_id,
                "endpoint": endpoint.rstrip("/"),
                "cwd": payload.get("cwd"),
                "alive": _pid_alive(pid),
            }
        )
    found.sort(key=lambda item: (item["pid"] is None, item["pid"] or 0))
    return found


def candidate_sessions(
    sessions: Sequence[Mapping[str, Any]],
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
    exclude: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Every dispatchable window, best-first.

    An explicit ``session_id`` yields at most that one window.  Otherwise only
    live windows open on ``cwd`` are offered — dispatching into a window that
    sits elsewhere hands the worker the wrong repo, so a cwd miss yields an
    empty list (a refusal), never a fallback to an arbitrary window.

    Returning the whole ordered list lets the caller skip a window that is
    busy (``delivered: false``) and try the next, instead of collapsing a
    multi-node sweep onto the first window that happens to be occupied.

    ``exclude`` is how the orchestrator keeps itself out of the pool: a task
    delivered into the window that is running the monitor lands as a user turn
    in that same conversation, which collapses the roles the whole dispatch
    topology exists to keep apart.
    """
    skip = set(exclude) if exclude else set()
    live = [
        dict(item)
        for item in sessions
        if item.get("alive") and item.get("session_id") not in skip
    ]
    if session_id:
        return [item for item in live if item.get("session_id") == session_id]
    if cwd:
        return [item for item in live if item.get("cwd") == cwd]
    return live


def pick_session(
    sessions: Sequence[Mapping[str, Any]],
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
    exclude: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Choose the single best window to dispatch into.

    Thin wrapper over :func:`candidate_sessions` for callers that only need the
    first choice; see there for the selection rules.
    """
    candidates = candidate_sessions(
        sessions, session_id=session_id, cwd=cwd, exclude=exclude
    )
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
class SessionDispatch:
    """Deliver prompts into running windows and read their replies back."""

    def __init__(
        self,
        token: Optional[str] = None,
        sessions: Optional[Sequence[Mapping[str, Any]]] = None,
        handle_root: Optional[Path] = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = token if token is not None else gateway_token()
        self._sessions = [dict(item) for item in sessions] if sessions is not None else None
        # Honour the override env var: the module-level ``handle_root()`` does,
        # and a writer/reader that disagree on the directory lose every handle.
        if handle_root is not None:
            self._handle_root = Path(handle_root)
        else:
            override = os.environ.get(HANDLE_ROOT_ENV)
            self._handle_root = Path(override) if override else DEFAULT_HANDLE_ROOT
        self._sleep = sleeper

    # -- transport ---------------------------------------------------------
    def _call(
        self,
        endpoint: str,
        path: str,
        method: str = "GET",
        body: Optional[Mapping[str, Any]] = None,
        timeout: float = 30.0,
    ) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(endpoint + path, method=method, data=data)
        for name, value in _REQUEST_HEADERS.items():
            request.add_header(name, value)
        request.add_header("Authorization", "Bearer " + self._token)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(_MAX_BODY)
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = error.read(400).decode("utf-8", "replace").strip()
            except Exception:
                detail = ""
            raise SessionError(
                "%s %s failed: HTTP %s%s"
                % (method, path, error.code, (" " + detail) if detail else "")
            ) from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise TransportError(
                "%s %s failed: %s" % (method, path, error),
                reached=_may_have_reached(error),
            ) from error
        if not raw:
            return {}
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise SessionError("%s %s returned invalid JSON" % (method, path)) from error
        if isinstance(decoded, Mapping) and "data" in decoded:
            return decoded["data"]
        return decoded

    # -- discovery ---------------------------------------------------------
    def sessions(self, refresh: bool = False) -> List[Dict[str, Any]]:
        if self._sessions is None or refresh:
            self._sessions = discover_sessions()
        return [dict(item) for item in self._sessions]

    def _endpoint_for(self, session_id: str) -> str:
        for item in self.sessions(refresh=True):
            if item.get("session_id") == session_id:
                return str(item["endpoint"])
        raise SessionError("no live window session %s" % session_id)

    # -- actions -----------------------------------------------------------
    def deliver(self, session_id: str, text: str) -> Dict[str, Any]:
        """Deliver a turn, keeping refusal distinct from an unknown outcome.

        The gateway answers ``{"delivered": true}`` when it queues the turn and
        ``{"delivered": false}`` when it refuses one (a busy window).  Any other
        shape -- an empty body, a missing or non-boolean field -- tells us
        nothing about whether the turn was queued.  Collapsing that into
        ``False`` would let a caller retry the same prompt on another window and
        deliver it twice, so the three outcomes stay distinct:

        * ``accepted`` -- queued; the caller may proceed
        * ``refused``  -- explicitly rejected; the caller may try another window
        * ``unknown``  -- cannot tell; the caller must fail closed, never retry

        A POST that fails in transit is ``unknown`` too, and for the same
        reason: a timeout or a dropped connection means the request may well
        have arrived and been queued before its answer was lost.  Only the
        lookup of the endpoint happens outside that judgement, because a window
        that is not there was never sent anything.
        """
        if not isinstance(text, str) or not text.strip():
            raise SessionError("text is required")
        endpoint = self._endpoint_for(session_id)
        try:
            data = self._call(
                endpoint,
                "/api/v1/sessions/%s/reply" % session_id,
                method="POST",
                body={"text": text},
            )
        except TransportError as error:
            if not error.reached:
                # A refused connection never got as far as the gateway, so the
                # turn was definitely not queued and retrying is safe.  Failing
                # the node over a gateway that is not listening would be a
                # false alarm.
                raise
            raise DeliveryUnknown(
                "the reply to window %s was lost in transit: %s"
                % (session_id, error)
            ) from error
        except SessionError as error:
            # The gateway answered with an error status.  Whether it queued the
            # turn before failing is unknowable, so this is not a refusal.
            raise DeliveryUnknown(
                "window %s answered the delivery with an error: %s"
                % (session_id, error)
            ) from error
        flag = data.get("delivered") if isinstance(data, Mapping) else None
        if flag is True:
            state = "accepted"
        elif flag is False:
            state = "refused"
        else:
            state = "unknown"
        return {
            "session_id": session_id,
            "state": state,
            "delivered": flag if isinstance(flag, bool) else None,
        }

    def history(self, session_id: str) -> Dict[str, Any]:
        data = self._call(
            self._endpoint_for(session_id),
            "/api/v1/sessions/%s/history" % session_id,
        )
        requests = data.get("requests") if isinstance(data, Mapping) else None
        if not isinstance(requests, list):
            requests = []
        return {"session_id": session_id, "requests": requests, "count": len(requests)}

    def live(self, session_id: str) -> Dict[str, Any]:
        data = self._call(self._endpoint_for(session_id), "/api/v1/sessions/live")
        return {
            "session_id": session_id,
            "writer_occupied": data.get("writerOccupied") is True,
        }

    def latest_reply(self, session_id: str) -> Optional[str]:
        """The newest ``finalReply`` in the window, or None if none yet."""
        for entry in reversed(self.history(session_id)["requests"]):
            if isinstance(entry, Mapping):
                reply = entry.get("finalReply")
                if isinstance(reply, str) and reply.strip():
                    return reply
        return None

    def reply_after(self, session_id: str, start_index: int) -> Optional[str]:
        """The first non-empty ``finalReply`` at or after ``start_index``.

        ``latest_reply`` returns whichever reply is newest, which on a window
        that already answered an earlier turn is the *previous* turn's reply.
        A fast poll would then read the stale answer as this turn's output.
        Binding the read to the index the dispatch started from keeps the reply
        attached to the request that produced it.
        """
        requests = self.history(session_id)["requests"]
        if not isinstance(start_index, int) or isinstance(start_index, bool) or start_index < 0:
            start_index = 0
        for entry in requests[start_index:]:
            if isinstance(entry, Mapping):
                reply = entry.get("finalReply")
                if isinstance(reply, str) and reply.strip():
                    return reply
        return None

    # -- handles -----------------------------------------------------------
    def save_handle(self, record: Mapping[str, Any]) -> None:
        save_handle(record, root=self._handle_root)

    def load_handle(self, handle_id: str) -> Optional[Dict[str, Any]]:
        return load_handle(handle_id, root=self._handle_root)

    def wait(
        self,
        handle_id: str,
        timeout_seconds: float = 600.0,
        poll_seconds: float = 2.0,
    ) -> Dict[str, Any]:
        """Poll until the delivered turn produces a reply newer than baseline."""
        handle = self.load_handle(handle_id)
        if handle is None:
            raise SessionError("unknown session handle: %s" % handle_id)
        baseline = handle.get("baseline")
        if not isinstance(baseline, int) or isinstance(baseline, bool):
            # No baseline means we cannot tell this turn's reply from an
            # earlier one; refuse rather than risk returning a stale reply.
            raise SessionError("session handle %s has no baseline" % handle_id)
        session_id = str(handle["session_id"])
        deadline = time.monotonic() + timeout_seconds
        while True:
            current = self.history(session_id)
            if current["count"] > baseline:
                reply = self.reply_after(session_id, int(baseline or 0))
                if reply is not None:
                    return {
                        "id": handle_id,
                        "session_id": session_id,
                        "state": "done",
                        "settled": True,
                        "output": reply,
                    }
            if time.monotonic() >= deadline:
                return {
                    "id": handle_id,
                    "session_id": session_id,
                    "state": "working",
                    "settled": False,
                    "wait_timed_out": True,
                    "timeout_seconds": timeout_seconds,
                }
            self._sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))


# ---------------------------------------------------------------------------
# mailbox servicer
# ---------------------------------------------------------------------------
class MailboxServicer:
    """Consume vibe's provider-action mailbox for the WorkBuddy provider.

    ``runners/provider_action.py`` leaves ``create`` / ``locate`` /
    ``visibility`` requests in ``.vibe/provider-actions/requests/`` and blocks
    until a result file appears.  Nothing in the package writes those three —
    only ``wait`` is ever completed internally, and that by worker
    self-report.  For Codex the desktop app services them; on WorkBuddy the
    session has to, so this is the loop that does it.

    ``wait`` is deliberately left alone: it is settled by
    ``vibe worker-deliver``, and answering it here would fabricate a delivery.
    """

    def __init__(
        self,
        dispatch: SessionDispatch,
        exclude: Optional[Sequence[str]] = None,
    ) -> None:
        self.dispatch = dispatch
        self.exclude = list(exclude) if exclude is not None else None
        self._skip: Optional[List[str]] = None

    @staticmethod
    def project_dir(cwd: Optional[str] = None) -> Path:
        for name in PROJECT_DIR_ENVS:
            value = os.environ.get(name)
            if value:
                return Path(value)
        return Path(cwd or os.getcwd())

    def serve(
        self,
        project_dir: Optional[str] = None,
        limit: int = 50,
        exclude: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        self._skip = list(exclude) if exclude is not None else self.exclude
        root = Path(project_dir) if project_dir else self.project_dir()
        mailbox = root / ".vibe" / "provider-actions"
        request_dir = mailbox / "requests"
        result_dir = mailbox / "results"
        #: One marker per in-flight delivery.  See ``_deliver``.
        marker_dir = mailbox / "delivering"
        if not request_dir.is_dir():
            return {
                "served": [],
                "skipped": [],
                "failed": [],
                "project_dir": str(root),
            }

        # One servicer loop at a time: two concurrent ``serve`` calls would
        # otherwise both read the same unanswered request and both deliver it.
        lock = _exclusive_lock(mailbox / ".serve.lock")
        try:
            served: List[Dict[str, Any]] = []
            skipped: List[Dict[str, Any]] = []
            #: Requests answered with a terminal negative result.  They are not
            #: failures of this sweep but answers the operator has to see: the
            #: action will not be retried, so nobody else is coming.
            failed: List[Dict[str, Any]] = []
            # Windows already used in this pass, so several nodes in one sweep
            # fan out across distinct windows instead of piling onto the first.
            used: List[str] = []
            for path in sorted(request_dir.glob("action-*.json")):
                if len(served) + len(skipped) + len(failed) >= limit:
                    break
                action_id = path.stem
                marker = marker_dir / path.name
                if (result_dir / path.name).exists():
                    # Answered already, so any marker left over is stale -- but
                    # only clear it here, where a result proves the turn was
                    # accounted for.
                    _unlink(marker)
                    continue
                action = _load_json(path)
                if action is None:
                    skipped.append({"action_id": action_id, "reason": "unreadable"})
                    continue
                if marker.exists():
                    # A previous sweep raised this marker and never took it
                    # down: it crashed, or the result write failed, between
                    # delivering the turn and recording the outcome.  The turn
                    # may already be queued, so answering the request again
                    # would run the task twice.  Fail closed and say why.
                    reason = (
                        "a previous sweep delivered this turn but never recorded "
                        "an outcome; the turn may already be queued"
                    )
                    if self._fail_terminally(action, action_id, result_dir, reason):
                        _unlink(marker)
                    failed.append({"action_id": action_id, "reason": reason})
                    continue
                operation = action.get("operation")
                request = action.get("request")
                request = request if isinstance(request, dict) else {}
                try:
                    payload = self._answer(
                        operation, request, action, root, used, marker
                    )
                except DeliveryUnknown as error:
                    # Never retried: see ``DeliveryUnknown``.  A terminal
                    # negative result is the only answer that neither
                    # fabricates a worker nor invites a second delivery.
                    # The marker only comes down once that answer is on disk;
                    # clearing it while the write failed would leave the next
                    # sweep with neither marker nor result, and it would
                    # deliver the same prompt again.
                    if self._fail_terminally(action, action_id, result_dir, str(error)):
                        _unlink(marker)
                    failed.append({"action_id": action_id, "reason": str(error)})
                    continue
                except (SessionError, ValueError, OSError) as error:
                    # ``_deliver`` has already decided whether anything was
                    # queued, and lowered the marker only when it proved nothing
                    # was.  An error raised *after* an accepted delivery -- a
                    # failed ``save_handle``, say -- leaves the marker up, and
                    # the next sweep answers this request terminally instead of
                    # sending the prompt a second time.  Clearing it here would
                    # undo exactly that.
                    skipped.append({"action_id": action_id, "reason": str(error)})
                    continue
                if payload is None:
                    skipped.append(
                        {"action_id": action_id, "reason": "unsupported operation"}
                    )
                    continue
                try:
                    self._record(action, action_id, result_dir, payload)
                except OSError as error:
                    # Delivered but unrecorded.  Keep the marker up so the next
                    # sweep fails closed rather than delivering a second time.
                    failed.append(
                        {
                            "action_id": action_id,
                            "reason": "result write failed: %s" % error,
                        }
                    )
                    continue
                _unlink(marker)
                served.append({"action_id": action_id, "operation": operation})
                if operation == "create":
                    chosen = payload.get("sessionId")
                    if isinstance(chosen, str) and chosen:
                        used.append(chosen)
            return {
                "served": served,
                "skipped": skipped,
                "failed": failed,
                "project_dir": str(root),
            }
        finally:
            _release_lock(lock)

    @staticmethod
    def _record(
        action: Mapping[str, Any],
        action_id: str,
        result_dir: Path,
        payload: Mapping[str, Any],
    ) -> None:
        _write_json(
            result_dir / (action_id + ".json"),
            {
                "schema_version": action.get("schema_version", 1),
                "action_id": action_id,
                "request_digest": action.get("request_digest"),
                "payload": dict(payload),
            },
        )

    def _fail_terminally(
        self,
        action: Mapping[str, Any],
        action_id: str,
        result_dir: Path,
        reason: str,
    ) -> bool:
        """Answer a request we can never honestly answer, so it stops retrying.

        The payload claims no ``binding``, no ``located`` and no ``resumed``, so
        the provider reads it as a failed action rather than a successful one.
        That is deliberate: a success would fabricate a worker, and no answer at
        all would leave the request pending for another delivery attempt.

        Returns whether the answer reached the disk.  A caller must not lower
        the delivery marker when it did not: with no marker *and* no result the
        next sweep has no way to tell this turn was already delivered and sends
        it a second time.
        """
        payload = {"delivery": "unknown", "failed": True, "reason": reason}
        try:
            self._record(action, action_id, result_dir, payload)
        except OSError:
            return False
        return True

    def _deliver(
        self,
        marker: Optional[Path],
        session_id: str,
        text: str,
    ) -> Dict[str, Any]:
        """Deliver one turn, holding a marker until we know nothing was queued.

        The marker goes up before the POST and comes down only when something
        proves the turn was not queued -- an explicit refusal, or an error that
        happened before the request left.  A crash, a lost reply or a failed
        result write all leave it up, and the next sweep fails the action closed
        instead of delivering the same prompt twice.
        """
        if marker is not None:
            _write_json(marker, {"session_id": session_id, "at": time.time()})
        try:
            outcome = self.dispatch.deliver(session_id, text)
        except DeliveryUnknown:
            # The turn may already be queued, and the marker is the only record
            # of that.  It must survive for the next sweep to fail closed.
            raise
        except SessionError:
            # ``deliver`` raises a plain error only before the request is sent:
            # a window that is gone, or a connection that was refused.  Nothing
            # was queued, so the marker comes down and the request may retry.
            if marker is not None:
                _unlink(marker)
            raise
        if marker is not None and outcome.get("state") == "refused":
            _unlink(marker)
        return outcome

    def _answer(
        self,
        operation: Any,
        request: Mapping[str, Any],
        action: Mapping[str, Any],
        root: Path,
        used: Optional[Sequence[str]] = None,
        marker: Optional[Path] = None,
    ) -> Optional[Dict[str, Any]]:
        if operation == "create":
            return self._answer_create(request, action, root, used, marker)
        if operation == "locate":
            return self._answer_locate(request)
        if operation == "visibility":
            return self._answer_visibility(request)
        if operation == "resume":
            return self._answer_resume(request, marker)
        return None

    def _resolve_session(self, thread_id: str) -> str:
        """Map a ``task_id`` (a handle id) back to the window it dispatched into.

        Falls back to treating the id as a bare session id, so a binding made
        before handles carried unique ids still resolves.
        """
        handle = self.dispatch.load_handle(thread_id)
        if isinstance(handle, Mapping) and isinstance(handle.get("session_id"), str):
            return str(handle["session_id"])
        return thread_id

    def _answer_create(
        self,
        request: Mapping[str, Any],
        action: Mapping[str, Any],
        root: Path,
        used: Optional[Sequence[str]] = None,
        marker: Optional[Path] = None,
    ) -> Dict[str, Any]:
        prompt = request.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise SessionError("create request carries no prompt")
        # Exclude the orchestrator's own window (env + constructor) and every
        # window already claimed in this sweep, so a multi-node run fans out
        # across windows instead of piling every node onto the first one.
        skip = set(excluded_session_ids())
        # The host's own conversation is a live window too, and it is the one
        # target that must never be chosen: a task delivered back into the
        # supervisor's thread collapses the two roles into one conversation.
        # Excluded by default rather than left to an operator to remember.
        own = host_session_id()
        if own:
            skip.add(own)
        if self._skip is not None:
            skip.update(self._skip)
        if used:
            skip.update(used)
        candidates = candidate_sessions(
            self.dispatch.sessions(refresh=True),
            cwd=str(root),
            exclude=sorted(skip),
        )
        if not candidates:
            raise SessionError(
                "no live WorkBuddy window to dispatch into; open one and retry"
            )
        # Try windows best-first: a busy window answers ``delivered: false``,
        # and the sweep must move on to the next one instead of collapsing
        # every node onto the single occupied window.
        session: Optional[Dict[str, Any]] = None
        baseline = 0
        refused: List[str] = []
        unreachable: List[str] = []
        for candidate in candidates:
            candidate_id = str(candidate["session_id"])
            try:
                candidate_baseline = self.dispatch.history(candidate_id)["count"]
            except SessionError as error:
                # The window died between discovery and use.  That is a reason
                # to try the next candidate, not to abandon the whole action.
                unreachable.append("%s (%s)" % (candidate_id, error))
                continue
            state = self._deliver(marker, candidate_id, prompt).get("state")
            if state == "accepted":
                session = candidate
                baseline = candidate_baseline
                break
            if state == "refused":
                refused.append(candidate_id)
                continue
            # `unknown`: the turn may already be queued, so retrying on another
            # window could deliver it twice.  Fail closed instead of rotating,
            # and never leave the request pending -- see ``DeliveryUnknown``.
            raise DeliveryUnknown(
                "window %s returned an unrecognised delivery outcome; "
                "the turn may already be queued" % candidate_id
            )
        if session is None:
            # Every candidate refused.  Writing a success here is the exact
            # false green this module exists to prevent: the monitor would
            # believe a worker is running and wait forever for a self-report
            # that can never come.
            raise SessionError(
                "no live window accepted the turn (refused by %s%s)"
                % (
                    ", ".join(refused) or "none",
                    "; unreachable: " + ", ".join(unreachable) if unreachable else "",
                )
            )
        session_id = str(session["session_id"])
        endpoint = str(session["endpoint"])
        # One handle per request: the action id is unique, so two nodes sharing
        # a window no longer collide on the handle id or the `task_id` binding.
        handle_id = str(action.get("action_id") or session_id[:8])
        self.dispatch.save_handle(
            {
                "id": handle_id,
                "session_id": session_id,
                "endpoint": endpoint,
                "baseline": baseline,
                "cwd": str(root),
                "issue_id": action.get("issue_id"),
                "role": action.get("role"),
            }
        )
        # `binding` is what `provider_action.create` reads: `task_id` is the
        # provider-neutral identity, `host` the endpoint that owns it.
        return {
            "id": handle_id,
            "shortId": handle_id,
            "sessionId": session_id,
            "kind": "window-session",
            "state": "working",
            "detail": "delivered",
            "alive": True,
            "settled": False,
            "cwd": str(root),
            "endpoint": endpoint,
            "binding": {"task_id": handle_id, "host": endpoint},
        }

    def _answer_locate(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        thread_id = request.get("threadId") or request.get("task_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise SessionError("locate request carries no thread identity")
        session_id = self._resolve_session(thread_id)
        # Fail closed: a window that is gone cannot be located, and claiming
        # otherwise would let a dead binding through the supervisor's gate.
        located = any(
            item.get("session_id") == session_id and item.get("alive")
            for item in self.dispatch.sessions(refresh=True)
        )
        return {"located": located, "threadId": thread_id}

    def _answer_visibility(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        targets = request.get("targets")
        if not isinstance(targets, list) or not targets:
            raise SessionError("visibility request carries no targets")
        sessions = self.dispatch.sessions(refresh=True)
        alive = {
            str(item.get("session_id"))
            for item in sessions
            if item.get("alive")
        }
        results = []
        for target in targets:
            if not isinstance(target, Mapping):
                results.append({"visible": False, "direct_enter": False})
                continue
            thread_id = str(target.get("threadId") or target.get("task_id") or "")
            session_id = self._resolve_session(thread_id) if thread_id else ""
            present = session_id in alive
            results.append({"visible": present, "direct_enter": present})
        # `provider_action.visibility` reads the top level, not the list.
        visible = all(item["visible"] for item in results)
        return {
            "visible": visible,
            "direct_enter": visible and all(item["direct_enter"] for item in results),
            "targets": results,
        }

    def _answer_resume(
        self, request: Mapping[str, Any], marker: Optional[Path] = None
    ) -> Dict[str, Any]:
        """Resume a turn on the window that owns the thread.

        ``provider_action.poll`` refuses a resume result unless it carries
        ``resumed: true``.  A window that is gone must therefore answer
        ``false`` here instead of being silently dropped: a dropped resume
        parks the run at "provider resume is pending" forever.
        """
        thread_id = request.get("threadId") or request.get("task_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise SessionError("resume request carries no thread identity")
        prompt = request.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise SessionError("resume request carries no prompt")
        session_id = self._resolve_session(thread_id)
        live = any(
            item.get("session_id") == session_id and item.get("alive")
            for item in self.dispatch.sessions(refresh=True)
        )
        if not live:
            return {"resumed": False, "threadId": thread_id, "reason": "window is gone"}
        baseline = self.dispatch.history(session_id)["count"]
        state = self._deliver(marker, session_id, prompt).get("state")
        if state == "refused":
            return {
                "resumed": False,
                "threadId": thread_id,
                "reason": "window did not accept the turn",
            }
        if state != "accepted":
            # Unknown: the turn may already be queued, so answering `resumed`
            # would be a guess and retrying would run it twice.  Never leave it
            # pending -- see ``DeliveryUnknown``.
            raise DeliveryUnknown(
                "window %s returned an unrecognised delivery outcome; "
                "the turn may already be queued" % session_id
            )
        # Advance the handle's baseline so a later `wait`/`get` on this handle
        # reads the resumed turn's reply, not the previous one.
        handle = self.dispatch.load_handle(thread_id)
        if isinstance(handle, Mapping):
            record = dict(handle)
            record["baseline"] = baseline
            self.dispatch.save_handle(record)
        return {"resumed": True, "threadId": thread_id, "sessionId": session_id}
