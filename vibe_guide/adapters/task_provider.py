"""Platform-neutral task/session bridges.

The public Codex App calls are injected into :class:`CodexAppBridge`; this
package never imports or fabricates the desktop tool implementation.
"""

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
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
_WORKER_DELIVERY_EVENTS = {"delivered", "complete", "accepted", "review_finding"}


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
    ) -> None:
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
        }
        # The project id is optional: visible-task routing needs it, other
        # routes do not, and only the recording session can vouch for it.
        if project_id is not None:
            if not isinstance(project_id, str) or not project_id.strip():
                raise ValueError("provider capability project_id must be a non-empty string")
            payload["project_id"] = project_id.strip()
        self.root.mkdir(parents=True, exist_ok=True)
        # The per-adapter file is authoritative (issue #279): each adapter's
        # session attests its own record and no later attest of *another*
        # adapter can overwrite it.  The legacy single-file bridge is kept as
        # a mirror of the latest record so pre-split readers still work.
        # Before the mirror is overwritten, a pre-split record that belongs to
        # another adapter and has no per-adapter file yet is migrated into its
        # own file, so the first attest after an upgrade loses nothing.
        legacy = self.root / "capabilities.json"
        if legacy.is_file() and not legacy.is_symlink():
            try:
                previous = self._validate_capability(self._read(legacy))
            except (OSError, ValueError):
                previous = None
            if previous is not None:
                previous_id = str(previous["adapter_id"])
                migrated = self.root / ("capabilities-" + previous_id + ".json")
                if previous_id != adapter_id and not migrated.exists():
                    self._atomic(migrated, previous)
        self._atomic(self.root / ("capabilities-" + adapter_id + ".json"), payload)
        self._atomic(legacy, payload)

    def _validate_capability(self, value: Dict[str, Any]) -> Dict[str, Any]:
        required = {"schema_version", "adapter_id", "facts", "provenance"}
        if not isinstance(value, dict) or not required.issubset(value) or set(value) - required - {"project_id"}:
            raise ValueError("provider capability schema is invalid")
        if value["schema_version"] != self.schema_version:
            raise ValueError("provider capability schema is unsupported")
        return value

    def capabilities_all(self) -> Dict[str, Dict[str, Any]]:
        """Every recorded adapter capability, keyed by its own ``adapter_id``.

        The per-adapter files win; the legacy ``capabilities.json`` record
        joins only for adapters that predate the split and never got a
        per-adapter file.  No record is ever attributed to another adapter.
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
        directory = self._directory("requests")
        path = directory / (action_id + ".json")
        existing = self._read(path) if path.exists() else None
        if existing is not None and existing != payload:
            if not self._prompt_only_upgrade(existing, payload):
                if isinstance(request, dict) and isinstance(request.get("child_binding"), dict):
                    self._record_dispatch_head(
                        run_id, issue_id, role, generation, request["child_binding"],
                        _requested_project_mount(request, operation),
                    )
                raise ValueError("provider action request identity drift")
            # The worker already acted on the original prompt; keep it, and do
            # not record a dispatch baseline after the fact.
            return existing
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
        path = self._directory("results") / (action_id + ".json")
        if not path.exists():
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
                 request_digest: Optional[str] = None, native: bool = True) -> bool:
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
        if request_digest is not None and request_digest != request["request_digest"]:
            raise ValueError("provider action request digest mismatch")
        # ``native=False``: a worker self-report, not a native tool reply.
        called_tool = _unresolved_tool_name(payload) if native else None
        if called_tool is not None:
            self._record_unresolved_tool(request, called_tool, payload)
        record = {
            "schema_version": self.schema_version,
            "action_id": action_id,
            "request_digest": request["request_digest"],
            "payload": payload,
        }
        with interprocess_lock(self._directory("locks") / (action_id + ".lock")):
            existing = self.result(action_id)
            if existing is not None:
                if existing == payload:
                    return True
                self._atomic(self._directory("observations") /
                             (action_id + "-" + _canonical_digest(record) + ".json"), record)
                return False
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
            if (result_dir / path.name).exists():
                continue
            action = self._read(path)
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
        delivery = payload.get('delivery_evidence')
        if not isinstance(delivery, dict):
            raise ValueError(
                'worker delivery requires a nested delivery_evidence object'
            )
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
        event = payload.get('event', 'delivered')
        if event not in _WORKER_DELIVERY_EVENTS:
            raise ValueError('worker delivery event is unsupported')

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
        if role == 'developer':
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
