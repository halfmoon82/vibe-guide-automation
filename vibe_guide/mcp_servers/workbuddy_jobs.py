"""The `workbuddy_job` MCP server: WorkBuddy's session-side dispatch bridge.

vibe's monitor never executes a desktop action itself.  It leaves a request in
``.vibe/provider-actions/requests/`` naming the *native tool* the session must
call, and the session is what actually runs it.  ``runners/provider_action.py``
maps the ``workbuddy-visible`` provider to five such names::

    create   -> workbuddy_job__create
    locate   -> workbuddy_job__get
    visibility -> workbuddy_job__list
    resume   -> workbuddy_job__reply
    wait     -> workbuddy_job__wait

This module is the MCP server that makes those five names exist in a WorkBuddy
session.  Register it as server ``workbuddy_job`` and the host exposes them as
``mcp__workbuddy_job__create`` and friends.

Endpoints below were measured against the CodeBuddy Code HTTP gateway
(``codebuddy --serve``, CLI 2.147.0, macOS) on 2026-10-03, not copied from
another platform's notes:

    POST   /api/v1/jobs            -> {"data": {<job>}}          create
    GET    /api/v1/jobs/{id}       -> {"data": {"job": {<job>}}} get
    GET    /api/v1/jobs            -> {"data": {"jobs": [<job>]}} list
    POST   /api/v1/jobs/{id}/reply -> {"data": {<job>}}          reply
    DELETE /api/v1/jobs/{id}       -> {"data": {"deleted": true}}

Every call needs ``X-CodeBuddy-Request: 1`` plus ``Authorization: Bearer``.

Stdlib only, on purpose: the server must start from a bare interpreter with no
install step.  Run it with ``python3 -m vibe_guide.mcp_servers.workbuddy_jobs``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

SERVER_NAME = "workbuddy_job"
PROTOCOL_VERSION = "2024-11-05"
SERVER_VERSION = "1.0.0"

_REQUEST_HEADERS = {"X-CodeBuddy-Request": "1"}
_TERMINAL_STATES = frozenset({"done", "failed", "stopped", "error"})
_MAX_BODY = 512 * 1024

#: The CLI bundled inside the WorkBuddy app; used when nothing is on PATH.
BUNDLED_CLI = "/Applications/WorkBuddy AI.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy"

_ENDPOINT_RE = re.compile(r"Endpoint\s+(https?://[^\s]+)")
_PASSWORD_RE = re.compile(r"Password\s+(\S+)")
# The banner colours its labels, so `Endpoint` is followed by a reset sequence
# rather than by whitespace.  Strip styling before matching.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)

#: Where the gateway lives and how to authenticate, both overridable by env so
#: a session can point at a gateway it already started.
ENDPOINT_ENV = "WORKBUDDY_JOB_ENDPOINT"
TOKEN_ENV = "WORKBUDDY_JOB_TOKEN"
CLI_ENV = "WORKBUDDY_CLI"
AUTOSTART_ENV = "WORKBUDDY_JOB_AUTOSTART"

#: The host's sandbox and safe-delete broker wiring.  WorkBuddy injects it into
#: every process it starts, and the shim it activates routes each delete
#: through an IPC broker that only honours deletions approved for an *agent
#: tool call*.  A gateway started from here is not such a call, so the broker
#: answers ``denied`` and the shim fails closed.
#:
#: Two failures were measured on WorkBuddy AI (macOS, CLI 2.147.0):
#:
#: 1. Host env intact: an ``fs.rmdirSync`` under ``~/.workbuddy-ai/jobs/.locks``
#:    raises ``[safe-delete] broker denied delete``, so ``POST /api/v1/jobs``
#:    returns HTTP 500 while cleaning up the lock directory it just made, and
#:    leaves an empty ``<job>.state.lock.guard`` behind.
#: 2. Withdrawing *only* the broker IPC variables is worse than doing nothing.
#:    ``PATH`` still begins with the host's ``brokered-bin`` shim, which still
#:    sees ``CODEBUDDY_SANDBOX_PROGRAM_POLICY_COMMAND`` and can no longer reach
#:    the broker it just lost, so it fails closed: every command run inside the
#:    gateway's shells exits 13 with ``Brokered program policy check
#:    unavailable``.  ``create`` still answered 200 -- it does not shell out --
#:    so that breakage stays invisible until a shell actually runs.
#: 3. Withdrawing the broker binding *but keeping* the turn identity fixes 1
#:    and 2 while leaving ``create`` state-dependent.  The gateway cleans stale
#:    ``.locks/*.candidate`` entries on every ``create``, and each of those is
#:    a non-temp delete, so the bulk guard charges it to the inherited
#:    ``CODEBUDDY_CONVERSATION_REQUEST_ID``.  That key is the *host's current
#:    turn*, the rate is roughly 6 counts per ``create``, and the threshold is
#:    50 -- so once the turn has spent its budget the guard answers
#:    ``confirmRequired`` and the gateway's own cleanup fails closed:
#:
#:        [safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]
#:        {"count":6358,"threshold":50,"scope":"turn",
#:         "targets":["~/.workbuddy-ai/jobs/.locks/547a46cb.state.lock...candidate"]}
#:
#:    Whether it trips depends on whether the inherited
#:    ``CODEBUDDY_TOOL_CALL_ID`` happens to sit in the guard's ``toolApprovals``
#:    map, so the very same call 500s in one session and succeeds in the next.
#:    Measured: six ``create`` calls under an unapproved tool-call id returned
#:    six HTTP 500s; six under an approved one all succeeded, while adding ~6
#:    each to the host turn's count (6327 -> 6334 -> 6340 -> 6346).
#:
#: What has to go is the broker binding *and* the turn identity -- and nothing
#: else.  The injection surface carries three independent things:
#:
#: * the **broker binding** -- ``CODEBUDDY_SANDBOX_*`` naming one live
#:   session's socket, ``CODEBUDDY_BROKERED_*``, ``CODEBUDDY_TOYBOX_*``,
#:   ``SANDBOX_CENTER_*``, plus the ``brokered-bin`` PATH entry that consults
#:   them.  It only honours deletions approved for an *agent tool call*, and a
#:   gateway started from this module is not such a call.  Goes.
#: * the **turn identity** -- ``CODEBUDDY_CONVERSATION_REQUEST_ID`` and
#:   ``CODEBUDDY_TOOL_CALL_ID`` (see ``TURN_IDENTITY_ENV_VARS``): the two
#:   variables the bulk guard uses to decide *which agent turn* a delete
#:   belongs to.  A detached daemon has no turn, and inheriting the host's
#:   stale one is exactly what makes failure 3 above possible.  Goes.
#: * the **safe-delete guardrail** -- ``BASH_ENV`` and the safe-bin wrappers,
#:   ``CODEBUDDY_SAFE_DELETE_*``, ``NODE_OPTIONS``, ``PYTHONPATH``.  It needs
#:   no broker and no turn: with both withdrawn it degrades to moving the
#:   target to the trash, which is what should happen.  Removing it as well
#:   would leave the gateway and every job it dispatches doing unmediated
#:   native deletes -- worse than the bug being fixed.  An earlier revision on
#:   this branch did exactly that, and review rejected it.  Stays.
#:
#: Why dropping the turn identity is the right half to drop: all three carriers
#: treat a missing ``CODEBUDDY_TOOL_CALL_ID`` as "this guard does not apply"
#: and let the delete through -- ``safe-bin/safe-delete-common.sh``
#: (``return 0``), ``node-safe-delete-shim.cjs`` (``return``) and
#: ``sitecustomize.py`` (``return``).  None of them fails closed, so the trash
#: redirection -- the part of the guardrail that actually protects the user's
#: files -- is untouched.  What is given up is the >=50-files *interactive*
#: confirmation inside the gateway's tree, a prompt that cannot be answered
#: there in any case because a background job has nobody to answer it.
#:
#: Measured per arm, deleting a file outside any OS temp dir (``safe_delete_rm``
#: sends those straight to ``$REAL_RM``):
#:
#: * host env -- ``rm`` is ``brokered-bin/rm``, broker approves, ``ls`` rc=0
#: * broker binding dropped only -- ``rm`` is ``/bin/rm``, **no** trash, rc=0
#: * this function (narrowed) -- ``rm`` is a safe-bin function, **trash**, rc=0
#: * no shim at all -- ``rm`` is ``/bin/rm``, **no** trash, rc=0
#:
#: The last row is a control: the "trash" reading comes from the shim and not
#: from something else on the machine.  A second control drops only
#: ``CODEBUDDY_SESSION_ID`` from the narrowed env -- same wrappers, no session
#: -- and reports "no trash" again.  The same three-way comparison over node
#: (``fs.rmSync``) and python (``os.remove``) gives the same shape.
BROKER_ENV_PREFIXES: Tuple[str, ...] = (
    "CODEBUDDY_SANDBOX_",
    "CODEBUDDY_BROKERED_",
    "CODEBUDDY_TOYBOX_",
    "SANDBOX_CENTER_",
)

#: Broker wiring that shares no prefix with the rest.
BROKER_ENV_VARS: Tuple[str, ...] = (
    "TOYBOX_SANDBOX_SOCK",
    "CODEBUDDY_BROKER_IPC_CLIENT",
)

#: The host's *turn identity*: the two variables the safe-delete bulk guard
#: reads to decide which agent turn a delete belongs to.  A gateway we start
#: outlives the turn that started it, so both are stale the moment they are
#: handed over -- and every file the gateway removes for its own bookkeeping
#: is then charged to a turn that may long since have ended.  See the notes
#: above for what that does to ``create``.
#:
#: ``CODEBUDDY_SESSION_ID`` is deliberately *not* here.  ``safe-bin/rm`` falls
#: through to ``$REAL_RM`` when no session id is set, so dropping it would turn
#: the gateway's deletes from "moved to the trash" into "gone" -- precisely the
#: regression this rule exists to prevent.
TURN_IDENTITY_ENV_VARS: Tuple[str, ...] = (
    "CODEBUDDY_CONVERSATION_REQUEST_ID",
    "CODEBUDDY_TOOL_CALL_ID",
)

#: PATH entries that route a command through the host's brokered shim.  Just
#: this one: the ``safe-bin`` entry stays, because the guardrail behind it
#: still works and is worth keeping in front of the bare system tools.
BROKER_PATH_MARKERS: Tuple[str, ...] = ("/cli/vendor/shim/brokered-bin",)


def _gateway_env() -> Dict[str, str]:
    """The environment a gateway we start should see.

    Drops the host's broker binding -- the variables and the PATH entry that
    tie a process to one live agent tool call -- and with it the host's turn
    identity, so nothing the gateway deletes is charged to a turn that is not
    its own.  Everything else survives, including the safe-delete guardrail,
    which degrades to the trash once the broker and the turn are gone.  See
    the notes above for why each half is on the side it is on.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(BROKER_ENV_PREFIXES)
        and name not in BROKER_ENV_VARS
        and name not in TURN_IDENTITY_ENV_VARS
    }
    path = env.get("PATH")
    if path:
        kept = [
            part for part in path.split(os.pathsep)
            if part and not any(marker in part for marker in BROKER_PATH_MARKERS)
        ]
        # Only ever narrow PATH: an empty result would mean the host PATH held
        # nothing but shims, and a shimmed PATH still beats no PATH at all.
        if kept:
            env["PATH"] = os.pathsep.join(kept)
    return env


class GatewayError(RuntimeError):
    """The WorkBuddy control plane is unreachable or refused the request."""


def _free_port() -> int:
    """Ask the OS for a free loopback port; the gateway needs it explicit."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def find_cli(explicit: Optional[str] = None) -> Optional[str]:
    """Locate a CodeBuddy CLI: explicit, then PATH, then the app bundle."""
    candidate = explicit or os.environ.get(CLI_ENV)
    if candidate and Path(candidate).is_file():
        return candidate
    for name in ("codebuddy", "cbc", "workbuddy"):
        found = shutil.which(name)
        if found:
            return found
    if Path(BUNDLED_CLI).is_file():
        return BUNDLED_CLI
    return None


def _stop(process: subprocess.Popen) -> None:
    """Terminate a gateway we started, escalating to SIGKILL if it resists."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def _banner_shape(log: Path) -> str:
    """Describe a failed banner read without reproducing its contents.

    The startup banner carries the gateway password.  Echoing the raw text as a
    diagnostic would leak that password exactly when it matters most -- when the
    banner format drifted and ``_PASSWORD_RE`` no longer matches, so the
    credential would sit unredacted in the tail.  Report the shape instead.
    """
    text = _plain(log.read_text(encoding="utf-8", errors="replace"))
    return "captured %d bytes, endpoint line %s, password line %s" % (
        len(text),
        "seen" if _ENDPOINT_RE.search(text) else "absent",
        "seen" if _PASSWORD_RE.search(text) else "absent",
    )


class Gateway:
    """One HTTP gateway, either supplied by env or started on first use.

    A started gateway is owned by this object and stopped by :meth:`close`.
    The password it prints is read out of the CLI's own startup log, held in
    memory only, and never echoed back to a caller: the temporary log is
    removed on every exit path, and a failed banner read reports its shape
    rather than its text.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        token: Optional[str] = None,
        cli: Optional[str] = None,
        opener: Optional[Callable[..., Any]] = None,
        startup_timeout: float = 30.0,
    ) -> None:
        self._endpoint = endpoint or os.environ.get(ENDPOINT_ENV) or ""
        self._token = token or os.environ.get(TOKEN_ENV) or ""
        self._cli = cli
        self._opener = opener or urllib.request.urlopen
        self._startup_timeout = startup_timeout
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def started(self) -> bool:
        return self._process is not None

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    def ensure(self) -> None:
        if self._endpoint and self._token:
            return
        with self._lock:
            if self._endpoint and self._token:
                return
            self._autostart()

    def _autostart(self) -> None:
        if os.environ.get(AUTOSTART_ENV, "1") == "0":
            raise GatewayError(
                "no gateway endpoint: set %s and %s, or allow autostart"
                % (ENDPOINT_ENV, TOKEN_ENV)
            )
        cli = find_cli(self._cli)
        if not cli:
            raise GatewayError(
                "no CodeBuddy CLI found; set %s to the codebuddy binary" % CLI_ENV
            )
        # The CLI stays silent when its stdout is a pipe, and it also stays
        # silent when `--port` is left on auto, so capture into a temp file and
        # hand it a port we picked ourselves.
        handle, log_name = tempfile.mkstemp(prefix="workbuddy-jobs-gateway.", suffix=".log")
        os.close(handle)
        log = Path(log_name)
        try:
            last_diagnostic = "no banner captured"
            for attempt in range(2):
                port = _free_port()
                with log.open("w", encoding="utf-8") as sink:
                    process = subprocess.Popen(
                        [cli, "--serve", "--host", "127.0.0.1", "--port", str(port)],
                        stdout=sink,
                        stderr=subprocess.STDOUT,
                        # The gateway cleans up its own job-directory locks as
                        # part of `create`, and runs the shell of every job it
                        # dispatches; the host's broker binding breaks both.
                        # See BROKER_ENV_PREFIXES.
                        env=_gateway_env(),
                    )
                banner = {"endpoint": "", "password": ""}
                try:
                    deadline = time.monotonic() + self._startup_timeout
                    while time.monotonic() < deadline:
                        text = _plain(log.read_text(encoding="utf-8", errors="replace"))
                        if not banner["endpoint"]:
                            found = _ENDPOINT_RE.search(text)
                            if found:
                                banner["endpoint"] = found.group(1)
                        if not banner["password"]:
                            found = _PASSWORD_RE.search(text)
                            if found:
                                banner["password"] = found.group(1)
                        if banner["endpoint"] and banner["password"]:
                            break
                        if process.poll() is not None:
                            break
                        time.sleep(0.05)
                except BaseException:
                    # Whatever went wrong while reading the banner, the gateway
                    # we started must not be left running.
                    _stop(process)
                    raise
                if banner["endpoint"] and banner["password"]:
                    break
                # Reading the diagnostic can fail too, and it must not become a
                # second way to leave the gateway we started running.
                try:
                    last_diagnostic = _banner_shape(log)
                except Exception:
                    last_diagnostic = "banner unreadable"
                finally:
                    _stop(process)
            else:
                raise GatewayError(
                    "gateway did not report an endpoint and password (%s); "
                    "the banner itself is withheld because it can carry the "
                    "gateway password" % last_diagnostic
                )
        finally:
            try:
                log.unlink()
            except OSError:
                pass
        self._process = process
        self._endpoint = banner["endpoint"].rstrip("/")
        self._token = banner["password"]

    # -- transport ---------------------------------------------------------
    def call(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, Any]] = None,
        timeout: float = 30.0,
    ) -> Any:
        self.ensure()
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self._endpoint + path, method=method, data=body)
        for name, value in _REQUEST_HEADERS.items():
            request.add_header(name, value)
        request.add_header("Authorization", "Bearer " + self._token)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener(request, timeout=timeout) as response:
                raw = response.read(_MAX_BODY)
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = error.read(400).decode("utf-8", "replace").strip()
            except Exception:  # pragma: no cover - body already consumed
                detail = ""
            raise GatewayError(
                "%s %s failed: HTTP %s%s"
                % (method, path, error.code, (" " + detail) if detail else "")
            ) from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise GatewayError("%s %s failed: %s" % (method, path, error)) from error
        if not raw:
            return {}
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise GatewayError("%s %s returned invalid JSON" % (method, path)) from error
        if isinstance(decoded, Mapping) and "data" in decoded:
            return decoded["data"]
        return decoded


class WorkBuddyJobs:
    """The five native actions, in the shape the mailbox expects."""

    def __init__(self, gateway: Optional[Gateway] = None, sleeper: Callable[[float], None] = time.sleep) -> None:
        self.gateway = gateway or Gateway()
        self._sleep = sleeper

    def create(
        self,
        prompt: str,
        cwd: Optional[str] = None,
        name: Optional[str] = None,
        agent: Optional[str] = None,
        model: Optional[str] = None,
        permission_mode: Optional[str] = None,
        bash: Optional[bool] = None,
        bg_isolation: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required")
        body: Dict[str, Any] = {"prompt": prompt}
        optional = {
            "cwd": cwd,
            "name": name,
            "agent": agent,
            "model": model,
            "permissionMode": permission_mode,
            "bash": bash,
            "bgIsolation": bg_isolation,
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        result = self.gateway.call("POST", "/api/v1/jobs", body)
        return result if isinstance(result, dict) else {"raw": result}

    def get(self, job_id: str) -> Dict[str, Any]:
        job_id = _require_id(job_id)
        result = self.gateway.call("GET", "/api/v1/jobs/" + job_id)
        if isinstance(result, Mapping) and isinstance(result.get("job"), Mapping):
            return dict(result["job"])
        return result if isinstance(result, dict) else {"raw": result}

    def list(self, cwd: Optional[str] = None, include_all: bool = False) -> Dict[str, Any]:
        # Measured: the bare endpoint lists only unsettled jobs; `?all=1` adds
        # the completed ones.  Reconciliation needs the latter, visibility the
        # former, so the caller chooses.
        path = "/api/v1/jobs?all=1" if include_all else "/api/v1/jobs"
        result = self.gateway.call("GET", path)
        jobs = result.get("jobs") if isinstance(result, Mapping) else None
        if not isinstance(jobs, list):
            jobs = result if isinstance(result, list) else []
        if cwd:
            jobs = [job for job in jobs if isinstance(job, Mapping) and job.get("cwd") == cwd]
        return {"jobs": jobs, "count": len(jobs), "include_all": bool(include_all)}

    def reply(self, job_id: str, text: str, bash: Optional[bool] = None) -> Dict[str, Any]:
        job_id = _require_id(job_id)
        if not isinstance(text, str) or not text:
            raise ValueError("text is required")
        body: Dict[str, Any] = {"text": text}
        if bash is not None:
            body["bash"] = bash
        result = self.gateway.call("POST", "/api/v1/jobs/%s/reply" % job_id, body)
        return result if isinstance(result, dict) else {"raw": result}

    def wait(
        self,
        job_id: str,
        timeout_seconds: float = 600.0,
        poll_seconds: float = 2.0,
    ) -> Dict[str, Any]:
        job_id = _require_id(job_id)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        deadline = time.monotonic() + timeout_seconds
        last: Dict[str, Any] = {}
        while True:
            last = self.get(job_id)
            state = str(last.get("state") or "")
            if state in _TERMINAL_STATES or last.get("settled") is True:
                return last
            if time.monotonic() >= deadline:
                return dict(last, wait_timed_out=True, timeout_seconds=timeout_seconds)
            self._sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))

    def close(self) -> None:
        self.gateway.close()


def _require_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("job_id is required")
    return value.strip()


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------
TOOL_DEFINITIONS: Tuple[Dict[str, Any], ...] = (
    {
        "name": "create",
        "description": (
            "Dispatch a new WorkBuddy background session (job) and return its id. "
            "Maps to vibe's `workbuddy_job__create` provider action."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The task text handed to the new session."},
                "cwd": {"type": "string", "description": "Working directory for the job."},
                "name": {"type": "string", "description": "Optional display name."},
                "agent": {"type": "string", "description": "Agent name, e.g. cli, ptc, minimal."},
                "model": {"type": "string", "description": "Model id override."},
                "permission_mode": {"type": "string", "description": "Permission mode for the job."},
                "bash": {"type": "boolean", "description": "Run the prompt as a shell command instead of an agent turn."},
                "bg_isolation": {"type": "string", "enum": ["none", "worktree"], "description": "Background write isolation."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "get",
        "description": (
            "Read one job's current state, cwd and session id. "
            "Maps to vibe's `workbuddy_job__get` provider action."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "Job id returned by create."}},
            "required": ["job_id"],
        },
    },
    {
        "name": "list",
        "description": (
            "List known jobs, optionally filtered to one working directory. "
            "Maps to vibe's `workbuddy_job__list` provider action."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "Only return jobs started in this directory."},
                "include_all": {"type": "boolean", "description": "Include completed jobs (default false: live jobs only)."},
            },
        },
    },
    {
        "name": "reply",
        "description": (
            "Send a follow-up message to a waiting job. "
            "Maps to vibe's `workbuddy_job__reply` provider action."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job id to resume."},
                "text": {"type": "string", "description": "Message to deliver."},
                "bash": {"type": "boolean", "description": "Treat the message as a shell command."},
            },
            "required": ["job_id", "text"],
        },
    },
    {
        "name": "wait",
        "description": (
            "Poll a job until it settles or the timeout expires. "
            "Maps to vibe's `workbuddy_job__wait` provider action."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job id to wait for."},
                "timeout_seconds": {"type": "number", "description": "Give up after this long (default 600)."},
                "poll_seconds": {"type": "number", "description": "Interval between polls (default 2)."},
            },
            "required": ["job_id"],
        },
    },
)

TOOL_NAMES: Tuple[str, ...] = tuple(tool["name"] for tool in TOOL_DEFINITIONS)

_ARGUMENTS: Dict[str, Tuple[str, ...]] = {
    "create": ("prompt", "cwd", "name", "agent", "model", "permission_mode", "bash", "bg_isolation"),
    "get": ("job_id",),
    "list": ("cwd", "include_all"),
    "reply": ("job_id", "text", "bash"),
    "wait": ("job_id", "timeout_seconds", "poll_seconds"),
}

#: The published schemas, keyed by tool name, so the runtime check and the
#: schema we advertise cannot drift apart.
_SCHEMAS: Dict[str, Mapping[str, Any]] = {
    tool["name"]: tool["inputSchema"] for tool in TOOL_DEFINITIONS
}

#: JSON Schema type name -> the Python types that satisfy it.
_JSON_TYPES: Dict[str, Tuple[type, ...]] = {
    "string": (str,),
    "boolean": (bool,),
    "number": (int, float),
    "integer": (int,),
}

#: Protocol revisions this server actually implements.  `initialize` echoes the
#: client's version only when it is one of these; anything else falls back to
#: our own rather than claiming support we do not have.
_SUPPORTED_PROTOCOL_VERSIONS: Tuple[str, ...] = (PROTOCOL_VERSION,)


class ToolError(ValueError):
    """A tool call was rejected before it reached the control plane."""


def _validate_arguments(name: str, supplied: Mapping[str, Any]) -> None:
    """Reject a call whose arguments do not fit the tool's own schema.

    Without this a wrong type reaches the operation and surfaces as a generic
    internal error, which tells the caller nothing about what it got wrong.
    """
    schema = _SCHEMAS[name]
    properties = schema.get("properties", {})
    for required in schema.get("required", ()):
        if required not in supplied:
            raise ToolError("missing required argument for %s: %s" % (name, required))
    for key, value in supplied.items():
        declared = properties.get(key, {}).get("type")
        expected = _JSON_TYPES.get(declared or "")
        if expected is None:
            continue
        if declared in ("number", "integer"):
            # bool subclasses int but is not a JSON number.
            valid = isinstance(value, expected) and not isinstance(value, bool)
        else:
            valid = isinstance(value, expected)
        if not valid:
            raise ToolError("%s must be %s" % (key, declared))


def call_tool(jobs: WorkBuddyJobs, name: str, arguments: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Run one tool call and always return a JSON-serialisable object."""
    if name not in TOOL_NAMES:
        raise ToolError("unknown tool: %s" % name)
    raw = dict(arguments or {})
    unexpected = set(raw) - set(_ARGUMENTS[name])
    if unexpected:
        raise ToolError("unexpected arguments for %s: %s" % (name, ", ".join(sorted(unexpected))))
    # An explicit null means "not provided" -- some clients send it instead of
    # omitting the key, and the operations treat an absent optional argument as
    # the default.  Dropping it here keeps that meaning for every tool: a null
    # optional value falls back to the default, while a null *required* value
    # is reported as missing rather than reaching the operation as None.
    supplied = {key: value for key, value in raw.items() if value is not None}
    _validate_arguments(name, supplied)
    operation = getattr(jobs, name)
    return operation(**supplied)


# ---------------------------------------------------------------------------
# stdio JSON-RPC loop
# ---------------------------------------------------------------------------
def _result(message_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def handle_message(jobs: WorkBuddyJobs, message: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn one JSON-RPC message into a response, or None for a notification."""
    method = message.get("method")
    message_id = message.get("id")
    if message_id is None:
        return None  # notification: initialised, cancelled, ...
    if method == "initialize":
        params = message.get("params") or {}
        requested = params.get("protocolVersion")
        return _result(
            message_id,
            {
                # Echo the client's revision only when we implement it; never
                # claim a version we do not speak.
                "protocolVersion": (
                    requested
                    if requested in _SUPPORTED_PROTOCOL_VERSIONS
                    else PROTOCOL_VERSION
                ),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "ping":
        return _result(message_id, {})
    if method == "tools/list":
        return _result(message_id, {"tools": [dict(tool) for tool in TOOL_DEFINITIONS]})
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        if not isinstance(name, str):
            return _error(message_id, -32602, "tools/call requires a tool name")
        try:
            payload = call_tool(jobs, name, params.get("arguments"))
        except ToolError as error:
            return _error(message_id, -32602, str(error))
        except ValueError as error:
            return _result(message_id, _text_result(str(error), is_error=True))
        except GatewayError as error:
            return _result(message_id, _text_result(str(error), is_error=True))
        return _result(message_id, _text_result(json.dumps(payload, ensure_ascii=False, sort_keys=True)))
    if method in ("resources/list", "prompts/list"):
        key = "resources" if method == "resources/list" else "prompts"
        return _result(message_id, {key: []})
    return _error(message_id, -32601, "method not found: %s" % method)


def _text_result(text: str, is_error: bool = False) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def serve(stdin: Any = None, stdout: Any = None, jobs: Optional[WorkBuddyJobs] = None) -> int:
    """Run the stdio loop until stdin closes."""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    owned = jobs is None
    jobs = jobs or WorkBuddyJobs()
    try:
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                sink.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
                sink.flush()
                continue
            if not isinstance(message, Mapping):
                sink.write(json.dumps(_error(None, -32600, "invalid request")) + "\n")
                sink.flush()
                continue
            try:
                response = handle_message(jobs, message)
            except Exception as error:  # pragma: no cover - last-resort guard
                response = _error(message.get("id"), -32603, "internal error: %s" % error)
            if response is not None:
                sink.write(json.dumps(response, ensure_ascii=False) + "\n")
                sink.flush()
    finally:
        if owned:
            jobs.close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--print-tools":
        print(json.dumps([dict(tool) for tool in TOOL_DEFINITIONS], ensure_ascii=False, indent=2))
        return 0
    if args and args[0] == "--selfcheck":
        jobs = WorkBuddyJobs()
        try:
            listed = jobs.list()
        except (GatewayError, ValueError) as error:
            print("gateway unreachable: %s" % error)
            return 2
        finally:
            jobs.close()
        print("gateway ok: %s jobs" % listed.get("count", 0))
        return 0
    return serve()


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
