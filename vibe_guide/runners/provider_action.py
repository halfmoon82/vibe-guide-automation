"""Durable provider-neutral runner backed by desktop action requests/results."""

from dataclasses import replace
import hashlib
import json
import os
import shlex
from pathlib import Path
import subprocess
from typing import Any, Dict, Optional

from ..adapters.task_provider import (
    WARNINGS_KEY,
    ProviderActionStore,
    ProviderPending,
    ProviderUnavailable,
    require_complex_monitor_dispatch,
)
from ..contracts import RunEvent, RunHandle, Runner
from ..capability_contract import load_contract
from ..models import (
    BindingIntent,
    BindingObservation,
    BindingVerification,
    WaitThreadsCursorObservation,
    WorkerProfile,
)
from ..model_router import provider_thinking_for
from ..authorization import REMOTE_GIT_BAN_TEXT, remote_git_denied
from ..paths import ProjectPaths
from ..protocols import load_protocol, protocol_path, published_protocol_digest
from ..task_registry import (
    DEFAULT_TOPOLOGY,
    _TOPOLOGIES,
    TaskBinding,
    binding_contract_enabled,
    load_task_binding,
    runtime_binding_gate,
    save_task_binding,
)
from ..workflow_gate import session_contract_prompt
from ..prompts import rework_findings_section
from ..worker_questions import supervisor_answer_section
from ..state import read_writer_lease
from ..providers import (
    CLAUDE_CODE_PROVIDER,
    CODEX_PROVIDER,
    WORKBUDDY_VISIBLE_PROVIDER,
)


#: Native desktop tools per visible provider, one definition for the five
#: mailbox operations.  The desktop session that services the mailbox reads
#: ``native_tool`` from each request and calls that tool.  Claude Code maps to
#: the desktop app's session tools: spawn a visible session, show it, read its
#: state, message it, and page its transcript (the page cursor is the wait
#: cursor).  Binding results for Claude Code use the provider-neutral
#: ``task_id`` / ``host`` fields; ``threadId`` / ``hostId`` remain Codex-only.
NATIVE_TOOL_MAP: Dict[str, Dict[str, str]] = {
    CODEX_PROVIDER: {
        "create": "codex_app__create_thread",
        "locate": "codex_app__navigate_to_codex_page",
        "visibility": "codex_app__wait_threads",
        "resume": "codex_app__send_message_to_thread",
        "wait": "codex_app__wait_threads",
    },
    CLAUDE_CODE_PROVIDER: {
        "create": "ccd_session__spawn_task",
        "locate": "ccd_window__open_session_in",
        "visibility": "ccd_session_mgmt__get_session",
        "resume": "ccd_session_mgmt__send_message",
        "wait": "ccd_session_mgmt__list_events",
    },
    # Reachable once a WorkBuddy session attests its full visible lifecycle:
    # `workbuddy.yaml` declares the native control plane since 5.0.2.  The
    # mailbox request is emitted either way; actually running these tool names
    # still needs the session-side bridge that exposes them.
    WORKBUDDY_VISIBLE_PROVIDER: {
        "create": "workbuddy_job__create",
        "locate": "workbuddy_job__get",
        "visibility": "workbuddy_job__list",
        "resume": "workbuddy_job__reply",
        "wait": "workbuddy_job__wait",
    },
}


#: Result keys a worker report hands to the monitor as event data.
#: ``in_session_review`` carries the visible-sdd worker session's review
#: clearance (protocol/evidence_ref/p0-p2) that Monitor needs to accept a
#: single-session node (ISSUE-04).
RESULT_EVENT_KEYS = (
    "evidence", "finding", "in_contract", "consistency",
    "delivery_evidence", "completion_marker", "delivery_path",
    "thread_status", "in_session_review",
)

#: Advisory observations a wait result may carry *beside* the delivery payload
#: (issue #240).  The provider store recomputes them from the report at
#: hand-off, so they describe the delivery itself and never anything the worker
#: claims; they are recorded as event data for the supervisor's next
#: preflight/resume and are read by no gate.  Kept out of
#: :data:`RESULT_EVENT_KEYS` because that tuple is a payload projection -- these
#: keys never come from the payload.  The key name is imported from the store
#: that writes it, so the two ends of the hand-off cannot drift.
ADVISORY_EVENT_KEYS = (WARNINGS_KEY,)

#: One advisory note is a diagnostic line, not a channel.
_ADVISORY_NOTE_LIMIT = 2000
_ADVISORY_NOTES_LIMIT = 5


def _advisory_notes(value):
    """Sanitized advisory notes from a provider result, or None.

    Anything malformed is "nothing to say": this field is observational, so it
    must never be able to turn a delivery result into a rejection.
    """
    if not isinstance(value, (list, tuple)):
        return None
    notes = [note.strip() for note in value if isinstance(note, str) and note.strip()]
    if not notes:
        return None
    return [note[:_ADVISORY_NOTE_LIMIT] for note in notes[:_ADVISORY_NOTES_LIMIT]]


#: Receipt shapes this runner reads.  ``poll``/``_wait_result`` and
#: ``heal_pack`` share these definitions, so the shape a heal pack reports as
#: expected is the one the reader enforces.
RESUME_RECEIPT_FIELD = "resumed"
WAIT_EVENT_NAMES = (
    "complete", "delivered", "accepted", "review_finding", "failed", "stopped",
)
WAIT_TERMINAL_STATUSES = ("complete", "completed", "failed", "stopped")
WAIT_TRANSIENT_STATUSES = ("timeout", "unknown", "error")
WAIT_STATUS_AS_EVENT = ("failed", "stopped")
WAIT_CURSOR_MAX_LENGTH = 4096
_MISSING = object()


def cursor_is_valid(cursor: Any) -> bool:
    return (
        isinstance(cursor, str)
        and bool(cursor)
        and len(cursor) <= WAIT_CURSOR_MAX_LENGTH
        and "\x00" not in cursor
    )


def resume_receipt_differences(payload: Dict[str, Any]) -> list:
    """Why the reader would refuse a resume receipt; empty when it accepts."""
    value = payload.get(RESUME_RECEIPT_FIELD, _MISSING)
    if value is _MISSING:
        return ["missing field: %s" % RESUME_RECEIPT_FIELD]
    if value is not True:
        return [
            "field %s must be true (got type %s)"
            % (RESUME_RECEIPT_FIELD, type(value).__name__)
        ]
    return []


def wait_cursor_differences(payload: Dict[str, Any]) -> list:
    """The one wait-result defect ``_wait_result`` keeps pending.

    A bad cursor returns before the pending action is cleared, so the same
    result is read again on every poll.  Every other shape problem is read
    once, consumed, and answered by a fresh wait -- nothing left to repair.
    """
    cursor = payload.get("cursor")
    if cursor is not None and not cursor_is_valid(cursor):
        return [
            "field cursor is invalid (needs a non-empty string, at most %d "
            "characters, no NUL)" % WAIT_CURSOR_MAX_LENGTH
        ]
    return []


#: ``.vibe/config.json`` field choosing how the visible-sdd protocol reaches
#: the worker prompt: ``ref`` (default, path + sha256) or ``inline`` (full text,
#: explicit opt-in).
FIELD_SDD_PROTOCOL_DELIVERY = "sdd_protocol_delivery"


def _sdd_protocol_delivery(root: Path) -> str:
    """Return the project's delivery mode; an explicit illegal value fails closed."""
    path = Path(root) / ".vibe" / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "ref"
    except (OSError, ValueError) as exc:
        raise ValueError("project config is unreadable: {}".format(exc))
    if not isinstance(data, dict) or FIELD_SDD_PROTOCOL_DELIVERY not in data:
        return "ref"
    value = data[FIELD_SDD_PROTOCOL_DELIVERY]
    if value not in ("inline", "ref"):
        raise ValueError(
            "{} must be 'inline' or 'ref'".format(FIELD_SDD_PROTOCOL_DELIVERY)
        )
    return value


#: Fields a ``wait`` result may carry; the rest of the reader's rules live in
#: ``ProviderActionRunner._wait_result`` and are mirrored below.
_WAIT_RESULT_FIELDS = frozenset(
    ("status", "event", "cursor", "error", "reason", "reason_class",
     "credential_required", "login_required", "permission_required",
     WARNINGS_KEY) + RESULT_EVENT_KEYS
)
_CREATE_RESULT_FIELDS = frozenset(("binding", "threadId", "thread_id", "status", "cursor"))
_BINDING_IDENTITY_FIELDS = ("threadId", "thread_id", "task_id", "clientThreadId")
_RESUME_RESULT_FIELDS = frozenset((RESUME_RECEIPT_FIELD, "status"))


def _shape_errors(required, allowed, payload):
    errors = []
    missing = sorted(key for key in required if key not in payload)
    extra = sorted(key for key in payload if key not in allowed)
    if missing:
        errors.append("missing fields: " + ", ".join(missing))
    if extra:
        errors.append("unexpected fields: " + ", ".join(extra))
    return errors


def validate_result_shape(operation, payload, *, role=None):
    """Reject a mailbox result the reader would not accept, naming the fields.

    Mirrors the rules the runner applies when it consumes the result, so a
    wrong shape fails here, before the write, instead of becoming a
    ``blocked_unknown`` after it.  Operations without a reader-side shape
    (locate, visibility, ...) are only required to be objects.  ``role`` is the
    request's role: a developer's ``delivered``/``complete`` wait result must
    carry ``delivery_evidence``.  Raises ``ValueError``; returns None.
    """
    if not isinstance(payload, dict):
        raise ValueError("provider action result payload must be an object")
    errors = []
    if operation == "wait":
        status = payload.get("status")
        # failed/stopped carry their event in the status; timeout/unknown/error
        # are transient (the reader only records visibility_unknown), so they
        # need neither event nor delivery_evidence.
        no_event = status in WAIT_STATUS_AS_EVENT or status in WAIT_TRANSIENT_STATUSES
        required = ["status"] + ([] if no_event else ["event"])
        if role == "developer" and payload.get("event") in ("delivered", "complete"):
            required.append("delivery_evidence")
        errors = _shape_errors(required, _WAIT_RESULT_FIELDS, payload)
        if "status" in payload and not isinstance(status, str):
            errors.append("field status must be a string")
        event = payload.get("event")
        if event is not None and event not in WAIT_EVENT_NAMES:
            errors.append("field event must be one of: " + ", ".join(WAIT_EVENT_NAMES))
        if payload.get("cursor") is not None and not cursor_is_valid(payload["cursor"]):
            errors.append("field cursor is invalid")
        if "delivery_evidence" in payload and not isinstance(payload["delivery_evidence"], dict):
            errors.append("field delivery_evidence must be an object")
    elif operation == "create":
        errors = _shape_errors(["binding"], _CREATE_RESULT_FIELDS, payload)
        binding = payload.get("binding")
        if "binding" in payload and (
            not isinstance(binding, dict)
            or not any(isinstance(binding.get(key), str) and binding[key]
                       for key in _BINDING_IDENTITY_FIELDS)
        ):
            errors.append("field binding needs a task identity: " + " or ".join(_BINDING_IDENTITY_FIELDS))
    elif operation == "resume":
        errors = _shape_errors([RESUME_RECEIPT_FIELD], _RESUME_RESULT_FIELDS, payload)
        if RESUME_RECEIPT_FIELD in payload and payload[RESUME_RECEIPT_FIELD] is not True:
            errors.append("field {} must be true".format(RESUME_RECEIPT_FIELD))
    if errors:
        raise ValueError("{} result shape invalid: {}".format(operation, "; ".join(errors)))


def complete_mailbox_action(paths, run_id, action_id, payload, *, native=True):
    """Validate a mailbox result against its request, then store it.

    The shape is checked before anything is written, so a rejected result
    leaves the request pending (not consumed) and creates no observation.
    Storage is ``ProviderActionStore.complete``: the first bound result wins.
    """
    store = ProviderActionStore(paths)
    request = store.action(action_id)
    if request.get("run_id") != run_id:
        raise ValueError("provider action belongs to a different run")
    validate_result_shape(request.get("operation"), payload, role=request.get("role"))
    return store.complete(action_id, payload, native=native)


class ProviderActionRunner(Runner):
    """Expose a public CLI runner while the App owns native tool execution."""

    def __init__(
        self,
        paths: ProjectPaths,
        adapter_id: str,
        provider: str,
    ):
        self.paths = paths
        self.adapter_id = adapter_id
        self.provider = provider
        self.store = ProviderActionStore(paths)

    def _native_tool(self, operation: str) -> str:
        tools = NATIVE_TOOL_MAP.get(self.provider)
        if tools is None:
            # A placeholder name would sit in the mailbox forever; only
            # providers with a verified native control plane may dispatch.
            raise ProviderUnavailable(
                "no verified native control plane for provider {}".format(self.provider)
            )
        return tools[operation]

    def _remote_git_ban(self, contract: Dict[str, Any]) -> str:
        """The fixed ban line, only when the card says ``deny`` (else empty)."""
        return "\n" + REMOTE_GIT_BAN_TEXT + "\n" if remote_git_denied(self.paths, contract) else ""

    def _consistency_instruction(self, contract: Dict[str, Any]) -> str:
        try:
            capability = session_contract_prompt(load_contract(self.paths))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            capability = "Capability contract: {\"status\":\"unknown\"}"
        consistency = "一致性纠偏证据必须原样绑定：{}".format(
            json.dumps(
                contract.get("consistency_binding", {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return capability + "\n" + consistency

    @staticmethod
    def _continuation_prompt(contract: Dict[str, Any], project_root: Optional[Path] = None) -> str:
        """Build a role-specific continuation contract.

        Reviewer continuations must resume the current business review round;
        a generic "continue Issue" prompt can otherwise repeat an old
        administrative check and leave the durable status marker unchanged.
        """
        root_flag = " --project-root " + shlex.quote(str(project_root)) if project_root else ""
        node_id = str(contract.get("node_id", ""))
        generation = int(contract.get("generation", 0))
        reviewer = contract.get("role") == "reviewer"
        objective = contract.get("goal") or contract.get("objective") or contract.get("title") or contract.get("output") or "完成原 Issue 合同中的实现、测试及交付"
        business = {key: contract[key] for key in ("input", "output", "error_behavior", "acceptance_example") if key in contract}
        purpose = ("业务 review：只审查当前 developer 交付，独立记录发现及验收，禁止代改业务代码。"
                   if reviewer else "开发：继续已有实现、测试与返工，完成本代交付。")
        # ISSUE-249: pin the reviewer's acceptance event value and the
        # top-level evidence carrier on continuations as well; the same
        # payload-shape gap bricked a node when the event value was left
        # to the model.
        acceptance_pin = (
            "验收交付约定：payload 必须带顶层 \"event\": \"accepted\" 与顶层 \"evidence\" 对象"
            "（记录本轮 P0-P2 清理结论），缺任一字段 monitor 会按格式错误退回本会话重报；"
            "仍有未清 P0-P2 发现时用顶层 \"event\": \"review_finding\" 携带 finding 上报，不得用 accepted。"
            if reviewer else ""
        )
        return (
            "执行请求：继续 Issue {node} 的{purpose}本轮 generation {generation}。\n"
            "目标：{objective}；业务合同：{business}。"
            "入口规则或后续规则注入不能替代该任务目标，不得仅回复收到规则。\n"
            "合同：{spec}；writer worktree：{worktree}；branch：{branch}；允许路径：{files}。\n"
            "当前 developer 交付：{delivery}；状态文件：{status}；交付文件：{handoff}；完成 marker：{marker}。"
            "完成后必须写入本代状态/交付；在主项目目录执行 `vibe worker-deliver{root_flag} --run-id {run} --node {node} --role {role} --generation {generation} --payload '<交付 JSON>'`。"
            "{acceptance_pin}"
            "随后执行 `vibe supervisor-address{root_flag} --run-id {run} --node {node} --role {role} --generation {generation} --json`；"
            "`wake.channel` 非空时用该工具向 `wake.target` 原样发送 `wake.signal`，唤醒登记主监工；为 null 或地址 unknown 时不发，由心跳领取。"
            "自报时把你本会话的工作目录原样写进交付 JSON 的 `delivery_evidence.session_cwd`：监工用它核对本会话是否真的挂载了 project，不填就无法核对。"
            "运行消费由登记主监工负责，worker 不得调用 core resume、reconcile 或消费 mailbox。\n"
            "决策边界：不改变 PRD 决策的工程问题（实现方案、测试取舍、基线）自行判断并决定，"
            "把理由写进交付，不用 AskUserQuestion，不停下等人；"
            "需要改允许路径外的文件时，不要改路径外文件，也不要停下等人：先完成路径内的部分并正常交付，"
            "在交付说明里写清要改哪些文件、为什么，范围由监工和审查员裁决。"
            "只有会改变 PRD 决策、涉及部署发布、凭据或授权卡外不可撤销动作、新增第三方依赖、"
            "可见降级为后台不可见时，才在交付里附 decision_requests。\n"
            "{rework_findings}{supervisor_answer}"
        ).format(root_flag=root_flag, node=node_id, generation=generation, purpose=purpose, objective=objective,
                 role=contract.get('role', 'developer'),
                 run=contract.get('run_id', '<原 run-id>'),
                 business=json.dumps(business, ensure_ascii=False),
                 spec=contract.get("spec_path", "原 Issue 合同"),
                 worktree=contract.get("worktree", "原绑定工作目录"),
                 branch=contract.get("branch", "原绑定分支"),
                 files=json.dumps(contract.get("files", []), ensure_ascii=False),
                 delivery=contract.get("delivery_path", "原开发交付"),
                 status=contract.get("status_file", "原状态文件"),
                 handoff=contract.get("handoff_file") or contract.get("delivery_path", "原交付文件"),
                 marker=contract.get("completion_marker", "原交付 marker"),
                 acceptance_pin=acceptance_pin,
                 rework_findings=rework_findings_section(contract),
                 supervisor_answer=supervisor_answer_section(contract))

    def _validate_v39_create_binding(
        self, contract: Dict[str, Any], runtime_worktree: Path
    ) -> None:
        """Fail closed before a V3.9 provider create request is emitted."""
        required = ("project_id", "worktree", "managed_root", "branch", "base_sha")
        values = {name: contract.get(name) for name in required}
        project_id = values["project_id"]
        if not isinstance(project_id, str) or not project_id.strip() or "\x00" in project_id:
            raise ProviderUnavailable("provider binding project_id is blocked_unknown")
        for name in ("worktree", "managed_root"):
            value = values[name]
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ProviderUnavailable("provider binding %s is blocked_unknown" % name)
            if not Path(value).is_absolute():
                raise ProviderUnavailable("provider binding %s is blocked_unknown" % name)

        branch = values["branch"]
        invalid_branch = (
            not isinstance(branch, str)
            or not branch.strip()
            or branch != branch.strip()
            or "\x00" in branch
            or any(char.isspace() for char in branch)
            or Path(branch).is_absolute()
            or branch.startswith("-")
            or branch.endswith("/")
            or branch.endswith(".")
            or branch.startswith(".")
            or branch.endswith(".lock")
            or branch in {".", "..", "@"}
            or "//" in branch
            or ".." in branch
            or "@{" in branch
            or any(char in branch for char in "~^:?*[\\")
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
        )
        if invalid_branch:
            raise ProviderUnavailable("provider binding branch is blocked_unknown")

        base_sha = values["base_sha"]
        if not isinstance(base_sha, str) or len(base_sha) != 40 or any(
            char not in "0123456789abcdefABCDEF" for char in base_sha
        ):
            raise ProviderUnavailable("provider binding base_sha is blocked_unknown")
        if not isinstance(runtime_worktree, (str, Path)):
            raise ProviderUnavailable("provider binding path is blocked_unknown")
        try:
            contract_worktree = Path(values["worktree"]).resolve()
            managed_root = Path(values["managed_root"]).resolve()
            if contract_worktree == Path(contract_worktree.anchor) or managed_root == Path(managed_root.anchor):
                raise ValueError
            if contract_worktree.exists() and not contract_worktree.is_dir():
                raise ValueError
            if managed_root.exists() and not managed_root.is_dir():
                raise ValueError
            contract_worktree.relative_to(managed_root)
            runtime = Path(runtime_worktree)
            if not runtime.is_absolute() or runtime.resolve() != contract_worktree:
                raise ValueError
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ProviderUnavailable("provider binding path is blocked_unknown")
        if "project" in contract and contract["project"] != project_id:
            raise ProviderUnavailable("provider binding alias is blocked_unknown")
        nested = contract.get("binding_contract")
        if nested is not None:
            if not isinstance(nested, dict):
                raise ProviderUnavailable("provider binding contract is blocked_unknown")
            for name in required:
                if name in nested and nested[name] != values[name]:
                    raise ProviderUnavailable("provider binding contract is blocked_unknown")

    @staticmethod
    def _request_envelope(contract, request):
        request = dict(request)
        for key in (
            "dispatcher", "complexity_band", "execution_engine", "engine_mode",
            "engine_evidence_ref", "authorization_digest", "plan_id", "plan_version",
        ):
            if key in contract:
                request[key] = contract[key]
        if contract.get("child_origin") == "worker_dispatch":
            request["origin"] = "worker_dispatch"
            request["child_binding"] = contract.get("child_binding")
        continuation_route = contract.get("continuation_route")
        if isinstance(continuation_route, dict):
            request["continuation_route"] = dict(continuation_route)
        return request

    def _action(self, contract, run_id, operation, request, sequence=0):
        request = self._request_envelope(contract, request)
        return self.store.request(
            operation=operation,
            provider=self.provider,
            run_id=run_id,
            issue_id=str(contract["node_id"]),
            role=str(contract["role"]),
            generation=int(contract["generation"]),
            native_tool=self._native_tool(operation),
            request=request,
            sequence=sequence,
        )

    @staticmethod
    def _routing_profile(contract: Dict[str, Any]) -> Optional[WorkerProfile]:
        """Validate the selected profile before any provider request."""
        raw = contract.get("worker_profile")
        child = contract.get("child_binding")
        child_raw = child.get("worker_profile") if isinstance(child, dict) else None
        if raw is None:
            raw = child_raw
        if raw is None:
            if contract.get("routing_required") is True or any(
                key in contract for key in ("issue_complexity", "model_probes", "models", "required_capabilities")
            ):
                raise ProviderUnavailable("worker model routing is missing")
            return None
        if isinstance(raw, WorkerProfile):
            profile = raw
        elif isinstance(raw, dict):
            try:
                profile = WorkerProfile.from_dict(raw)
            except (TypeError, ValueError) as error:
                raise ProviderUnavailable("worker model routing is invalid") from error
        else:
            raise ProviderUnavailable("worker model routing is invalid")
        if child_raw is not None and raw is not child_raw:
            try:
                child_profile = child_raw if isinstance(child_raw, WorkerProfile) else WorkerProfile.from_dict(child_raw)
            except (TypeError, ValueError) as error:
                raise ProviderUnavailable("child worker model routing is invalid") from error
            if child_profile != profile:
                raise ProviderUnavailable("worker model route conflicts with child binding")
        if isinstance(child, dict):
            for key, expected in (("model", profile.model), ("reasoning", profile.reasoning), ("route_digest", profile.route_digest)):
                if child.get(key) not in (None, "", expected):
                    raise ProviderUnavailable("worker model route conflicts with child binding")
        # A profile may only be used for the same Issue and child binding.
        basis_ref = profile.selection_basis.get("issue_complexity_ref")
        if basis_ref not in (None, contract.get("node_id")):
            raise ProviderUnavailable("worker model route Issue identity conflicts")
        return profile

    def _require_result(
        self,
        contract: Dict[str, Any],
        run_id: str,
        operation: str,
        request: Dict[str, Any],
    ) -> Dict[str, Any]:
        sequence = contract.get("_create_recovery_sequence", 0) if operation in {"create", "locate", "visibility"} else 0
        action = self._action(contract, run_id, operation, request, sequence=sequence)
        result = self.store.result(action["action_id"])
        if result is None:
            pending = ProviderPending(
                "provider %s action is pending" % operation
            )
            pending.action_id = action["action_id"]
            pending.request_digest = action["request_digest"]
            raise pending
        if result.get("status") in ("failed", "error", "stopped") or result.get("failed") is True:
            error = result.get("reason") or result.get("error") or "provider action failed"
            if isinstance(error, dict):
                error = error.get("message") or "provider action failed"
            unavailable = ProviderUnavailable(str(error))
            unavailable.observation = dict(result)
            raise unavailable
        return result

    def _parameter_recovery_contract(self, contract, run_id, request):
        # Only a confirmed rejected create permits a second create sequence.
        # Unknown outcomes and returned task/setup identities never do.
        contract = dict(contract)
        contract.pop("_create_recovery_sequence", None)
        options = contract.get("provider_options")
        if not isinstance(options, dict) or options.get("omit_thinking") is not True:
            return contract
        basis = {"provider": self.provider, "run_id": run_id,
            "issue_id": str(contract["node_id"]), "role": str(contract["role"]),
            "generation": int(contract["generation"]), "sequence": 0}
        original = self.store.related_action(basis, "create", required=False)
        if original is None:
            return contract  # First request may already omit the optional field.
        envelope = self._request_envelope(contract, request)
        previous = dict(original["request"])
        if previous == envelope:
            return contract
        if "thinking" not in previous:
            raise ValueError("provider parameter recovery request identity drift")
        previous.pop("thinking")
        if previous != envelope:
            raise ValueError("provider parameter recovery request identity drift")
        result = self.store.result(original["action_id"])
        from ..monitor import classify_provider_failure
        if (not isinstance(result, dict) or result.get("task_created") is not False
                or not (result.get("status") in ("failed", "error")
                    or (result.get("status") is None and result.get("failed") is True))
                or result.get("created") is True
                or classify_provider_failure(result)["kind"] != "retryable"
                or any(key in result for key in ("binding", "threadId", "taskId", "task_id",
                    "sessionId", "session_id", "platform_task_id", "clientThreadId", "client_thread_id"))):
            raise ProviderUnavailable("pending parameter recovery: requires confirmed create rejection without a task")
        contract["_create_recovery_sequence"] = 1
        return contract

    def _validate_v39_create_binding(
        self, contract: Dict[str, Any], runtime_worktree: Path
    ) -> None:
        """Fail closed before a V3.9 provider create request is emitted.

        The create probe is still an external side effect, so every target
        constraint must come from the supervisor-owned contract.  In
        particular, the runtime ``worktree`` argument is only compared with
        the contract value and is never used to fill a missing value.
        """
        required = ("project_id", "worktree", "managed_root", "branch", "base_sha")
        values = {name: contract.get(name) for name in required}
        if (
            not isinstance(values["project_id"], str)
            or not values["project_id"].strip()
            or "\x00" in values["project_id"]
        ):
            raise ProviderUnavailable("provider binding project_id is blocked_unknown")

        for name in ("worktree", "managed_root"):
            value = values[name]
            if not isinstance(value, str) or not value.strip():
                raise ProviderUnavailable("provider binding %s is blocked_unknown" % name)
            path = Path(value)
            if not path.is_absolute():
                raise ProviderUnavailable("provider binding %s is blocked_unknown" % name)
            if "\x00" in value:
                raise ProviderUnavailable("provider binding %s is blocked_unknown" % name)

        branch = values["branch"]
        invalid_branch = (
            not isinstance(branch, str)
            or not branch.strip()
            or branch != branch.strip()
            or "\x00" in branch
            or any(char.isspace() for char in branch)
            or Path(branch).is_absolute()
            or branch.startswith("-")
            or branch.endswith("/")
            or branch.endswith(".")
            or branch.startswith(".")
            or branch.endswith(".lock")
            or branch in {".", "..", "@"}
            or "//" in branch
            or ".." in branch
            or "@{" in branch
            or any(char in branch for char in "~^:?*[\\")
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
        )
        if invalid_branch:
            raise ProviderUnavailable("provider binding branch is blocked_unknown")

        base_sha = values["base_sha"]
        if not isinstance(base_sha, str) or len(base_sha) != 40 or any(
            char not in "0123456789abcdefABCDEF" for char in base_sha
        ):
            raise ProviderUnavailable("provider binding base_sha is blocked_unknown")
        if not isinstance(runtime_worktree, (str, Path)):
            raise ProviderUnavailable("provider binding path is blocked_unknown")

        try:
            contract_worktree = Path(values["worktree"]).resolve()
            managed_root = Path(values["managed_root"]).resolve()
            if contract_worktree == Path(contract_worktree.anchor):
                raise ValueError
            if managed_root == Path(managed_root.anchor):
                raise ValueError
            if contract_worktree.exists() and not contract_worktree.is_dir():
                raise ValueError
            if managed_root.exists() and not managed_root.is_dir():
                raise ValueError
            contract_worktree.relative_to(managed_root)
            runtime = Path(runtime_worktree)
            if not runtime.is_absolute() or runtime.resolve() != contract_worktree:
                raise ValueError
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ProviderUnavailable("provider binding path is blocked_unknown")

        # The runtime argument is an identity check only.  It is never used
        # to repair or populate a missing contract worktree.
        # Accept legacy aliases only when they agree with the canonical
        # supervisor fields; never promote an alias or nested provider value.
        aliases = {"project": "project_id"}
        for alias, canonical in aliases.items():
            if alias in contract and contract[alias] != values[canonical]:
                raise ProviderUnavailable("provider binding alias is blocked_unknown")
        nested = contract.get("binding_contract")
        if nested is not None:
            if not isinstance(nested, dict):
                raise ProviderUnavailable("provider binding contract is blocked_unknown")
            for name in ("project_id", "worktree", "managed_root", "branch", "base_sha"):
                if name in nested and nested[name] != values[name]:
                    raise ProviderUnavailable("provider binding contract is blocked_unknown")

    def task_binding(
        self,
        contract: Dict[str, Any],
        worktree: Path,
        run_id: str,
        status: str,
    ) -> TaskBinding:
        node_id = str(contract["node_id"])
        role = str(contract["role"])
        generation = int(contract["generation"])
        try:
            existing = load_task_binding(
                self.paths, node_id, role, run_id=run_id
            )
        except FileNotFoundError:
            existing = None
        successor = bool(contract.get("successor"))
        # #289: a successor created on this platform after a recorded
        # platform_handoff may follow a predecessor bound by another provider.
        # The monitor sets the flag only after proving the old task terminal;
        # the lineage must name that predecessor's provider exactly, and the
        # stopped-predecessor gate below still applies.
        handoff_lineage = (
            successor
            and contract.get("platform_handoff_successor") is True
            and existing is not None
            and contract.get("handoff_from_provider") == existing.provider
        )
        if existing is not None and existing.provider != self.provider and not handoff_lineage:
            raise ValueError(
                "task binding provider mismatch: explicit handoff required "
                "(after a platform handoff, resume through a successor task "
                "on the new platform instead of the old binding)"
            )
        v39 = binding_contract_enabled(contract)
        live_intent = contract.get("binding_intent")
        live_observation = contract.get("binding_observation")
        if v39:
            if not isinstance(live_intent, BindingIntent) or not isinstance(
                live_observation, BindingObservation
            ):
                # A task id is not known before provider create.  The only
                # permitted pre-create side effect is an explicitly-labelled
                # binding probe; it can never become a business write.
                if contract.get("binding_probe") is not True:
                    raise ProviderUnavailable("provider binding live evidence is missing")
            elif not runtime_binding_gate(contract).verified:
                raise ProviderUnavailable("provider binding preflight is blocked_unknown")
        if existing is not None and not successor:
            expected_allowlist = list(contract.get("files", []))
            if existing.allowlist != expected_allowlist:
                if (
                    not contract.get("continuation")
                    or not existing.allowlist
                    or not set(existing.allowlist).issubset(
                        set(expected_allowlist)
                    )
                ):
                    raise ValueError(
                        "provider continuation allowlist is not a narrow expansion"
                    )
                existing.allowlist = expected_allowlist
            existing.status = status
            existing.generation = generation
            if v39:
                existing.binding_intent = live_intent
                existing.binding_observation = live_observation
                verification = self.binding_gate(contract, existing)
                if not verification.verified:
                    raise ProviderUnavailable("provider continuation binding is blocked_unknown")
                existing.binding_state = verification.binding_state
                existing.business_write_allowed = verification.business_write_allowed
            return existing
        if existing is not None and existing.status not in {
            "stopped",
            "failed",
            "archived",
        }:
            raise ValueError("visible successor requires a stopped predecessor")
        if v39 and contract.get("binding_probe") is not True:
            raise ProviderUnavailable(
                "V3.9 provider create requires explicit binding_probe=true"
            )
        if v39:
            self._validate_v39_create_binding(contract, worktree)
        predecessor = contract.get("predecessor_task_id")
        if predecessor is None and existing is not None:
            predecessor = existing.task_id

        project_id = contract.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("visible provider contract requires project_id")
        if role in {"developer", "reviewer"}:
            # ISSUE-91 worker-push-delivery: the worker session ends by
            # self-reporting.  The signal content is never trusted -- the
            # monitor's existing gates still decide; without a push
            # capability this degrades to plain pull on the next resume.
            # Both commands run from the supervisor's project root: inside a
            # worktree the CLI would resolve that worktree's own .vibe and
            # the report would never reach the supervisor.
            root = shlex.quote(str(self.paths.root))
            if role == "reviewer":
                # ISSUE-249: pin the acceptance event value and the
                # top-level evidence carrier.  A generic "deliver JSON"
                # left the event value to the model, and an accepted
                # payload without the top-level evidence object used to
                # brick the node with no recoverable handling.
                event_pin = (
                    "你是 reviewer：全部 P0-P2 清零后的验收交付，payload 必须带顶层 "
                    "\"event\": \"accepted\" 与顶层 \"evidence\" 对象（记录本轮验收结论，"
                    "如 findings、覆盖情况、越界观察），缺任一字段 monitor 会按格式错误退回本会话重报；"
                    "仍有未清 P0-P2 发现时用顶层 \"event\": \"review_finding\" 携带 finding 上报，不得用 accepted。"
                )
            else:
                event_pin = ""
            head = "请执行 {} 任务，Issue {}。完工步骤（固定顺序，两条命令都必须带 --project-root 指向主项目目录，不要依赖当前目录，也不要在 worktree 里执行）：0) 先把**你本会话的工作目录**（会话环境给出的 cwd；project 挂载的会话在项目树或 worktree 内，projectless 会话在宿主生成的临时目录）原样写进交付 JSON 的 `delivery_evidence.session_cwd`——监工用它核对本会话是否真的挂载了 project，不填就无法核对；1) 先执行 `vibe worker-deliver --project-root {} --run-id {} --node {} --role {} --generation {} --payload '<交付 JSON>'` 自报落盘（交付 JSON 形状同交付事件：嵌套的 delivery_evidence 对象含 completion_marker、delivery_path、thread_status，visible-sdd 另带 in_session_review；{}格式错误会当场返回）；2) `vibe supervisor-address --project-root {} --run-id {} --node {} --role {} --generation {} --json` 查询监工地址与唤醒方式；3) 返回的 `wake.channel` 非空时，用该工具向 `wake.target` 原样发送 `wake.signal` 一行（Codex 为 `send_message_to_thread`，Claude Code 为 `SendMessage`）；`wake.channel` 为 null 或地址为 unknown 时不发，到此为止，监工心跳兜底拉取（没有登记推送通道的平台，如 WorkBuddy）。唤醒信号内容不影响验收。运行消费由登记主监工负责，worker 不得调用 core resume、reconcile 或消费 mailbox。".format(
                role, node_id, root, run_id, node_id, role, generation,
                event_pin,
                root, run_id, node_id, role, generation,
            )
        else:
            head = "请执行 {} 任务，Issue {}。".format(role, node_id)
        # Consumers split the prompt on the consistency marker and parse what
        # follows as one JSON document, so the consistency block always goes
        # last -- after any inlined protocol text.
        consistency = self._consistency_instruction(contract)
        prompt = head + self._remote_git_ban(contract) + consistency
        create_request = {
            "prompt": prompt,
            "target": {
                "type": "project",
                "projectId": project_id,
                "environment": {"type": "local"},
            },
        }
        profile = self._routing_profile(contract)
        if profile is not None:
            thinking = provider_thinking_for(profile.reasoning)
            create_request.update({
                "model": profile.model,
                "issue_id": node_id,
                "route_digest": profile.route_digest,
                "worker_profile": profile.to_dict(),
            })
            # Some native Codex App versions reject an otherwise valid
            # model/reasoning pair at request validation time.  The monitor's
            # same-task recovery records this option after the first real
            # rejection; omit only the optional thinking field and let the
            # provider choose its supported default.
            provider_options = contract.get("provider_options")
            if not (
                isinstance(provider_options, dict)
                and provider_options.get("omit_thinking") is True
            ):
                create_request["thinking"] = thinking
        topology = str(contract.get("topology") or DEFAULT_TOPOLOGY)
        if topology not in _TOPOLOGIES:
            raise ValueError("task binding topology is invalid")
        # Preflight the rulings this visible-only bridge can never satisfy
        # *before* the create side effect: a background ruling would
        # otherwise leak an armed, unregistered visible task on the desktop
        # (R3 P1-1), and a visible-sdd ruling without its protocol pointer
        # would arm a session that does not know the rules it must follow.
        if topology == "background":
            raise ValueError("background topology requires background mode")
        if topology == "visible-sdd":
            if role != "developer":
                raise ValueError("visible-sdd topology only binds a developer")
            sdd_protocol = contract.get("sdd_protocol")
            if not isinstance(sdd_protocol, str) or not sdd_protocol.strip():
                raise ValueError("visible-sdd dispatch requires an sdd_protocol pointer")
            # The desktop session servicing the mailbox must know it is
            # creating the single visible SDD worker session and which
            # protocol that session has to follow.  The pointer stays a
            # verbatim version reference; the protocol body is inlined
            # into the worker prompt so the session does not need the
            # repository to obey it.  A missing packaged protocol fails
            # closed here, before the create request leaves the mailbox.
            protocol_name = Path(sdd_protocol).stem
            if _sdd_protocol_delivery(self.paths.root) == "ref":
                # Ref mode: pin path + published sha256 instead of the full
                # text.  An unpublished/edited file raises here, before the
                # create request leaves the mailbox.
                digest = published_protocol_digest(protocol_name)
                protocol_block = (
                    "## 会话必须遵循的协议（按引用交付）\n\n"
                    "协议文件：{path}\nsha256：{digest}\n\n"
                    "开工前先读取该文件并计算 sha256（如 `shasum -a 256 <路径>`）；"
                    "与上面的 sha256 逐字相同才可遵守其全文。文件不存在、读取失败或摘要不符时，"
                    "不得按该协议工作、不得写任何文件，立即停止并向监工报告协议摘要校验失败。"
                ).format(path=protocol_path(protocol_name), digest=digest)
            else:
                protocol_block = (
                    "## 会话必须遵循的协议（随包全文，逐字执行）\n\n"
                    + load_protocol(protocol_name)
                )
            create_request["topology"] = topology
            create_request["sdd_protocol"] = sdd_protocol
            create_request["prompt"] = (
                head + self._remote_git_ban(contract) + "\n\n" + protocol_block + "\n\n" + consistency
            )
        if v39:
            # Keep the provider request bound to the same supervisor target
            # that will later be checked against live binding evidence.  These
            # are constraints, not evidence; the provider response can never
            # promote them to verified state by echoing them back.
            binding_contract = {
                "project_id": project_id,
                "worktree": contract.get("worktree"),
                "managed_root": contract.get("managed_root"),
                "branch": contract.get("branch"),
                "base_sha": contract.get("base_sha"),
            }
            create_request["target"]["binding_contract"] = binding_contract
            # Keep the original top-level shape for bridge consumers that
            # already inspect probe constraints there.
            create_request["binding"] = {
                key: binding_contract[key]
                for key in ("worktree", "managed_root", "branch", "base_sha")
            }
            create_request.update(
                {
                    "binding_probe": True,
                    "purpose": "binding_probe",
                    "business_write_allowed": False,
                }
            )
        if successor:
            create_request["successor"] = True
            create_request["predecessor_task_id"] = predecessor
            if handoff_lineage:
                # Durable lineage on the create action itself, so a later
                # false-start recovery can verify it without trusting state.
                create_request["handoff_from_provider"] = existing.provider
        contract = self._parameter_recovery_contract(contract, run_id, create_request)
        created = self._require_result(
            contract, run_id, "create", create_request
        )
        routed_profile = profile is not None and (
            contract.get("routing_required") is True
            or "selected_model" in profile.selection_basis
        )
        if routed_profile:
            # Native create responses from older Codex App versions may omit
            # these optional telemetry fields even though they return a
            # verified task binding.  The selected worker profile remains the
            # supervisor's routing authority; absence of telemetry must not
            # discard the task identity and strand the DAG.  If a provider
            # does return either field, keep the existing fail-closed checks
            # for malformed or conflicting telemetry.
            if "actual_model" in created:
                actual_model = created.get("actual_model")
                if (
                    not isinstance(actual_model, str)
                    or not actual_model.strip()
                    or actual_model.casefold() == "unknown"
                    or actual_model != profile.model
                ):
                    raise ProviderUnavailable("provider actual model is blocked_unknown")
            if "actual_thinking" in created:
                actual_thinking = created.get("actual_thinking")
                expected_thinking = create_request.get("thinking")
                if (
                    not isinstance(actual_thinking, str)
                    or not actual_thinking.strip()
                    or actual_thinking.casefold() == "unknown"
                    or (expected_thinking is not None and actual_thinking != expected_thinking)
                ):
                    raise ProviderUnavailable("provider actual thinking is blocked_unknown")
        binding_data = created.get("binding")
        if not isinstance(binding_data, dict):
            raise ValueError("provider create result has no verified binding")
        task_id = binding_data.get("threadId") or binding_data.get("task_id")
        host = binding_data.get("hostId") or binding_data.get("host")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("provider create result has no task identity")
        if not isinstance(host, str) or not host:
            raise ValueError("provider create result has no host identity")
        if successor and predecessor and task_id == predecessor:
            raise ValueError("provider successor reused predecessor identity")

        # Once create returns a provider task id, no further provider action
        # may run until supervisor-owned protected evidence binds that id.
        # A probe result is not permission to locate, enter, or write.
        if v39:
            if not isinstance(live_intent, BindingIntent) or not isinstance(
                live_observation, BindingObservation
            ):
                raise ProviderUnavailable("provider probe returned no live binding evidence")
            if live_intent.task_id != task_id or live_intent.host_id != host:
                raise ProviderUnavailable("provider probe identity is not supervisor verified")
            lease = read_writer_lease(self.paths, node_id, live_intent.worktree)
            if lease is None or live_observation.lease != lease:
                raise ProviderUnavailable("provider probe lease is not supervisor verified")
            if not runtime_binding_gate(contract).verified:
                raise ProviderUnavailable("provider probe binding is blocked_unknown")

        located = self._require_result(
            contract,
            run_id,
            "locate",
            {"threadId": task_id},
        )
        if located.get("located") is not True:
            raise ValueError("provider task cannot be located")
        visible = self._require_result(
            contract,
            run_id,
            "visibility",
            {
                "targets": [{"threadId": task_id, "hostId": host}],
                "timeoutMs": 0,
            },
        )
        if visible.get("visible") is not True or visible.get("direct_enter") is not True:
            raise ValueError("provider task visibility is not verified")
        binding = TaskBinding(
            provider=self.provider,
            mode="visible",
            issue_id=node_id,
            role=role,
            # Propagate the supervisor-ruled dispatch topology.  The
            # TaskBinding constructor is fail-closed: a background ruling
            # cannot be satisfied by this visible-only bridge, and a
            # visible-sdd ruling only binds a developer session.
            topology=str(contract.get("topology") or DEFAULT_TOPOLOGY),
            limitations=list(contract.get("dispatch_limitations", [])),
            task_id=task_id,
            host=host,
            worktree=str(contract.get("worktree") or worktree),
            branch=str(contract.get("branch", "")),
            status_file=str(contract.get("status_file", "")),
            handoff_file=str(contract.get("handoff_file", "")),
            threadId=task_id if self.provider == CODEX_PROVIDER else None,
            hostId=host if self.provider == CODEX_PROVIDER else None,
            run_id=run_id,
            status=status,
            visible=True,
            generation=generation,
            allowlist=list(contract.get("files", [])),
            capability_contract_digest=contract.get("capability_contract_digest"),
            successor_of=predecessor if successor else None,
            handoff_from_provider=existing.provider if handoff_lineage else None,
        )
        if profile is not None:
            binding.route_digest = profile.route_digest
            binding.model = profile.model
            binding.reasoning = profile.reasoning
        # Provider-returned host/project/lease/cursor and nested dictionaries
        # are self-report only.  Only supervisor-injected protected objects
        # from the contract can attest this final task binding.
        if v39:
            if not isinstance(live_intent, BindingIntent) or not isinstance(
                live_observation, BindingObservation
            ):
                raise ProviderUnavailable("provider task requires live binding evidence")
            if live_intent.task_id != task_id or live_intent.host_id != host:
                raise ProviderUnavailable("provider task identity disagrees with live evidence")
            for key, expected in (
                ("project_id", live_observation.project_id),
                ("managed_root", live_observation.managed_root),
                ("worktree", live_observation.worktree),
                ("branch", live_observation.branch),
                ("base_sha", live_observation.base_sha),
                ("head_sha", live_observation.head_sha),
                ("clean", live_observation.clean),
                ("cursor", live_observation.cursor),
            ):
                if key in binding_data and binding_data.get(key) != expected:
                    raise ProviderUnavailable("provider self-report conflicts with live evidence")
            binding.binding_intent = live_intent
            binding.binding_observation = live_observation
            verification = self.binding_gate(contract, binding)
            if not verification.verified:
                raise ProviderUnavailable("provider task binding is blocked_unknown")
            binding.binding_state = verification.binding_state
            binding.business_write_allowed = verification.business_write_allowed
        return binding

    @staticmethod
    def _handle_id(contract: Dict[str, Any], run_id: str) -> str:
        basis = "\0".join(
            (
                run_id,
                str(contract["node_id"]),
                str(contract["role"]),
                str(contract["generation"]),
            )
        )
        return "bridge-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]

    def _handle_path(self, handle_id: str) -> Path:
        directory = self.store._directory("handles")
        return directory / (handle_id + ".json")

    def binding_gate(self, contract: Dict[str, Any], binding: TaskBinding):
        """Run the V3.9 binding gate at the provider boundary."""
        verification = runtime_binding_gate(contract, binding)
        if not binding_contract_enabled(contract) or not verification.verified:
            return verification
        intent = binding.binding_intent
        observation = binding.binding_observation
        lease = read_writer_lease(self.paths, str(contract["node_id"]), intent.worktree)
        if lease is None or observation.lease != lease:
            return BindingVerification("blocked_unknown", False, [], ["lease"])
        return verification

    # Named recovery probes are intentionally idempotent and read-only.  A
    # timeout or malformed response therefore remains ``blocked_unknown``;
    # callers may retry the same task without allocating a successor writer.
    def provider_binding_probe(self, contract: Dict[str, Any], binding: TaskBinding):
        return self.binding_gate(contract, binding)

    def binding_bootstrap(self, contract: Dict[str, Any], binding: TaskBinding, worktree: Path):
        """Safely repair detached/wrong-branch metadata without content reset.

        Only a clean worktree at the requested base may be attached to a new
        node branch.  Existing branch occupancy, path drift, dirty content or
        a wrong base remain blocked; no reset/clean/stash/checkout is used.
        """
        if binding.provider != self.provider:
            raise ValueError(
                "task binding provider mismatch: explicit handoff required "
                "(after a platform handoff, resume through a successor task "
                "on the new platform instead of the old binding)"
            )
        if not binding_contract_enabled(contract):
            return self.binding_gate(contract, binding)

        def blocked(reason: str):
            return BindingVerification("blocked_unknown", False, [reason], [])

        def read_git(root_path: Path, target_branch: str):
            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(root_path), *args],
                    check=False,
                    capture_output=True,
                    text=True,
                )

            commands = {
                "head": ("rev-parse", "HEAD"),
                "status": ("status", "--porcelain", "--untracked-files=all"),
                "symbolic": ("symbolic-ref", "--short", "-q", "HEAD"),
                "ref": ("show-ref", "--verify", "--quiet", "refs/heads/" + target_branch),
                "worktrees": ("worktree", "list", "--porcelain"),
            }
            result = {name: git(*args) for name, args in commands.items()}
            if result["head"].returncode != 0 or result["status"].returncode != 0:
                return None
            if result["symbolic"].returncode not in (0, 1):
                return None
            if result["ref"].returncode not in (0, 1):
                return None
            if result["worktrees"].returncode != 0:
                return None
            return result

        def worktree_has_root(text: str, root_path: Path) -> bool:
            expected = "worktree " + str(root_path)
            return any(line.strip() == expected for line in text.splitlines())

        def worktree_owns_branch(text: str, root_path: Path, target_branch: str) -> bool:
            expected_root = "worktree " + str(root_path)
            expected_branch = "branch refs/heads/" + target_branch
            for block in text.strip().split("\n\n"):
                lines = {line.strip() for line in block.splitlines()}
                if expected_root in lines and expected_branch in lines:
                    return True
            return False
        root = Path(worktree)
        if isinstance(contract.get("binding_intent"), BindingIntent) and isinstance(
            contract.get("binding_observation"), BindingObservation
        ):
            binding.binding_intent = contract["binding_intent"]
            binding.binding_observation = contract["binding_observation"]
        live_intent = binding.binding_intent
        managed_root = contract.get("managed_root") or (
            live_intent.managed_root if isinstance(live_intent, BindingIntent) else None
        )
        branch = contract.get("branch") or binding.branch
        base_sha = contract.get("base_sha")
        if not isinstance(managed_root, str) or not isinstance(branch, str) or not isinstance(base_sha, str):
            return blocked("git_binding_contract")
        try:
            root = root.resolve(strict=True)
            managed = Path(managed_root).resolve(strict=True)
            root.relative_to(managed)
            if binding.worktree:
                bound_worktree = Path(binding.worktree)
                if not bound_worktree.is_absolute():
                    bound_worktree = self.paths.root / bound_worktree
                if bound_worktree.resolve() != root:
                    return blocked("worktree")
            node_id = str(contract.get("node_id") or binding.issue_id)
            run_id = str(contract.get("run_id") or binding.run_id or "")
            if not run_id or not binding.task_id or not binding.host:
                return blocked("task_identity")
            lease = read_writer_lease(self.paths, node_id, str(root))
            if lease is None:
                return blocked("lease")
            state = read_git(root, branch)
            if state is None:
                return blocked("git_observation")
            head = state["head"]
            status = state["status"]
            current = state["symbolic"]
            refs = state["ref"]
            occupied = state["worktrees"]
            if status.stdout:
                return blocked("dirty")
            if head.stdout.strip().lower() != base_sha.lower():
                return blocked("base_sha")
            branch_marker = "branch refs/heads/" + branch
            if not worktree_has_root(occupied.stdout, root):
                return blocked("worktree_root")
            if current.stdout.strip() == branch and not worktree_owns_branch(
                occupied.stdout, root, branch
            ):
                return blocked("worktree_branch")
            if branch_marker in occupied.stdout and current.stdout.strip() != branch:
                return blocked("branch_occupied")
            if refs.returncode == 0:
                # A branch already present may belong to another worktree;
                # never steal or rewrite it.
                if current.stdout.strip() == branch:
                    pass
                elif branch_marker in occupied.stdout:
                    return blocked("branch_occupied")
                else:
                    return blocked("branch_exists")
            elif current.stdout.strip() != branch:
                switched = subprocess.run(
                    ["git", "-C", str(root), "switch", "-c", branch, base_sha],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if switched.returncode != 0:
                    return blocked("git_switch")
                # Never reuse pre-switch stdout.  All binding fields are
                # re-read after the metadata mutation before re-gating.
                state = read_git(root, branch)
                if state is None or state["status"].stdout:
                    return blocked("post_read")
                if state["head"].stdout.strip().lower() != base_sha.lower():
                    return blocked("post_base_sha")
                if state["symbolic"].stdout.strip() != branch:
                    return blocked("post_branch")
                if not worktree_owns_branch(state["worktrees"].stdout, root, branch):
                    return blocked("post_worktree")
                head = state["head"]
            has_provenance = isinstance(binding.binding_intent, BindingIntent) and isinstance(
                binding.binding_observation, BindingObservation
            )
            if not has_provenance:
                project_id = contract.get("project_id")
                if not isinstance(project_id, str) or not project_id:
                    return blocked("project_id")
                wait_request = {
                    "purpose": "binding_probe",
                    "business_write_allowed": False,
                    "targets": [
                        {"threadId": binding.task_id, "hostId": binding.host}
                    ],
                    "timeoutMs": 0,
                }
                try:
                    action = self._action(
                        contract, run_id, "wait", wait_request, sequence=0
                    )
                    result = self.store.result(action["action_id"])
                except (OSError, TypeError, ValueError) as error:
                    return blocked("wait_result")
                if result is None:
                    raise ProviderPending("binding recovery wait probe is pending")
                if not isinstance(result, dict):
                    return blocked("wait_result")
                polls = result.get("polls")
                candidates = []
                if isinstance(polls, list):
                    candidates.extend(item for item in polls if isinstance(item, dict))
                poll = result.get("poll")
                if isinstance(poll, dict):
                    candidates.append(poll)
                matched = None
                for candidate in reversed(candidates):
                    thread = candidate.get("thread")
                    if not isinstance(thread, dict):
                        continue
                    if thread.get("id") != binding.task_id:
                        continue
                    if thread.get("hostId") != binding.host:
                        continue
                    cursor = candidate.get("cursor")
                    if isinstance(cursor, str) and cursor:
                        matched = (candidate, cursor)
                        break
                if matched is None:
                    return blocked("wait_result")
                _poll, cursor = matched
                try:
                    # The wait result is a separate observation boundary.  Do
                    # not construct evidence from the pre-wait Git stdout;
                    # re-read every Git field before binding the cursor.
                    state = read_git(root, branch)
                    if state is None or state["status"].stdout:
                        return blocked("post_read")
                    if state["head"].stdout.strip().lower() != base_sha.lower():
                        return blocked("post_base_sha")
                    if not worktree_has_root(state["worktrees"].stdout, root):
                        return blocked("post_worktree")
                    if state["symbolic"].stdout.strip() != branch:
                        return blocked("post_branch")
                    if not worktree_owns_branch(
                        state["worktrees"].stdout, root, branch
                    ):
                        return blocked("post_worktree")
                    head = state["head"]
                    cursor_observation = WaitThreadsCursorObservation.from_wait_threads(
                        binding.task_id, binding.host, cursor
                    )
                    fresh_lease = read_writer_lease(self.paths, node_id, str(root))
                    if fresh_lease is None or fresh_lease != lease:
                        return blocked("lease")
                    live_head = head.stdout.strip().lower()
                    contract_conflicts = []
                    expected_head = contract.get("head_sha")
                    if expected_head not in (None, "") and expected_head != live_head:
                        contract_conflicts.append("head_sha")
                    expected_clean = contract.get("clean")
                    if expected_clean is not None and expected_clean is not True:
                        contract_conflicts.append("clean")
                    expected_cursor = contract.get("cursor")
                    if expected_cursor not in (None, "") and expected_cursor != cursor:
                        contract_conflicts.append("cursor")
                    expected_lease = contract.get("lease")
                    if expected_lease not in (None, ""):
                        live_lease = fresh_lease.to_dict()
                        if hasattr(expected_lease, "to_dict"):
                            expected_lease = expected_lease.to_dict()
                        if expected_lease != live_lease:
                            contract_conflicts.append("lease")
                    if contract_conflicts:
                        return BindingVerification(
                            "blocked_unknown", False, [], contract_conflicts
                        )
                    intent = BindingIntent(
                        project_id=project_id,
                        task_id=binding.task_id,
                        host_id=binding.host,
                        node_id=node_id,
                        worktree=str(root),
                        managed_root=str(managed),
                        branch=branch,
                        base_sha=base_sha,
                        head_sha=live_head,
                        clean=True,
                        lease_id=fresh_lease.lease_id,
                        cursor=cursor,
                    )
                    observation = BindingObservation(
                        project_id=project_id,
                        task_id=binding.task_id,
                        host_id=binding.host,
                        node_id=node_id,
                        worktree=str(root),
                        managed_root=str(managed),
                        branch=branch,
                        base_sha=base_sha,
                        head_sha=live_head,
                        clean=True,
                        lease=fresh_lease,
                        cursor=cursor,
                        source="codex_app__wait_threads",
                        cursor_source="codex_app__wait_threads",
                        cursor_task_id=binding.task_id,
                        cursor_host_id=binding.host,
                        cursor_lineage=cursor_observation.lineage,
                        cursor_observation=cursor_observation,
                    )
                except (TypeError, ValueError):
                    return blocked("wait_result")
                binding.binding_intent = intent
                binding.binding_observation = observation
                binding.cursor = cursor
                contract = dict(contract)
                contract.update(
                    {
                        "binding_intent": intent,
                        "binding_observation": observation,
                        "managed_root": str(managed),
                        "worktree": str(root),
                        "branch": branch,
                        "base_sha": base_sha,
                        "head_sha": live_head,
                        "clean": True,
                        "lease": fresh_lease,
                        "cursor": cursor,
                    }
                )
            if isinstance(binding.binding_intent, BindingIntent) and isinstance(
                binding.binding_observation, BindingObservation
            ):
                # Refresh immutable Git metadata after a safe branch attach;
                # lease/cursor provenance is either the current protected
                # evidence above or the protected evidence supplied by the
                # already-verified contract.
                binding.binding_intent = replace(
                    binding.binding_intent, branch=branch, worktree=str(root), base_sha=base_sha
                )
                binding.binding_observation = replace(
                    binding.binding_observation,
                    branch=branch,
                    worktree=str(root),
                    base_sha=base_sha,
                    head_sha=head.stdout.strip().lower(),
                )
                contract = dict(contract)
                contract["binding_intent"] = binding.binding_intent
                contract["binding_observation"] = binding.binding_observation
        except (OSError, ValueError, subprocess.SubprocessError):
            return blocked("git_observation")
        verification = runtime_binding_gate(contract, binding)
        if not verification.verified:
            return verification
        return verification

    def start(self, contract: dict, worktree: Path) -> RunHandle:
        # Reject direct complex starts before task-binding reads or Provider I/O.
        require_complex_monitor_dispatch(contract, self.paths)
        run_id = str(contract.get("run_id", ""))
        if not run_id:
            raise ValueError("provider runner requires run_id")
        handle = RunHandle(self._handle_id(contract, run_id))
        binding = load_task_binding(
            self.paths,
            str(contract["node_id"]),
            str(contract["role"]),
            run_id=run_id,
        )
        if binding.provider != self.provider:
            raise ValueError(
                "task binding provider mismatch: explicit handoff required "
                "(after a platform handoff, resume through a successor task "
                "on the new platform instead of the old binding)"
            )
        expected_capability_digest = contract.get("capability_contract_digest")
        if expected_capability_digest and binding.capability_contract_digest != expected_capability_digest:
            raise ValueError("task binding capability contract digest is stale")
        if contract.get("successor") is True:
            predecessor = contract.get("predecessor_task_id")
            if predecessor and binding.task_id == predecessor:
                raise ValueError("successor task identity reused the predecessor")
        if binding_contract_enabled(contract):
            live_intent = contract.get("binding_intent")
            live_observation = contract.get("binding_observation")
            if isinstance(live_intent, BindingIntent) and isinstance(
                live_observation, BindingObservation
            ):
                binding.binding_intent = live_intent
                binding.binding_observation = live_observation
        profile = self._routing_profile(contract)
        if profile is not None:
            for field_name, expected in (
                ("route_digest", profile.route_digest),
                ("model", profile.model),
                ("reasoning", profile.reasoning),
            ):
                current_value = getattr(binding, field_name, None)
                if contract.get("continuation") and current_value not in (None, ""):
                    # The persisted binding is the verified record of the
                    # route that created this task; resuming it must not be
                    # gated by a stale plan-side profile projection.
                    continue
                if current_value not in (None, "", expected):
                    raise ProviderUnavailable("task binding worker model route is stale")
        verification = self.binding_gate(contract, binding)
        if binding_contract_enabled(contract) and not verification.verified:
            raise ProviderUnavailable(
                "provider binding gate blocked_unknown: missing={} conflicts={}".format(
                    ",".join(verification.missing), ",".join(verification.conflicts)
                )
            )
        metadata = {
            "schema_version": 1,
            "handle_id": handle.run_id,
            "run_id": run_id,
            "node_id": str(contract["node_id"]),
            "role": str(contract["role"]),
            "task_id": binding.task_id,
            "host": binding.host,
            "generation": int(contract["generation"]),
            "pending_action": None,
            "pending_operation": None,
            "wait_sequence": 0,
            "cursor": binding.cursor,
            "terminal_confirmed": False,
            "successor": bool(contract.get("successor")),
            "predecessor_task_id": contract.get("predecessor_task_id"),
            "capability_contract_digest": expected_capability_digest,
            "dispatcher": contract.get("dispatcher"),
            "complexity_band": contract.get("complexity_band"),
            "execution_engine": contract.get("execution_engine"),
            "engine_mode": contract.get("engine_mode"),
            "engine_evidence_ref": contract.get("engine_evidence_ref"),
            "authorization_digest": contract.get("authorization_digest"),
            "plan_id": contract.get("plan_id"),
            "plan_version": contract.get("plan_version"),
        }
        route_model = profile.model if profile is not None else None
        route_reasoning = profile.reasoning if profile is not None else None
        route_digest = profile.route_digest if profile is not None else None
        if contract.get("continuation"):
            # For continuation the registered binding route is authoritative;
            # the contract profile is only a stale plan projection there.
            route_model = binding.model or route_model
            route_reasoning = binding.reasoning or route_reasoning
            route_digest = binding.route_digest or route_digest
            # ``child_binding`` remains the creation-time authorization and
            # workspace proof required by ProviderActionStore.  It must not be
            # rewritten with a later route: its WorkerProfile digest is tied
            # to its original selection basis.  Carry the verified persisted
            # route in a separate continuation field so the native request
            # has an unambiguous current route without invalidating the child
            # proof or inventing a new binding identity.
            if route_model and route_reasoning and route_digest:
                contract = dict(contract)
                contract["continuation_route"] = {
                    "model": route_model,
                    "reasoning": route_reasoning,
                    "route_digest": route_digest,
                }
        if route_model is not None:
            metadata.update(
                {
                    "model": route_model,
                    "reasoning": route_reasoning,
                    "thinking": provider_thinking_for(route_reasoning),
                    "route_digest": route_digest,
                }
            )
        if contract.get("continuation"):
            request = {
                "threadId": binding.task_id,
                "hostId": binding.host,
                "prompt": self._continuation_prompt(contract, self.paths.root) + self._remote_git_ban(contract) + "\n" + self._consistency_instruction(contract),
            }
            if route_model is not None:
                request.update({"model": route_model, "thinking": provider_thinking_for(route_reasoning)})
            action = self._action(contract, run_id, "resume", request)
            metadata["pending_action"] = action["action_id"]
            metadata["pending_operation"] = "resume"
        self.store._atomic(self._handle_path(handle.run_id), metadata)
        return handle

    def send_notice(self, handle: RunHandle, message: str) -> None:
        """Queue a fire-and-forget message to the bound thread (advisory).

        Uses its own ``resume`` action sequence and leaves ``pending_action``
        untouched, so the wait/poll state machine is unaffected.
        """
        metadata = self.store._read(self._handle_path(handle.run_id))
        sequence = int(metadata.get("notice_sequence", 0)) + 1
        contract = {
            key: metadata.get(key)
            for key in (
                "node_id", "role", "generation", "dispatcher", "complexity_band",
                "execution_engine", "engine_mode", "engine_evidence_ref",
                "authorization_digest", "plan_id", "plan_version",
            )
            if metadata.get(key) is not None
        }
        self._action(
            contract,
            metadata["run_id"],
            "resume",
            {"threadId": metadata["task_id"], "hostId": metadata["host"], "prompt": message},
            sequence=sequence,
        )
        metadata["notice_sequence"] = sequence
        self.store._atomic(self._handle_path(handle.run_id), metadata)

    def is_pending(self, handle: RunHandle) -> bool:
        metadata = self.store._read(self._handle_path(handle.run_id))
        action_id = metadata.get("pending_action")
        return bool(action_id and self.store.result(action_id) is None)

    def poll(self, handle: RunHandle):
        metadata = self.store._read(self._handle_path(handle.run_id))
        claims = {
            "node_id": metadata["node_id"],
            "role": metadata["role"],
            "task_id": metadata["task_id"],
            "handle_id": handle.run_id,
            "generation": metadata["generation"],
        }
        pending_action = metadata.get("pending_action")
        if pending_action:
            result = self.store.result(pending_action)
            if result is None:
                operation = str(metadata.get("pending_operation") or "action")
                return [
                    RunEvent(
                        "visibility_unknown",
                        {**claims, "reason": "provider %s is pending" % operation},
                    )
                ]
            operation = metadata.get("pending_operation")
            if operation == "wait":
                return self._wait_result(handle, metadata, claims, result)
            if operation != "resume" or result.get("resumed") is not True:
                return [
                    RunEvent(
                        "visibility_unknown",
                        {**claims, "reason": "provider resume is unverified"},
                    )
                ]
            metadata["pending_action"] = None
            metadata["pending_operation"] = None
            self.store._atomic(self._handle_path(handle.run_id), metadata)

        contract = {
            "node_id": metadata["node_id"],
            "role": metadata["role"],
            "generation": metadata["generation"],
            "dispatcher": metadata.get("dispatcher"),
            "complexity_band": metadata.get("complexity_band"),
            "execution_engine": metadata.get("execution_engine"),
            "engine_mode": metadata.get("engine_mode"),
            "engine_evidence_ref": metadata.get("engine_evidence_ref"),
            "authorization_digest": metadata.get("authorization_digest"),
            "plan_id": metadata.get("plan_id"),
            "plan_version": metadata.get("plan_version"),
        }
        wait_request = {
            "targets": [
                {
                    "threadId": metadata["task_id"],
                    "hostId": metadata["host"],
                }
            ],
            "timeoutMs": 0,
        }
        cursor = metadata.get("cursor")
        if isinstance(cursor, str) and cursor:
            wait_request["targets"][0]["afterCursor"] = cursor
        sequence = int(metadata.get("wait_sequence", 0)) + 1
        action = self._action(
            contract,
            metadata["run_id"],
            "wait",
            wait_request,
            sequence=sequence,
        )
        metadata["wait_sequence"] = sequence
        metadata["pending_action"] = action["action_id"]
        metadata["pending_operation"] = "wait"
        self.store._atomic(self._handle_path(handle.run_id), metadata)
        result = self.store.result(action["action_id"])
        if result is None:
            return [
                RunEvent(
                    "visibility_unknown",
                    {**claims, "reason": "provider wait is pending"},
                )
            ]
        return self._wait_result(handle, metadata, claims, result)

    def _wait_result(
        self,
        handle: RunHandle,
        metadata: Dict[str, Any],
        claims: Dict[str, Any],
        result: Dict[str, Any],
    ):
        status = result.get("status")
        cursor = result.get("cursor")
        if cursor is not None and not cursor_is_valid(cursor):
            return [
                RunEvent(
                    "visibility_unknown",
                    {**claims, "reason": "provider cursor is invalid"},
                )
            ]
        if cursor is not None:
            binding = load_task_binding(
                self.paths,
                metadata["node_id"],
                metadata["role"],
                run_id=metadata["run_id"],
            )
            binding.cursor = cursor
            save_task_binding(self.paths, binding)
            metadata["cursor"] = cursor
        metadata["pending_action"] = None
        metadata["pending_operation"] = None
        self.store._atomic(self._handle_path(handle.run_id), metadata)
        if status in WAIT_TRANSIENT_STATUSES:
            return [RunEvent("visibility_unknown", {**claims, "reason": str(status)})]
        event_name = result.get("event")
        if event_name is None and status in WAIT_STATUS_AS_EVENT:
            event_name = status
        if event_name not in WAIT_EVENT_NAMES:
            return [
                RunEvent(
                    "visibility_unknown",
                    {**claims, "reason": "provider event is unsupported"},
                )
            ]
        if status not in WAIT_TERMINAL_STATUSES:
            return [
                RunEvent(
                    "visibility_unknown",
                    {**claims, "reason": "provider terminal state is unverified"},
                )
            ]
        metadata["terminal_confirmed"] = True
        self.store._atomic(self._handle_path(handle.run_id), metadata)
        data = dict(claims)
        if event_name in {"failed", "stopped"}:
            # Native terminal errors must reach the monitor's recovery
            # classifier, including explicit authorization boundaries.
            for key in ("error", "reason", "reason_class", "credential_required", "login_required", "permission_required"):
                if key in result:
                    data[key] = result[key]
            if not data.get("reason"):
                error = result.get("error")
                data["reason"] = (error.get("message", event_name) if isinstance(error, dict)
                                  else str(error or event_name))
        for key in RESULT_EVENT_KEYS:
            if key in result:
                data[key] = result[key]
        for key in ADVISORY_EVENT_KEYS:
            notes = _advisory_notes(result.get(key))
            if notes is not None:
                data[key] = notes
        return [RunEvent(str(event_name), data)]

    def stop(self, handle: RunHandle) -> None:
        metadata = self.store._read(self._handle_path(handle.run_id))
        if metadata.get("terminal_confirmed") is True:
            return
        pending_action = metadata.get("pending_action")
        if not isinstance(pending_action, str) or not pending_action:
            raise ProviderPending(
                "provider stop requires a pending terminal wait reconciliation"
            )
        if metadata.get("pending_operation") != "wait":
            raise ProviderPending(
                "provider stop cannot reconcile a non-terminal provider action"
            )
        result = self.store.result(pending_action)
        if result is None:
            raise ProviderPending("provider terminal wait is still pending")
        if result.get("status") not in {"stopped", "complete", "completed"} or result.get(
            "event"
        ) != "stopped":
            raise ProviderPending("provider terminal wait did not prove a stop")
        cursor = result.get("cursor")
        if cursor is not None and not cursor_is_valid(cursor):
            raise ProviderPending("provider terminal wait cursor is invalid")
        claims = {
            "node_id": metadata["node_id"],
            "role": metadata["role"],
            "task_id": metadata["task_id"],
            "handle_id": handle.run_id,
            "generation": metadata["generation"],
        }
        events = self._wait_result(handle, metadata, claims, result)
        if len(events) != 1 or events[0].event != "stopped":
            raise ProviderPending("provider terminal wait did not prove a stop")


def archive_run_provider_actions(
    paths: ProjectPaths, run_id: str, target_root: Path
) -> list:
    """Sweep one run's provider-action artifacts into ``target_root``.

    Every mailbox file the run owns (requests, results, stale markers,
    observations, deliveries, dispatch heads, prompts, handles and
    claude-bg session records) is moved under
    ``<target_root>/provider-actions/<subdir>/`` so a dead run's requests no
    longer surface in :meth:`ProviderActionStore.pending`.  Advisory lock
    files stay in place -- they are crash leftovers, not evidence.  Idempotent:
    files already archived are matched again through the archived requests.
    Returns the list of archived-relative paths moved this call.
    """
    from ..state import validate_run_id

    validate_run_id(run_id)
    if target_root.is_symlink():
        raise ValueError("run archive target may not be a symlink")
    store = ProviderActionStore(paths)
    root = store.root
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("provider action path may not be a symlink")

    def _payload(path: Path) -> Optional[Dict[str, Any]]:
        if path.is_symlink() or not path.is_file():
            return None
        try:
            return store._read(path)
        except (OSError, ValueError):
            return None

    action_ids = set()
    moved = []

    def _collect_requests(directory: Path) -> None:
        if not directory.is_dir() or directory.is_symlink():
            return
        for path in sorted(directory.glob("action-*.json")):
            payload = _payload(path)
            if payload is None:
                continue
            if payload.get("run_id") != run_id:
                continue
            action_id = payload.get("action_id") or path.stem
            if isinstance(action_id, str) and action_id:
                action_ids.add(action_id)
            action_ids.add(path.stem)
            destination = target_root / "provider-actions" / "requests" / path.name
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(path), str(destination))
            moved.append("provider-actions/requests/" + path.name)

    # Requests already archived by an earlier crashed sweep still name this
    # run's action ids for the result/observation match below.
    _collect_requests(target_root / "provider-actions" / "requests")
    _collect_requests(root / "requests")

    def _owns(path: Path, payload: Optional[Dict[str, Any]]) -> bool:
        if payload is not None:
            if payload.get("run_id") == run_id:
                return True
            action_id = payload.get("action_id")
            if isinstance(action_id, str) and action_id in action_ids:
                return True
            claims = payload.get("claims")
            if isinstance(claims, dict) and claims.get("run_id") == run_id:
                return True
        stem = path.name
        if stem.startswith(run_id + "-"):
            return True
        return any(stem == item or stem.startswith(item + "-")
                   for item in action_ids)

    for name in ("results", "stale", "observations", "deliveries",
                 "dispatch-heads", "prompts", "handles", "claude-bg-sessions"):
        directory = root / name
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.is_symlink():
                continue
            payload = _payload(path)
            if not _owns(path, payload):
                continue
            destination = target_root / "provider-actions" / name / path.name
            if destination.exists():
                if destination.is_file() and destination.read_bytes() == path.read_bytes():
                    path.unlink()
                    continue
                raise ValueError(
                    "archive provider action destination already exists: " + path.name
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(path), str(destination))
            moved.append("provider-actions/" + name + "/" + path.name)
    return moved
