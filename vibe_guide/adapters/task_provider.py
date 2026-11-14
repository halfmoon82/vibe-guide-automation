"""Platform-neutral task/session bridges.

The public Codex App calls are injected into :class:`CodexAppBridge`; this
package never imports or fabricates the desktop tool implementation.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import unquote, urlsplit
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..paths import ProjectPaths
from ..workflow_gate import (
    require_capability_contract,
    require_entry,
    require_child_origin,
)
from ..diagnostics import validate_child_session_binding
from ..models import WorkerProfile
from ..providers import CLAUDE_CODE_PROVIDER, CODEX_PROVIDER


@contextmanager
def recovery_action_lock(path: Path, timeout: float = 10.0):
    """Lock an existing mailbox inode without erasing its claim metadata."""
    from .._wincompat import flock_exclusive, flock_release
    import stat
    if path.parent.is_symlink() or path.is_symlink():
        raise PermissionError("provider lock path is unverifiable")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as error:
        raise PermissionError("provider lock is missing or unreadable") from error
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PermissionError("provider lock is not a regular file")
        deadline = time.monotonic() + timeout
        while True:
            try:
                flock_exclusive(descriptor, blocking=False)
                locked = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for provider action lock")
                time.sleep(0.01)
        metadata = os.fstat(descriptor)
        named = os.lstat(path)
        if (metadata.st_dev, metadata.st_ino) != (named.st_dev, named.st_ino) or metadata.st_size > 4096:
            raise PermissionError("provider lock identity changed")
        yield os.read(descriptor, 4097)
    finally:
        if locked:
            flock_release(descriptor)
        os.close(descriptor)


def _native_command_identity(item):
    """Normalize only audited representations, not shell semantics."""
    command, cwd = item.get("command"), item.get("cwd")
    if isinstance(command, list):
        if (len(command) != 3 or command[0] != "/bin/zsh"
                or command[1] not in {"-c", "-lc"} or not isinstance(command[2], str)):
            return None
        command = command[2]
    if not isinstance(command, str) or not isinstance(cwd, str):
        return None
    if cwd.startswith("file:"):
        url = urlsplit(cwd)
        if (url.scheme != "file" or url.netloc not in {"", "localhost"}
                or url.query or url.fragment or re.search(r"%(?![0-9a-fA-F]{2})", url.path)):
            return None
        cwd = unquote(url.path, errors="strict")
    if not Path(cwd).is_absolute() or "\x00" in cwd:
        return None
    return {"command": command, "cwd": cwd}


def _native_command_exit(item):
    """A completed command may fail; conflicting exit facts stay unknown."""
    values = [item[key] for key in ("exitCode", "exit_code") if key in item]
    if not values or any(type(value) is not int for value in values) or len(set(values)) != 1:
        return None
    code = values[0]
    status = item.get("status")
    # Historical camelCase records used completed for nonzero exits too.
    if status == "completed" and (code == 0 or "exit_code" not in item):
        return code
    if status == "failed" and code != 0:
        return code
    return None


class ProviderUnavailable(RuntimeError):
    """A provider cannot safely create or continue a task."""


class UnresolvedNativeTool(RuntimeError):
    """The host could not resolve the native tool the caller asked for.

    Observed 2026-10-14: a model gateway route stripped the ``fn__<server>__``
    prefix from helper tool calls, so the host answered "No such tool
    available".  That is a routing fault in the calling session, not a
    provider outcome; the request stays pending.
    """

    def __init__(self, action_id: str, called_tool: str, expected_tool: str, attempts: int):
        self.action_id = action_id
        self.called_tool = called_tool
        self.expected_tool = expected_tool
        self.attempts = attempts
        if attempts < 2:
            advice = "retry the native call once; the request stays pending"
        else:
            advice = ("stop servicing the mailbox and report a tool routing fault to the user; "
                      "do not hand off, a new session hits the same route")
        super().__init__(
            "host could not resolve native tool {!r} (expected {!r}) for {}; attempt {}: {}".format(
                called_tool, expected_tool, action_id, attempts, advice))


class ForkedProviderSession(ValueError):
    """A ``create`` result names a Codex thread that was forked, not created.

    run-63e7cfc2 (2026-10-26): ``fork_thread`` copies the caller's whole
    history into the new thread, so a worker "created" that way started at the
    supervisor's ~650k tokens.  The request stays pending.
    """

    def __init__(self, action_id: str, thread_id: str, forked_from: str):
        self.action_id = action_id
        self.thread_id = thread_id
        self.forked_from = forked_from
        super().__init__(
            "thread {} for {} was made by fork_thread from {} and carries that session's "
            "full history; archive it and call create_thread with the project target "
            "instead (the request stays pending). If a created thread cannot write the "
            "worktree, stop and report the sandbox to the user rather than forking".format(
                thread_id, action_id, forked_from))


def _codex_forked_from(thread_id: str) -> Optional[str]:
    """``forked_from_id`` from the thread's Codex session record, or None.

    Codex Desktop keeps one ``rollout-<time>-<thread id>.jsonl`` per thread
    under ``$CODEX_HOME/sessions/<y>/<m>/<d>/`` (or ``archived_sessions/``),
    whose first line is ``session_meta``.  A missing or unreadable record is
    no evidence either way.
    """
    if not isinstance(thread_id, str) or not _SAFE_ID.fullmatch(thread_id):
        return None
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    pattern = "rollout-*-{}.jsonl".format(thread_id)
    candidates = list(home.glob("sessions/*/*/*/" + pattern)) + list(
        home.glob("archived_sessions/" + pattern))
    for path in candidates:
        try:
            with path.open(encoding="utf-8") as stream:
                first = json.loads(stream.readline())
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        meta = first.get("payload") if isinstance(first, dict) else None
        if isinstance(first, dict) and first.get("type") == "session_meta" and isinstance(meta, dict):
            forked = meta.get("forked_from_id")
            return forked if isinstance(forked, str) and forked else None
    return None


def _result_thread_id(payload: Dict[str, Any]) -> Optional[str]:
    binding = payload.get("binding")
    for source in (binding if isinstance(binding, dict) else {}, payload):
        value = source.get("threadId") or source.get("thread_id")
        if isinstance(value, str) and value:
            return value
    return None


_UNRESOLVED_TOOL = re.compile(
    r"\s*(?:<tool_use_error>)?\s*(?:Error:\s*)?No such tool available:\s*`?([\w.-]+)", re.IGNORECASE)


def _unresolved_tool_name(payload: Dict[str, Any]) -> Optional[str]:
    # Top-level values that *are* the host reply; nested transcript events and
    # mid-sentence mentions are ordinary results.
    for value in payload.values():
        if isinstance(value, dict):
            value = value.get("message")
        if isinstance(value, str):
            match = _UNRESOLVED_TOOL.match(value)
            if match:
                return match.group(1)
    return None


class ProviderPending(RuntimeError):
    """A provider returned a pending task handle without a real task id."""


_PROVIDER_ACTIONS = {"create", "locate", "visibility", "resume", "wait"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
#: Capability-record keys beyond the required four; older records lack them.
_OPTIONAL_CAPABILITY_KEYS = frozenset({"project_id", "observed_at", "host_version", "session_id"})
_WORKER_DELIVERY_EVENTS = {"delivered", "complete", "accepted", "review_finding",
                           "blocked_engineering"}


def _is_complex_contract(value: Any) -> bool:
    """Recognize complex execution requests without changing light routes."""
    if not isinstance(value, Mapping):
        return False
    if value.get("complex") is True or value.get("is_complex") is True:
        return True
    band = value.get("complexity_band") or value.get("route")
    if isinstance(band, str) and band.strip().casefold() == "complex":
        return True
    issue = value.get("issue_complexity")
    if isinstance(issue, Mapping):
        issue_band = issue.get("complexity_band") or issue.get("route")
        if isinstance(issue_band, str) and issue_band.strip().casefold() == "complex":
            return True
    plan = value.get("plan")
    if isinstance(plan, Mapping):
        plan_band = plan.get("complexity_band") or plan.get("route")
        if isinstance(plan_band, str) and plan_band.strip().casefold() == "complex":
            return True
    # An SDD-only execution mode is itself a complex-DAG bypass attempt.
    mode = value.get("execution_mode") or value.get("engine_mode")
    return isinstance(mode, str) and mode.strip().casefold() in {"sdd", "serial", "sdd_only"}


def require_complex_monitor_dispatch(
    contract: Mapping[str, Any], paths: Optional[ProjectPaths] = None
) -> None:
    """Fail closed for complex work unless Monitor supplied the full proof.

    This guard intentionally runs before provider mailbox writes, process
    creation, task binding or writer-lease acquisition.  Simple/light and
    read-only callers do not enter it.
    """
    if not _is_complex_contract(contract):
        return
    dispatcher = contract.get("dispatcher") or contract.get("dispatch_origin")
    if dispatcher != "monitor":
        raise PermissionError("complex_monitor_required: dispatcher origin must be monitor")
    if paths is None:
        raise PermissionError("complex_monitor_required: project paths are required")
    try:
        from ..workflow_gate import require_v42_sdd_first
        require_v42_sdd_first(paths)
    except PermissionError as error:
        raise PermissionError("complex_monitor_required: " + str(error)) from error
    if contract.get("execution_engine") != "vibeguide_monitor" or contract.get("engine_mode") != "dag":
        raise PermissionError("complex_monitor_required: execution engine is not Monitor DAG")
    evidence_ref = contract.get("engine_evidence_ref")
    if not isinstance(evidence_ref, str) or not evidence_ref.strip() or evidence_ref.startswith("unverified:"):
        raise PermissionError("complex_monitor_required: engine attestation is missing")
    authorization_digest = contract.get("authorization_digest")
    if not isinstance(authorization_digest, str) or len(authorization_digest) != 64:
        raise PermissionError("complex_monitor_required: executable authorization is missing")
    plan_id = contract.get("plan_id")
    plan_version = contract.get("plan_version")
    if not isinstance(plan_id, str) or not plan_id.strip() or not isinstance(plan_version, int):
        raise PermissionError("complex_monitor_required: plan binding is missing")
    try:
        from ..engine_attestation import validate_engine_attestation
        attestation_path = paths.resolve_vibe_path(
            Path("plans") / plan_id / "engine-attestation.json"
        )
        if attestation_path.is_symlink() or not attestation_path.is_file():
            raise ValueError("attestation is not a regular file")
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
        validate_engine_attestation(attestation, plan_id, plan_version)
        if attestation.get("evidence_ref") != evidence_ref:
            raise ValueError("attestation evidence reference mismatch")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise PermissionError("complex_monitor_required: engine attestation is invalid") from error


class TaskProviderAdapter:
    """Provider-neutral Agent-session upgrade entry.

    This small bridge delegates to a verified provider callback/object and
    never reimplements installation or migration. Provider observations,
    including ``unknown`` and ``unknown_timeout``, are returned unchanged.
    """

    def __init__(self, provider: str, delegate: Any = None, *, mode: str = "visible", paths: Optional[ProjectPaths] = None):
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider is required")
        if mode not in {"visible", "background", "guide"}:
            raise ValueError("unsupported task provider mode")
        self.provider = provider.strip()
        self.delegate = delegate
        self.mode = mode
        self.paths = paths

    def describe_upgrade_entry(self) -> Dict[str, Any]:
        return {
            "entry": "upgrade",
            "entrypoint": "upgrade",
            "provider": self.provider,
            "mode": self.mode,
            "provider_neutral": True,
            "limitations": (["不可见", "不可直接进入", "返工续接受限"] if self.mode == "background" else []),
            "evidence": {"provider": "adapter-manifest", "mode": "capability-observation"},
        }

    def invoke_upgrade(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(request, Mapping):
            raise TypeError("upgrade request must be a mapping")
        require_complex_monitor_dispatch(request, self.paths)
        target = self.delegate
        method = getattr(target, "invoke_upgrade", None)
        if callable(method):
            result = method(dict(request))
        elif callable(target):
            result = target(dict(request))
        else:
            return {
                "status": "unknown",
                "provider": self.provider,
                "evidence": ["provider adapter delegate is not configured"],
            }
        if isinstance(result, Mapping):
            return dict(result)
        raise ProviderUnavailable("provider upgrade entry returned invalid result")

    def start(self, request: Mapping[str, Any]):
        """Compatibility entry for task adapters, guarded for complex DAGs."""
        if not isinstance(request, Mapping):
            raise TypeError("task request must be a mapping")
        require_complex_monitor_dispatch(request, self.paths)
        target = self.delegate
        method = getattr(target, "start", None)
        if callable(method):
            return method(dict(request))
        if callable(target):
            return target(dict(request))
        raise ProviderUnavailable("provider task start delegate is not configured")


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


_PROJECT_MOUNT_NONE = 'none'

#: Result key that carries advisory observations *beside* a delivery (issue
#: #240).  It is recomputed at hand-off from the report itself and is never
#: copied out of the worker payload: a delivery must not be able to smuggle a
#: warning into the monitor's event data, and the payload digest that forms
#: ``idempotent_key`` must stay bit-for-bit what it was before this field
#: existed.  The runner imports this name, so the two sides of the hand-off
#: cannot drift apart.
WARNINGS_KEY = 'warnings'

_ADVISORY_RESULT_KEYS = (WARNINGS_KEY,)
#: fixed payload marker: the worker could not send the wake signal; the daemon's next cycle takes the report
WAKE_FAILED_KEY = 'wake_failed'


def _requested_project_mount(request: Any, operation: str) -> Optional[str]:
    """Mount to record for a dispatch request, or ``None`` to inherit one.

    ``'project:<id>'`` when a session-creating request named a mount,
    ``'none'`` when it named none -- the shape that asks the host for a
    projectless worker (issue #219) -- and ``None`` for locate / resume /
    wait.  Those cannot move a session that already exists to another
    project, so their record inherits the generation that created it; without
    that, a rework would look exactly like a dispatch that asked for no
    mount at all.

    Only the *requested* mount is knowable here: vibe never sees the sandbox
    roots the host actually granted a session.  Recording what was asked for
    keeps "this dispatch never asked for a project" distinguishable from
    "this dispatch asked and the host did something else" (which the worker's
    own reported session root is the only way to see).
    """
    if operation != 'create':
        return None
    target = request.get('target') if isinstance(request, Mapping) else None
    if not isinstance(target, Mapping):
        return _PROJECT_MOUNT_NONE
    value = target.get('projectId')
    if isinstance(value, str) and value.strip():
        return 'project:' + value.strip()
    return _PROJECT_MOUNT_NONE


class ProviderActionStore:
    """Bounded request/result mailbox between the CLI and a desktop session."""

    schema_version = 1

    def __init__(self, paths: ProjectPaths):
        self.paths = paths
        self.root = paths.resolve_vibe_path("provider-actions")

    def _directory(self, name: str) -> Path:
        directory = self.root / name
        if self.root.is_symlink() or directory.is_symlink():
            raise ValueError("provider action path may not be a symlink")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def _atomic(path: Path, payload: Dict[str, Any]) -> None:
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("provider action path may not be a symlink")
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > 64 * 1024:
            raise ValueError("provider action record exceeds the size bound")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="." + path.name + ".", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(str(temporary), str(path))
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _read(path: Path) -> Dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise ValueError("provider action record must be a regular file")
        raw = path.read_bytes()
        if len(raw) > 64 * 1024:
            raise ValueError("provider action record exceeds the size bound")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("provider action record is invalid") from error
        if not isinstance(value, dict):
            raise ValueError("provider action record must be an object")
        return value

    def publish_capabilities(
        self,
        adapter_id: str,
        facts: Mapping[str, Any],
        provenance: str,
        project_id: Optional[str] = None,
        *,
        observed_at: Optional[str] = None,
        host_version: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """Write this adapter's own capability record, and nothing else's.

        ``observed_at`` (UTC, ISO 8601; defaults to now), ``host_version`` and
        ``session_id`` date the observation so ``vibe doctor`` can tell when
        it needs a second look.  The pre-split single ``capabilities.json`` is
        migrated here, once: a record that names another adapter moves to its
        own file unless that file exists, then the single file is removed.
        Read paths never perform this migration.
        """
        if not isinstance(adapter_id, str) or not adapter_id:
            raise ValueError("adapter id is required")
        if not _SAFE_ID.fullmatch(adapter_id):
            raise ValueError("adapter id must be a simple identifier")
        if (
            not isinstance(facts, Mapping)
            or not facts
            or any(
                not isinstance(key, str)
                or not key.startswith(adapter_id + ".")
                or type(value) is not bool
                for key, value in facts.items()
            )
        ):
            raise ValueError("provider capability facts are invalid")
        if not isinstance(provenance, str) or not provenance:
            raise ValueError("provider capability provenance is required")
        payload = {
            "schema_version": self.schema_version,
            "adapter_id": adapter_id,
            "facts": dict(facts),
            "provenance": provenance,
            "observed_at": observed_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        # The project id is optional: visible-task routing needs it, other
        # routes do not, and only the recording session can vouch for it.
        if project_id is not None:
            if not isinstance(project_id, str) or not project_id.strip():
                raise ValueError("provider capability project_id must be a non-empty string")
            payload["project_id"] = project_id.strip()
        for key, value in (("host_version", host_version), ("session_id", session_id)):
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError("provider capability {} must be a short non-empty string".format(key))
            payload[key] = value.strip()
        self._validate_capability(payload)
        self.root.mkdir(parents=True, exist_ok=True)
        # The per-adapter file is authoritative (issue #279): each adapter's
        # session attests its own record and no attest of *another* adapter
        # overwrites it.
        self._atomic(self.root / ("capabilities-" + adapter_id + ".json"), payload)
        self._migrate_legacy_capability(adapter_id)

    def _migrate_legacy_capability(self, attested: str) -> None:
        """One-time move of the pre-split ``capabilities.json`` (attest only).

        A record for another adapter moves into its own file when that file
        does not exist yet; a record for the adapter just attested is
        superseded.  Either way the single file is then removed, as is one
        that no longer validates (no reader can use it, and the pre-split
        attest overwrote it the same way).  A record naming an unsafe id is
        left untouched and readers skip it.
        """
        legacy = self.root / "capabilities.json"
        if not legacy.is_file() or legacy.is_symlink():
            return
        try:
            previous = self._validate_capability(self._read(legacy))
        except (OSError, ValueError):
            legacy.unlink()
            return
        previous_id = str(previous["adapter_id"])
        if not _SAFE_ID.fullmatch(previous_id):
            return
        migrated = self.root / ("capabilities-" + previous_id + ".json")
        if previous_id != attested and not migrated.exists():
            self._atomic(migrated, previous)
        legacy.unlink()

    def _validate_capability(self, value: Dict[str, Any]) -> Dict[str, Any]:
        required = {"schema_version", "adapter_id", "facts", "provenance"}
        if not isinstance(value, dict) or not required.issubset(value) or set(value) - required - _OPTIONAL_CAPABILITY_KEYS:
            raise ValueError("provider capability schema is invalid")
        if value["schema_version"] != self.schema_version:
            raise ValueError("provider capability schema is unsupported")
        return value

    def capabilities_all(self) -> Dict[str, Dict[str, Any]]:
        """Every recorded adapter capability, keyed by its own ``adapter_id``.

        The per-adapter files win; the legacy ``capabilities.json`` record
        joins only for adapters that predate the split and never got a
        per-adapter file.  No record is ever attributed to another adapter.
        Read-only: the legacy file is migrated by ``publish_capabilities``
        (attest), never here.
        """
        records: Dict[str, Dict[str, Any]] = {}
        if not self.root.is_dir():
            return records
        if self.root.is_symlink():
            raise ValueError("provider action path may not be a symlink")
        for path in sorted(self.root.glob("capabilities-*.json")):
            if not path.is_file() or path.is_symlink():
                continue
            record = self._validate_capability(self._read(path))
            records[str(record["adapter_id"])] = record
        legacy = self.root / "capabilities.json"
        if legacy.is_file() and not legacy.is_symlink():
            record = self._validate_capability(self._read(legacy))
            if _SAFE_ID.fullmatch(str(record["adapter_id"])):
                records.setdefault(str(record["adapter_id"]), record)
        return records

    def capabilities(self, adapter_id: Optional[str] = None) -> Dict[str, Any]:
        """The recorded capability bridge for one adapter.

        With ``adapter_id`` the call reads exactly that adapter's record: a
        per-adapter file when it exists, else the legacy single file only when
        it names this adapter.  A missing record raises ``FileNotFoundError``;
        another adapter's record is never borrowed in its place.

        Without an adapter the call is the legacy convenience view: it needs
        exactly one recorded adapter, and refuses rather than guessing when
        several adapters have attested.
        """
        if adapter_id is not None:
            if not isinstance(adapter_id, str) or not _SAFE_ID.fullmatch(adapter_id.strip()):
                raise ValueError("adapter id must be a simple identifier")
            adapter_id = adapter_id.strip()
            per_adapter = self.root / ("capabilities-" + adapter_id + ".json")
            # Same symlink posture as capabilities_all: a symlinked record is
            # untrusted evidence and reads as "not registered", never borrowed.
            if per_adapter.is_file() and not per_adapter.is_symlink():
                record = self._validate_capability(self._read(per_adapter))
                if record["adapter_id"] != adapter_id:
                    raise ValueError("provider capability record names a different adapter")
                return record
            legacy = self.root / "capabilities.json"
            if legacy.is_file() and not legacy.is_symlink():
                record = self._validate_capability(self._read(legacy))
                if record["adapter_id"] == adapter_id:
                    return record
            raise FileNotFoundError(
                "no capability record for adapter: {}".format(adapter_id)
            )
        records = self.capabilities_all()
        if not records:
            raise FileNotFoundError("provider capability record is missing")
        if len(records) > 1:
            raise ValueError(
                "multiple adapters have capability records; specify the adapter"
            )
        return next(iter(records.values()))

    def request(
        self,
        *,
        operation: str,
        provider: str,
        run_id: str,
        issue_id: str,
        role: str,
        generation: int,
        native_tool: str,
        request: Dict[str, Any],
        sequence: int = 0,
        _initial_anchor_sequence: Optional[int] = None,
        _controlled_create: bool = False,
    ) -> Dict[str, Any]:
        # Complex requests may only be emitted by Monitor after its complete
        # V4.2 state, engine attestation and authorization checks.
        require_complex_monitor_dispatch(request, self.paths)
        state = self.paths.vibe / "state.json"
        if state.is_file():
            try:
                origin = request.get("origin", "user_entry") if isinstance(request, dict) else "user_entry"
                if origin == "worker_dispatch":
                    require_child_origin(origin)
                    capability_contract = require_capability_contract(self.paths)
                    binding = request.get("child_binding")
                    required = {"parent_run_id", "plan_revision", "authorization_digest", "node_id", "role", "writer", "worktree", "branch", "allowlist", "worker_profile", "capability_contract_digest"}
                    if not isinstance(binding, dict) or not required.issubset(binding):
                        raise PermissionError("child binding is incomplete")
                    if binding.get("capability_contract_digest") != capability_contract.contract_digest:
                        raise PermissionError("child capability contract digest mismatch")
                    if binding.get("parent_run_id") != run_id or binding.get("node_id") != issue_id or binding.get("role") != role:
                        raise PermissionError("child binding identity mismatch")
                    if not (isinstance(binding.get("plan_revision"), str) and binding["plan_revision"].isdigit() and int(binding["plan_revision"]) > 0 and isinstance(binding.get("authorization_digest"), str) and len(binding["authorization_digest"]) == 64 and all(ch in "0123456789abcdef" for ch in binding["authorization_digest"].lower())):
                        raise PermissionError("child binding parent context is unverifiable")
                    profile = WorkerProfile(**binding["worker_profile"])
                    validate_child_session_binding(binding["parent_run_id"], binding["plan_revision"], binding["authorization_digest"], binding["node_id"], binding["role"], profile)
                else:
                    require_entry(self.paths, "provider:" + run_id + ":" + operation, operation)
            except (OSError, ValueError, TypeError, PermissionError) as error:
                raise ProviderUnavailable("session_gate_blocked") from error
        elif self.paths.vibe.exists():
            raise ProviderUnavailable("session_gate_blocked")
        if operation not in _PROVIDER_ACTIONS:
            raise ValueError("provider action is unsupported")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("provider action sequence is invalid")
        basis = {
            "operation": operation,
            "provider": provider,
            "run_id": run_id,
            "issue_id": issue_id,
            "role": role,
            "generation": generation,
            "sequence": sequence,
        }
        action_id = "action-" + _canonical_digest(basis)[:32]
        payload = {
            "schema_version": self.schema_version,
            "action_id": action_id,
            **basis,
            "native_tool": native_tool,
            "request": request,
        }
        payload["request_digest"] = _canonical_digest(payload)
        controlled_payload = None
        if _controlled_create:
            controlled_payload = payload
            payload = dict(payload)
            payload["native_tool"] = "controlled_create"
            payload["request"] = {
                "controlled": True,
                "executable_digest": _canonical_digest({
                    "native_tool": controlled_payload["native_tool"],
                    "request": controlled_payload["request"],
                }),
            }
            payload["request_digest"] = _canonical_digest({
                key: value for key, value in payload.items() if key != "request_digest"
            })
        directory = self._directory("requests")
        path = directory / (action_id + ".json")
        if _controlled_create and operation != "create":
            raise ValueError("controlled publication requires create")
        if (operation == "create" and sequence == 0
                and (request.get("origin") == "worker_dispatch"
                     or native_tool == "controlled_create")
                and not request.get("binding_probe")
                and not request.get("successor")):
            # A run-bound pre-publication anchor protects an initial create
            # from both identity drift and a concurrent recovery transaction.
            # Legacy requests and strict probe creates have no such anchor and
            # retain their established handling outside a recovery fence.
            from ..state import interprocess_lock, load_events
            plan_id = request.get("plan_id")
            fence = self.paths.resolve_vibe_path(Path("plans") / str(plan_id) / ".initial-recovery-pending.json")
            lock_path = self._directory("locks") / (action_id + ".lock")
            lock_context = (interprocess_lock(lock_path) if _initial_anchor_sequence is not None
                            else recovery_action_lock(lock_path))
            with lock_context:
                if fence.exists() or fence.is_symlink():
                    raise PermissionError("initial recovery fence prevents provider publication")
                rows = load_events(self.paths, run_id)
                anchors = [row for row in rows if row.get("event") == "initial_create_anchored"
                           and row.get("data", {}).get("action_id") == action_id]
                if not anchors and not request.get("binding_probe") and not request.get("successor"):
                    if _initial_anchor_sequence is not None or not (path.exists() or path.is_symlink()):
                        raise PermissionError("initial create request has no pre-publication anchor")
                    if self.action(action_id) != payload:
                        raise PermissionError("legacy initial create request cannot be replaced")
                    return payload
                if anchors:
                    effective_request = request
                    effective_native_tool = native_tool
                    if native_tool == "controlled_create":
                        controlled = controlled_payload or self._read_controlled_payload(payload)
                        if controlled is not None:
                            effective_request = controlled["request"]
                            effective_native_tool = controlled["native_tool"]
                    expected_raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                    anchor = anchors[0].get("data", {})
                    if (len(anchors) != 1 or anchor.get("request_digest") != payload["request_digest"]
                            or anchor.get("proof") != {"request_sha256": hashlib.sha256(expected_raw).hexdigest()}):
                        raise PermissionError("initial create request differs from pre-publication anchor")
                    if (native_tool == "controlled_create"):
                        anchored_digest = anchor.get("controlled_payload_digest")
                        if anchored_digest is not None and anchored_digest != self._controlled_payload_digest(
                                payload, {"native_tool": effective_native_tool, "request": effective_request}):
                            raise PermissionError("initial create controlled payload differs from pre-publication anchor")
                    started = [row for row in rows if row.get("event") == "run_started"]
                    start = started[0].get("data", {}) if len(started) == 1 else {}
                    owner = anchors[0].get("provenance", {})
                    expected = {"run_id": run_id, "node_id": issue_id, "role": role,
                                "generation": generation, "action_id": action_id,
                                "request_digest": payload["request_digest"],
                                "authorization_digest": effective_request.get("authorization_digest"),
                                "node_contract_digest": start.get("node_contract_digest"),
                                "capability_contract_digest": effective_request.get("capability_contract_digest")}
                    if (any(anchor.get(key) != value for key, value in expected.items())
                            or start.get("run_id") != run_id
                            or start.get("authorization_digest") != expected["authorization_digest"]
                            or start.get("capability_contract_digest") != expected["capability_contract_digest"]
                            or owner.get("role") != "system"
                            or owner.get("authorization_digest") != expected["authorization_digest"]
                            or owner.get("node_contract_digest") != expected["node_contract_digest"]):
                        raise PermissionError("initial create anchor lineage differs")
                    if path.exists() or path.is_symlink():
                        current = self.action(action_id)
                        if current != payload or path.read_bytes() != expected_raw:
                            raise PermissionError("initial create request differs from immutable mailbox")
                        return current
                    if _initial_anchor_sequence is None:
                        raise PermissionError("initial create publication requires its original invocation")
                    if (_initial_anchor_sequence != anchors[0].get("sequence")
                            or rows[-1].get("sequence") != _initial_anchor_sequence):
                        raise PermissionError("initial create request missing after publication window")
                    for folder in ("results", "native-receipts", "observations", "stale"):
                        evidence_dir = self.root / folder
                        if (evidence_dir.is_symlink() or (evidence_dir.exists() and not evidence_dir.is_dir())
                                or (evidence_dir.exists() and any(evidence_dir.glob(action_id + "*")))):
                            raise PermissionError("initial create already has provider evidence")
                    if isinstance(effective_request.get("child_binding"), dict):
                        self._record_dispatch_head(
                            run_id, issue_id, role, generation, effective_request["child_binding"],
                            _requested_project_mount(effective_request, operation),
                        )
                    lock_path = self._directory("locks") / (action_id + ".lock")
                    lock_record = {"kind": "initial_create_publication", "action_id": action_id,
                                   "request_digest": payload["request_digest"],
                                   "anchor_digest": anchors[0]["event_digest"]}
                    with lock_path.open("wb") as stream:
                        stream.write(json.dumps(lock_record, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                        stream.flush()
                        os.fsync(stream.fileno())
                    if _controlled_create:
                        self._initialize_create_state(payload)
                        self._write_controlled_payload(payload, controlled_payload)
                    self._atomic(path, payload)
                    return payload
        if _controlled_create:
            from ..state import interprocess_lock
            with interprocess_lock(self._directory("locks") / (action_id + ".lock")):
                if path.exists() or path.is_symlink():
                    existing = self.action(action_id)
                    if existing != payload or self._read_create_state(existing) is None:
                        raise PermissionError("existing create is not a controlled publication")
                    return existing
                self._initialize_create_state(payload)
                self._write_controlled_payload(payload, controlled_payload)
                self._atomic(path, payload)
                return payload
        existing = self._read(path) if path.exists() else None
        if existing is not None and existing != payload:
            if self._prompt_only_upgrade(existing, payload):
                # The worker already acted on the original prompt; keep it, and
                # do not record a dispatch baseline after the fact.
                return existing
            try:
                superseded = self._supersede_same_generation_create(existing, payload)
            except ValueError:
                superseded = False
                refusal = sys.exc_info()[1]
            else:
                refusal = ValueError("provider action request identity drift")
            if superseded:
                existing = None
            else:
                if isinstance(request, dict) and isinstance(request.get("child_binding"), dict):
                    self._record_dispatch_head(
                        run_id, issue_id, role, generation, request["child_binding"],
                        _requested_project_mount(request, operation),
                    )
                raise refusal
        if isinstance(request, dict) and isinstance(request.get("child_binding"), dict):
            # Before the request is visible to the host: the baseline must
            # predate any work the dispatched worker could do.
            self._record_dispatch_head(
                run_id, issue_id, role, generation, request["child_binding"],
                _requested_project_mount(request, operation),
            )
        if existing is None:
            self._atomic(path, payload)
        probe = isinstance(request, dict) and request.get("purpose") == "binding_probe"
        if operation == "wait" and not probe:
            self._consume_archived_delivery(run_id, issue_id, role, generation)
        return payload

    def _supersede_same_generation_create(
        self, existing: Dict[str, Any], payload: Dict[str, Any]
    ) -> bool:
        """Replace a leftover create of the same node/role/generation (#344 #363).

        A redispatch after a shift or re-authorization rebuilds the same
        action id with a different request, which used to be refused as
        identity drift until a person moved the old file away.  The old
        request is replaced (and an event recorded) only when it can no
        longer matter: its task already has termination evidence in the run's
        ``tasks.json``, or it never produced a task at all (no result, no
        binding).  A live or undecidable old task keeps the refusal, and the
        message names the evidence that is missing.
        """
        from ..contracts import RunEvent
        from ..state import append_event, interprocess_lock
        from ..task_registry import (
            _TERMINAL_STATUSES, _read_registry, _registry_lock, _run_tasks_path,
        )

        if existing.get("operation") != "create" or payload.get("operation") != "create":
            return False
        run_id = existing.get("run_id")
        issue_id = existing.get("issue_id")
        role = existing.get("role")
        generation = existing.get("generation")
        action_id = existing.get("action_id")
        label = "{} {} generation {}".format(issue_id, role, generation)
        try:
            old_result = self.result(action_id)
        except (OSError, TypeError, ValueError):
            old_result = False  # unreadable: cannot tell whether a task exists
        try:
            lock = _registry_lock(self.paths)
            lock.parent.mkdir(parents=True, exist_ok=True)
            with interprocess_lock(lock):
                _revision, rows = _read_registry(
                    _run_tasks_path(self.paths, run_id, create=False), run_id
                )
        except (OSError, TypeError, ValueError):
            rows = None
        if old_result is False or rows is None:
            raise ValueError(
                "provider action request identity drift: the leftover create of {} "
                "cannot be judged (its result or the task registry is unreadable); "
                "needs termination evidence of the old task".format(label)
            )
        bindings = [
            item for item in rows
            if item.issue_id == issue_id and item.role == role and item.generation == generation
        ]
        if bindings:
            live = [item for item in bindings if item.status not in _TERMINAL_STATUSES]
            if live:
                raise ValueError(
                    "provider action request identity drift: the old task of {} is still "
                    "{}; needs termination evidence (status stopped/failed/archived "
                    "in tasks.json) before the create can be replaced".format(
                        label, live[0].status)
                )
            reason = "old_task_terminated"
        elif old_result is None:
            reason = "old_request_never_produced_a_task"
        else:
            raise ValueError(
                "provider action request identity drift: the old create of {} has a "
                "result but no task binding yet, so the old task may be alive; needs "
                "termination evidence of the old task".format(label)
            )
        append_event(self.paths, RunEvent("create_request_superseded", {
            "run_id": run_id,
            "node_id": issue_id,
            "role": role,
            "generation": generation,
            "reason_code": reason,
            "superseded_creates": [action_id],
        }))
        archive = self._directory("superseded")
        with interprocess_lock(self._directory("locks") / (action_id + ".lock")):
            self._atomic(archive / "{}-{}.json".format(action_id, existing["request_digest"][:16]), {
                "schema_version": self.schema_version,
                "reason": reason,
                "request": existing,
                "result": old_result,
            })
            result_path = self._directory("results") / (action_id + ".json")
            if result_path.exists() and not result_path.is_symlink():
                result_path.unlink()
        return True

    def _prompt_only_upgrade(self, existing: Dict[str, Any], payload: Dict[str, Any]) -> bool:
        """A newer vibe rewrote only the prompt template of an answered create.

        Only ``create``: continuation prompts carry contract facts (paths,
        files, delivery) that exist nowhere else.  Every other field must
        match, both prompts must be strings, and the host must already have
        bound a result to the original request.
        """
        if existing.get("operation") != "create" or payload.get("operation") != "create":
            return False
        old_request = existing.get("request")
        new_request = payload.get("request")
        if not isinstance(old_request, dict) or not isinstance(new_request, dict):
            return False
        if not isinstance(old_request.get("prompt"), str) or not isinstance(new_request.get("prompt"), str):
            return False
        strip = lambda value: {k: v for k, v in value.items() if k not in {"request", "request_digest"}}
        if strip(existing) != strip(payload):
            return False
        if {k: v for k, v in old_request.items() if k != "prompt"} != {
            k: v for k, v in new_request.items() if k != "prompt"
        }:
            return False
        try:
            return self.result(existing["action_id"]) is not None
        except (OSError, TypeError, ValueError):
            return False

    def action(self, action_id: str) -> Dict[str, Any]:
        """Read immutable request identity, including after its callback finishes."""
        if not isinstance(action_id, str) or not _SAFE_ID.fullmatch(action_id):
            raise ValueError("provider action id is invalid")
        action = self._read(self._directory("requests") / (action_id + ".json"))
        unsigned = dict(action)
        digest = unsigned.pop("request_digest", None)
        basis = {key: action.get(key) for key in (
            "operation", "provider", "run_id", "issue_id", "role", "generation", "sequence"
        )}
        if (action.get("schema_version") != self.schema_version
                or action.get("action_id") != action_id
                or action_id != "action-" + _canonical_digest(basis)[:32]
                or digest != _canonical_digest(unsigned)
                or not isinstance(action.get("request"), dict)):
            raise ValueError("provider action request identity is invalid")
        return action

    def _create_state_path(self, action_id: str) -> Path:
        if not isinstance(action_id, str) or not _SAFE_ID.fullmatch(action_id):
            raise ValueError("provider action id is invalid")
        return self._directory("create-claims") / (action_id + ".json")

    def _create_payload_path(self, action_id: str) -> Path:
        if not isinstance(action_id, str) or not _SAFE_ID.fullmatch(action_id):
            raise ValueError("provider action id is invalid")
        return self._directory("create-payloads") / (action_id + ".json")

    def _write_controlled_payload(self, public: Dict[str, Any], full: Optional[Dict[str, Any]]) -> None:
        if full is None:
            return
        record = {
            "schema_version": 1,
            "action_id": public["action_id"],
            "public_request_digest": public["request_digest"],
            "native_tool": full["native_tool"],
            "request": full["request"],
        }
        record["payload_digest"] = _canonical_digest({key: value for key, value in record.items() if key != "payload_digest"})
        self._atomic(self._create_payload_path(public["action_id"]), record)

    @staticmethod
    def _controlled_payload_digest(public: Dict[str, Any], full: Dict[str, Any]) -> str:
        record = {
            "schema_version": 1,
            "action_id": public["action_id"],
            "public_request_digest": public["request_digest"],
            "native_tool": full["native_tool"],
            "request": full["request"],
        }
        return _canonical_digest(record)

    def _read_controlled_payload(self, public: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        path = self._create_payload_path(public["action_id"])
        if not path.exists() and not path.is_symlink():
            return None
        record = self._read(path)
        if (record.get("schema_version") != 1
                or record.get("action_id") != public["action_id"]
                or record.get("public_request_digest") != public["request_digest"]
                or not isinstance(record.get("native_tool"), str)
                or not isinstance(record.get("request"), dict)
                or (public.get("native_tool") == "controlled_create" and
                    public.get("request", {}).get("executable_digest") != _canonical_digest({
                        "native_tool": record.get("native_tool"), "request": record.get("request")}))
                or record.get("payload_digest") != _canonical_digest({key: value for key, value in record.items() if key != "payload_digest"})):
            raise ValueError("controlled create payload is invalid")
        return record

    def _effective_create_action(self, public: Dict[str, Any]) -> Dict[str, Any]:
        record = self._read_controlled_payload(public)
        if record is None:
            return public
        action = dict(public)
        action["native_tool"] = record["native_tool"]
        action["request"] = record["request"]
        return action

    def _read_create_state(self, action: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        path = self._create_state_path(action["action_id"])
        if not path.exists() and not path.is_symlink():
            return None
        state = self._read(path)
        if (state.get("schema_version") != 1 or state.get("action_id") != action["action_id"]
                or state.get("request_digest") != action["request_digest"]
                or state.get("status") not in {"pending", "claimed", "stale"}
                or state.get("state_digest") != _canonical_digest(
                    {key: value for key, value in state.items() if key != "state_digest"})):
            raise ValueError("controlled create state is invalid")
        if state.get("status") == "claimed":
            lineage = state.get("lineage")
            if not isinstance(lineage, dict) or lineage != self._claim_lineage(action):
                raise PermissionError("controlled create claim lineage differs")
        return state

    def _write_create_state(self, action: Dict[str, Any], status: str, **extra) -> Dict[str, Any]:
        state = {"schema_version": 1, "action_id": action["action_id"],
                 "request_digest": action["request_digest"], "status": status, **extra}
        state["state_digest"] = _canonical_digest(state)
        self._atomic(self._create_state_path(action["action_id"]), state)
        return state

    def _claim_lineage(self, action: Dict[str, Any], executable: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        request = (executable or self._effective_create_action(action)).get("request", {})
        binding = request.get("child_binding") if isinstance(request, dict) else None
        return {
            "run_id": action.get("run_id"), "node_id": action.get("issue_id"),
            "role": action.get("role"), "generation": action.get("generation"),
            "action_id": action.get("action_id"), "request_digest": action.get("request_digest"),
            "authorization_digest": request.get("authorization_digest"),
            "node_contract_digest": request.get("node_contract_digest"),
            "capability_contract_digest": request.get("capability_contract_digest"),
            "plan_id": request.get("plan_id"), "plan_version": request.get("plan_version"),
            "binding_digest": _canonical_digest(binding) if isinstance(binding, dict) else None,
        }

    def _initialize_create_state(self, action: Dict[str, Any]) -> None:
        path = self._create_state_path(action["action_id"])
        if path.exists() or path.is_symlink():
            raise PermissionError("controlled create state already exists")
        self._write_create_state(action, "pending")

    def stage_create(self, action_id: str, exposure_id: str, request_digest: str) -> Dict[str, Any]:
        """Claim once before exposing a controlled create to a consumer."""
        if not isinstance(action_id, str) or not _SAFE_ID.fullmatch(action_id):
            raise ValueError("provider action id is invalid")
        if not isinstance(exposure_id, str) or not exposure_id or len(exposure_id) > 200:
            raise ValueError("invalid create exposure id")
        lock = self._directory("locks") / (action_id + ".lock")
        with recovery_action_lock(lock):
            action = self.action(action_id)
            if action.get("operation") != "create" or action["request_digest"] != request_digest:
                raise ValueError("create claim request identity differs")
            executable = self._effective_create_action(action)
            state = self._read_create_state(action)
            if state is None:
                return {"status": "unknown", "action_id": action_id}
            if executable["request"].get("origin") == "worker_dispatch" and not executable["request"].get("successor") and not executable["request"].get("binding_probe"):
                from ..state import load_events
                anchors = [row for row in load_events(self.paths, action["run_id"])
                           if row.get("event") == "initial_create_anchored"
                           and row.get("data", {}).get("action_id") == action_id]
                anchor_data = anchors[0]["data"] if len(anchors) == 1 else {}
                payload_digest_matches = (
                    "controlled_payload_digest" not in anchor_data
                    or anchor_data.get("controlled_payload_digest") ==
                    self._controlled_payload_digest(action, executable)
                )
                if (len(anchors) != 1 or anchor_data.get("request_digest") != request_digest
                        or anchor_data.get("proof", {}).get("request_sha256") != hashlib.sha256(
                            (self._directory("requests") / (action_id + ".json")).read_bytes()).hexdigest()
                        or not payload_digest_matches):
                    raise PermissionError("controlled create anchor differs")
            plan_id = executable["request"].get("plan_id")
            if plan_id:
                fence = self.paths.resolve_vibe_path(Path("plans") / str(plan_id) / ".initial-recovery-pending.json")
                if fence.exists() or fence.is_symlink():
                    return {"status": "unknown", "action_id": action_id}
            if state["status"] == "stale":
                return {"status": "stale", "action_id": action_id}
            if state["status"] == "claimed":
                return {"status": "already_claimed", "action_id": action_id,
                        "request_digest": request_digest,
                        "exposure_id": state.get("exposure_id")}
            else:
                self._write_create_state(action, "claimed", exposure_id=exposure_id,
                                         claimed_at=time.time(), lineage=self._claim_lineage(action, executable))
            return {"status": "claimed", "action_id": action_id,
                    "request_digest": request_digest, "request": executable["request"],
                    "native_tool": executable["native_tool"]}

    def revoke_create(self, action_id: str, request_digest: str) -> Dict[str, Any]:
        """Make an unexposed controlled create stale under its action lock."""
        if not isinstance(action_id, str) or not _SAFE_ID.fullmatch(action_id):
            raise ValueError("provider action id is invalid")
        action = self.action(action_id)
        if action.get("operation") != "create" or action["request_digest"] != request_digest:
            raise ValueError("create revoke request identity differs")
        # An old publication has no claim protocol and may not have a lock.
        # Never create one after the fact as proof of non-exposure.
        if self._read_create_state(action) is None:
            return {"status": "unknown", "action_id": action_id}
        lock = self._directory("locks") / (action_id + ".lock")
        with recovery_action_lock(lock):
            action = self.action(action_id)
            if action.get("operation") != "create" or action["request_digest"] != request_digest:
                raise ValueError("create revoke request identity differs")
            state = self._read_create_state(action)
            if state is None:
                return {"status": "unknown", "action_id": action_id}
            if state["status"] == "claimed":
                return {"status": "already_claimed", "action_id": action_id}
            if state["status"] == "pending":
                for name in ("results", "native-receipts", "observations"):
                    folder = self._directory(name)
                    if any(folder.glob(action_id + "*")):
                        return {"status": "unknown", "action_id": action_id}
                self._write_create_state(action, "stale")
            return {"status": "stale", "action_id": action_id}

    def related_action(self, action: Dict[str, Any], operation: str, *, required=True) -> Optional[Dict[str, Any]]:
        basis = {key: action[key] for key in (
            "provider", "run_id", "issue_id", "role", "generation", "sequence"
        )}
        basis["operation"] = operation
        action_id = "action-" + _canonical_digest(basis)[:32]
        path = self._directory("requests") / (action_id + ".json")
        if not required and not path.exists() and not path.is_symlink():
            return None
        return self.action(action_id)

    def result(self, action_id: str) -> Optional[Dict[str, Any]]:
        from ..state import interprocess_lock
        if not isinstance(action_id, str) or not _SAFE_ID.fullmatch(action_id):
            raise ValueError("provider action id is invalid")
        # A read of an unanswered request must not create a completion lock:
        # claim_evidence treats that durable lock as an in-flight side effect.
        result_path = self._directory("results") / (action_id + ".json")
        # Legacy unanswered requests need not carry the newer canonical
        # identity. Only authenticate a request when consuming evidence;
        # absence is not evidence and must not create a completion lock.
        raw_dir = self._directory("native-receipts")
        has_raw = any(raw_dir.glob(action_id + "-*.json"))
        if not result_path.exists() and not result_path.is_symlink() and not has_raw:
            return None
        with interprocess_lock(self._directory("locks") / (action_id + ".lock")):
            return self._result_locked(action_id)

    def _native_receipt_path(self, action_id: str) -> Path:
        request = self.action(action_id)
        if request.get("operation") == "create" and self._read_create_state(request) is not None:
            request = self._effective_create_action(request)
        return self._directory("native-receipts") / (
            action_id + "-" + request["request_digest"] + ".json")

    def _result_locked(self, action_id: str) -> Optional[Dict[str, Any]]:
        path = self._directory("results") / (action_id + ".json")
        raw_path = self._native_receipt_path(action_id)
        if raw_path.exists():
            raw = self._read(raw_path)
            body = {key: value for key, value in raw.items() if key != "receipt_digest"}
            request = self.action(action_id)
            if (set(raw) not in ({"schema_version", "action_id", "request_digest", "payload", "receipt_digest"},
                                 {"schema_version", "action_id", "request_digest", "payload", "receipt_digest", "native_response"})
                    or raw.get("receipt_digest") != _canonical_digest(body)
                    or raw.get("action_id") != action_id
                    or raw.get("schema_version") != self.schema_version
                    or raw.get("request_digest") != request["request_digest"]
                    or not isinstance(raw.get("payload"), dict)):
                raise ValueError("native receipt integrity mismatch")
            body.pop("native_response", None)
            if path.exists():
                if self._read(path) != body:
                    raise ValueError("projection differs from native receipt")
            else:
                self._atomic(path, body)
        elif not path.exists():
            return None
        result = self._read(path)
        request = self.action(action_id)
        if (
            set(result)
            != {"schema_version", "action_id", "request_digest", "payload"}
            or result["schema_version"] != self.schema_version
            or result["action_id"] != action_id
            or result["request_digest"] != request["request_digest"]
            or not isinstance(result["payload"], dict)
        ):
            raise ValueError("provider action result is not bound to its request")
        return result["payload"]

    def complete(self, action_id: str, payload: Dict[str, Any], *,
                 request_digest: Optional[str] = None, native: bool = True,
                 native_response: Optional[Dict[str, Any]] = None) -> bool:
        """Keep the first bound result; retain conflicting callbacks as evidence.

        A new observation belongs to a new action sequence, never a rewrite
        of an action already consumed by a monitor or an earlier shift.
        """
        from ..state import interprocess_lock

        if not isinstance(action_id, str) or not _SAFE_ID.fullmatch(action_id):
            raise ValueError("provider action id is invalid")
        request = self.action(action_id)
        if not isinstance(payload, dict):
            raise ValueError("provider action result payload must be an object")
        if native_response is not None and (not native or not isinstance(native_response, dict)):
            raise ValueError("native response requires an object and native provenance")
        if request_digest is not None and request_digest != request["request_digest"]:
            raise ValueError("provider action request digest mismatch")
        record = {
            "schema_version": self.schema_version,
            "action_id": action_id,
            "request_digest": request["request_digest"],
            "payload": payload,
        }
        effective_request = self._effective_create_action(request) if request.get("operation") == "create" else request
        controlled = self._read_create_state(request) if request.get("operation") == "create" else None
        controlled_payload = (self._read_controlled_payload(request)
                              if request.get("operation") == "create" else None)
        if request.get("operation") == "create" and controlled is None and controlled_payload is not None:
            raise PermissionError("controlled create payload exists without claim state")
        if controlled is not None and request_digest is None:
            # The immutable public action already carries the receipt binding.
            # Callers using the public mailbox API may omit the optional
            # argument; an explicitly supplied digest is still checked above.
            request_digest = request["request_digest"]
        lock_path = self._directory("locks") / (action_id + ".lock")
        lock_context = recovery_action_lock(lock_path) if controlled is not None else interprocess_lock(lock_path)
        with lock_context:
            if controlled is not None:
                current = self._read_create_state(request)
                if current is None or current["status"] == "stale":
                    observation = dict(record, kind="unclaimed_create_callback")
                    if native_response is not None:
                        observation["native_response"] = native_response
                    self._atomic(self._directory("observations") /
                                 (action_id + "-" + _canonical_digest(observation) + ".json"), observation)
                    return False
                if current["status"] == "pending":
                    self._write_create_state(
                        request, "claimed",
                        exposure_id="receipt:" + _canonical_digest(record),
                        claimed_at=time.time(),
                        lineage=self._claim_lineage(request, effective_request),
                    )
            # Every callback observation shares the retirement boundary.
            # ``native=False`` is a worker report, not a native reply.
            called_tool = _unresolved_tool_name(payload) if native else None
            if called_tool is not None:
                self._record_unresolved_tool(request, called_tool, payload)
            if native and effective_request.get("native_tool") == "codex_app__create_thread":
                thread_id = _result_thread_id(payload)
                forked_from = _codex_forked_from(thread_id) if thread_id else None
                if forked_from:
                    self._atomic(
                        self._directory("observations") / "{}-forked-{}.json".format(
                            action_id, _canonical_digest(payload)[:16]),
                        {"schema_version": self.schema_version, "kind": "forked_provider_session",
                         "action_id": action_id, "request_digest": request["request_digest"],
                         "thread_id": thread_id, "forked_from": forked_from,
                         "observed_at": time.time(), "payload": payload})
                    raise ForkedProviderSession(action_id, thread_id, forked_from)
            existing = self._result_locked(action_id)
            if existing is not None:
                prior_raw = self._read(self._native_receipt_path(action_id)) if self._native_receipt_path(action_id).exists() else {}
                if existing == payload and (native_response is None or prior_raw.get("native_response") == native_response):
                    return True
                observation = dict(record)
                if native_response is not None:
                    observation["native_response"] = native_response
                self._atomic(self._directory("observations") /
                             (action_id + "-" + _canonical_digest(observation) + ".json"), observation)
                return False
            if native:
                raw = dict(record)
                if native_response is not None:
                    raw["native_response"] = native_response
                raw["receipt_digest"] = _canonical_digest(raw)
                self._atomic(self._native_receipt_path(action_id), raw)
            self._atomic(self._directory("results") / (action_id + ".json"), record)
            return True

    def _record_unresolved_tool(self, request: Dict[str, Any], called_tool: str,
                                payload: Dict[str, Any]) -> None:
        """Keep the host reply as evidence and raise; never store it as a result."""
        action_id = request["action_id"]
        directory = self._directory("observations")
        prefix = action_id + "-unresolved-tool-"
        record = {
            "schema_version": self.schema_version,
            "kind": "unresolved_native_tool",
            "action_id": action_id,
            "request_digest": request["request_digest"],
            "expected_tool": request.get("native_tool"),
            "called_tool": called_tool,
            "observed_at": time.time(),
            "payload": payload,
        }
        self._atomic(directory / (prefix + _canonical_digest(record)[:16] + ".json"), record)
        attempts = len(list(directory.glob(prefix + "*.json")))
        raise UnresolvedNativeTool(action_id, called_tool, str(request.get("native_tool")), attempts)

    def pending(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        request_dir = self._directory("requests")
        result_dir = self._directory("results")
        stale_dir = self.root / "stale"
        if stale_dir.is_symlink() or (stale_dir.exists() and not stale_dir.is_dir()):
            raise ValueError("provider stale directory may not be a symlink or non-directory")
        result = []
        for path in sorted(request_dir.glob("action-*.json")):
            if (result_dir / path.name).exists() or self.result(path.stem) is not None:
                continue
            action = self._read(path)
            if action.get("operation") == "create":
                state = self._read_create_state(action)
                if state is not None and state["status"] == "stale":
                    continue
            stale_path = stale_dir / path.name
            if stale_path.is_symlink():
                # A symlink marker cannot hide an immutable request.
                result.append(action)
                continue
            if stale_path.exists():
                try:
                    stale = self._read(stale_path)
                except (OSError, ValueError):
                    stale = None
                if (
                    isinstance(stale, dict)
                    and stale.get("status") == "stale"
                    and stale.get("action_id") == action.get("action_id")
                    and stale.get("request_digest") == action.get("request_digest")
                ):
                    continue
            if run_id is None or action.get("run_id") == run_id:
                result.append(action)
        return result

    def reconcile_stale(self, run_id: str, evidence: Mapping[str, Any]) -> List[str]:
        """Audit and hide superseded requests only with complete run evidence.

        A stale marker is an audit record beside the immutable request.  It is
        never a provider result and never invokes the native tool.  Missing or
        mismatched run, authorization, node, role, generation, or accepted
        evidence leaves the request pending for human/runtime reconciliation.
        """
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run id is required")
        if not isinstance(evidence, Mapping) or evidence.get("run_id") != run_id:
            return []
        authorization = evidence.get("authorization_digest")
        if not isinstance(authorization, str) or len(authorization) != 64:
            return []
        nodes = evidence.get("nodes")
        if not isinstance(nodes, Mapping):
            return []
        changed = []
        request_dir = self._directory("requests")
        stale_dir = self._directory("stale")
        for path in sorted(request_dir.glob("action-*.json")):
            if (self.root / "results" / path.name).exists() or (stale_dir / path.name).exists():
                continue
            action = self._read(path)
            # Converge superseded actions for every mailbox operation. Older
            # heartbeats could leave resume/wait requests after acceptance;
            # keeping them pending causes a zombie native retry loop.
            if action.get("run_id") != run_id:
                continue
            request_envelope = action.get("request")
            if isinstance(request_envelope, Mapping):
                # Real ProviderActionStore.request places the authorization
                # proof in the request envelope. If that envelope is present,
                # a top-level copy cannot substitute for a missing value.
                request_authorization = request_envelope.get("authorization_digest")
            else:
                # Legacy records without an envelope may use the top-level
                # field, but this path is never combined with a partial
                # envelope.
                request_authorization = action.get("authorization_digest")
            if request_authorization != authorization:
                continue
            issue_id = action.get("issue_id")
            role = action.get("role")
            generation = action.get("generation")
            node = nodes.get(issue_id)
            if not isinstance(node, Mapping) or node.get("status") != "accepted":
                continue
            accepted_generation = node.get("accepted_generation")
            roles = node.get("roles")
            role_evidence = roles.get(role) if isinstance(roles, Mapping) else None
            role_accepted_generation = (
                role_evidence.get("accepted_generation")
                if isinstance(role_evidence, Mapping)
                else None
            )
            if role_accepted_generation is None and isinstance(role_evidence, Mapping):
                role_accepted_generation = role_evidence.get("generation")
            if (
                isinstance(generation, bool) or not isinstance(generation, int)
                or not isinstance(accepted_generation, int)
                or not isinstance(role_accepted_generation, int)
                or role_accepted_generation <= generation
                or not isinstance(role_evidence, Mapping)
                or role_evidence.get("generation") != role_accepted_generation
            ):
                continue
            audit = {
                "schema_version": self.schema_version,
                "status": "stale",
                "reason": "accepted_generation_superseded",
                "action_id": action.get("action_id"),
                "request_digest": action.get("request_digest"),
                "run_id": run_id,
                "issue_id": issue_id,
                "role": role,
                "generation": generation,
                "accepted_generation": accepted_generation,
                "authorization_digest": authorization,
            }
            self._atomic(stale_dir / path.name, audit)
            changed.append(str(action.get("action_id")))
        return changed

    def _require_recovery_binding(self, snapshot, binding, *, retiring=False):
        """Registry identity alone cannot replace the authorized current writer."""
        node = snapshot.nodes.get(binding.issue_id) or {}
        generation = "review_generation" if binding.role == "reviewer" else "developer_generation"
        active = node.get("active_task") or {}
        root = Path(binding.worktree)
        expected = Path(node.get("worktree") or "")
        root = root if root.is_absolute() else self.paths.root / root
        expected = expected if expected.is_absolute() else self.paths.root / expected
        if (binding.run_id != snapshot.run_id or not binding.task_id
                or binding.task_id != node.get(binding.role + "_identity")
                or binding.generation != node.get(generation)
                or binding.branch != node.get("branch") or root.resolve() != expected.resolve()
                or binding.status not in ({"running", "blocked_unknown", "rework", "review", "stopped"}
                                          if retiring else {"running", "blocked_unknown", "rework", "review"})
                or (binding.role == "reviewer" and node.get("reviewer_replacement_required") is True)
                or node.get("status") in {"accepted", "archived"}
                or (active and (active.get("role") != binding.role
                    or active.get("task_id") != binding.task_id
                    or active.get("generation") != binding.generation))):
            raise PermissionError("original recovery task or writer is not current")

    def prepare_recovery(self, run_id, issue_id, role, recovery_input, *, owner_epoch, owner_session_id):
        """Current-owner public preparation, preserving the original task and scope.

        Returns an exact native invocation only after durable claim. Repeating
        an unresolved preparation reconciles the same action, never re-sends.
        """
        from ..state import interprocess_lock, run_dir, verify_run_evidence
        from ..supervisor import SupervisorLease, _read_lease_record
        from ..task_registry import load_task_binding
        from ..protocol_adoption import effective_responsibility
        required = {"prompt", "original_step", "evidence_ref", "evidence_sha256"}
        if not isinstance(recovery_input, dict) or set(recovery_input) not in (required, required | {"help_digest"}):
            raise ValueError("recovery input requires prompt, original_step and diagnostic evidence")
        lease = SupervisorLease(self.paths)
        with interprocess_lock(run_dir(self.paths, run_id) / ".monitor.lock"), lease.lock(run_id):
            snapshot = verify_run_evidence(self.paths, run_id)
            owner = _read_lease_record(lease.path(run_id))
            if (not isinstance(owner, dict) or owner.get("released") is True
                    or type(owner_epoch) is not int or owner.get("lease_epoch") != owner_epoch
                    or (owner.get("owner") or {}).get("session_id") != owner_session_id
                    or effective_responsibility(self.paths, snapshot).get("status") != "effective"):
                raise PermissionError("recovery owner is not current")
            binding = load_task_binding(self.paths, issue_id, role, run_id=run_id)
            self._require_recovery_binding(snapshot, binding)
            if binding.provider != CODEX_PROVIDER or binding.mode != "visible" or binding.status in {"archived", "accepted"}:
                raise PermissionError("original visible recovery provider or eligibility is unknown")
            native = {"threadId": binding.task_id, "hostId": binding.host, "prompt": recovery_input["prompt"]}
            request = {**native, "authorization_digest": snapshot.authorization_digest,
                "node_contract_digest": snapshot.node_contract_digest,
                "recovery": {**recovery_input, "task_id": binding.task_id, "owner_epoch": owner_epoch,
                    "action": "resume_original", "native_request": native}}
            if snapshot.execution_engine or snapshot.engine_mode or snapshot.engine_evidence_ref:
                from ..runners.provider_action import ProviderActionRunner
                request = ProviderActionRunner._request_envelope({
                    "dispatcher": "monitor", "complexity_band": "complex",
                    "execution_engine": snapshot.execution_engine, "engine_mode": snapshot.engine_mode,
                    "engine_evidence_ref": snapshot.engine_evidence_ref,
                    "authorization_digest": snapshot.authorization_digest,
                    "plan_id": snapshot.plan_id, "plan_version": snapshot.plan_version}, request)
                require_complex_monitor_dispatch(request, self.paths)
            sequence = 0
            existing = None
            for path in self._directory("requests").glob("*.json"):
                item = self.action(path.stem)
                if (item["run_id"] == run_id and item["issue_id"] == issue_id
                        and item["role"] == role and item["generation"] == binding.generation
                        and item["operation"] == "resume"):
                    sequence = max(sequence, item["sequence"] + 1)
                    previous = item["request"].get("recovery") or {}
                    if all(previous.get(key) == recovery_input.get(key) for key in required | {"help_digest"}):
                        if existing is None or existing["sequence"] < item["sequence"]:
                            existing = item
            if existing is None:
                probe = {"provider": binding.provider, "run_id": run_id, "issue_id": issue_id,
                    "role": role, "request": request}
                if self._recovery_baseline(probe, owner_session_id) is None:
                    raise PermissionError("failed original step or native baseline is unknown")
                evidence = self.paths.resolve_relative(recovery_input["evidence_ref"])
                if hashlib.sha256(evidence.read_bytes()).hexdigest() != recovery_input["evidence_sha256"]:
                    raise PermissionError("recovery evidence is unverifiable")
                existing = self.request(operation="resume", provider=binding.provider, run_id=run_id,
                    issue_id=issue_id, role=role, generation=binding.generation,
                    native_tool="codex_app__send_message_to_thread", request=request, sequence=sequence)
        result = self.claim_recovery(existing["action_id"], owner_epoch=owner_epoch, owner_session_id=owner_session_id)
        return {**result, "action_id": existing["action_id"], "request_digest": existing["request_digest"],
                "native_tool": existing["native_tool"], "native_args": existing["request"]["recovery"]["native_request"]}

    def claim_recovery(self, action_id: str, *, owner_epoch: int,
                       owner_session_id: str, _existing_only=False) -> Dict[str, Any]:
        """Claim one explicit recovery, preserving unresolved work across shifts.

        A claim is permission to invoke the existing native action once. An
        already-claimed action is reconciliation only. Counts derive from
        immutable native receipts, never from polls, claims or worker reports.
        """
        from ..state import interprocess_lock, verify_run_evidence, run_dir
        from ..supervisor import SupervisorLease, _read_lease_record
        from ..task_registry import load_task_binding
        from ..protocol_adoption import effective_responsibility

        action = self.action(action_id)
        run_id = action["run_id"]
        request = action["request"]
        recovery = request.get("recovery")
        if (action["operation"] != "resume" or not isinstance(recovery, dict)
                or recovery.get("action") != "resume_original"
                or not isinstance(recovery.get("evidence_ref"), str)
                or not recovery["evidence_ref"].strip()):
            raise ValueError("explicit recovery action and evidence are required")
        try:
            evidence = self.paths.resolve_relative(recovery["evidence_ref"])
            if (not evidence.is_file() or hashlib.sha256(evidence.read_bytes()).hexdigest()
                    != recovery.get("evidence_sha256")):
                raise ValueError("diagnostic digest mismatch")
        except (OSError, ValueError) as error:
            raise PermissionError("recovery diagnostic evidence is unverifiable") from error
        lease = SupervisorLease(self.paths)
        with interprocess_lock(run_dir(self.paths, run_id) / ".monitor.lock"), lease.lock(run_id):
            current = _read_lease_record(lease.path(run_id))
            if (type(owner_epoch) is not int or not isinstance(current, dict)
                    or current.get("released") is True
                    or current.get("lease_epoch") != owner_epoch
                    or not isinstance(current.get("owner"), dict)
                    or current["owner"].get("session_id") != owner_session_id):
                raise PermissionError("recovery owner is not current")
            snapshot = verify_run_evidence(self.paths, run_id)
            if effective_responsibility(self.paths, snapshot).get("status") != "effective":
                raise PermissionError("recovery requires effective responsibility rules")
            binding = load_task_binding(self.paths, action["issue_id"], action["role"], run_id=run_id)
            self._require_recovery_binding(snapshot, binding)
            if (binding.task_id != recovery.get("task_id")
                    or binding.task_id != request.get("threadId")
                    or binding.provider != action["provider"]
                    or binding.host != request.get("hostId")
                    or binding.generation != action["generation"]
                    or request.get("authorization_digest") != snapshot.authorization_digest
                    or request.get("node_contract_digest") != snapshot.node_contract_digest):
                raise PermissionError("recovery task or authorization lineage mismatch")
            basis = {"run_id": run_id, "node_id": action["issue_id"], "role": action["role"],
                     "task_id": binding.task_id, "generation": binding.generation,
                     "authorization_digest": snapshot.authorization_digest,
                     "node_contract_digest": snapshot.node_contract_digest}
            path = self._directory("recovery-claims") / (_canonical_digest(basis) + ".json")
            state = self._read(path) if path.exists() else {"basis": basis, "attempts": []}
            if state.get("basis") != basis or not isinstance(state.get("attempts"), list):
                raise ValueError("recovery claim integrity mismatch")
            failures = 0
            active = None
            same = None
            for attempt in state["attempts"]:
                previous = self.action(attempt["action_id"])
                if (attempt.get("request_digest") != previous["request_digest"]
                        or any(previous.get(key) != action.get(key) for key in
                               ("run_id", "issue_id", "role", "generation", "provider"))):
                    raise ValueError("recovery claim request mismatch")
                outcome = self._verified_recovery_terminal(previous, attempt)
                # A send receipt proves neither execution nor an original-step
                # result. Capacity/transport errors likewise do not establish
                # a completed recovery attempt. Keep the claim in flight until
                # a correlated terminal observation is independently verified.
                if outcome == "success":
                    failures = 0
                elif outcome == "failed":
                    failures += 1
                else:
                    active = attempt["action_id"]
                if attempt["action_id"] == action_id:
                    same = attempt
            common = {"consecutive_failures": failures, "active_action_id": active}
            if same:
                return dict(common, status="already_claimed",
                            claimed_owner_epoch=same["owner_epoch"],
                            reconcile_only=same["owner_epoch"] != owner_epoch)
            if active:
                return dict(common, status="unknown", reason="reconcile the in-flight recovery before another action")
            if _existing_only:
                return dict(common, status="unknown", reason="recovery action has not been claimed")
            if recovery.get("owner_epoch") != owner_epoch:
                raise PermissionError("new recovery action owner is stale")
            help_digest = None
            native_baseline = self._recovery_baseline(action, owner_session_id)
            help_path = run_dir(self.paths, run_id) / "heal-state.json"
            if help_path.exists():
                responsibility = (self._read(help_path).get("nodes", {}).get(action["issue_id"], {})
                                  .get("supervisor_responsibility") or {})
                helps = responsibility.get("attempted_actions") or []
                matching_helps = [helped for helped in helps
                    if helped.get("help_digest") == recovery.get("help_digest")
                    and helped.get("causal_binding") is not None]
                if (responsibility.get("status") == "pending" and len(matching_helps) == 1
                        and responsibility.get("authorization_digest") == snapshot.authorization_digest
                        and responsibility.get("node_contract_digest") == snapshot.node_contract_digest
                        and responsibility.get("role") == action["role"]
                        and matching_helps[0].get("task_id") == binding.task_id
                        and matching_helps[0].get("generation") == binding.generation
                        and isinstance(matching_helps[0].get("causal_binding"), dict)
                        and native_baseline is not None
                        and matching_helps[0]["causal_binding"] == {"original_step": recovery.get("original_step"),
                            "failure": native_baseline["worker"].get("failed_step"),
                            "task_id": binding.task_id}):
                    help_digest = matching_helps[0]["help_digest"]
            state["attempts"].append({"action_id": action_id,
                "request_digest": action["request_digest"], "owner_epoch": owner_epoch,
                "owner_session_id": owner_session_id,
                "help_digest": help_digest,
                "native_baseline": native_baseline})
            self._atomic(path, state)
            return dict(common, status="claimed", claimed_owner_epoch=owner_epoch, reconcile_only=False)

    def _recovery_baseline(self, action, observer):
        """Freeze provider-owned log prefixes before permitting a native send.

        Missing logs preserve legacy claim/reconciliation but cannot establish
        a completed attempt later. These hashes check continuity, not authorship.
        """
        if action.get("provider") != CODEX_PROVIDER:
            return None
        from ..plan_amendment import _native_session_record
        recovery = action["request"].get("recovery") or {}
        step = recovery.get("original_step")
        native = recovery.get("native_request")
        if (not isinstance(step, dict) or set(step) != {"command", "cwd"}
                or any(not isinstance(value, str) or not value.strip() for value in step.values())
                or not isinstance(native, dict) or set(native) != {"threadId", "hostId", "prompt"}
                or any(native.get(key) != action["request"].get(key) for key in native)
                or not isinstance(native.get("prompt"), str) or not native["prompt"].strip()):
            return None
        try:
            from ..task_registry import load_task_binding
            binding = load_task_binding(self.paths, action["issue_id"], action["role"], run_id=action["run_id"])
            writer = Path(binding.worktree)
            if not writer.is_absolute():
                writer = self.paths.root / writer
            cwd = Path(step["cwd"])
            if not cwd.is_absolute():
                return None
            cwd.resolve().relative_to(writer.resolve())
            result = {}
            for role, session in (("observer", observer), ("worker", recovery["task_id"])):
                path, rows = _native_session_record(session, False)
                data = path.read_bytes()
                if (not data.endswith(b"\n") or
                        [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()] != rows):
                    return None
                if role == "worker":
                    if (any(type(row.get("ordinal")) is not int for row in rows)
                            or [row["ordinal"] for row in rows] != list(range(len(rows)))):
                        return None
                    matching = []
                    for row_index, row in enumerate(rows):
                        payload = row.get("payload") or {}
                        if row.get("type") != "event_msg":
                            continue
                        item = payload.get("item") or {}
                        if (payload.get("type") == "item_completed"
                                and item.get("type") == "CommandExecution"
                                and _native_command_identity(item) == step):
                            matching.append((payload.get("turn_id"), _native_command_exit(item), row_index))
                    if (not matching or matching[-1][1] in (None, 0)
                            or not isinstance(matching[-1][0], str) or not matching[-1][0]
                            or sum(row[0] == matching[-1][0] for row in matching) != 1):
                        return None
                    turn, _, command_index = matching[-1]
                    starts, terminals = [], []
                    for index, row in enumerate(rows):
                        payload = row.get("payload") or {}
                        if row.get("type") != "event_msg" or payload.get("turn_id") != turn:
                            continue
                        if payload.get("type") == "task_started":
                            starts.append(index)
                        elif payload.get("type") in {"task_complete", "task_completed", "turn_aborted"}:
                            terminals.append(index)
                    if len(starts) != 1 or len(terminals) != 1:
                        return None
                    start, end = starts[0], terminals[0]
                    if (not start < command_index < end
                            or rows[end]["payload"]["type"] == "turn_aborted"
                            or any(row.get("type") == "event_msg"
                                and (row.get("payload") or {}).get("type") == "task_started"
                                for row in rows[start + 1:end])
                            or rows[command_index]["payload"].get("thread_id") != recovery["task_id"]
                            or any("thread_id" in rows[i]["payload"]
                                and rows[i]["payload"]["thread_id"] != recovery["task_id"] for i in (start, end))):
                        return None
                    command = rows[command_index]["payload"]
                    if (type(command.get("started_at_ms")) is not int
                            or type(command.get("completed_at_ms")) is not int
                            or command["completed_at_ms"] < command["started_at_ms"]):
                        return None
                    # Native turn clocks are seconds, command clocks milliseconds.
                    # Legacy turns omit these clocks; verified ordinal boundaries
                    # still establish their order. Present clocks must agree.
                    for index, field, bound in ((start, "started_at", command["started_at_ms"]),
                                                (end, "completed_at", command["completed_at_ms"])):
                        payload = rows[index]["payload"]
                        if field in payload:
                            clock = payload[field]
                            if (type(clock) not in (int, float)
                                    or not (clock * 1000 <= bound if index == start else clock * 1000 >= bound)):
                                return None
                result[role] = {"session_id": session, "bytes": len(data),
                                "sha256": hashlib.sha256(data).hexdigest(), "rows": len(rows)}
                if role == "worker":
                    result[role]["failed_step"] = {"turn_id": matching[-1][0],
                        "exit_code": matching[-1][1], "row_index": matching[-1][2],
                        "original_step": step}
            return result
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _observe_recovery_terminal(self, action, attempt):
        """Correlate one actual native send to one new original-step execution.

        Only the existing provider-owned host journal is read. Sender receipt,
        worker prose, turn completion alone, or a different command are unknown.
        Unrecognized providers/schemas intentionally stay unknown.
        """
        from ..plan_amendment import _native_session_record, _raw_host_ledger
        baseline = attempt.get("native_baseline")
        if not isinstance(baseline, dict) or action.get("provider") != CODEX_PROVIDER:
            return None
        recovery = action["request"].get("recovery") or {}
        native, step = recovery.get("native_request"), recovery.get("original_step")
        try:
            journals = {}
            prefixes = {}
            for role in ("observer", "worker"):
                frozen = baseline[role]
                path, rows = _native_session_record(frozen["session_id"], False)
                data = path.read_bytes()
                if (not data.endswith(b"\n") or
                        [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()] != rows):
                    return None
                size = frozen["bytes"]
                if (type(size) is not int or size <= 0 or len(data) < size
                        or hashlib.sha256(data[:size]).hexdigest() != frozen["sha256"]):
                    return None
                journals[role] = rows
                prefixes[role] = {"session_id": frozen["session_id"], "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}
            ledger = _raw_host_ledger(journals["observer"], baseline["observer"]["session_id"])
            if ledger["issues"]:
                return None
            calls = [item for item in ledger["items"]
                if item["line"] > baseline["observer"]["rows"]
                and item["invocation_verified"] and item["outcome_class"] == "completed"
                and item["item"].get("server") == "codex_app"
                and item["item"].get("tool") == "send_message_to_thread"
                and item["item"].get("arguments") == native]
            if len(calls) != 1:
                return None
            fresh = journals["worker"][baseline["worker"]["rows"]:]
            turns = []
            for index, row in enumerate(fresh):
                payload = row.get("payload") or {}
                if row.get("type") == "event_msg" and payload.get("type") == "task_started":
                    turns.append((index, payload.get("turn_id")))
            matched = []
            for offset, (start, turn_id) in enumerate(turns):
                end = turns[offset + 1][0] if offset + 1 < len(turns) else len(fresh)
                rows = fresh[start:end]
                messages = [row.get("payload") for row in rows if row.get("type") == "response_item"
                    and (row.get("payload") or {}).get("type") == "message"
                    and (row.get("payload") or {}).get("role") == "user"]
                expected = [{"type": "input_text", "text": native["prompt"]}]
                if len(messages) != 1 or messages[0].get("content") != expected:
                    continue
                terminals = [i for i, row in enumerate(rows) if row.get("type") == "event_msg"
                    and (row.get("payload") or {}).get("type") in {"task_complete", "task_completed", "turn_aborted"}
                    and row["payload"].get("turn_id") == turn_id]
                if (not isinstance(turn_id, str) or not turn_id or len(terminals) != 1
                        or rows[terminals[0]]["payload"]["type"] == "turn_aborted"):
                    continue
                commands = []
                for i, row in enumerate(rows):
                    payload = row.get("payload") or {}
                    item = payload.get("item") or {}
                    if (row.get("type") == "event_msg" and payload.get("type") == "item_completed"
                            and payload.get("thread_id") == recovery["task_id"]
                            and payload.get("turn_id") == turn_id and item.get("type") == "CommandExecution"
                            and _native_command_identity(item) == step
                            and type(payload.get("started_at_ms")) is int
                            and type(payload.get("completed_at_ms")) is int
                            and payload["completed_at_ms"] >= payload["started_at_ms"]
                            and payload["started_at_ms"] >= journals["observer"][calls[0]["line"] - 1]["payload"]["started_at_ms"]
                            and i < terminals[0]):
                        commands.append(item)
                if len(commands) != 1 or _native_command_exit(commands[0]) is None:
                    return None
                matched.append((turn_id, commands[0]))
            if len(matched) != 1:
                return None
            turn_id, command = matched[0]
            return {"outcome": "success" if _native_command_exit(command) == 0 else "failed",
                "turn_id": turn_id, "host_item_id": command.get("id"),
                "native_call_id": calls[0]["host_id"], "sources": prefixes}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    def _verified_recovery_terminal(self, action, attempt):
        path = self._directory("recovery-terminals") / (action["action_id"] + ".json")
        if not path.exists():
            return None
        terminal = self._read(path)
        body = {key: value for key, value in terminal.items() if key != "digest"}
        if (terminal.get("digest") != _canonical_digest(body)
                or terminal.get("request_digest") != action["request_digest"]
                or terminal.get("action_id") != action["action_id"]):
            raise ValueError("recovery terminal integrity mismatch")
        observed = self._observe_recovery_terminal(action, attempt)
        # Journals append normally. Re-check the exact frozen receipt prefix,
        # not today's whole-journal digest; the raw observation remains immutable.
        if observed is None or any(observed.get(key) != terminal.get(key) for key in
                ("outcome", "turn_id", "host_item_id", "native_call_id")):
            return None
        from ..plan_amendment import _native_session_record
        for source in terminal["sources"].values():
            path, _ = _native_session_record(source["session_id"], False)
            if hashlib.sha256(path.read_bytes()[:source["bytes"]]).hexdigest() != source["sha256"]:
                return None
        return terminal["outcome"]

    def reconcile_recovery(self, action_id, *, owner_epoch, owner_session_id):
        """Public current-owner reconciliation; never invokes a provider action."""
        from ..state import interprocess_lock, run_dir, verify_run_evidence
        from ..supervisor import SupervisorLease, _read_lease_record
        from ..task_registry import load_task_binding
        from ..protocol_adoption import effective_responsibility
        claimed = self.claim_recovery(action_id, owner_epoch=owner_epoch,
                                      owner_session_id=owner_session_id, _existing_only=True)
        if claimed["status"] != "already_claimed":
            return dict(claimed, status="unknown", outcome=None)
        action = self.action(action_id)
        lease = SupervisorLease(self.paths)
        with interprocess_lock(run_dir(self.paths, action["run_id"]) / ".monitor.lock"), lease.lock(action["run_id"]):
            owner = _read_lease_record(lease.path(action["run_id"]))
            if (not isinstance(owner, dict) or owner.get("released") is True
                    or owner.get("lease_epoch") != owner_epoch
                    or (owner.get("owner") or {}).get("session_id") != owner_session_id):
                raise PermissionError("recovery owner is not current")
            snapshot = verify_run_evidence(self.paths, action["run_id"])
            binding = load_task_binding(self.paths, action["issue_id"], action["role"], run_id=action["run_id"])
            self._require_recovery_binding(snapshot, binding)
            if (effective_responsibility(self.paths, snapshot).get("status") != "effective"
                    or action["request"].get("authorization_digest") != snapshot.authorization_digest
                    or action["request"].get("node_contract_digest") != snapshot.node_contract_digest
                    or binding.generation != action["generation"] or binding.provider != action["provider"]
                    or binding.task_id != action["request"].get("threadId")
                    or binding.host != action["request"].get("hostId")):
                raise PermissionError("recovery task or authorization lineage changed")
            attempts = []
            for path in self._directory("recovery-claims").glob("*.json"):
                state = self._read(path)
                attempts.extend(attempt for attempt in state["attempts"] if attempt.get("action_id") == action_id)
            if len(attempts) != 1:
                raise ValueError("recovery claim is missing or ambiguous")
            attempt = attempts[0]
            observed = self._observe_recovery_terminal(action, attempt)
            if observed is None:
                return dict(claimed, status="unknown", outcome=None)
            path = self._directory("recovery-terminals") / (action_id + ".json")
            if not path.exists():
                body = {"action_id": action_id, "request_digest": action["request_digest"], **observed}
                self._atomic(path, {**body, "digest": _canonical_digest(body)})
            outcome = self._verified_recovery_terminal(action, attempt)
            if outcome and attempt.get("help_digest"):
                self._project_recovery_help(action, attempt, outcome)
        result = self.claim_recovery(action_id, owner_epoch=owner_epoch, owner_session_id=owner_session_id)
        return dict(result, status="verified" if outcome else "unknown", outcome=outcome)

    def successor_failure_sequence(self, action_id, *, owner_epoch, owner_session_id, _locked=False, _retiring=False, _retired_binding=None):
        """Verify consecutive failures; never authorize retirement or creation.

        The monitor must additionally verify native termination, preservation
        and writer-lease retirement in its protected successor transaction.
        """
        from ..state import interprocess_lock, run_dir, verify_run_evidence
        from ..supervisor import SupervisorLease, _read_lease_record
        from ..task_registry import load_task_binding
        from ..protocol_adoption import effective_responsibility
        from contextlib import nullcontext
        action = self.action(action_id)
        lease = SupervisorLease(self.paths)
        with (nullcontext() if _locked else interprocess_lock(run_dir(self.paths, action["run_id"]) / ".monitor.lock")), (nullcontext() if _locked else lease.lock(action["run_id"])):
            # Revalidate ownership and lineage after entering the transaction.
            owner = _read_lease_record(lease.path(action["run_id"]))
            if (not isinstance(owner, dict) or owner.get("released") is True
                    or owner.get("lease_epoch") != owner_epoch
                    or (owner.get("owner") or {}).get("session_id") != owner_session_id):
                raise PermissionError("recovery owner is not current")
            snapshot = verify_run_evidence(self.paths, action["run_id"])
            binding = load_task_binding(self.paths, action["issue_id"], action["role"],
                                        run_id=action["run_id"])
            if _retiring and _retired_binding is not None:
                # The protected successor transaction has already committed
                # its new registry row. Verify the preserved historical row,
                # not the latest writer as though it were the retired task.
                from ..task_registry import _read_registry, _run_tasks_path
                _, history = _read_registry(_run_tasks_path(self.paths, action["run_id"], create=False), action["run_id"])
                matches = [item for item in history if item.to_dict() == _retired_binding]
                if len(matches) != 1 or matches[0].status != "stopped":
                    raise PermissionError("retired historical binding changed")
                binding = matches[0]
            else:
                self._require_recovery_binding(snapshot, binding, retiring=_retiring)
            if (effective_responsibility(self.paths, snapshot).get("status") != "effective"
                    or binding.task_id != action["request"].get("threadId")
                    or binding.host != action["request"].get("hostId")
                    or binding.provider != action["provider"]
                    or binding.generation != action["generation"]
                    or snapshot.authorization_digest != action["request"].get("authorization_digest")
                    or snapshot.node_contract_digest != action["request"].get("node_contract_digest")):
                raise PermissionError("successor failure sequence lineage changed")
            rows = []
            seen = set()
            active = False
            claimed = False
            for path in self._directory("recovery-claims").glob("*.json"):
                state = self._read(path)
                basis = state.get("basis") or {}
                if any(basis.get(key) != value for key, value in (
                        ("run_id", action["run_id"]), ("node_id", action["issue_id"]),
                        ("role", action["role"]), ("generation", action["generation"]),
                        ("task_id", action["request"].get("threadId")),
                        ("authorization_digest", action["request"].get("authorization_digest")),
                        ("node_contract_digest", action["request"].get("node_contract_digest")))):
                    continue
                for attempt in state["attempts"]:
                    previous = self.action(attempt["action_id"])
                    if (any(previous.get(key) != action.get(key) for key in
                            ("run_id", "issue_id", "role", "generation", "provider"))
                            or any(previous["request"].get(key) != action["request"].get(key)
                                   for key in ("threadId", "hostId", "authorization_digest",
                                               "node_contract_digest"))):
                        raise ValueError("recovery claim lineage mismatch")
                    if previous["action_id"] in seen:
                        raise ValueError("duplicate recovery claim evidence")
                    seen.add(previous["action_id"])
                    claimed = claimed or previous["action_id"] == action_id
                    if attempt.get("request_digest") != previous["request_digest"]:
                        raise ValueError("recovery claim request mismatch")
                    outcome = self._verified_recovery_terminal(previous, attempt)
                    if outcome != "failed":
                        rows = []
                        active = active or outcome is None
                        continue
                    terminal = self._read(self._directory("recovery-terminals") /
                                          (previous["action_id"] + ".json"))
                    rows.append(terminal)
            native_calls = [row["native_call_id"] for row in rows]
            verified = (claimed and not active and len(rows) >= 3
                        and len(set(native_calls)) == len(rows))
            return {"status": "verified" if verified else "unknown",
                    "failure_sequence": rows if verified else [],
                    "successor_authorized": False}

    def verify_successor_termination(self, action_id, observation, *,
                                     owner_epoch, owner_session_id, _locked=False):
        from contextlib import nullcontext
        from ..state import interprocess_lock, run_dir
        from ..supervisor import SupervisorLease
        action = self.action(action_id)
        lease = SupervisorLease(self.paths)
        with (nullcontext() if _locked else interprocess_lock(run_dir(self.paths, action["run_id"]) / ".monitor.lock")), (nullcontext() if _locked else lease.lock(action["run_id"])):
            return self._verify_successor_termination_locked(action_id, observation,
                owner_epoch=owner_epoch, owner_session_id=owner_session_id)

    def _verify_successor_termination_locked(self, action_id, observation, *,
                                            owner_epoch, owner_session_id, _retiring=False, _retired_binding=None):
        """Read-only verification of provider-owned retirement evidence.

        Reuses the existing native source verifier; caller flags, mailbox
        envelopes and archived/idle alone cannot establish quiescence.
        This does not release a writer lease or authorize a successor.
        """
        from ..state import run_dir, verify_run_evidence
        from ..plan_amendment import _verify_terminal_receipt, _native_session_record, _rollout_native_calls
        failures = self.successor_failure_sequence(action_id, owner_epoch=owner_epoch,
                                                   owner_session_id=owner_session_id, _locked=True, _retiring=_retiring,
                                                   _retired_binding=_retired_binding)
        if failures["status"] != "verified":
            raise PermissionError("three verified recovery failures are required")
        return self._verify_successor_termination_source(action_id, observation,
            observer_epoch=owner_epoch, observer_session_id=owner_session_id,
            failure_sequence=failures["failure_sequence"])

    def _verify_successor_termination_source(self, action_id, observation, *,
                                             observer_epoch, observer_session_id, failure_sequence):
        """Authenticate one current or historical native observer, not write authority.

        Historical registry membership proves source identity only. Callers
        must separately fence the current owner and verify the failure sequence.
        """
        from ..state import run_dir, verify_run_evidence
        from ..plan_amendment import _verify_terminal_receipt, _native_session_record, _rollout_native_calls
        action = self.action(action_id)
        if (action["provider"] != CODEX_PROVIDER or not isinstance(observation, dict)
                or observation.get("session_id") != action["request"].get("threadId")
                or observation.get("host") != action["request"].get("hostId")
                or observation.get("inherited_unreturned")):
            raise PermissionError("provider termination source is unknown")
        snapshot = verify_run_evidence(self.paths, action["run_id"])
        registry = self._read(run_dir(self.paths, action["run_id"]) / "supervisor-registry.json")
        terminal = observation["native_terminal"]
        receipt = self._read(Path(terminal["receipt_path"]))
        observer = receipt.get("observer") or {}
        if (observer.get("session_id") != observer_session_id
                or observer.get("lease_epoch") != observer_epoch):
            raise PermissionError("termination observer does not match the recorded owner")
        if hashlib.sha256(Path(terminal["receipt_path"]).read_bytes()).hexdigest() != terminal["receipt_sha256"]:
            raise PermissionError("termination receipt digest changed")
        _verify_terminal_receipt(observation, snapshot.plan_id, snapshot.run_id,
                                 observation["host"], registry)
        native_path, native_rows = _native_session_record(observation["session_id"], True)
        data = native_path.read_bytes()
        if (hashlib.sha256(data).hexdigest() != observation.get("rollout_sha256")
                or Path(observation["rollout_path"]).read_bytes() != data
                or [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()] != native_rows
                or [row.get("ordinal") for row in native_rows] != list(range(len(native_rows)))):
            raise PermissionError("native retired record changed")
        row, = [row for row in receipt["consumers"] if row.get("thread_id") == observation["session_id"]]
        last = row["native_observation"]["latest_turn"][-1]
        tail = native_rows[-1].get("payload") or {}
        started_turns = [row["payload"].get("turn_id") for row in native_rows
                         if row.get("type") == "event_msg"
                         and (row.get("payload") or {}).get("type") == "task_started"]
        finished_turns = [row["payload"].get("turn_id") for row in native_rows
                          if row.get("type") == "event_msg"
                          and (row.get("payload") or {}).get("type") in
                          {"task_complete", "task_completed", "turn_aborted"}]
        if (terminal.get("read_thread_status") not in {"terminated", "closed", "stopped"}
                or terminal.get("archive_status") != "archived"
                or not started_turns or any(not turn for turn in started_turns)
                or len(set(started_turns)) != len(started_turns)
                or set(started_turns) != set(finished_turns)
                or tail.get("turn_id") != last.get("id")
                or tail.get("type") not in {"task_complete", "turn_aborted"}):
            raise PermissionError("permanent termination semantics are unknown")
        audit = _rollout_native_calls(native_path)
        unresolved = []
        for issue in audit.get("structured_host_issues", []):
            index = issue.get("line", 0) - 1
            payload = (native_rows[index].get("payload") or {}) if 0 <= index < len(native_rows) else {}
            item = payload.get("item") or {}
            completed_command = (issue.get("reason") == "unreconciled host item"
                and payload.get("type") == "item_completed"
                and payload.get("thread_id") == observation["session_id"]
                and item.get("type") == "CommandExecution"
                and _native_command_identity(item) is not None
                and _native_command_exit(item) is not None
                and type(payload.get("started_at_ms")) is int
                and type(payload.get("completed_at_ms")) is int
                and payload["completed_at_ms"] >= payload["started_at_ms"]
                and not any(key in item for key in ("server", "tool", "arguments", "result", "parent_id")))
            if not completed_command:
                unresolved.append(issue)
        if audit.get("unpaired") or unresolved:
            raise PermissionError("retiring task has unresolved native actions")
        # Completed command items are execution evidence, not new task
        # creation. Every other structured boundary remains unproven here.
        if any((entry.get("item") or {}).get("type") != "CommandExecution"
               or _native_command_identity(entry.get("item") or {}) is None
               or _native_command_exit(entry.get("item") or {}) is None
               for entry in audit.get("structured_host", [])):
            raise PermissionError("retiring task has unresolved host side effects")
        if native_path.read_bytes() != data:
            raise PermissionError("retiring task record changed during verification")
        return {"status": "verified", "successor_authorized": False,
                "predecessor_task_id": observation["session_id"],
                "failure_sequence": failure_sequence,
                "termination": {"receipt_sha256": terminal["receipt_sha256"],
                    "source_rollout_sha256": terminal["source_rollout_sha256"],
                    "rollout_sha256": observation["rollout_sha256"]}}

    def _project_recovery_help(self, action, attempt, outcome):
        """Settle only the explicitly bound help, preserving other obligations."""
        from ..state import run_dir, verify_run_evidence
        snapshot = verify_run_evidence(self.paths, action["run_id"])
        path = run_dir(self.paths, action["run_id"]) / "heal-state.json"
        if not path.exists():
            return
        state = self._read(path)
        item = (state.get("nodes", {}).get(action["issue_id"], {}).get("supervisor_responsibility") or {})
        helps = item.get("attempted_actions") or []
        if (not helps or item.get("status") not in {"pending", "verified"}
                or item.get("role") != action["role"]
                or item.get("authorization_digest") != snapshot.authorization_digest
                or item.get("node_contract_digest") != snapshot.node_contract_digest):
            return
        matched = [helped for helped in helps if helped.get("help_digest") == attempt["help_digest"]]
        if len(matched) != 1:
            return
        help_record = matched[0]
        recovery = action["request"].get("recovery") or {}
        worker = (attempt.get("native_baseline") or {}).get("worker") or {}
        if (not worker.get("failed_step")
                or recovery.get("help_digest") != attempt["help_digest"]
                or help_record.get("causal_binding") != {"original_step": recovery.get("original_step"),
                    "failure": worker["failed_step"], "task_id": recovery.get("task_id")}):
            return  # Legacy/unbound claims remain pending; never infer causality.
        if help_record.get("recovery_action_id") not in (None, action["action_id"]):
            return
        help_record.update(status="verified_effective" if outcome == "success" else "verified_ineffective",
                           recovery_action_id=action["action_id"], recovery_request_digest=action["request_digest"])
        item["status"] = ("verified" if all(
            helped.get("status") == "verified_effective" or
            (helped.get("status") == "verified_ineffective"
             and any(later.get("status") == "verified_effective"
                     and all(later.get(key) == helped.get(key)
                             for key in ("original_step", "task_id", "generation"))
                     for later in helps[index + 1:]))
            for index, helped in enumerate(helps)) else "pending")
        verified = [entry for entry in helps if entry.get("recovery_action_id")
                    and entry.get("status") in {"verified_effective", "verified_ineffective"}]
        latest = verified[-1]
        latest_outcome = "success" if latest["status"] == "verified_effective" else "failed"
        item["next_action"] = helps[-1]["next_action"]
        if latest is helps[-1] and latest_outcome == "failed":
            item["next_action"] = "原失败步骤复核仍失败；更新事实与诊断，采用有依据的不同行动"
        item["verification"] = {"action_id": latest["recovery_action_id"], "outcome": latest_outcome,
                                "help_digest": latest["help_digest"]}
        self._atomic(path, state)

    def claim_evidence(self, run_id: str, issue_id: str) -> Optional[str]:
        """Name the first durable mark that this node's dispatch was claimed.

        A result, completion lock, conflicting observation, bridge handle,
        session, delivery or prompt is a durable mark of progress.  A legacy
        create without those marks remains unknown: the consumer may have
        read it without recording an acknowledgement. Unreadable evidence
        likewise counts as claimed (fail closed).
        """
        if not all(isinstance(value, str) and value for value in (run_id, issue_id)):
            raise ValueError("provider request identity is required")
        try:
            action_ids = set()
            exposed_create = False
            controlled_pending = set()
            controlled_stale = set()
            request_dir = self.root / "requests"
            if request_dir.is_dir():
                for path in sorted(request_dir.glob("action-*.json")):
                    action = self._read(path)
                    if action.get("run_id") == run_id and action.get("issue_id") == issue_id:
                        action_ids.add(path.stem)
                        if action.get("operation") == "create":
                            state = self._read_create_state(self.action(path.stem))
                            if state is not None and state["status"] == "pending":
                                controlled_pending.add(path.stem)
                            elif state is not None and state["status"] == "stale":
                                controlled_stale.add(path.stem)
                            elif state is None:
                                # Legacy/unmarked requests may have been read
                                # by an old consumer without a durable claim.
                                exposed_create = True
            for action_id in sorted(action_ids):
                if (self.root / "results" / (action_id + ".json")).exists():
                    return "result"
                if (self.root / "locks" / (action_id + ".lock")).exists() and action_id not in controlled_pending:
                    return "lock"
                observations = self.root / "observations"
                if observations.is_dir() and any(observations.glob(action_id + "-*.json")):
                    return "observation"
            handles = self.root / "handles"
            if handles.is_dir():
                for path in sorted(handles.glob("*.json")):
                    meta = self._read(path)
                    if meta.get("run_id") == run_id and meta.get("node_id") == issue_id:
                        return "handle"
            sessions = self.root / "claude-bg-sessions"
            if sessions.is_dir():
                for path in sorted(sessions.glob("*.json")):
                    claims = self._read(path).get("claims")
                    if (isinstance(claims, Mapping) and claims.get("run_id") == run_id
                            and claims.get("node_id") == issue_id):
                        return "session"
            name = re.compile(
                "^" + re.escape(run_id) + "-" + re.escape(issue_id) + r"-(developer|reviewer)-g\d+\.(json|md)$"
            )
            for directory in ("deliveries", "prompts"):
                folder = self.root / directory
                if folder.is_dir() and any(name.match(item.name) for item in folder.iterdir()):
                    return directory
        except (OSError, ValueError, TypeError, AttributeError):
            return "unreadable"
        # Legacy create requests can be read without a durable pre-call claim.
        # No receipt, lock or result proves that the consumer has not read it.
        if controlled_pending:
            return "controlled_pending_create"
        if controlled_stale:
            return "controlled_stale_create"
        if exposed_create:
            return "unknown_create_exposure"
        return None

    def unresolved_create_slots(
        self, run_id: str, issue_id: str, authorization_digest: str,
        except_provider: Optional[str] = None,
    ) -> List[Tuple[str, int]]:
        """(role, generation) of this node's create requests that have no
        result and no stale marker and were written under exactly
        ``authorization_digest``; the candidates a handoff may hide."""
        if not all(isinstance(value, str) and value for value in (run_id, issue_id, authorization_digest)):
            raise ValueError("provider request identity is required")
        slots = set()
        stale_dir = self._directory("stale")
        for path in sorted(self._directory("requests").glob("action-*.json")):
            if (self.root / "results" / path.name).exists() or (stale_dir / path.name).exists():
                continue
            action = self._read(path)
            envelope = action.get("request")
            if action.get("operation") == "create" and self._read_create_state(action) is not None:
                envelope = self._effective_create_action(action).get("request")
            if (
                action.get("run_id") == run_id and action.get("issue_id") == issue_id
                and action.get("operation") == "create"
                and action.get("provider") != except_provider
                and isinstance(action.get("role"), str)
                and isinstance(action.get("generation"), int)
                and isinstance(envelope, Mapping)
                and envelope.get("authorization_digest") == authorization_digest
            ):
                slots.add((action["role"], action["generation"]))
        return sorted(slots)

    def supersede_unclaimed_creates(
        self, run_id: str, issue_id: str, role: str, generation: int,
        previous_authorization_digest: str, except_provider: Optional[str] = None,
    ) -> List[str]:
        """Revoke only pending controlled creates; legacy requests stay unknown."""
        if not all(isinstance(value, str) and value for value in (run_id, issue_id, role, previous_authorization_digest)):
            raise ValueError("provider request identity is required")
        changed = []
        for path in sorted(self._directory("requests").glob("action-*.json")):
            action = self.action(path.stem)
            request = action["request"]
            if action.get("operation") == "create" and self._read_create_state(action) is not None:
                request = self._effective_create_action(action)["request"]
            if (action.get("operation") != "create" or action.get("run_id") != run_id
                    or action.get("issue_id") != issue_id or action.get("role") != role
                    or action.get("generation") != generation
                    or request.get("authorization_digest") != previous_authorization_digest
                    or action.get("provider") == except_provider):
                continue
            state = self._read_create_state(action)
            if state is None:
                # Legacy/unmarked requests remain unknown forever.
                continue
            if state["status"] != "pending":
                continue
            effective = self._effective_create_action(action)
            request = effective["request"]
            if request.get("authorization_digest") != previous_authorization_digest:
                continue
            if self.revoke_create(action["action_id"], action["request_digest"]).get("status") == "stale":
                changed.append(action["action_id"])
        return changed

    def record_worker_delivery(self, run_id, issue_id, role, payload, generation):
        """Worker self-reported delivery, idempotent and format-gated.

        The payload carries the same shape as a delivery event (protocol
        section 5): a nested ``delivery_evidence`` object, plus
        ``in_session_review`` for visible-sdd.  It is validated here so the
        worker hears a format error immediately, archived under
        ``deliveries/``, and handed to the pending ``wait`` request for this
        node/role/generation as a terminal ``delivered`` result the
        monitor's existing gates then judge.  With no pending wait yet, the
        archive stays unconsumed and the next ``wait`` of the same generation
        picks it up; a later generation (rework) never takes an older report.
        Re-reporting an identical payload in one generation never produces a
        second result.
        """
        from ..state import validate_run_id

        validate_run_id(run_id)
        if not isinstance(issue_id, str) or not _SAFE_ID.fullmatch(issue_id):
            raise ValueError('worker delivery node id must be a simple identifier')
        if role not in {'developer', 'reviewer'}:
            raise ValueError('worker delivery role must be developer or reviewer')
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ValueError('worker delivery generation must be a positive integer')
        if not isinstance(payload, dict):
            raise ValueError('worker delivery payload must be an object')
        event = payload.get('event', 'delivered')
        if event not in _WORKER_DELIVERY_EVENTS:
            raise ValueError('worker delivery event is unsupported')
        delivery = payload.get('delivery_evidence')
        if delivery is None and event == 'blocked_engineering':
            delivery = {}
        if not isinstance(delivery, dict):
            raise ValueError(
                'worker delivery requires a nested delivery_evidence object'
            )
        if event != 'blocked_engineering':
            missing = [
                key for key in ('completion_marker', 'delivery_path', 'thread_status')
                if not (isinstance(delivery.get(key), str) and delivery[key].strip())
            ]
            if missing:
                raise ValueError(
                    'worker delivery is missing: {}'.format(', '.join(missing))
                )
            if delivery.get('thread_status') not in {'complete', 'completed', 'DELIVERED'}:
                raise ValueError('worker delivery thread_status is not terminal')
        from ..evidence import blocked_self_report_fields
        blocked = blocked_self_report_fields(payload)
        if blocked and event in {'delivered', 'complete', 'accepted'}:
            # #335: a payload that says it is blocked is not a delivery.  The
            # worker hears the format error immediately and re-reports with
            # event=blocked_engineering instead; the refusal names only the
            # field names, never the worker's own values.
            raise ValueError(
                'worker delivery event cannot be {} with blocked self-report '
                'fields: {}'.format(event, ', '.join(blocked))
            )
        if (
            issue_id == 'integration-review' and role == 'reviewer'
            and event == 'accepted'
        ):
            # The integration reviewer's claim is judged by the same shape
            # rules the monitor applies later; failing here lets the reviewer
            # fix it in its own session instead of exiting on "recorded".
            # The error text is built from program constants only.
            from ..evidence import validate_integration_claim_shape
            validate_integration_claim_shape(payload.get('evidence'))

        latest = self._latest_generation(run_id, issue_id, role)
        if latest is None:
            # No dispatch for this run/node/role: a mistyped node or role
            # would be archived forever and keep the heartbeat busy.
            raise ValueError(
                'worker delivery has no dispatched task for this run/node/role; '
                'run it from the main project directory and check --run-id/--node'
            )
        if generation != latest:
            # Older: a rework superseded it.  Newer: never dispatched, so no
            # wait would ever take it and it would keep the heartbeat busy.
            raise ValueError(
                'worker delivery generation {} is {}; current generation is {}'.format(
                    generation, 'stale' if generation < latest else 'unknown', latest
                )
            )
        path = self._delivery_path(run_id, issue_id, role, generation)
        existing = self._read(path) if path.exists() else None
        warnings = self._delivery_warnings(run_id, issue_id, role, generation, payload)
        if isinstance(existing, dict) and existing.get('payload') == payload:
            if existing.get('consumed_by'):
                return self._delivery_outcome(False, True, warnings)
            consumed = self._consume_archived_delivery(
                run_id, issue_id, role, generation
            )
            return self._delivery_outcome(consumed, True, warnings)
        if role == 'developer' and event != 'blocked_engineering':
            problem = self._landing_problem(run_id, issue_id, role, generation)
            if problem:
                kind, detail = problem
                if kind == 'unverifiable':
                    raise ValueError('cannot verify delivery landing: ' + detail)
                if kind == 'committed':
                    raise ValueError(
                        'worker delivery is not in the node worktree: ' + detail
                        + '; if this node committed onto main by mistake, redo the work '
                        'in the contract worktree and tell the supervisor which commits '
                        'to undo'
                    )
                raise ValueError(
                    'worker delivery is not confined to the node worktree: ' + detail
                    + '; if this node forgot `cd <worktree>`, move the work into the '
                    'contract worktree, restore the main tree, and report again'
                )
        self._directory('deliveries')
        self._atomic(path, {
            'run_id': run_id,
            'issue_id': issue_id,
            'role': role,
            'generation': generation,
            'payload': payload,
            'reported_at': __import__('time').time(),
            'consumed_by': None,
        })
        consumed = self._consume_archived_delivery(run_id, issue_id, role, generation)
        return self._delivery_outcome(consumed, False, warnings)

    @staticmethod
    def _delivery_outcome(consumed, duplicate, warnings):
        outcome = {'recorded': True, 'consumed': consumed, 'duplicate': duplicate}
        if warnings:
            outcome['warnings'] = list(warnings)
        return outcome

    def _delivery_warnings(self, run_id, issue_id, role, generation, payload):
        """Advisory notes that must reach the worker without blocking delivery."""
        warnings = [
            self._safe_warning(
                self._project_mount_warning, run_id, issue_id, role, generation
            ),
            self._safe_warning(
                self._session_root_warning, run_id, issue_id, role, generation, payload
            ),
        ]
        return [warning for warning in warnings if warning]

    @staticmethod
    def _safe_warning(check, *args):
        """One advisory note, or ``None`` -- never an exception.

        Both warnings are observations, so a record this process cannot read
        is "not reported", the same as a missing field (#219 boundary).  The
        two hand-off paths -- the worker's own report and the monitor's wait
        result -- go through this one wrapper so neither can turn a torn
        mailbox record into a failed delivery or a failed wait.
        """
        # Each caught type has a real source: ``OSError`` for path/permission/
        # ``unlink`` failures, ``RuntimeError`` when ``Path.expanduser()``
        # cannot expand ``~``, ``ValueError`` when ``_read`` parses a torn or
        # half-written mailbox record.  ``TypeError`` is deliberately not
        # caught: both checks are isinstance-guarded and take only str/dict,
        # so it can only signal a programming error (a changed signature or a
        # wrong argument type), and silencing it would leave the warning
        # permanently blind -- the very defect this node removes.  For the
        # same reason ``KeyboardInterrupt`` and friends must keep bubbling.
        try:
            return check(*args)
        except (OSError, RuntimeError, ValueError):
            return None

    def _project_mount_warning(self, run_id, issue_id, role, generation):
        """Warn when the create that started this session asked for no mount.

        Only an explicit ``'none'`` warns.  A generation with no record at
        all, and a record written before the field existed, are both legacy:
        they are skipped, because warning there would fire on runs that
        already worked (review P2).  A rework generation inherits its
        creator's value, so it never looks like a dispatch that asked for
        nothing (review P1).

        Advisory only -- delivery still lands.  This reports what the request
        asked for, never what the host granted: a host that opens a
        projectless thread despite a named mount is invisible here and is
        caught by :meth:`_session_root_warning` instead.
        """
        record = self._dispatch_record(run_id, issue_id, role, generation)
        if record.get('project_mount') != _PROJECT_MOUNT_NONE:
            return None
        return (
            'this node was dispatched by a create request that asked the host for no '
            'project mount, so its worker session may be projectless: the contract '
            'worktree is then outside the writable roots, and writes, commits and '
            'self-reports each raise their own approval prompt.  Tell the supervisor '
            'so the node can be re-dispatched against a saved project.'
        )

    def _session_root_warning(self, run_id, issue_id, role, generation, payload):
        """Warn when the worker reports a session root outside the project.

        The one ground truth for issue #219 is what the worker session observes
        about itself: the failing run's create request *did* name a valid
        ``projectId`` and the session was still opened projectless, starting in
        a host-generated directory under ``Documents/Codex`` while the contract
        worktree and the main project tree sat elsewhere.  Neither the request
        nor vibe can see the sandbox roots the host granted, so the session
        directory the worker reports is the only checkable fact.

        Silent when the worker reports nothing -- prompts older than this check
        do not carry the field, and a missing field is not evidence of a bad
        session.  Advisory only: a value that cannot be read as a path, or is
        not absolute, is treated as not reported rather than refused, so this
        check can never be the reason a delivery fails.  The message names both
        roots it compared so a host that legitimately places a session
        elsewhere can be told apart by eye.
        """
        delivery = payload.get('delivery_evidence') if isinstance(payload, Mapping) else None
        raw = delivery.get('session_cwd') if isinstance(delivery, Mapping) else None
        if not isinstance(raw, str) or not raw.strip():
            return None
        # ``expanduser`` can raise on its own (a ``~user`` with no such user),
        # so it stays inside the guard: this field is advisory and must never
        # be the reason a delivery fails.
        try:
            reported = Path(raw.strip()).expanduser()
            if not reported.is_absolute():
                # A relative path would be resolved against whatever directory
                # the CLI happened to run in, which says nothing about the
                # session.
                return None
            session = reported.resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        root = Path(self.paths.root).resolve()
        roots = [root]
        worktree = self._binding_worktree(
            self._child_binding(run_id, issue_id, role, generation)
        )
        if worktree is not None:
            roots.append(worktree)
        if any(session == candidate or candidate in session.parents for candidate in roots):
            return None
        return (
            'this worker reported its session root as {}, which is outside the contract '
            'project root {} and its node worktree {} -- such a session is very likely '
            'projectless, so its write sandbox may not cover the contract paths and '
            'every write, commit and self-report raises its own approval prompt.  Tell '
            'the supervisor so the node can be re-dispatched against a saved project.'
            .format(session, root, worktree if worktree is not None else '(none)')
        )

    def _latest_generation(self, run_id, issue_id, role):
        """Highest generation the monitor has dispatched for this node/role."""
        latest = None
        for path in self._directory('requests').glob('action-*.json'):
            action = self._read(path)
            if not isinstance(action, dict):
                continue
            generation = action.get('generation')
            if (
                action.get('run_id') == run_id
                and action.get('issue_id') == issue_id
                and action.get('role') == role
                and isinstance(generation, int)
                and not isinstance(generation, bool)
                and (latest is None or generation > latest)
            ):
                latest = generation
        if latest is None:
            latest = self._background_generation(run_id, issue_id, role)
        return latest

    def _background_generation(self, run_id, issue_id, role):
        """Generation of a background-dispatched task from the run's tasks.json.

        Background runners (``claude --bg``) start sessions directly and
        write no ``requests/`` record; the monitor's task binding is then the
        only dispatch evidence.  Visible bindings never qualify here: their
        dispatch always leaves a request record.
        """
        from ..task_registry import load_task_binding

        try:
            binding = load_task_binding(self.paths, issue_id, role, run_id=run_id)
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return None
        generation = binding.generation
        if (
            binding.mode != 'background'
            or binding.run_id != run_id
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            return None
        return generation

    def _child_binding(self, run_id, issue_id, role, generation):
        """The monitor-written child binding for this exact generation."""
        for path in sorted(self._directory('requests').glob('action-*.json')):
            action = self._read(path)
            if not isinstance(action, dict):
                continue
            request = action.get('request')
            binding = request.get('child_binding') if isinstance(request, dict) else None
            if (
                isinstance(binding, dict)
                and action.get('run_id') == run_id
                and action.get('issue_id') == issue_id
                and action.get('role') == role
                and action.get('generation') == generation
            ):
                return binding
        return None

    def _dispatch_head_path(self, run_id, issue_id, role, generation):
        return self.root / 'dispatch-heads' / '{}-{}-{}-g{}.json'.format(
            run_id, issue_id, role, generation
        )

    def _record_dispatch_head(self, run_id, issue_id, role, generation, binding=None,
                              project_mount=None):
        """Write-once: main project HEAD (and node worktree HEAD) at dispatch.

        Kept beside, not inside, the request so the request digest and its
        identity-drift check are untouched.  Later requests of the same
        generation (locate / resume) keep the first value.

        ``project_mount`` is the mount a session-creating request asked the
        host for (see :func:`_requested_project_mount`).  A generation that
        cannot create a session (locate / resume) passes ``None`` and inherits
        the newest earlier generation's value, because a resume cannot move an
        existing session to another project; without that, every rework would
        record "no mount asked for" and the delivery report would warn on it.
        An absent key means the record predates the field (legacy) -- never
        "no mount".

        ponytail: an unreadable HEAD (no git, unborn branch) or an unwritable
        record (OSError) records nothing, and the committed-landing check is
        then skipped for this generation; it never fails the dispatch.
        """
        import subprocess

        path = self._dispatch_head_path(run_id, issue_id, role, generation)
        if path.exists():
            return
        try:
            done = subprocess.run(
                ['git', '-C', str(self.paths.root), 'rev-parse', '--verify', '-q', 'HEAD'],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return
        head = done.stdout.strip()
        if done.returncode != 0 or len(head) not in (40, 64):
            return
        record = {'head': head}
        mount = project_mount or self._inherited_project_mount(
            run_id, issue_id, role, generation
        )
        if mount is not None:
            record['project_mount'] = mount
        worktree = self._binding_worktree(binding)
        if worktree is not None and worktree.is_dir():
            try:
                done = subprocess.run(
                    ['git', '-C', str(worktree), 'rev-parse', '--verify', '-q', 'HEAD'],
                    capture_output=True, text=True, timeout=10,
                )
                own = done.stdout.strip()
                if done.returncode == 0 and len(own) in (40, 64):
                    record['worktree_head'] = own
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            directory = self._directory('dispatch-heads')
        except OSError:
            return
        # O_EXCL: concurrent first requests of one generation keep one baseline
        # (an existing file, symlink included, is never written through).
        try:
            descriptor = os.open(
                str(directory / path.name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644
            )
        except OSError:
            return
        try:
            try:
                os.write(descriptor, (json.dumps(record) + '\n').encode('utf-8'))
            finally:
                os.close(descriptor)
        except OSError:
            # Never fail the dispatch; drop the partial record (read as absent).
            try:
                (directory / path.name).unlink()
            except OSError:
                pass

    def _dispatch_record(self, run_id, issue_id, role, generation):
        path = self._dispatch_head_path(run_id, issue_id, role, generation)
        if not path.is_file():
            return {}
        # ponytail: a torn or unreadable record (crash between create and
        # write) is treated as absent -- the history check is skipped.
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        return {k: v for k, v in value.items()
                if k in ('head', 'worktree_head', 'project_mount')
                and isinstance(v, str) and v}

    def _inherited_project_mount(self, run_id, issue_id, role, generation):
        """Mount recorded for the newest earlier generation, or ``None``.

        A resume or locate cannot change a session's mount: the session
        already exists.  Inheriting is what keeps a rework generation -- whose
        request carries no target -- from reading as a dispatch that asked for
        no project at all (review P1).
        """
        try:
            earlier = int(generation) - 1
        except (TypeError, ValueError):
            return None
        while earlier >= 1:
            mount = self._dispatch_record(run_id, issue_id, role, earlier).get(
                'project_mount'
            )
            if mount:
                return mount
            earlier -= 1
        return None

    def _dispatch_head(self, run_id, issue_id, role, generation):
        return self._dispatch_record(run_id, issue_id, role, generation).get('head')

    def _binding_worktree(self, binding):
        """Resolved contract worktree, or None when the binding names none."""
        raw = str((binding or {}).get('worktree') or '').strip() if isinstance(binding, dict) else ''
        if not raw:
            return None
        root = Path(self.paths.root).resolve()
        worktree = Path(raw)
        if not worktree.is_absolute():
            worktree = root / worktree
        return worktree.resolve()

    def _landing_problem(self, run_id, issue_id, role, generation):
        """``(kind, detail)`` when a developer's work is misplaced, else None.

        ``kind`` is ``'misplaced'``, ``'committed'`` (work landed as a main
        project commit) or ``'unverifiable'`` (git unreadable).
        Checks real git facts, never the worker's own claim: the contract
        worktree must exist on the contract branch, and no allowlisted file
        may be uncommitted (modified, staged or untracked) in the main project
        tree.  A worker that forgot to ``cd`` would otherwise edit the main
        tree while the monitor runs acceptance in the untouched worktree and
        reads a false green.

        Work *committed* onto the main tree is caught from the HEADs recorded
        at dispatch (``dispatch-heads/``): when the node worktree holds no
        allowlisted change since its own dispatch HEAD, a main-project commit
        since the main dispatch HEAD touching an allowlisted file is refused
        (kind ``'committed'``).
        ponytail: dispatches made before that record existed (or with no
        readable HEAD) are not history-checked.
        """
        import subprocess

        binding = self._child_binding(run_id, issue_id, role, generation)
        # ponytail: dispatches without a child_binding (legacy / non-visible
        # runners) are not checked; add a binding there to cover them.
        if binding is None:
            return None
        root = Path(self.paths.root).resolve()
        raw = str(binding.get('worktree') or '').strip()
        if not raw:
            return 'misplaced', 'contract has no worktree'
        worktree = self._binding_worktree(binding)
        if worktree == root:
            return None  # the node writes the main tree by contract
        if not worktree.is_dir():
            return 'misplaced', 'worktree {} does not exist'.format(raw)

        def git(cwd, *args):
            try:
                done = subprocess.run(
                    ['git', '-C', str(cwd)] + list(args),
                    capture_output=True, text=True, timeout=10,
                )
            except (OSError, subprocess.SubprocessError) as error:
                return None, type(error).__name__
            if done.returncode != 0:
                return None, (done.stderr.strip() or 'git failed')
            return done.stdout, None

        branch, error = git(worktree, 'branch', '--show-current')
        if error:
            return 'unverifiable', 'cannot read worktree branch ({})'.format(error)
        expected = str(binding.get('branch') or '')
        if expected and branch.strip() != expected:
            return 'misplaced', 'worktree is on branch {!r}, contract says {!r}'.format(
                branch.strip(), expected
            )
        # ponytail: "." or path-escaping allowlist entries are skipped -- they
        # would match unrelated main-tree changes; real node allowlists are
        # explicit relative files.
        allowlist = [
            item for item in (binding.get('allowlist') or [])
            if isinstance(item, str) and item.strip() not in ('', '.')
            and not Path(item).is_absolute() and '..' not in Path(item).parts
        ]
        if not allowlist:
            return None
        # -z: NUL-separated, unquoted paths; a rename entry is followed by
        # its source path, which is skipped.  ponytail: gitignored files are
        # not seen; node allowlists name tracked sources.
        status, error = git(
            root, 'status', '--porcelain', '-z', '--untracked-files=all', '--', *allowlist
        )
        if error:
            return 'unverifiable', 'cannot read main project status ({})'.format(error)
        changed = set()
        entries = iter(status.split('\0'))
        for entry in entries:
            if len(entry) < 4:
                continue
            changed.add(entry[3:])
            if 'R' in entry[:2] or 'C' in entry[:2]:  # index or worktree side
                next(entries, None)
        if changed:
            return 'misplaced', (
                'the main project has uncommitted changes to this node\'s allowlisted '
                'files: {} (a missing `cd <worktree>`, another writer, or a pending '
                'merge -- check the main tree first)'.format(', '.join(sorted(changed)))
            )
        record = self._dispatch_record(run_id, issue_id, role, generation)
        head = record.get('head')
        if head is None:
            return None
        # History is only consulted when the worktree holds none of this
        # node's work: a worker that forgot `cd` leaves it untouched, while a
        # worker that did its job there is never blocked by legitimate merges
        # of overlapping nodes into main.  ponytail: a report-only node whose
        # allowlist overlaps a merge landed during its run is still refused;
        # re-dispatching (new generation, new baseline) clears it.  Leftover
        # uncommitted edits already in the worktree at dispatch, or a worktree
        # created after dispatch (no worktree_head, main head used instead),
        # count as own work and pass.
        dirty, error = git(
            worktree, 'status', '--porcelain', '--untracked-files=all', '--', *allowlist
        )
        if error:
            return 'unverifiable', 'cannot read worktree status ({})'.format(error)
        if dirty.strip():
            return None
        # Own commits are counted from the worktree's own dispatch HEAD, so a
        # rework's earlier generations on the node branch do not count.
        own_base = record.get('worktree_head') or head
        own, error = git(worktree, 'log', '--format=%h', own_base + '..HEAD', '--', *allowlist)
        if error:
            return 'unverifiable', (
                'cannot read worktree history since dispatch ({}); re-dispatch the '
                'node'.format(error)
            )
        if own.split():
            return None
        _, error = git(root, 'merge-base', '--is-ancestor', head, 'HEAD')
        if error:
            return 'unverifiable', (
                'main project HEAD no longer descends from the dispatch baseline {} '
                '(branch switched, history rewritten or gc); re-dispatch the node'.format(head[:12])
            )
        log, error = git(root, 'log', '--format=%h', head + '..HEAD', '--', *allowlist)
        if error:
            return 'unverifiable', 'cannot read main project history since dispatch ({})'.format(error)
        commits = log.split()
        if commits:
            return 'committed', (
                'the main project committed this node\'s allowlisted files after it was '
                'dispatched while the node worktree has no such change: {} -- check '
                '`git log {}..HEAD` in the main tree; do not revert commits you did not '
                'make'.format(', '.join(commits[:5]) + (' ...' if len(commits) > 5 else ''), head[:12])
            )
        return None

    def _delivery_path(self, run_id, issue_id, role, generation):
        return self.root / 'deliveries' / '{}-{}-{}-g{}.json'.format(
            run_id, issue_id, role, generation
        )

    def _consume_archived_delivery(self, run_id, issue_id, role, generation):
        """Hand this generation's archived delivery to its pending wait.

        Reports from earlier generations of the same node/role are marked
        superseded so a rework never inherits them and the preflight stops
        counting them as pending work.
        """
        if not isinstance(issue_id, str) or not _SAFE_ID.fullmatch(issue_id):
            return False
        if isinstance(generation, bool) or not isinstance(generation, int):
            return False
        from ..state import interprocess_lock
        identity = _canonical_digest([run_id, issue_id, role])
        with interprocess_lock(self._directory('locks') / ('delivery-' + identity + '.lock')):
            return self._consume_delivery_locked(run_id, issue_id, role, generation)

    def _consume_delivery_locked(self, run_id, issue_id, role, generation):
        deliveries = self.root / 'deliveries'
        if not deliveries.is_dir():
            return False
        prefix = '{}-{}-{}-g'.format(run_id, issue_id, role)
        for older in deliveries.glob(prefix + '*.json'):
            report = self._read(older)
            if (
                isinstance(report, dict)
                and report.get('run_id') == run_id
                and report.get('issue_id') == issue_id
                and report.get('role') == role
                and isinstance(report.get('generation'), int)
                and report['generation'] < generation
                and not report.get('consumed_by')
            ):
                self._atomic(older, dict(report, consumed_by='superseded'))
        path = self._delivery_path(run_id, issue_id, role, generation)
        if not path.is_file():
            return False
        report = self._read(path)
        if not isinstance(report, dict) or report.get('consumed_by'):
            return False
        payload = report.get('payload')
        if not isinstance(payload, dict):
            return False
        for action in self.pending(run_id):
            request = action.get('request')
            if (
                action.get('issue_id') == issue_id
                and action.get('role') == role
                and action.get('operation') == 'wait'
                and action.get('generation') == generation
                and not (
                    isinstance(request, dict)
                    and request.get('purpose') == 'binding_probe'
                )
            ):
                # The cursor is the servicer's read position in the session,
                # never the worker's claim: a self-report carries none, and
                # the delivery gate accepts the receipt below instead.
                result = {
                    key: value for key, value in payload.items()
                    if key != 'cursor' and key not in _ADVISORY_RESULT_KEYS
                }
                result['event'] = payload.get('event', 'delivered')
                result['status'] = 'completed'
                # ISSUE-240: the session-root advisory rides beside the
                # delivery instead of inside it.  It is recomputed here from
                # the same payload the worker reported, so it never enters the
                # payload copy above and never feeds the digest below: the
                # result of a delivery is unchanged when there is nothing to
                # say, and ``idempotent_key`` stays exactly the payload digest
                # it was before this field existed.  The runner forwards it as
                # event data; the delivery gate above has already decided, and
                # this field can never change that decision.
                warning = self._safe_warning(
                    self._session_root_warning,
                    run_id, issue_id, role, generation, payload,
                )
                if warning:
                    result[WARNINGS_KEY] = [warning]
                result['idempotent_key'] = _canonical_digest(payload)
                if not self.complete(action['action_id'], result,
                                     request_digest=action['request_digest'], native=False):
                    continue
                self._atomic(path, dict(report, consumed_by=action['action_id']))
                return True
        return False

    def archived_payload(self, run_id, issue_id, role, generation):
        """The full worker payload archived for this task, or None.

        The archive keeps everything the worker reported, unlike the runner
        projections that forward only the delivery-event vocabulary, so gates
        that must see the worker's own top-level state (#335 blocked
        self-report fields) read it here.  Consumption does not delete the
        file, and a missing or malformed archive is None, never a guess.
        """
        if not isinstance(issue_id, str) or not _SAFE_ID.fullmatch(issue_id):
            return None
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            return None
        path = self._delivery_path(run_id, issue_id, role, generation)
        if not path.is_file():
            return None
        try:
            report = self._read(path)
        except (ValueError, OSError):
            # A corrupt or hostile archive never aborts the monitor tick:
            # the caller falls back to the projected event data, which is
            # exactly today's behavior.  Same fail-soft pattern as
            # ``self_report_receipt``.
            return None
        if not isinstance(report, dict):
            return None
        payload = report.get("payload")
        return payload if isinstance(payload, dict) else None

    def self_report_receipt(self, run_id, issue_id, role, generation, task_id, host,
                            delivery_evidence):
        """The wait action that took this generation's self-report, or None.

        Proves the delivery came through ``worker-deliver`` for exactly this
        task: the archive was consumed by a same-generation ``wait`` aimed at
        the registered task/host, and that wait's bound result is the
        archive's delivery.  It stands in for a cursor, which a self-report
        never has; it never vouches for any other identity field.
        """
        try:
            path = self._delivery_path(run_id, issue_id, role, generation)
            archived = self._read(path)
            payload = archived.get('payload')
            action_id = archived.get('consumed_by')
            if (archived.get('run_id') != run_id or archived.get('issue_id') != issue_id
                    or archived.get('role') != role or archived.get('generation') != generation
                    or not isinstance(payload, dict) or not isinstance(action_id, str)
                    or action_id == 'superseded'):
                return None
            action = self.action(action_id)
            result = self.result(action_id)
            targets = action['request'].get('targets')
        except (OSError, TypeError, ValueError, KeyError):
            return None
        if (action.get('operation') != 'wait' or action.get('run_id') != run_id
                or action.get('issue_id') != issue_id or action.get('role') != role
                or action.get('generation') != generation
                or targets != [{'threadId': task_id, 'hostId': host}]
                or not isinstance(result, dict) or 'cursor' in result
                # Written only by _consume_delivery_locked, from this archive.
                or result.get('idempotent_key') != _canonical_digest(payload)
                or result.get('event') not in {'delivered', 'complete'}
                or not isinstance(delivery_evidence, dict)
                or result.get('delivery_evidence') != payload.get('delivery_evidence')
                or delivery_evidence != payload.get('delivery_evidence')):
            return None
        return action

    @staticmethod
    def background_consumer(task_id, generation):
        """``consumed_by`` marker of a background self-report the monitor took."""
        return 'background:{}:g{}'.format(task_id, generation)

    def background_delivery_receipt(self, run_id, issue_id, role, generation, task_id,
                                    delivery_evidence):
        """The archived self-report of a background task, or None.

        Background dispatch (``claude --bg``) never pages the session, so no
        ``wait`` request ever consumes the archive and :meth:`self_report_receipt`
        cannot exist.  This receipt instead requires the run's registered
        binding to be a ``mode=background`` task of exactly this
        run/node/role/generation/task, and this generation's archive to carry
        the same identity and the same delivery evidence, unconsumed or taken
        only by this task.  It stands in for the host and cursor a background
        session never has; it never vouches for a visible binding.
        """
        from ..task_registry import load_task_binding

        try:
            binding = load_task_binding(self.paths, issue_id, role, run_id=run_id)
            archived = self._read(self._delivery_path(run_id, issue_id, role, generation))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return None
        if (binding.mode != 'background' or binding.run_id != run_id
                or binding.issue_id != issue_id or binding.role != role
                or binding.generation != generation or binding.task_id != task_id
                or not isinstance(archived, dict)
                or archived.get('run_id') != run_id or archived.get('issue_id') != issue_id
                or archived.get('role') != role or archived.get('generation') != generation
                or not isinstance(archived.get('payload'), dict)
                or not isinstance(delivery_evidence, dict)
                or archived['payload'].get('delivery_evidence') != delivery_evidence
                or archived.get('consumed_by') not in (
                    None, self.background_consumer(task_id, generation))):
            return None
        return archived

    def consume_background_delivery(self, run_id, issue_id, role, generation, task_id):
        """Mark a background self-report handed to the monitor; True once owned.

        Same lock as the wait hand-off, so a report is consumed exactly once:
        a later identical ``worker-deliver`` is then a duplicate, and the
        heartbeat stops counting it as pending work.  Re-taking a report this
        task already owns stays True so a restarted runner can re-emit it.
        """
        from ..state import interprocess_lock

        marker = self.background_consumer(task_id, generation)
        identity = _canonical_digest([run_id, issue_id, role])
        with interprocess_lock(self._directory('locks') / ('delivery-' + identity + '.lock')):
            path = self._delivery_path(run_id, issue_id, role, generation)
            report = self._read(path)
            if not isinstance(report, dict) or report.get('consumed_by') not in (None, marker):
                return False
            if report.get('consumed_by') is None:
                self._atomic(path, dict(report, consumed_by=marker))
            return True

    def retire_wait(self, action_id, reason):
        """Hide one unanswered ``wait`` with an audit marker; never a result.

        Only for a wait whose question was already answered by an earlier
        bound result of the same task (the self-report recovery).  A wait that
        has a result is history and stays visible.
        """
        action = self.action(action_id)
        if action.get('operation') != 'wait':
            raise ValueError('only a wait request can be retired')
        if self.result(action_id) is not None:
            raise ValueError('an answered wait cannot be retired')
        hanging = self._hanging_handle(action)
        self._atomic(self._directory('stale') / (action_id + '.json'), {
            'schema_version': self.schema_version,
            'status': 'stale',
            'reason': reason,
            'action_id': action_id,
            'request_digest': action['request_digest'],
        })
        if hanging is not None:
            path, metadata = hanging
            metadata['pending_action'] = None
            metadata['pending_operation'] = None
            self._atomic(path, metadata)

    def _hanging_handle(self, action):
        """The handle of the retired wait's task, when it still points at it.

        Returns ``(path, metadata)`` for a handle whose ``pending_action`` is
        this wait, ``None`` when no handle waits on it.  A handle of the same
        task that waits on something else is refused: retiring must not hide
        or leave behind a different in-flight action.
        """
        handles = self.root / 'handles'
        if not handles.is_dir():
            return None
        for path in sorted(handles.glob('*.json')):
            metadata = self._read(path)
            if (metadata.get('run_id') != action.get('run_id')
                    or metadata.get('node_id') != action.get('issue_id')
                    or metadata.get('role') != action.get('role')
                    or metadata.get('generation') != action.get('generation')):
                continue
            pending = metadata.get('pending_action')
            if pending == action['action_id']:
                return path, metadata
            if pending:
                raise ValueError(
                    'handle {} of {}/{} is waiting on {} ({}), not on the wait being retired'.format(
                        metadata.get('handle_id', path.stem), action.get('issue_id'),
                        action.get('role'), pending, metadata.get('pending_operation')))
        return None

    def answered_wait_warnings(self, run_id):
        """Read-only advisory notes carried by this run's answered waits.

        ``pending()`` drops a request the moment it has a result, so the
        warning written beside a completed wait is unreachable from there.
        Scanning the requests against the results is the only way to read it
        back (#240): the note has to reach the supervisor's preflight, not
        just sit next to a result nobody opens.
        """
        notes = []
        request_dir = self._directory("requests")
        result_dir = self._directory("results")
        if not request_dir.is_dir():
            return notes
        for path in sorted(request_dir.glob("action-*.json")):
            if not (result_dir / path.name).exists():
                continue
            try:
                action = self._read(path)
            except (OSError, ValueError):
                continue
            if not isinstance(action, dict):
                continue
            if action.get("run_id") != run_id or action.get("operation") != "wait":
                continue
            result = self.result(action.get("action_id"))
            if not isinstance(result, dict):
                continue
            warnings = result.get("warnings")
            if not isinstance(warnings, list):
                continue
            collected = [text for text in warnings if isinstance(text, str) and text.strip()]
            if collected:
                notes.append({"node_id": action.get("issue_id"), "warnings": collected})
        return notes

    def unpolled_results(self, run_id):
        """Read-only list of nodes whose pending action already has a result.

        A self-report can complete a wait the monitor has not polled yet;
        until it does, the run still needs the supervisor's next resume.
        """
        handles = self.root / 'handles'
        if not handles.is_dir():
            return []
        found = []
        for path in sorted(handles.glob('*.json')):
            metadata = self._read(path)
            if not isinstance(metadata, dict) or metadata.get('run_id') != run_id:
                continue
            action_id = metadata.get('pending_action')
            if isinstance(action_id, str) and action_id and self.result(action_id) is not None:
                found.append(metadata.get('node_id'))
        return found

    def unconsumed_deliveries(self, run_id):
        """Read-only list of archived self-reports no wait has taken yet."""
        deliveries = self.root / 'deliveries'
        if not deliveries.is_dir():
            return []
        found = []
        for path in sorted(deliveries.glob('*.json')):
            report = self._read(path)
            if (
                isinstance(report, dict)
                and report.get('run_id') == run_id
                and not report.get('consumed_by')
            ):
                found.append(report.get('issue_id'))
        return found

    def unconsumed_delivery_records(self, run_id):
        """Read-only ``[{issue_id, role, generation, wake_failed}]`` of archived
        self-reports nothing has taken yet.

        ``wake_failed`` is the worker's fixed marker that it could not send the
        wake signal; it only tells the daemon to expect the report, since the
        daemon receives every unconsumed report on its next cycle anyway.
        """
        deliveries = self.root / 'deliveries'
        if not deliveries.is_dir():
            return []
        found = []
        for path in sorted(deliveries.glob('*.json')):
            report = self._read(path)
            if (
                isinstance(report, dict)
                and report.get('run_id') == run_id
                and not report.get('consumed_by')
            ):
                payload = report.get('payload')
                found.append({
                    'issue_id': report.get('issue_id'),
                    'role': report.get('role'),
                    'generation': report.get('generation'),
                    'wake_failed': isinstance(payload, dict) and payload.get(WAKE_FAILED_KEY) is True,
                })
        return found

    def has_request(
        self,
        run_id: str,
        issue_id: str,
        role: str,
        operation: Optional[str] = None,
    ) -> bool:
        """Read-only check for a durable provider request in one run scope.

        Recovery uses this to distinguish a missing canonical task from an
        unresolved provider side effect.  Unlike :meth:`pending`, this probe
        does not create the mailbox directories while checking an absent
        request.
        """
        if not all(isinstance(value, str) and value for value in (run_id, issue_id, role)):
            raise ValueError("provider request identity is required")
        if operation is not None and (
            not isinstance(operation, str) or operation not in _PROVIDER_ACTIONS
        ):
            raise ValueError("provider request operation is invalid")
        if self.root.is_symlink() or (self.root.exists() and not self.root.is_dir()):
            raise ValueError("provider action path may not be a symlink")
        request_dir = self.root / "requests"
        if request_dir.is_symlink() or (request_dir.exists() and not request_dir.is_dir()):
            raise ValueError("provider request directory may not be a symlink")
        if not request_dir.is_dir():
            return False
        for path in sorted(request_dir.glob("action-*.json")):
            if path.is_symlink() or not path.is_file():
                raise ValueError("provider action request must be a regular file")
            record = self._read(path)
            if (
                record.get("run_id") == run_id
                and record.get("issue_id") == issue_id
                and record.get("role") == role
                and (operation is None or record.get("operation") == operation)
            ):
                return True
        return False


@dataclass(frozen=True)
class RepositoryTaskRouting:
    """Confirmed repository and host context for public task creation."""

    project_id: str
    host_id: str
    environment: str
    worktree: str
    branch: str

    def __post_init__(self):
        for name in ("project_id", "host_id", "worktree", "branch"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError("repository routing %s is required" % name)
        if self.environment not in {"worktree", "local"}:
            raise ValueError("repository routing environment must be worktree or local")

    def target(self):
        environment = {"type": self.environment}
        if self.environment == "worktree":
            environment["startingState"] = {
                "type": "branch",
                "branchName": self.branch,
            }
        return {
            "type": "project",
            "projectId": self.project_id,
            "environment": environment,
        }


@dataclass(frozen=True)
class BackgroundTaskRouting:
    """Immutable authorized routing expected from a background launcher."""

    host: str
    worktree: str
    branch: str
    status_file: str
    handoff_file: str

    def __post_init__(self):
        for name in ("host", "worktree", "branch", "status_file", "handoff_file"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("background routing %s is required" % name)

    def matches(self, binding: "TaskBinding") -> bool:
        return all(
            getattr(binding, name) == getattr(self, name)
            for name in ("host", "worktree", "branch", "status_file", "handoff_file")
        )


@dataclass(frozen=True)
class TaskBinding:
    provider: str
    mode: str
    role: str
    issue_id: str
    task_id: Optional[str] = None
    host: Optional[str] = None
    worktree: Optional[str] = None
    branch: Optional[str] = None
    status_file: Optional[str] = None
    handoff_file: Optional[str] = None
    cursor: Optional[str] = None
    client_thread_id: Optional[str] = None
    visible: bool = False
    limitations: Tuple[str, ...] = field(default_factory=tuple)
    allowlist: Tuple[str, ...] = field(default_factory=tuple)
    capability_contract_digest: Optional[str] = None
    successor_of: Optional[str] = None

    @property
    def thread_id(self):
        return self.task_id if self.provider == CODEX_PROVIDER else None

    @property
    def host_id(self):
        return self.host if self.provider == CODEX_PROVIDER else None

    @property
    def pending(self):
        return bool(self.client_thread_id and not self.task_id)

    def to_dict(self):
        result = self.__dict__.copy()
        result["limitations"] = list(self.limitations)
        result["allowlist"] = list(self.allowlist)
        result["capability_contract_digest"] = self.capability_contract_digest
        result["successor_of"] = self.successor_of
        if self.provider == CODEX_PROVIDER and self.task_id:
            result["threadId"] = self.task_id
            result["hostId"] = self.host
        return result


@dataclass(frozen=True)
class TaskUpdate:
    cursor: Optional[str]
    status: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def token(self):
        return self.cursor


@dataclass(frozen=True)
class VisibilityResult:
    visible: bool
    direct_enter: bool
    limitations: Tuple[str, ...] = ()
    source: Optional[str] = None

    def to_dict(self):
        return {
            "visible": self.visible,
            "direct_enter": self.direct_enter,
            "limitations": list(self.limitations),
            "source": self.source,
        }


class CodexAppBridge:
    """Exact structured wrapper around the public Codex App tool schemas."""

    def __init__(
        self,
        *,
        create_thread: Callable[[Mapping[str, Any]], Any],
        navigate_to_codex_page: Callable[[Mapping[str, Any]], Any],
        send_message_to_thread: Callable[[Mapping[str, Any]], Any],
        wait_threads: Callable[[Mapping[str, Any]], Any],
        list_threads: Optional[Callable[[Mapping[str, Any]], Any]] = None,
    ):
        self._create_thread = create_thread
        self._navigate = navigate_to_codex_page
        self._send = send_message_to_thread
        self._wait = wait_threads
        self._list = list_threads

    def create(self, request: Mapping[str, Any]):
        _require_keys(request, ("prompt", "target"), "create_thread")
        if not isinstance(request["prompt"], str) or not isinstance(request["target"], Mapping):
            raise ProviderUnavailable("Codex create_thread request has invalid types")
        # Keep supervisor metadata in the durable action envelope, but never
        # forward it to the native tool.  The public Codex App schema rejects
        # unknown arguments (for example issue_id, worker_profile, binding).
        public = {
            key: request[key]
            for key in ("prompt", "model", "thinking", "title")
            if key in request
        }
        target = request["target"]
        # A visible worker create must name a project mount.  The host answers
        # a create without one by opening a *projectless* thread whose write
        # sandbox covers only its generated directory -- not the contract
        # worktree -- so every write, commit and self-report then raises its
        # own approval prompt (issue #219).  vibe never sees the sandbox roots
        # a session ends up with, so the one checkable thing here is that the
        # request asked for a mount at all: fail loudly rather than let a
        # malformed target reach the native tool.
        #
        # Reach: this is the choke point for a desktop session that routes its
        # create through this bridge.  It is not the fix for the observed
        # incident -- that run's create named a valid ``projectId`` and still
        # landed projectless, so a session calling the native tool directly is
        # outside this guard's reach.  The delivery report's session-root check
        # is the part that can see that.
        project_id = target.get("projectId")
        if str(target.get("type") or "").strip() != "project" or not (
            isinstance(project_id, str) and project_id.strip()
        ):
            raise ProviderUnavailable(
                "visible worker create requires a project mount: target.type must be "
                "project and target.projectId must be a non-blank string.  A "
                "projectless worker session cannot write the contract worktree, so "
                "every write, commit and self-report would need its own user "
                "approval.  Save the project on the host and re-attest project_id "
                "before dispatching."
            )
        public_target = {
            key: target[key]
            for key in ("type", "projectId", "environment")
            if key in target
        }
        if isinstance(public_target.get("environment"), Mapping):
            environment = public_target["environment"]
            public_target["environment"] = {
                key: environment[key]
                for key in ("type", "startingState")
                if key in environment
            }
            if isinstance(public_target["environment"].get("startingState"), Mapping):
                starting = public_target["environment"]["startingState"]
                public_target["environment"]["startingState"] = {
                    key: starting[key]
                    for key in ("type", "branchName")
                    if key in starting
                }
        public["target"] = public_target
        return self._create_thread(public)

    def enter_or_locate(self, binding: TaskBinding):
        self._require_real(binding)
        return self._navigate({"threadId": binding.task_id})

    def resume(self, binding: TaskBinding, prompt: str):
        self._require_real(binding)
        request = {"threadId": binding.task_id, "prompt": prompt}
        if binding.host:
            request["hostId"] = binding.host
        return self._send(request)

    def wait(self, binding: TaskBinding, cursor: Optional[str], timeout_ms: int = 120000):
        self._require_real(binding)
        target = {"threadId": binding.task_id}
        if binding.host:
            target["hostId"] = binding.host
        if cursor:
            target["afterCursor"] = cursor
        raw = self._wait({"targets": [target], "timeoutMs": timeout_ms})
        if isinstance(raw, TaskUpdate):
            return raw
        if not isinstance(raw, Mapping):
            raise ProviderUnavailable("Codex wait_threads returned invalid result")
        polls = raw.get("polls")
        if not isinstance(polls, list):
            raise ProviderUnavailable("Codex wait_threads result has no polls list")
        poll = next(
            (
                item for item in polls
                if isinstance(item, Mapping)
                and isinstance(item.get("thread"), Mapping)
                and item["thread"].get("id") == binding.task_id
                and (not binding.host or item["thread"].get("hostId") == binding.host)
            ),
            None,
        )
        if poll is None:
            raise ProviderUnavailable("Codex wait_threads result has no matching poll")
        latest_turn = poll.get("latestTurn")
        turn_error = latest_turn.get("error") if isinstance(latest_turn, Mapping) else None
        if turn_error:
            status = "error"
        elif poll.get("timedOut") or raw.get("timedOut"):
            status = "timeout"
        else:
            status = _poll_status(poll)
        return TaskUpdate(
            cursor=poll.get("cursor") or cursor,
            status=status,
            payload=dict(poll),
        )

    def visibility(self, binding: TaskBinding):
        if binding.pending:
            return VisibilityResult(False, False, ("任务创建中",), "clientThreadId")
        if not binding.task_id or self._list is None:
            raise ProviderUnavailable("Codex list_threads visibility probe is unavailable")
        raw = self._list({"limit": 100})
        entries = ()
        if isinstance(raw, Mapping):
            entries = tuple(raw.get("pinnedThreads", ())) + tuple(raw.get("threads", ()))
        found = any(
            isinstance(item, Mapping)
            and item.get("id") == binding.task_id
            and (not binding.host or item.get("hostId") in (None, binding.host))
            for item in entries
        )
        return VisibilityResult(found, found, () if found else ("任务未出现在列表",), "list_threads")

    def resolve_pending(self, binding: TaskBinding):
        if not binding.pending:
            return binding
        raise ProviderPending(
            "clientThreadId remains pending; no public client-to-thread mapping is verified"
        )

    @staticmethod
    def _require_real(binding: TaskBinding):
        if binding.pending:
            raise ProviderPending("clientThreadId is pending and is not a threadId")
        if not binding.task_id:
            raise ProviderUnavailable("Codex binding has no threadId")


class VisibleTaskProvider:
    """Provider for user-visible tasks with exact identity and cursor data."""

    mode = "visible"

    def __init__(
        self,
        provider: str,
        bridge: Any = None,
        prompt_factory: Optional[Callable[[str, str, Path], str]] = None,
        routing: Optional[RepositoryTaskRouting] = None,
    ):
        self.provider = provider
        self.bridge = bridge
        self.prompt_factory = prompt_factory
        self.routing = routing

    def create(self, role: str, issue_id: str, contract_path: Path) -> TaskBinding:
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        if not isinstance(self.routing, RepositoryTaskRouting):
            raise ProviderUnavailable("confirmed repository task routing is required")
        contract_path = Path(contract_path)
        prompt = (
            self.prompt_factory(role, issue_id, contract_path)
            if self.prompt_factory
            else "请执行 %s 任务 %s，合同：%s。" % (role, issue_id, contract_path)
        )
        if isinstance(self.bridge, CodexAppBridge):
            raw = self.bridge.create({"prompt": prompt, "target": self.routing.target()})
        else:
            method = getattr(self.bridge, "create", None)
            if not callable(method):
                raise ProviderUnavailable("verified visible bridge create is missing")
            raw = method(role, issue_id, contract_path)
        binding = self._binding(raw, role, issue_id)
        if not binding.task_id and not binding.client_thread_id:
            raise ProviderUnavailable("visible task creation returned no durable task identity")
        return binding

    def enter_or_locate(self, binding: TaskBinding) -> None:
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        if isinstance(self.bridge, CodexAppBridge):
            self.bridge.enter_or_locate(binding)
            return
        self._invoke(binding, "enter_or_locate")

    def resume(self, binding: TaskBinding, contract_path: Path) -> None:
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        contract_path = Path(contract_path)
        if isinstance(self.bridge, CodexAppBridge):
            self.bridge.resume(binding, "请继续处理合同：%s。" % contract_path)
            return
        self._invoke(binding, "resume", contract_path)

    def wait(self, binding: TaskBinding, cursor: Optional[str] = None) -> TaskUpdate:
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        if isinstance(self.bridge, CodexAppBridge):
            return self.bridge.wait(binding, cursor)
        raw = self._invoke(binding, "wait", cursor)
        if isinstance(raw, TaskUpdate):
            return raw
        if isinstance(raw, Mapping):
            return TaskUpdate(raw.get("cursor"), str(raw.get("status", "unknown")), dict(raw))
        raise ProviderUnavailable("visible task wait returned invalid result")

    def visibility(self, binding: TaskBinding):
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        if isinstance(self.bridge, CodexAppBridge):
            return self.bridge.visibility(binding)
        raw = self._invoke(binding, "visibility")
        if isinstance(raw, VisibilityResult):
            return raw
        if isinstance(raw, Mapping):
            return VisibilityResult(bool(raw.get("visible")), bool(raw.get("direct_enter")))
        return VisibilityResult(bool(raw), bool(raw))

    def resolve_pending(self, binding: TaskBinding):
        if isinstance(self.bridge, CodexAppBridge):
            return self.bridge.resolve_pending(binding)
        return binding

    def _invoke(self, binding: TaskBinding, method_name: str, *args):
        method = getattr(self.bridge, method_name, None)
        if not callable(method):
            raise ProviderUnavailable("verified visible bridge method missing: %s" % method_name)
        return method(binding, *args)

    def _binding(self, raw: Any, role: str, issue_id: str) -> TaskBinding:
        if isinstance(raw, TaskBinding):
            if raw.provider != self.provider or raw.mode != self.mode:
                raise ProviderUnavailable("visible bridge returned mismatched provider binding")
            if raw.role != role or raw.issue_id != issue_id:
                raise ProviderUnavailable("visible bridge returned mismatched role/issue binding")
            if raw.host != self.routing.host_id or raw.worktree != self.routing.worktree or raw.branch != self.routing.branch:
                raise ProviderUnavailable("visible bridge returned mismatched repository routing")
            return raw
        if not isinstance(raw, Mapping):
            raise ProviderUnavailable("visible task creation returned invalid binding")
        task_id = raw.get("task_id") or raw.get("threadId")
        host = raw.get("host") or raw.get("hostId") or self.routing.host_id
        if host != self.routing.host_id:
            raise ProviderUnavailable("visible task host does not match confirmed routing")
        client_thread_id = raw.get("client_thread_id") or raw.get("clientThreadId")
        return TaskBinding(
            provider=self.provider,
            mode="visible",
            role=role,
            issue_id=issue_id,
            task_id=task_id,
            host=host,
            worktree=self.routing.worktree,
            branch=self.routing.branch,
            status_file=raw.get("status_file"),
            handoff_file=raw.get("handoff_file"),
            cursor=raw.get("cursor"),
            client_thread_id=client_thread_id,
            visible=bool(task_id),
            limitations=("任务创建中",) if client_thread_id and not task_id else (),
        )


class BackgroundTaskProvider:
    mode = "background"

    def __init__(
        self,
        provider: str,
        launcher: Optional[Callable[..., Any]] = None,
        expected_routing: Optional[BackgroundTaskRouting] = None,
    ):
        self.provider = provider
        self.launcher = launcher
        self.expected_routing = expected_routing

    def with_routing(self, expected_routing: BackgroundTaskRouting):
        """Bind an immutable authorized route before task creation."""
        return BackgroundTaskProvider(self.provider, self.launcher, expected_routing)

    def create(self, role: str, issue_id: str, contract_path: Path) -> TaskBinding:
        if not isinstance(self.expected_routing, BackgroundTaskRouting):
            raise ProviderUnavailable("authorized background routing is required")
        launch = self.launcher
        if not callable(launch) and callable(getattr(launch, "launch", None)):
            launch = launch.launch
        if not callable(launch):
            raise ProviderUnavailable("verified background launcher is not configured")
        raw = launch(role, issue_id, Path(contract_path))
        if isinstance(raw, TaskBinding):
            binding = raw
        elif isinstance(raw, Mapping):
            task_id = raw.get("task_id") or raw.get("run_id") or raw.get("handle")
            if not task_id:
                raise ProviderUnavailable("background launcher returned no durable handle")
            returned_role = raw.get("role")
            returned_issue = raw.get("issue_id")
            if returned_role != role or returned_issue != issue_id:
                raise ProviderUnavailable("background launcher returned mismatched role/issue")
            if raw.get("provider") != self.provider or raw.get("mode") != self.mode:
                raise ProviderUnavailable("background launcher returned mismatched provider/mode")
            binding = TaskBinding(
                provider=self.provider,
                mode="background",
                role=role,
                issue_id=issue_id,
                task_id=str(task_id),
                host=raw.get("host"),
                worktree=raw.get("worktree"),
                branch=raw.get("branch"),
                status_file=raw.get("status_file"),
                handoff_file=raw.get("handoff_file"),
                cursor=raw.get("cursor"),
                visible=False,
                limitations=("不可见", "不可直接进入", "返工续接受限"),
            )
        else:
            raise ProviderUnavailable("background launcher returned invalid handle")
        if binding.provider != self.provider or binding.mode != self.mode:
            raise ProviderUnavailable("background launcher returned mismatched provider binding")
        if binding.role != role or binding.issue_id != issue_id:
            raise ProviderUnavailable("background launcher returned mismatched role/issue binding")
        if not binding.task_id:
            raise ProviderUnavailable("background launcher returned no durable handle")
        for name in ("host", "worktree", "branch", "status_file", "handoff_file"):
            if not getattr(binding, name):
                raise ProviderUnavailable("background binding missing routing field: %s" % name)
        if not self.expected_routing.matches(binding):
            raise ProviderUnavailable("background binding does not match authorized routing")
        return binding

    def resume(self, binding: TaskBinding, contract_path: Path) -> None:
        raise ProviderUnavailable("background task cannot be directly resumed")

    def enter_or_locate(self, binding: TaskBinding) -> None:
        raise ProviderUnavailable("background task cannot be directly entered")

    def wait(self, binding: TaskBinding, cursor: Optional[str] = None) -> TaskUpdate:
        raise ProviderUnavailable("background task has no visible wait cursor")

    def visibility(self, binding: TaskBinding):
        return VisibilityResult(False, False, binding.limitations, "background")


def _require_keys(value: Mapping[str, Any], keys, operation: str):
    missing = [key for key in keys if key not in value]
    if missing:
        raise ProviderUnavailable("%s request missing: %s" % (operation, ", ".join(missing)))


def _poll_status(poll: Mapping[str, Any]) -> str:
    thread = poll.get("thread")
    thread_status = thread.get("status") if isinstance(thread, Mapping) else None
    thread_status_type = (
        thread_status.get("type") if isinstance(thread_status, Mapping) else None
    )
    for value in (
        poll.get("status"),
        poll.get("latestTurn", {}).get("status") if isinstance(poll.get("latestTurn"), Mapping) else None,
        thread_status_type,
    ):
        if isinstance(value, str) and value:
            return value
    return "unknown"
