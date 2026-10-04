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


class Gateway:
    """One HTTP gateway, either supplied by env or started on first use.

    A started gateway is owned by this object and stopped by :meth:`close`.
    The password it prints is held in memory only and never logged.
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
            last_banner = ""
            for attempt in range(2):
                port = _free_port()
                with log.open("w", encoding="utf-8") as sink:
                    process = subprocess.Popen(
                        [cli, "--serve", "--host", "127.0.0.1", "--port", str(port)],
                        stdout=sink,
                        stderr=subprocess.STDOUT,
                    )
                banner = {"endpoint": "", "password": ""}
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
                if banner["endpoint"] and banner["password"]:
                    break
                last_banner = _plain(log.read_text(encoding="utf-8", errors="replace"))[-400:].strip()
                process.terminate()
                if process.poll() is None:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
            else:
                raise GatewayError(
                    "gateway did not report an endpoint and password: %s" % last_banner
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


class ToolError(ValueError):
    """A tool call was rejected before it reached the control plane."""


def call_tool(jobs: WorkBuddyJobs, name: str, arguments: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Run one tool call and always return a JSON-serialisable object."""
    if name not in TOOL_NAMES:
        raise ToolError("unknown tool: %s" % name)
    supplied = dict(arguments or {})
    unexpected = set(supplied) - set(_ARGUMENTS[name])
    if unexpected:
        raise ToolError("unexpected arguments for %s: %s" % (name, ", ".join(sorted(unexpected))))
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
                "protocolVersion": requested if isinstance(requested, str) and requested else PROTOCOL_VERSION,
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
