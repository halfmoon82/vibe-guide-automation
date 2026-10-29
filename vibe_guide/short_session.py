"""One-shot short sessions: launch an agent CLI once, read its answer, let it go.

The daemon uses a short session only when a judgement is needed (second-level
self-heal).  This module only knows how each platform is started and how to read
what came back; it never decides whether the session fixed anything -- the
daemon re-checks that itself with ``heal-verify``.

Every outcome the daemon cannot trust is ``unknown`` (a missing command, a
timeout, an unreadable answer).  None of them is ever reported as success.
Platform command lines other than ``claude -p`` are *unverified* here: no real
session of those platforms was started, and ``capability_report`` says so.
"""

import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"
STATUS_QUEUED = "queued"

DEFAULT_TIMEOUT_SECONDS = 900.0
_POLL_SECONDS = 1.0
_OUTPUT_LIMIT = 20000

PLATFORM_CLAUDE_CODE = "claude-code"
PLATFORM_CODEX = "codex"
PLATFORM_KIMI_CODE = "kimi-code"
PLATFORM_WORKBUDDY = "workbuddy"

#: platform -> (executable, argv before the prompt, flag that selects the model,
#: verified).  WorkBuddy has no command line: it reads a mailbox instead.
_PLATFORMS = {
    PLATFORM_CLAUDE_CODE: ("claude", ["-p"], "--model", True),
    PLATFORM_CODEX: ("codex", ["exec"], "-m", False),
    PLATFORM_KIMI_CODE: ("kimi", ["-p"], "--model", False),
}

SUPPORTED_PLATFORMS = tuple(_PLATFORMS) + (PLATFORM_WORKBUDDY,)


@dataclass
class ShortSessionResult:
    platform: str
    status: str
    reason: str = ""
    text: str = ""
    returncode: Optional[int] = None
    #: {"input", "output", "cache_read", "cache_creation"} ints, or None when the
    #: platform returned no usage -- never zeros invented to fill the gap.
    usage: Optional[Dict[str, int]] = None
    mailbox_path: str = ""
    command: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform, "status": self.status, "reason": self.reason,
            "returncode": self.returncode, "usage": self.usage,
            "mailbox_path": self.mailbox_path,
        }


def capability_report() -> Dict[str, Dict[str, Any]]:
    """What is known about each platform's one-shot path.  Nothing here is probed."""
    report: Dict[str, Dict[str, Any]] = {}
    for platform, (executable, prefix, model_flag, verified) in _PLATFORMS.items():
        report[platform] = {
            "command": [executable] + prefix, "model_flag": model_flag,
            "status": "verified_flags" if verified else "unverified",
            "reason": "" if verified else "no real session of this platform has been started by vibe",
        }
    report[PLATFORM_WORKBUDDY] = {
        "command": None, "status": "unverified",
        "reason": "no command line: the request is written to a mailbox and handled by "
                  "the WorkBuddy automation on its next fresh session",
    }
    return report


def build_command(platform: str, prompt: str, model: Optional[str] = None,
                  executable: Optional[str] = None,
                  extra_args: Sequence[str] = ()) -> List[str]:
    if platform not in _PLATFORMS:
        raise ValueError("platform has no command line: %s" % platform)
    default_executable, prefix, model_flag, _ = _PLATFORMS[platform]
    command = [executable or default_executable] + list(prefix)
    if platform == PLATFORM_CLAUDE_CODE:
        command += ["--output-format", "json"]
    if model:
        command += [model_flag, model]
    command += [str(item) for item in extra_args]
    command.append(prompt)
    return command


def _non_negative_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def parse_claude_output(stdout: str):
    """(answer text, usage or None) from ``claude -p --output-format json``."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return stdout, None
    if isinstance(data, list):
        data = next((item for item in reversed(data)
                     if isinstance(item, dict) and item.get("type") == "result"), None)
    if not isinstance(data, dict):
        return stdout, None
    text = data.get("result")
    text = text if isinstance(text, str) else ""
    raw = data.get("usage")
    usage = None
    if isinstance(raw, Mapping):
        fields = {
            "input": raw.get("input_tokens"), "output": raw.get("output_tokens"),
            "cache_read": raw.get("cache_read_input_tokens"),
            "cache_creation": raw.get("cache_creation_input_tokens"),
        }
        parsed = {key: _non_negative_int(value) for key, value in fields.items()}
        # A usage block missing a field is not read as zero for that field.
        if all(value is not None for value in parsed.values()):
            usage = parsed
    return text, usage


def claude_error_text(stdout: str) -> str:
    """The ``result`` text of a ``claude -p --output-format json`` error, else ""."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return ""
    if isinstance(data, list):
        data = next((item for item in reversed(data)
                     if isinstance(item, dict) and item.get("type") == "result"), None)
    if not isinstance(data, dict) or data.get("is_error") is not True:
        return ""
    text = data.get("result")
    return " ".join(text.split())[:200] if isinstance(text, str) else "the session reported an error"


#: set by the Claude Code desktop host for its own sessions; a one-shot session
#: that inherits them comes up "Not logged in" or "Invalid API key".  Bisected on
#: the 5.2.1 rehearsal machine: these two are the whole set that matters.
_HOST_ONLY_ENV = ("CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "CLAUDE_CODE_HOST_AUTH_ENV_VAR")


def _kill_group(process: "subprocess.Popen") -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            process.kill()
        except OSError:
            pass


def _run_process(command: Sequence[str], *, cwd, env, timeout: float,
                 poll: Optional[Callable[[], None]]):
    """(returncode, stdout, stderr) or raises subprocess.TimeoutExpired."""
    process = subprocess.Popen(
        list(command), cwd=str(cwd) if cwd else None, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    while True:
        try:
            stdout, stderr = process.communicate(timeout=min(_POLL_SECONDS, max(0.01, deadline - time.monotonic())))
            return process.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            if poll is not None:
                poll()
            if time.monotonic() >= deadline:
                _kill_group(process)
                process.communicate()
                raise


def write_mailbox_request(mailbox_dir: Path, prompt: str, *, run_id: str = "",
                          node_id: str = "", model: Optional[str] = None) -> Path:
    """WorkBuddy path: one request file for the automation to pick up."""
    mailbox_dir = Path(mailbox_dir)
    mailbox_dir.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex[:16]
    path = mailbox_dir / ("short-session-%s.json" % request_id)
    record = {"request_id": request_id, "run_id": run_id, "node_id": node_id,
              "model": model, "prompt": prompt, "created_at": time.time()}
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(str(temporary), str(path))
    return path


def launch(platform: str, prompt: str, *, model: Optional[str] = None,
           cwd=None, timeout: float = DEFAULT_TIMEOUT_SECONDS,
           executable: Optional[str] = None, extra_args: Sequence[str] = (),
           env: Optional[Mapping[str, str]] = None, mailbox_dir=None,
           run_id: str = "", node_id: str = "",
           poll: Optional[Callable[[], None]] = None) -> ShortSessionResult:
    """Start one short session and wait for it (bounded by ``timeout``).

    ``poll`` is called about once a second while waiting so the caller can keep
    its own heartbeat alive.  ``model`` is whatever the caller resolved (the
    model-tiers conversion is not wired in yet: ``None`` means the platform
    default).
    """
    if platform == PLATFORM_WORKBUDDY:
        if mailbox_dir is None:
            return ShortSessionResult(platform, STATUS_UNKNOWN, "no mailbox directory given")
        try:
            path = write_mailbox_request(mailbox_dir, prompt, run_id=run_id, node_id=node_id, model=model)
        except OSError as error:
            return ShortSessionResult(platform, STATUS_UNKNOWN, "mailbox write failed: %s" % type(error).__name__)
        # Queued is not done: the automation's outcome is read later, never assumed.
        return ShortSessionResult(platform, STATUS_QUEUED, "waiting for the WorkBuddy automation",
                                  mailbox_path=str(path))
    if platform not in _PLATFORMS:
        return ShortSessionResult(platform, STATUS_UNKNOWN, "unsupported platform")
    command = build_command(platform, prompt, model, executable, extra_args)
    if shutil.which(command[0]) is None:
        return ShortSessionResult(platform, STATUS_UNKNOWN, "command not available: %s" % command[0],
                                  command=command[:-1])
    try:
        if env is None:
            env = {key: value for key, value in os.environ.items() if key not in _HOST_ONLY_ENV}
        returncode, stdout, stderr = _run_process(
            command, cwd=cwd, env=dict(env), timeout=timeout, poll=poll)
    except subprocess.TimeoutExpired:
        return ShortSessionResult(platform, STATUS_UNKNOWN, "timed out after %ds" % int(timeout),
                                  command=command[:-1])
    except OSError as error:
        return ShortSessionResult(platform, STATUS_UNKNOWN, "launch failed: %s" % type(error).__name__,
                                  command=command[:-1])
    usage = None
    text = stdout
    error_text = ""
    if platform == PLATFORM_CLAUDE_CODE:
        text, usage = parse_claude_output(stdout)
        error_text = claude_error_text(stdout)
    failed = returncode != 0 or bool(error_text)
    status = STATUS_FAILED if failed else STATUS_OK
    if not failed:
        reason = ""
    elif error_text:
        # The CLI's own words ("Not logged in", "Invalid API key") are what the
        # product manager needs; nothing here is a credential.
        reason = "exit code %s: %s" % (returncode, error_text)
    else:
        reason = "exit code %s" % returncode
    return ShortSessionResult(platform, status, reason, text[-_OUTPUT_LIMIT:], returncode, usage,
                              command=command[:-1])
