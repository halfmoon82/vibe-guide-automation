"""Durable provider-neutral runner backed by desktop action requests/results."""

from dataclasses import replace
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
import shlex
from pathlib import Path
import subprocess
import unicodedata
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
from ..authorization import worker_push_ban_text
from ..evidence import TEMPLATE_PLACEHOLDER, holds_placeholder
from ..paths import ProjectPaths
from ..protocols import load_protocol, protocol_path, published_protocol_digest
from ..task_registry import (
    DEFAULT_TOPOLOGY,
    _TOPOLOGIES,
    TaskBinding,
    binding_contract_enabled,
    load_task_binding,
    runtime_binding_gate,
    protected_retirement_allows,
    protected_writer_fence,
    _PROTECTED_RETIREMENT,
    save_task_binding,
)
from ..workflow_gate import session_contract_prompt
from ..prompts import auto_scope_rule_sentence, rework_findings_section
from ..worker_questions import supervisor_answer_section
from ..state import read_writer_lease
from ..providers import (
    CLAUDE_CODE_PROVIDER,
    CODEX_PROVIDER,
    WORKBUDDY_VISIBLE_PROVIDER,
)

_RECEIPT_CALLER = ContextVar("provider_receipt_caller", default=None)


def _supervisor_owner_projection(record):
    """Keep only stable lease identity fields for initial create binding."""
    if not isinstance(record, dict):
        return None
    projection = {key: record.get(key) for key in ("run_id", "lease_epoch", "owner", "released")}
    if projection.get("lease_epoch") == 0 and projection.get("owner") is None:
        return None
    return projection


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
    "thread_status", "in_session_review", "business_acceptance",
    # Integration-review routing: which accepted upstream node a finding
    # belongs to, the scope it asks for, and the defect files.
    "target_node_id", "scope_expansion", "files",
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
#: A failed create is stored whole and handed to the failure classifier, and the
#: parameter-recovery gate requires ``task_created is False`` in it, so the
#: structured fields those readers consume must pass the write-time check.
_CREATE_FAILURE_FIELDS = frozenset(
    ("failed", "status", "reason", "error", "reason_class", "task_created",
     "credential_required", "login_required", "permission_required")
)
#: The readers judge these by truthiness / ``is False``, so a string such as
#: ``"false"`` would read as true (or never match) and misroute the node.
_CREATE_FAILURE_FLAGS = ("task_created", "credential_required", "login_required", "permission_required")
#: Mirrors the recovery classes ``monitor.classify_provider_failure`` honours;
#: any other value is silently ignored there.
_FAILURE_REASON_CLASSES = frozenset(("engineering", "retryable", "capacity_wait", "repairable"))
#: The value ``--template`` prints where the supervisor must substitute a real
#: identity.  A result that still carries it is refused: storing it would bind
#: a task that does not exist, and a stored result cannot be rewritten.
#: Only the keys the create reader actually binds on; the shape check used to
#: accept ``thread_id`` / ``clientThreadId``, which the reader then refused.
_BINDING_IDENTITY_FIELDS = ("threadId", "task_id")
_RESUME_RESULT_FIELDS = frozenset((RESUME_RECEIPT_FIELD, "status"))


_holds_placeholder = holds_placeholder


def _placeholder_paths(value, path=""):
    """Dotted paths of every string leaf that still holds the placeholder.

    Blank text is judged only where a field requires a value (identities,
    reasons); here only the visible ``<填写>`` marker counts, so an optional
    empty string is not reported.
    """
    found = []
    if isinstance(value, str):
        if value.strip() and _holds_placeholder(value):
            found.append(path)
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_placeholder_paths(item, "{}.{}".format(path, key) if path else str(key)))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_placeholder_paths(item, "{}[{}]".format(path, index)))
    return found


#: The one table the mailbox template, the write-time check, the error hint and
#: the ``prd-guide`` §6.2 table all read: operation x role x branch -> the
#: example result.  ``role`` is ``None`` when the shape does not depend on it.
#: Every ``PLACEHOLDER`` stands where the supervisor must put a real value; a
#: result that keeps one is refused.  Changing a row changes all four at once
#: (tests/test_mailbox_shape_table.py pins that).
PLACEHOLDER = TEMPLATE_PLACEHOLDER
MAILBOX_FIELD_TABLE = (
    ("create", None, "ok", {"binding": {"threadId": PLACEHOLDER, "hostId": PLACEHOLDER}},
     "没有真实会话 id 就不回写；`task_id`/`host` 同样接受"),
    ("create", None, "failed", {"failed": True, "reason": PLACEHOLDER},
     "没能创建任务时如实说明原因"),
    ("locate", None, "ok", {"located": True}, "宿主原始输出 `{\"navigated\": true}` 不被接受"),
    ("locate", None, "failed", {"failed": True, "reason": PLACEHOLDER},
     "没能定位到任务时如实说明原因"),
    ("visibility", None, "ok", {"visible": True, "direct_enter": True}, "两个标志都必须为 true"),
    ("visibility", None, "failed", {"failed": True, "reason": PLACEHOLDER},
     "没能观察到任务时如实说明原因"),
    ("wait", None, "pending", {"status": "timeout"},
     "未完；`cursor` 给就必须是非空字符串（≤4096、无 NUL），不知道就不给"),
    ("wait", "developer", "complete",
     {"status": "completed", "event": "complete", "cursor": PLACEHOLDER,
      "delivery_evidence": {"completion_marker": PLACEHOLDER, "delivery_path": PLACEHOLDER,
                            "thread_status": "complete"}},
     "（嵌套对象，三键齐全，摊平无效）；`cursor` 填真实值（servicer 路径的交付门读它），不知道就整个删掉，别留占位符"),
    ("wait", "reviewer", "complete",
     {"status": "completed", "event": "accepted", "cursor": PLACEHOLDER,
      "evidence": {"summary": PLACEHOLDER}},
     "P0–P2 清零证据；`cursor` 填真实值，不知道就整个删掉，别留占位符"),
    ("resume", None, "ok", {"resumed": True}, "沿用原会话 id，不回写绑定"),
)


def mailbox_row(operation, role, branch):
    """The table row for ``operation`` x ``role`` x ``branch``, or None."""
    for row in MAILBOX_FIELD_TABLE:
        if row[0] == operation and row[1] in (None, role) and row[2] == branch:
            return row
    return None


def mailbox_example(operation, role=None, branch="ok"):
    """A deep copy of the example result; ``branch`` defaults to the success one."""
    row = mailbox_row(operation, role, branch)
    if row is None and branch == "ok":
        # wait has no "ok": its success example is the role's completion.
        row = mailbox_row(operation, role, "complete")
    if row is None:
        return None
    return json.loads(json.dumps(row[3], ensure_ascii=False))


#: Markers around the generated table in ``prd-guide.md`` §6.2.
DOC_TABLE_BEGIN = "<!-- mailbox-table:begin -->"
DOC_TABLE_END = "<!-- mailbox-table:end -->"
_BRANCH_LABEL = {"ok": "", "failed": "（失败）", "pending": "（未完）", "complete": "（完）"}


def render_doc_table():
    """The prd-guide §6.2 table, generated from ``MAILBOX_FIELD_TABLE``."""
    lines = ["| 操作 | 角色 | payload | 说明 |", "|---|---|---|---|"]
    for operation, role, branch, example, note in MAILBOX_FIELD_TABLE:
        lines.append("| `{}`{} | {} | `{}` | {} |".format(
            operation, _BRANCH_LABEL.get(branch, branch), role or "任意",
            json.dumps(example, ensure_ascii=False, sort_keys=True), note))
    return "\n".join(lines)


def _example_text(example):
    return json.dumps(example, ensure_ascii=False, sort_keys=True)


def _hint(operation, role, branch=None, wait_status=None):
    """``expected:`` tail naming the shape to copy, drawn from the table."""
    if branch is None:
        branch = "complete" if operation == "wait" else "ok"
    if operation == "wait" and branch == "complete" and wait_status in WAIT_TRANSIENT_STATUSES:
        branch = "pending"
    example = mailbox_example(operation, role, branch)
    if example is None:
        return ""
    text = "; expected shape ({}): {}".format(branch, _example_text(example))
    if branch not in ("failed",) and mailbox_row(operation, role, "failed") is not None:
        text += "; if the action could not be done: " + _example_text(
            mailbox_example(operation, role, "failed"))
    return text


def _coded(error, reason_code):
    """Attach the independent ``reason_code`` the monitor records for ``error``.

    ``observation`` is the channel ``Monitor._dispatch_with_intent`` already
    reads a provider failure's fixed reason code from.
    """
    error.reason_code = reason_code
    if reason_code is not None:
        error.observation = {"reason_code": reason_code}
    return error


def _identity_fields(operation, payload):
    """The fields whose value must be a real fact, not a template stand-in.

    A task identity, host identity or failure reason that kept ``<填写>`` is
    stored and then acted on (a binding to a task that does not exist, a
    reason that says nothing).  Evidence text in a ``wait`` result is judged by
    the delivery gate, not here.
    """
    found = {}
    binding = payload.get("binding")
    if operation == "create" and isinstance(binding, dict):
        found["binding"] = binding
    if _reports_failure(payload):
        for key in ("reason", "error"):
            if key in payload:
                found[key] = payload[key]
    return found


def _delivery_evidence_placeholders(operation, role, payload):
    """Paths of the three delivery keys that still hold the template marker.

    Only a developer's delivered/complete wait result carries them; the same
    predicate the delivery gate applies (``evidence.holds_placeholder``).
    """
    evidence = payload.get("delivery_evidence")
    if not (operation == "wait" and role == "developer" and isinstance(evidence, dict)
            and payload.get("event") in ("delivered", "complete")):
        return []
    return [
        "delivery_evidence." + key
        for key in ("completion_marker", "delivery_path", "thread_status")
        if isinstance(evidence.get(key), str) and evidence[key].strip() and holds_placeholder(evidence[key])
    ]


def _shape_errors(required, allowed, payload):
    errors = []
    missing = sorted(key for key in required if key not in payload)
    extra = sorted(key for key in payload if key not in allowed)
    if missing:
        errors.append("missing fields: " + ", ".join(missing))
    if extra:
        errors.append("unexpected fields: " + ", ".join(extra))
    return errors


def _reports_failure(payload):
    return payload.get("failed") is True or payload.get("status") in ("failed", "error", "stopped")


def _create_shape_errors(payload):
    if _reports_failure(payload):
        # The reader turns a failed create into ProviderUnavailable before it
        # looks for a binding, so a supervisor that could not create the task
        # must be able to say so with a reason.
        errors = _shape_errors([], _CREATE_FAILURE_FIELDS, payload)
        for flag in _CREATE_FAILURE_FLAGS:
            if flag in payload and not isinstance(payload[flag], bool):
                errors.append("field {} must be true or false".format(flag))
        reason_class = payload.get("reason_class")
        # Type first: a list or dict is unhashable, so the set lookup would
        # raise TypeError instead of naming the field.
        if "reason_class" in payload and not (
            isinstance(reason_class, str) and reason_class in _FAILURE_REASON_CLASSES
        ):
            errors.append("field reason_class must be one of: " + ", ".join(sorted(_FAILURE_REASON_CLASSES)))
        reason = payload.get("reason") or payload.get("error")
        if not (isinstance(reason, str) and reason.strip()):
            errors.append("field reason is required when the action failed")
        return errors
    errors = _shape_errors(["binding"], _CREATE_RESULT_FIELDS, payload)
    binding = payload.get("binding")
    if "binding" not in payload:
        return errors
    if not isinstance(binding, dict):
        errors.append("field binding needs a task identity: " + " or ".join(_BINDING_IDENTITY_FIELDS))
        return errors
    # Judge the value the reader binds on (first truthy key), not "any key is
    # valid": a valid second key must not hide a bad first one.
    task_id = binding.get("threadId") or binding.get("task_id")
    host = binding.get("hostId") or binding.get("host")
    if not (isinstance(task_id, str) and task_id.strip()):
        errors.append("field binding needs a task identity: " + " or ".join(_BINDING_IDENTITY_FIELDS))
    # The dispatcher binds on a host identity as well; without it the result is
    # stored and then refused as ``dispatch_contract_invalid``, and a consumed
    # result cannot be rewritten.
    if not (isinstance(host, str) and host.strip()):
        errors.append("field binding needs a host identity: hostId or host")
    return errors


def validate_result_shape(operation, payload, *, role=None):
    """Reject a mailbox result the reader would not accept, naming the fields.

    Mirrors the rules the runner applies when it consumes the result, so a
    wrong shape fails here, before the write, instead of becoming a
    ``blocked_unknown`` after it.  ``role`` is the request's role: a
    developer's ``delivered``/``complete`` wait result must carry
    ``delivery_evidence``.  Raises ``ValueError``; returns None.
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
        errors = _create_shape_errors(payload)
    elif operation in ("locate", "visibility"):
        # Both readers test the flag itself (``located is True`` /
        # ``visible is True and direct_enter is True``); a template carrying
        # ``status`` instead failed only after the task already existed.  A
        # supervisor that genuinely could not observe the task must still be
        # able to say so: the reader turns ``failed``/``status=error`` into a
        # provider error instead of a visibility claim.
        if _reports_failure(payload):
            reason = payload.get("reason") or payload.get("error")
            if not (isinstance(reason, str) and reason.strip()):
                errors.append("field reason is required when the action failed")
        else:
            required_flags = ("located",) if operation == "locate" else ("visible", "direct_enter")
            missing = [name for name in required_flags if payload.get(name) is not True]
            if missing:
                errors.append(
                    "field(s) must be true: " + ", ".join(missing)
                    + ", or report failed/reason when the action could not be observed"
                )
            if operation == "locate" and "navigated" in payload:
                # The host's raw navigation output; the reader never looks at it.
                errors.append(
                    'field navigated is the host\'s raw output, not the result: '
                    'write {"located": true} instead'
                )
    elif operation == "resume":
        errors = _shape_errors([RESUME_RECEIPT_FIELD], _RESUME_RESULT_FIELDS, payload)
        if RESUME_RECEIPT_FIELD in payload and payload[RESUME_RECEIPT_FIELD] is not True:
            errors.append("field {} must be true".format(RESUME_RECEIPT_FIELD))
    if operation in ("wait", "create"):
        # A cursor is stored and later compared (cursor-conflict check), so a
        # blank or placeholder one is a fake position, not an optional blank.
        cursors = [("cursor", payload["cursor"])] if "cursor" in payload else []
        binding = payload.get("binding")
        if operation == "create" and isinstance(binding, dict) and "cursor" in binding:
            cursors.append(("binding.cursor", binding["cursor"]))
        for path, value in cursors:
            if _holds_placeholder(value):
                errors.append(
                    "field {} is blank or still holds the template placeholder; "
                    "substitute the real cursor or omit the field".format(path)
                )
    for path in _placeholder_paths(_identity_fields(operation, payload)) + _delivery_evidence_placeholders(operation, role, payload):
        errors.append(
            "field {} still holds the template placeholder; substitute the real value".format(path)
        )
    if errors:
        branch = "failed" if operation in ("create", "locate", "visibility") and _reports_failure(payload) else None
        raise ValueError(
            "{} result shape invalid: {}{}".format(
                operation, "; ".join(errors), _hint(operation, role, branch, payload.get("status"))
            )
        )


def _native_wait_projection(action, structured):
    """Keep target-bound transport observations, never infer business events."""
    def unknown(reason):
        return {"status": "unknown", "reason": "native_wait:" + reason}
    targets = action["request"].get("targets")
    if not isinstance(targets, list) or len(targets) != 1 or not isinstance(targets[0], dict):
        return unknown("single_target_required")
    target = targets[0]
    if any(not isinstance(target.get(key), str) or not target[key].strip()
           for key in ("threadId", "hostId")):
        return unknown("target_identity_required")
    if any(key in structured and structured[key] is not False for key in ("partial", "hasMore")) or structured.get("nextCursor"):
        return unknown("incomplete_page")
    errors = structured.get("errors", [])
    if not isinstance(errors, list) or errors:
        return unknown("target_error_or_unverified_errors")
    polls = structured.get("polls")
    if not isinstance(polls, list) or len(polls) != 1 or not isinstance(polls[0], dict):
        return unknown("single_poll_required")
    poll = polls[0]
    thread = poll.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != target["threadId"] or thread.get("hostId") != target["hostId"]:
        return unknown("target_mismatch")
    cursor = poll.get("cursor")
    if not cursor_is_valid(cursor) or not cursor.strip():
        return unknown("cursor_missing_or_invalid")
    if "timedOut" in structured and type(structured["timedOut"]) is not bool:
        return unknown("timeout_invalid")
    # Preserve only structural observation facts. Assistant prose/tool markers
    # stay in the full raw envelope and cannot become delivery/acceptance.
    safe_poll = {"thread": {"id": thread["id"], "hostId": thread["hostId"]}, "cursor": cursor}
    return {"status": "timeout" if structured.get("timedOut") is True else "unknown",
            "reason": "native_wait:business_event_unproven", "cursor": cursor, "polls": [safe_poll]}


def _native_response_body(response):
    """Accept one unambiguous native JSON body across both public carriers."""
    candidates = []
    if isinstance(response.get("structuredContent"), dict):
        candidates.append(response["structuredContent"])
    content = response.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            try:
                item = json.loads(block.get("text", ""))
            except (ValueError, TypeError):
                continue
            if isinstance(item, dict):
                candidates.append(item)
    if not any(key in response for key in ("content", "structuredContent", "isError")):
        candidates.append(response)
    if response.get("isError") is True:
        return None
    return candidates[0] if candidates and all(item == candidates[0] for item in candidates) else None


def _native_response_projection(action, response):
    """One ingress interpretation shared by ordinary and protected consumers."""
    structured = _native_response_body(response)
    projection = {"status": "unknown"}
    if response.get("isError") is True:
        projection["reason"] = "native_response:tool_error"
    elif structured is None:
        projection["reason"] = "native_response:conflicting_structures"
    elif action["operation"] in ("locate", "visibility", "resume"):
        projection["reason"] = {
            "locate": "native_locate:target_navigation_evidence_missing",
            "visibility": "native_visibility:visible_direct_enter_capability_evidence_missing",
            "resume": "native_resume:acceptance_not_execution_evidence",
        }[action["operation"]]
    if structured is not None and response.get("isError") is not True and action["operation"] == "create":
        task_id, host = structured.get("threadId"), structured.get("hostId")
        status = structured.get("status")
        status_ready = ("status" not in structured
                        or status in ("ready", "completed")
                        or (isinstance(status, dict) and status.get("type") == "active"))
        if (status_ready
                and "clientThreadId" not in structured
                and isinstance(task_id, str) and task_id.strip()
                and isinstance(host, str) and host.strip()):
            projection = {"binding": {"threadId": task_id, "hostId": host}}
    elif response.get("isError") is not True and structured:
        operation = action["operation"]
        if operation == "resume" and structured.get("resumed") is True:
            projection = {"resumed": True}
        elif operation == "locate" and (structured.get("located") is True or (
                action.get("provider") == CODEX_PROVIDER
                and action.get("native_tool") == NATIVE_TOOL_MAP[CODEX_PROVIDER]["locate"]
                and isinstance(action["request"].get("threadId"), str)
                and action["request"]["threadId"].strip()
                and structured == {"navigated": True})):
            projection = {"located": True}
        elif operation == "visibility" and all(structured.get(key) is True for key in ("visible", "direct_enter")):
            projection = {"visible": True, "direct_enter": True}
        elif operation == "wait" and any(key in structured for key in ("polls", "errors", "timedOut")):
            projection = _native_wait_projection(action, structured)
        elif operation == "wait":
            candidate = {key: value for key, value in structured.items() if key in _WAIT_RESULT_FIELDS}
            try:
                validate_result_shape("wait", candidate, role=action.get("role"))
            except (TypeError, ValueError):
                pass
            else:
                projection = candidate
    return projection


def _native_initial_visibility_projection(store, action, response):
    """Compose native initial visibility; this never asserts a business turn."""
    from ..capability_contract import capability_status

    def receipt(item):
        result = store.result(item["action_id"])
        raw = store._read(store._native_receipt_path(item["action_id"]))
        envelope = raw.get("native_response")
        if not isinstance(envelope, dict) or result != _native_response_projection(item, envelope):
            raise ValueError("native visibility source is not replayable")
        return result, _native_response_body(envelope), {
            "action_id": item["action_id"], "request_digest": item["request_digest"],
            "receipt_digest": raw["receipt_digest"],
        }

    unknown = {"status": "unknown", "reason": "native_visibility:initial_evidence_incomplete"}
    try:
        if (action["provider"] != CODEX_PROVIDER or action["operation"] != "visibility"
                or action["native_tool"] != NATIVE_TOOL_MAP[CODEX_PROVIDER]["visibility"]):
            return unknown
        body = _native_response_body(response)
        poll = _native_wait_projection(action, body or {}).get("polls", [])
        if len(poll) != 1:
            return unknown
        task, host = poll[0]["thread"]["id"], poll[0]["thread"]["hostId"]
        create = store.related_action(action, "create")
        created, create_body, create_ref = receipt(create)
        if created != {"binding": {"threadId": task, "hostId": host}} or not create_body:
            return unknown
        target = create["request"].get("target", {})
        if not isinstance(target, dict) or not isinstance(target.get("binding_contract"), dict):
            return unknown
        project = target.get("projectId")
        if (create["native_tool"] != NATIVE_TOOL_MAP[CODEX_PROVIDER]["create"]
                or target.get("type") != "project" or not isinstance(project, str) or not project.strip()
                or target.get("environment") != {"type": "local"}
                or target["binding_contract"].get("project_id") != project
                or create_body.get("mode", "visible") != "visible"
                or create_body.get("visible", True) is not True
                or create_body.get("projectId", project) != project):
            return unknown
        source = create["request"].get("initial_binding_source")
        if not isinstance(source, dict) or not source:
            return unknown
        approved = source.get("consistency_binding")
        if not isinstance(approved, dict) or not approved.get("authorization_digest"):
            return unknown
        wait_basis = dict(action, sequence=0)
        wait = store.related_action(wait_basis, "wait")
        _, _, wait_ref = receipt(wait)
        initial_poll = native_wait_observation(store, wait)
        if (wait["native_tool"] != NATIVE_TOOL_MAP[CODEX_PROVIDER]["wait"]
                or wait["request"].get("initial_binding_source") != source
                or initial_poll["thread"] != poll[0]["thread"]):
            return unknown
        locate = store.related_action(action, "locate")
        located, locate_body, locate_ref = receipt(locate)
        if (locate["native_tool"] != NATIVE_TOOL_MAP[CODEX_PROVIDER]["locate"]
                or locate["request"].get("threadId") != task
                or located != {"located": True} or locate_body != {"navigated": True}):
            return unknown
        # Inspect only transport identity/visibility metadata, never assistant
        # prose or business payloads. Absence is allowed; a contradiction is not.
        identities = {
            "threadId": task, "thread_id": task, "taskId": task,
            "task_id": task, "platform_task_id": task,
            "hostId": host, "host": host, "host_id": host,
            "projectId": project, "project_id": project,
            "run_id": action["run_id"], "runId": action["run_id"],
            "node_id": action["issue_id"], "issue_id": action["issue_id"],
            "nodeId": action["issue_id"], "issueId": action["issue_id"],
            "role": action["role"], "generation": action["generation"],
        }

        def agrees(metadata, kind="envelope"):
            if not isinstance(metadata, dict):
                return False
            if (_reports_failure(metadata) or metadata.get("mode", "visible") != "visible"
                    or metadata.get("background", False) is not False):
                return False
            for key in ("visible", "direct_enter", "directEnter", "located", "navigated"):
                if key in metadata and metadata[key] is not True:
                    return False
            for key, expected in identities.items():
                if key in metadata and (type(metadata[key]) is not type(expected) or metadata[key] != expected):
                    return False
            if kind in ("thread", "task", "binding", "identity", "target", "project") and "id" in metadata:
                if metadata["id"] != (project if kind == "project" else task):
                    return False
            for key in ("binding", "thread", "task", "project", "identity", "metadata", "target"):
                if key in metadata and not agrees(metadata[key], key):
                    return False
            if "polls" in metadata:
                if not isinstance(metadata["polls"], list) or any(
                        not agrees(item, "poll") for item in metadata["polls"]):
                    return False
            return True

        for native_body in (create_body, locate_body, _native_response_body(response),
                            _native_response_body(store._read(store._native_receipt_path(
                                wait["action_id"]))["native_response"])):
            if not agrees(native_body):
                return unknown
        capability = load_contract(store.paths)
        digest = capability.contract_digest
        if (capability.provider not in ("codex", CODEX_PROVIDER) or capability.host_id != host
                or any(item["request"].get("capability_contract_digest") != digest
                       or item["request"].get("authorization_digest") != approved["authorization_digest"]
                       for item in (create, wait, locate, action))):
            return unknown
        refs = {}
        for name, operation in (("create", "create"), ("enter", "locate"),
                                ("resume", "resume"), ("wait", "wait")):
            key = "codex.visible_task." + name
            fact = capability.capabilities.get(key)
            if (capability_status(capability, key) != "verified_available" or fact is None
                    or fact.route != NATIVE_TOOL_MAP[CODEX_PROVIDER][operation]
                    or not fact.evidence_ref.strip()):
                return unknown
            refs[key] = fact.evidence_ref
        return {"visible": True, "direct_enter": True, "native_visibility_evidence": {
            "create": create_ref, "initial_wait": wait_ref, "locate": locate_ref,
            "capability_contract_digest": digest, "evidence_refs": refs}}
    except (KeyError, OSError, TypeError, ValueError, ProviderUnavailable):
        return unknown


def native_wait_observation(store, action):
    """Revalidate the durable native projection, never pick facts from rejected raw."""
    result = store.result(action["action_id"])
    raw = store._read(store._native_receipt_path(action["action_id"]))
    response = raw.get("native_response")
    if action.get("operation") != "wait" or not isinstance(response, dict):
        raise ProviderUnavailable("protected successor requires complete native wait receipt")
    projection = _native_response_projection(action, response)
    polls = projection.get("polls")
    if (result != projection or not isinstance(polls, list) or len(polls) != 1
            or not projection.get("cursor") or polls[0].get("cursor") != projection["cursor"]):
        raise ProviderUnavailable("protected successor native wait observation is unknown")
    return polls[0]


def complete_native_mailbox_action(paths, run_id, action_id, response, *, request_digest):
    """Persist full native envelope; transport acceptance is not execution."""
    store = ProviderActionStore(paths)
    action = store.action(action_id)
    if action.get("run_id") != run_id or request_digest != action["request_digest"]:
        raise ValueError("native response request binding mismatch")
    if not isinstance(response, dict):
        raise ValueError("native response must be an object")
    projection = _native_response_projection(action, response)
    if action.get("operation") == "visibility" and "polls" in (_native_response_body(response) or {}):
        projection = _native_initial_visibility_projection(store, action, response)
    return store.complete(action_id, projection, request_digest=request_digest,
                          native_response=response)


def _revalidate_native_visibility(store, action, result):
    """A saved positive projection cannot outlive its evidence or capabilities."""
    path = store._native_receipt_path(action["action_id"])
    if not path.exists():
        if "native_visibility_evidence" in result:
            raise ProviderUnavailable("native visibility raw evidence missing")
        return
    raw = store._read(path)
    response = raw.get("native_response")
    if isinstance(response, dict) and ("polls" in (_native_response_body(response) or {})
            or "native_visibility_evidence" in result):
        projected = _native_initial_visibility_projection(store, action, response)
        if projected != result or projected.get("visible") is not True:
            raise ProviderUnavailable("native visibility combination is blocked_unknown")


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


def _blocked_guidance():
    reference = protocol_path("blocked-engineering-examples")
    return ("工程阻塞时沿本合同 worker-deliver 的原 run/node/role/generation 上报，"
            'payload 示例：{"event":"blocked_engineering","status":"blocked",'
            '"implementation_complete":false,"failed_step":"实际失败步骤",'
            '"attempted_actions":[{"action":"已试动作","result":"实际结果"}],'
            '"missing_conditions":["具体缺口"],"evidence_refs":["真实证据引用"]}。'
            "不伪造 complete/accepted/delivery_evidence，不自行增加代次或扩大范围；"
            "上报后仍按 supervisor-address 返回值唤醒原监工。六角色阶段实例：" + str(reference))


class ProviderActionRunner(Runner):
    """Expose a public CLI runner while the App owns native tool execution."""

    def __init__(
        self,
        paths: ProjectPaths,
        adapter_id: str,
        provider: str,
        launchers=None,
    ):
        self.paths = paths
        self.adapter_id = adapter_id
        self.provider = provider
        self.store = ProviderActionStore(paths)
        #: Optional CLI-owned report collector.  Legacy direct callers retain
        #: their old behavior; public dispatch always supplies this list.
        self._worktree_launchers = launchers

    def _native_tool(self, operation: str) -> str:
        tools = NATIVE_TOOL_MAP.get(self.provider)
        if tools is None:
            # A placeholder name would sit in the mailbox forever; only
            # providers with a verified native control plane may dispatch.
            raise ProviderUnavailable(
                "no verified native control plane for provider {}".format(self.provider)
            )
        return tools[operation]

    def _prepare_visible_worktree(self, contract: Dict[str, Any], worktree: Path) -> None:
        """Use the background launcher's safe worktree preflight before create."""
        from ..adapters.claude_bg import ClaudeBgConfig, ClaudeBgLauncher
        from ..adapters.task_provider import BackgroundTaskRouting

        node_id = str(contract.get("node_id") or "")
        fallback = "provider-actions/deliveries/{}-{}.json".format(
            contract.get("run_id"), node_id
        )
        routing = BackgroundTaskRouting(
            host=str(contract.get("host") or "local"),
            worktree=str(worktree),
            branch=str(contract.get("branch") or ""),
            status_file=str(contract.get("status_file") or fallback),
            handoff_file=str(contract.get("handoff_file") or fallback),
        )
        upstream = contract.get("upstream_branches")
        launcher = ClaudeBgLauncher(
            routing,
            ClaudeBgConfig(settings_path=""),
            base_sha=contract.get("base_sha"),
            **({"upstream_branches": list(upstream)} if upstream else {}),
        )
        # Only the current project's own .git makes it vibe-git-managed.
        # An outer repository must not capture a nested legacy provider-owned
        # project merely because it is an ancestor of the target worktree.
        managed_root = Path(contract.get("managed_root") or self.paths.root)
        if not (managed_root / ".git").exists():
            return
        self._worktree_launchers.append((node_id, launcher))
        launcher._prepare_worktree()

    def _remote_git_ban(self, contract: Dict[str, Any]) -> str:
        """The fixed ban line: remote-Git ban under ``deny``, push ban otherwise (#379)."""
        return "\n" + worker_push_ban_text(self.paths, contract) + "\n"

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
        return _blocked_guidance() + "\n" + capability + "\n" + consistency

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
        return _blocked_guidance() + "\n" + (
            "执行请求：继续 Issue {node} 的{purpose}本轮 generation {generation}。\n"
            "目标：{objective}；业务合同：{business}。"
            "入口规则或后续规则注入不能替代该任务目标，不得仅回复收到规则。\n"
            "合同：{spec}；writer worktree：{worktree}；branch：{branch}；允许路径：{files}。\n"
            "当前 developer 交付：{delivery}；状态文件：{status}；交付文件：{handoff}；完成 marker：{marker}。"
            "完成后必须写入本代状态/交付；在主项目目录执行 `vibe worker-deliver{root_flag} --run-id {run} --node {node} --role {role} --generation {generation} --payload '<交付 JSON>'`。"
            "{acceptance_pin}"
            "随后执行 `vibe supervisor-address{root_flag} --run-id {run} --node {node} --role {role} --generation {generation} --json`；"
            "`wake.channel` 非空时用该工具向 `wake.target` 原样发送 `wake.signal`，唤醒登记主监工；为 null 或地址 unknown 时不发，由心跳领取。"
            "任何结束回合之前（交付、交付被拒、向监工提问、被阻塞）都要执行这条 supervisor-address 并按返回发信号；"
            "worker-deliver 拒收时返回值已带 `wake` 指令，同样照发。发信号失败（工具报错或收不到回应）时不要重试等待：用同一份交付 JSON 在顶层加 `\"wake_failed\": true` 再执行一次 worker-deliver，守护程序下一周期会据此接收。"
            "自报时把你本会话的工作目录原样写进交付 JSON 的 `delivery_evidence.session_cwd`：监工用它核对本会话是否真的挂载了 project，不填就无法核对。"
            "运行消费由登记主监工负责，worker 不得调用 core resume、reconcile 或消费 mailbox。\n"
            "{auto_scope}"
            "决策边界：不改变 PRD 决策的工程问题（实现方案、测试取舍、基线）自行判断并决定，"
            "把理由写进交付，不用 AskUserQuestion，不停下等人；"
            "需要改允许路径外、且不在自动放行范围内的文件时，不要改这些文件，也不要停下等人：先完成其余部分并正常交付，"
            "在交付说明里写清要改哪些文件、为什么，范围由监工和审查员裁决。"
            "只有会改变 PRD 决策、涉及部署发布、凭据或授权卡外不可撤销动作、新增第三方依赖、"
            "可见降级为后台不可见时，才在交付里附 decision_requests；"
            "唯一例外：跨平台交接把可见改成后台只披露、不算请求。\n"
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
                 auto_scope="" if reviewer else auto_scope_rule_sentence(project_root) + "\n",
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
                raise _coded(
                    ProviderUnavailable("provider binding %s is blocked_unknown" % name),
                    "worktree_missing" if name == "worktree" else None,
                )
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
            "capability_contract_digest",
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
        with protected_writer_fence():
            if contract.get("_initial_binding_source") and operation in {"locate", "visibility"}:
                intent = contract.get("binding_intent")
                if not isinstance(intent, BindingIntent):
                    raise ProviderUnavailable("initial binding action has no live intent")
                fresh_intent, fresh_observation = self._initial_live_binding_evidence(
                    contract, run_id, intent.task_id, intent.host_id, Path(intent.worktree))
                if (fresh_intent != intent or fresh_observation != contract.get("binding_observation")
                        or not runtime_binding_gate(contract).verified):
                    raise ProviderUnavailable("initial binding action observation changed")
            if _PROTECTED_RETIREMENT.get() is not None or (contract.get("successor") and binding_contract_enabled(contract)):
                if not protected_retirement_allows(None, None, contract=contract, runner=self):
                    raise ProviderUnavailable("protected successor scope or writer lease changed")
                if operation in {"locate", "visibility"} and not runtime_binding_gate(contract).verified:
                    raise ProviderUnavailable("protected successor action evidence changed")
            return self._action_owned(contract, run_id, operation, request, sequence)

    def _action_owned(self, contract, run_id, operation, request, sequence=0):
        request = self._request_envelope(contract, request)
        initial_anchor_sequence = None
        if (operation == "create" and sequence == 0 and not binding_contract_enabled(contract)
                and not contract.get("successor") and not contract.get("continuation")
                and request.get("origin") == "worker_dispatch" and request.get("plan_id")):
            # The monitor calls this under the supervisor lock, before the
            # mailbox makes a create visible. Pin the complete canonical
            # request in the run's existing hash chain, never after visibility.
            from ..adapters.task_provider import _canonical_digest
            from ..state import append_event, interprocess_lock, load_events, verify_run_evidence, _lease_lock, retirement_writer_lease
            from ..supervisor import SupervisorLease, _read_lease_record
            basis = {"operation": operation, "provider": self.provider, "run_id": run_id,
                     "issue_id": str(contract["node_id"]), "role": str(contract["role"]),
                     "generation": int(contract["generation"]), "sequence": sequence}
            action_id = "action-" + _canonical_digest(basis)[:32]
            full_native_tool = self._native_tool(operation)
            payload = {"schema_version": self.store.schema_version, "action_id": action_id,
                       **basis, "native_tool": "controlled_create", "request": {
                           "controlled": True,
                           "executable_digest": _canonical_digest({
                               "native_tool": full_native_tool, "request": request})}}
            request_digest = _canonical_digest(payload)
            payload["request_digest"] = request_digest
            raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            lease_path = SupervisorLease(self.paths).path(run_id)
            owner = _read_lease_record(lease_path)
            owner_digest = _canonical_digest(_supervisor_owner_projection(owner))
            snapshot = verify_run_evidence(self.paths, run_id)
            with interprocess_lock(_lease_lock(self.paths)):
                held = retirement_writer_lease(self.paths, basis["issue_id"], str(contract["worktree"]), run_id)
                if (held is None or held["record"].get("role") != basis["role"]
                        or held["record"].get("generation") != basis["generation"]):
                    raise ProviderUnavailable("initial create writer lease is unknown")
                anchor = {"run_id": run_id, "node_id": basis["issue_id"], "role": basis["role"],
                          "generation": basis["generation"], "action_id": action_id,
                          "request_digest": request_digest, "authorization_digest": request.get("authorization_digest"),
                          "node_contract_digest": snapshot.node_contract_digest,
                          "capability_contract_digest": request.get("capability_contract_digest"),
                          "intent_digest": held["record"].get("intent_digest"), "binding_digest": owner_digest,
                          "controlled_payload_digest": self.store._controlled_payload_digest(
                              payload, {"native_tool": full_native_tool, "request": request}),
                          "proof": {"request_sha256": hashlib.sha256(raw).hexdigest()}}
                events = load_events(self.paths, run_id)
                previous = [row for row in events if row.get("event") == "initial_create_anchored"
                            and row.get("data", {}).get("action_id") == action_id]
                path = self.store.root / "requests" / (action_id + ".json")
                if previous:
                    previous_data = previous[0].get("data", {}) if len(previous) == 1 else {}
                    comparable_anchor = dict(anchor)
                    if "controlled_payload_digest" not in previous_data:
                        comparable_anchor.pop("controlled_payload_digest", None)
                    if len(previous) != 1 or previous_data != comparable_anchor:
                        raise ProviderUnavailable("initial create anchor differs")
                    if path.exists() or path.is_symlink():
                        if (self.store.action(action_id).get("request_digest") != request_digest
                                or path.read_bytes() != raw):
                            raise ProviderUnavailable("initial create request differs from anchor")
                    else:
                        # A prior visible request may have been removed; an
                        # anchor alone cannot prove that it was never read.
                        raise ProviderUnavailable("initial create request missing after anchor")
                else:
                    if path.exists() or path.is_symlink():
                        if self.store.action(action_id) != payload or path.read_bytes() != raw:
                            raise ProviderUnavailable("legacy initial create identity differs")
                    else:
                        initial_anchor_sequence = append_event(self.paths, RunEvent("initial_create_anchored", anchor))
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
            _initial_anchor_sequence=initial_anchor_sequence,
            _controlled_create=(operation == "create" and sequence == 0
                                and not binding_contract_enabled(contract)
                                and not contract.get("successor")
                                and not contract.get("continuation")
                                and request.get("origin") == "worker_dispatch"
                                and bool(request.get("plan_id"))),
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
        if contract.get("successor") and binding_contract_enabled(contract):
            live = isinstance(contract.get("binding_intent"), BindingIntent) and isinstance(
                contract.get("binding_observation"), BindingObservation)
            if not live and not protected_retirement_allows(None, None):
                raise ProviderUnavailable("protected successor retirement changed before provider action")
            # The final evidence gate belongs to _action's writer-lock
            # transaction; a pre-read cannot fence request visibility.
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
        if operation == "visibility":
            _revalidate_native_visibility(self.store, action, result)
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
                raise _coded(
                    ProviderUnavailable("provider binding %s is blocked_unknown" % name),
                    "worktree_missing" if name == "worktree" else None,
                )
            path = Path(value)
            if not path.is_absolute():
                raise _coded(
                    ProviderUnavailable("provider binding %s is blocked_unknown" % name),
                    "worktree_missing" if name == "worktree" else None,
                )
            if "\x00" in value:
                raise _coded(
                    ProviderUnavailable("provider binding %s is blocked_unknown" % name),
                    "worktree_missing" if name == "worktree" else None,
                )

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
        protected_successor = v39 and successor and protected_retirement_allows(None, None)
        if (v39 and successor and not protected_successor
                and not (isinstance(contract.get("binding_intent"), BindingIntent)
                         and isinstance(contract.get("binding_observation"), BindingObservation))):
            raise ProviderUnavailable("protected successor retirement evidence is missing")
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
        writer_root = str(contract.get("worktree") or worktree)
        if successor and existing is not None:
            old_root = Path(existing.worktree)
            old_root = old_root if old_root.is_absolute() else self.paths.root / old_root
            new_root = Path(writer_root)
            new_root = new_root if new_root.is_absolute() else self.paths.root / new_root
            if old_root.resolve() != new_root.resolve() or existing.branch != contract.get("branch"):
                raise ValueError("successor writer root or branch changed")
            # The immutable registry pins the predecessor's representation,
            # not just its resolved location. Keep it after proving equality.
            writer_root = existing.worktree
        if v39 and contract.get("binding_probe") is not True:
            raise ProviderUnavailable(
                "V3.9 provider create requires explicit binding_probe=true"
            )
        if v39:
            self._validate_v39_create_binding(contract, worktree)
        if self._worktree_launchers is not None:
            self._prepare_visible_worktree(contract, worktree)
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
            head = "请执行 {} 任务，Issue {}。完工步骤（固定顺序，两条命令都必须带 --project-root 指向主项目目录，不要依赖当前目录，也不要在 worktree 里执行）：0) 先把**你本会话的工作目录**（会话环境给出的 cwd；project 挂载的会话在项目树或 worktree 内，projectless 会话在宿主生成的临时目录）原样写进交付 JSON 的 `delivery_evidence.session_cwd`——监工用它核对本会话是否真的挂载了 project，不填就无法核对；1) 先执行 `vibe worker-deliver --project-root {} --run-id {} --node {} --role {} --generation {} --payload '<交付 JSON>'` 自报落盘（交付 JSON 形状同交付事件：嵌套的 delivery_evidence 对象含 completion_marker、delivery_path、thread_status，visible-sdd 另带 in_session_review；{}格式错误会当场返回）；2) `vibe supervisor-address --project-root {} --run-id {} --node {} --role {} --generation {} --json` 查询监工地址与唤醒方式；3) 返回的 `wake.channel` 非空时，用该工具向 `wake.target` 原样发送 `wake.signal` 一行（Codex 为 `send_message_to_thread`，Claude Code 为 `SendMessage`）；`wake.channel` 为 null 或地址为 unknown 时不发，到此为止，监工心跳兜底拉取（没有登记推送通道的平台，如 WorkBuddy）。唤醒信号内容不影响验收。任何结束回合之前（交付、交付被拒、向监工提问、被阻塞）都要执行第 2)、3) 步并按返回发信号；worker-deliver 拒收时返回值已带 `wake` 指令，同样照发。发信号失败（工具报错或收不到回应）时不要重试等待：用同一份交付 JSON 在顶层加 `\"wake_failed\": true` 再执行一次 worker-deliver，守护程序下一周期会据此接收。运行消费由登记主监工负责，worker 不得调用 core resume、reconcile 或消费 mailbox。".format(
                role, node_id, root, run_id, node_id, role, generation,
                event_pin,
                root, run_id, node_id, role, generation,
            )
        else:
            head = "请执行 {} 任务，Issue {}。".format(role, node_id)
        if role == "developer":
            # #381: say up front which out-of-scope files the gate accepts.
            head += "\n" + auto_scope_rule_sentence(self.paths.root)
        # Consumers split the prompt on the consistency marker and parse what
        # follows as one JSON document, so the consistency block always goes
        # last -- after any inlined protocol text.
        consistency = self._consistency_instruction(contract)
        # Run-level decisions already answered in this run reach the worker on
        # first dispatch too, not only on continuation (#338).  Empty section
        # adds nothing, and the consistency block still goes last.
        answered_text = supervisor_answer_section(contract)
        answered_text = ("\n" + answered_text) if answered_text else ""
        prompt = head + self._remote_git_ban(contract) + answered_text + consistency
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
                head + self._remote_git_ban(contract) + answered_text
                + "\n\n" + protocol_block + "\n\n" + consistency
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
        initial_bridge = v39 and not successor and not isinstance(live_intent, BindingIntent)
        if initial_bridge:
            from ..supervisor import SupervisorLease, _read_lease_record
            from ..state import retirement_writer_lease
            record = _read_lease_record(SupervisorLease(self.paths).path(run_id))
            owner = ({key: record.get(key) for key in ("run_id", "lease_epoch", "owner", "released")}
                     if record is not None else None)
            caller = _RECEIPT_CALLER.get()
            if caller is not None and caller[0] is self:
                self._check_consumption_owner(run_id, caller[1])
            prior = self.store.related_action({
                "provider": self.provider, "run_id": run_id, "issue_id": node_id,
                "role": role, "generation": generation, "sequence": 0,
            }, "create", required=False)
            source = prior["request"].get("initial_binding_source") if prior is not None else None
            if prior is not None and (not isinstance(source, dict) or source.get("owner") != owner):
                raise ProviderUnavailable("initial create owner changed; immutable source retained")
            # Pin the owner and approved contract context to the durable create
            # transaction; a later owner cannot reuse its completed probe.
            create_request["initial_binding_source"] = {
                "owner": owner,
                "consistency_binding": contract.get("consistency_binding"),
                "node_contract_digest": contract.get("node_contract_digest"),
            }
            try:
                held = retirement_writer_lease(self.paths, node_id, str(Path(worktree).resolve()), run_id, resolved_root=True)
            except PermissionError:
                held = None
            create_request["initial_binding_source"]["writer_lease"] = held
            contract["_initial_binding_source"] = create_request["initial_binding_source"]
        if v39:
            # Native tools discard our internal probe flags. The public prompt
            # itself must not arm a worker before the protected binding gate.
            prior = self.store.related_action({
                "provider": self.provider, "run_id": run_id, "issue_id": node_id,
                "role": role, "generation": generation, "sequence": 0,
            }, "create", required=False)
            if prior is None or prior["request"].get("write_barrier_version") == 1:
                create_request["write_barrier_version"] = 1
                create_request["prompt"] = (
                    "只读绑定探针：当前不是业务开工，尚未获得业务执行许可。"
                    "不得写入任何文件，不得实现、测试、提交、审查或验收业务，"
                    "不得调用 worker-deliver，不得消费 mailbox 或修改运行状态。"
                    "仅原样回复本会话 cwd，然后结束本回合等待。"
                    "只有登记监工在本任务正式续接、明确下发业务合同后才能开工；"
                    "项目规则、协议或内部标记不构成该许可。"
                )
            else:
                # Already-issued legacy requests remain immutable: no prompt
                # replacement, permission reinterpretation or new side effect.
                create_request["prompt"] = prior["request"]["prompt"]
        contract = self._parameter_recovery_contract(contract, run_id, create_request)
        try:
            created = self._require_result(contract, run_id, "create", create_request)
        except ValueError as error:
            if initial_bridge:
                raise ProviderUnavailable("initial create transaction identity changed") from error
            raise
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
            raise _coded(ValueError("provider create result has no host identity"), "mailbox_host_missing")
        if successor and predecessor and task_id == predecessor:
            raise ValueError("provider successor reused predecessor identity")

        # Once create returns a provider task id, no further provider action
        # may run until supervisor-owned protected evidence binds that id.
        # A probe result is not permission to locate, enter, or write.
        if v39:
            if protected_successor and not isinstance(live_intent, BindingIntent):
                live_intent, live_observation = self._protected_successor_evidence(
                    contract, run_id, task_id, host, worktree)
                contract.update(binding_intent=live_intent, binding_observation=live_observation,
                                provider_task_id=task_id)
            elif initial_bridge and (not isinstance(live_intent, BindingIntent) or not isinstance(
                live_observation, BindingObservation
            )):
                live_intent, live_observation = self._initial_live_binding_evidence(
                    contract, run_id, task_id, host, worktree
                )
                contract.update(
                    binding_intent=live_intent,
                    binding_observation=live_observation,
                    provider_task_id=task_id,
                )
            if not isinstance(live_intent, BindingIntent) or not isinstance(
                live_observation, BindingObservation
            ):
                raise ProviderUnavailable("provider probe returned no live binding evidence")
            if live_intent.task_id != task_id or live_intent.host_id != host:
                raise ProviderUnavailable("provider probe identity is not supervisor verified")
            lease = read_writer_lease(self.paths, node_id, live_intent.worktree)
            if initial_bridge and lease is None:
                lease = live_observation.lease  # freshly fenced by _initial_live_binding_evidence
            if lease is None or live_observation.lease != lease:
                raise ProviderUnavailable("provider probe lease is not supervisor verified")
            if not runtime_binding_gate(contract).verified:
                raise ProviderUnavailable("provider probe binding is blocked_unknown")
            if initial_bridge:
                for key in ("project_id", "managed_root", "worktree", "branch",
                            "base_sha", "head_sha", "clean", "cursor"):
                    if key in binding_data and binding_data[key] != getattr(live_observation, key):
                        raise ProviderUnavailable("provider self-report conflicts with initial live evidence")

        located = self._require_result(
            contract,
            run_id,
            "locate",
            {"threadId": task_id},
        )
        if located.get("located") is not True:
            raise _coded(
                ValueError(
                    "provider task cannot be located: the locate result must be "
                    '{"located": true}' + (
                        ' (got the host\'s raw {"navigated": true})' if "navigated" in located else ""
                    )
                ),
                "mailbox_locate_shape",
            )
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
        if initial_bridge:
            fresh_intent, fresh_observation = self._initial_live_binding_evidence(
                contract, run_id, task_id, host, worktree)
            if fresh_intent != live_intent or fresh_observation != live_observation:
                raise ProviderUnavailable("initial binding changed during visibility consumption")
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
            worktree=writer_root,
            branch=str(contract.get("branch", "")),
            status_file=str(contract.get("status_file", "")),
            handoff_file=str(contract.get("handoff_file", "")),
            threadId=task_id if self.provider == CODEX_PROVIDER else None,
            hostId=host if self.provider == CODEX_PROVIDER else None,
            run_id=run_id,
            cursor=live_observation.cursor if initial_bridge and isinstance(live_observation, BindingObservation) else None,
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
        if initial_bridge:
            binding.legacy["initial_binding_source"] = contract["_initial_binding_source"]
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

    def _initial_live_binding_evidence(self, contract, run_id, task_id, host, worktree):
        """Create supervisor-owned evidence for a newly returned task identity."""
        from ..state import retirement_writer_lease
        source = contract.get("_initial_binding_source")
        if not isinstance(source, dict) or not isinstance(source.get("owner"), dict):
            raise ProviderUnavailable("initial live binding owner is blocked_unknown")
        consistency = source.get("consistency_binding")
        if (not isinstance(consistency, dict) or consistency.get("schema_version") != 1
                or not consistency.get("plan_id") or not consistency.get("plan_version")
                or consistency.get("authorization_digest") != contract.get("authorization_digest")):
            raise ProviderUnavailable("initial live binding approved source is blocked_unknown")
        for key in ("project_digest", "decision_digest", "authorization_digest", "issue_contract_digest"):
            value = consistency.get(key)
            if (not isinstance(value, str) or len(value) != 64
                    or any(char not in "0123456789abcdef" for char in value)):
                raise ProviderUnavailable("initial live binding source digest is blocked_unknown")
        self._check_consumption_owner(run_id, source["owner"])
        request = {
            "purpose": "binding_probe",
            "business_write_allowed": False,
            "targets": [{"threadId": task_id, "hostId": host}],
            "timeoutMs": 0,
            "initial_binding_source": source,
        }
        self._require_result(contract, run_id, "wait", request)
        action = self._action(contract, run_id, "wait", request, sequence=0)
        try:
            poll = native_wait_observation(self.store, action)
        except (OSError, TypeError, ValueError) as error:
            raise ProviderUnavailable("initial live binding native wait is blocked_unknown") from error
        thread = poll.get("thread")
        cursor = poll.get("cursor")
        if (not isinstance(thread, dict) or thread.get("id") != task_id
                or thread.get("hostId") != host or not isinstance(cursor, str) or not cursor):
            raise ProviderUnavailable("initial live binding wait identity is blocked_unknown")
        for key, expected in (("provider_task_id", task_id), ("host_id", host)):
            if contract.get(key) not in (None, "", expected):
                raise ProviderUnavailable("initial live binding contract identity is blocked_unknown")
        root = Path(worktree).resolve()
        managed = Path(contract.get("managed_root", "")).resolve()
        try:
            if Path(contract["worktree"]).resolve() != root:
                raise ValueError("writer root changed")
            root.relative_to(managed)
        except (KeyError, TypeError, ValueError, OSError, RuntimeError):
            raise ProviderUnavailable("initial live binding path is blocked_unknown")

        def git(*args):
            result = subprocess.run(
                ["git", "-C", str(root), *args],
                check=False, capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise ProviderUnavailable("initial live binding git observation is blocked_unknown")
            return result.stdout.strip()

        branch = git("symbolic-ref", "--short", "HEAD")
        head = git("rev-parse", "HEAD")
        status = git("status", "--porcelain", "--untracked-files=all")
        if branch != contract.get("branch") or head.lower() != str(contract.get("base_sha", "")).lower() or status:
            raise ProviderUnavailable("initial live binding git state is blocked_unknown")
        # Resolve for containment and Git observation, but preserve the exact
        # authorized spelling for the gate and its writer-lease identity.
        # On macOS /var and /private/var can name the same directory.
        try:
            held = retirement_writer_lease(self.paths, str(contract["node_id"]), str(root), run_id, resolved_root=True)
        except PermissionError as error:
            raise ProviderUnavailable("initial live binding writer lease identity is blocked_unknown") from error
        if (held is None or held != source.get("writer_lease")
                or held["record"].get("role") != contract.get("role")
                or held["record"].get("generation") != contract.get("generation")):
            raise ProviderUnavailable("initial live binding writer lease generation is blocked_unknown")
        lease = read_writer_lease(self.paths, str(contract["node_id"]), held["record"]["worktree"])
        if lease is None:
            raise ProviderUnavailable("initial live binding writer lease is blocked_unknown")
        if lease.worktree != contract["worktree"]:
            from ..state import supervisor_lease_id
            lease = replace(lease, worktree=contract["worktree"], lease_id=supervisor_lease_id(
                str(contract["node_id"]), contract["worktree"], run_id))
        if contract.get("head_sha") not in (None, "", head) or contract.get("clean") not in (None, "", True):
            raise ProviderUnavailable("initial live binding Git contract is blocked_unknown")
        if contract.get("cursor") not in (None, "", cursor):
            raise ProviderUnavailable("initial live binding cursor contract is blocked_unknown")
        if contract.get("lease") not in (None, "", lease):
            raise ProviderUnavailable("initial live binding lease contract is blocked_unknown")
        self._check_consumption_owner(run_id, source["owner"])
        cursor_observation = WaitThreadsCursorObservation.from_wait_threads(task_id, host, cursor)
        identity = dict(
            project_id=contract["project_id"], task_id=task_id, host_id=host,
            node_id=str(contract["node_id"]), worktree=contract["worktree"], managed_root=contract["managed_root"],
            branch=branch, base_sha=str(contract["base_sha"]), head_sha=head,
            clean=True, cursor=cursor,
        )
        intent = BindingIntent(**identity, lease_id=lease.lease_id)
        observation = BindingObservation(
            **identity, lease=lease, source="codex_app__wait_threads",
            cursor_source="codex_app__wait_threads", cursor_task_id=task_id,
            cursor_host_id=host, cursor_lineage=cursor_observation.lineage,
            cursor_observation=cursor_observation,
        )
        return intent, observation

    def _protected_successor_evidence(self, contract, run_id, task_id, host, worktree):
        """Observe the newly created task, never bootstrap its predecessor."""
        request = {"purpose": "binding_probe", "business_write_allowed": False,
                   "targets": [{"threadId": task_id, "hostId": host}], "timeoutMs": 0}
        self._require_result(contract, run_id, "wait", request)
        action = self._action(contract, run_id, "wait", request, sequence=0)
        poll = native_wait_observation(self.store, action)
        cursor = WaitThreadsCursorObservation.from_wait_threads(task_id, host, poll.get("cursor"))
        def git(*args):
            result = subprocess.run(["git", "-C", str(worktree), *args],
                                    check=True, capture_output=True, text=True)
            return result.stdout.strip()
        root = str(Path(worktree).resolve())
        if git("symbolic-ref", "--short", "HEAD") != contract["branch"]:
            raise ProviderUnavailable("protected successor branch drift")
        lease = read_writer_lease(self.paths, contract["node_id"], root)
        if lease is None:
            raise ProviderUnavailable("protected successor live lease missing")
        identity = dict(project_id=contract["project_id"], task_id=task_id, host_id=host,
            node_id=contract["node_id"], worktree=root, managed_root=contract["managed_root"],
            branch=contract["branch"], base_sha=contract["base_sha"], head_sha=git("rev-parse", "HEAD"),
            clean=not bool(git("status", "--porcelain")), cursor=cursor.cursor)
        intent = BindingIntent(**identity, lease_id=lease.lease_id)
        observation = BindingObservation(**identity, lease=lease, source="codex_app__wait_threads",
            cursor_source="codex_app__wait_threads", cursor_task_id=task_id, cursor_host_id=host,
            cursor_lineage=cursor.lineage, cursor_observation=cursor)
        if not protected_retirement_allows(intent, observation):
            raise ProviderUnavailable("protected successor live retirement proof changed")
        return intent, observation

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
        initial_source = None
        if (binding_contract_enabled(contract) and not contract.get("continuation")
                and not contract.get("successor") and not binding.successor_of):
            from ..digests import canonical_json_digest
            try:
                # The immutable create journal, rather than a mutable registry
                # carrier, decides whether fresh first-binding proof is due.
                for sequence in (0, 1):
                    action = self.store.related_action({
                        "provider": self.provider, "run_id": binding.run_id,
                        "issue_id": binding.issue_id, "role": binding.role,
                        "generation": binding.generation, "sequence": sequence,
                    }, "create", required=False)
                    if action is None or "initial_binding_source" not in action["request"]:
                        continue
                    result = self.store.result(action["action_id"])
                    returned = result.get("binding") if isinstance(result, dict) else None
                    if (not isinstance(returned, dict)
                            or (returned.get("threadId") or returned.get("task_id")) != binding.task_id
                            or (returned.get("hostId") or returned.get("host")) != binding.host):
                        continue
                    initial_source = action["request"]["initial_binding_source"]
                    carrier = binding.legacy.get("initial_binding_source")
                    if (not isinstance(initial_source, dict) or not initial_source
                            or not isinstance(carrier, dict) or not carrier
                            or canonical_json_digest(carrier) != canonical_json_digest(initial_source)):
                        raise ValueError("initial binding source carrier changed")
                    visibility = self.store.related_action(action, "visibility")
                    visible = self.store.result(visibility["action_id"])
                    if not isinstance(visible, dict):
                        raise ValueError("initial binding visibility missing")
                    _revalidate_native_visibility(self.store, visibility, visible)
                    break
                if initial_source is None:
                    raise ValueError("initial binding create source is unavailable")
            except (OSError, TypeError, ValueError, PermissionError, ProviderUnavailable):
                return BindingVerification("blocked_unknown", False, [], ["initial_binding_source"])
        if initial_source is not None:
            fresh_contract = dict(contract, _initial_binding_source=initial_source)
            try:
                intent, observation = self._initial_live_binding_evidence(
                    fresh_contract, str(binding.run_id), binding.task_id, binding.host, Path(binding.worktree))
            except (ProviderUnavailable, ProviderPending, PermissionError, ValueError, OSError):
                return BindingVerification("blocked_unknown", False, [], ["initial_binding_fresh_observation"])
            if intent != binding.binding_intent or observation != binding.binding_observation:
                return BindingVerification("blocked_unknown", False, [], ["initial_binding_fresh_observation"])
        if _PROTECTED_RETIREMENT.get() is not None or (contract.get("successor") and binding_contract_enabled(contract)):
            if not protected_retirement_allows(None, None, contract=contract, runner=self):
                return BindingVerification("blocked_unknown", False, [], ["protected_retirement_scope"])
        verification = runtime_binding_gate(contract, binding)
        if not binding_contract_enabled(contract) or not verification.verified:
            return verification
        intent = binding.binding_intent
        observation = binding.binding_observation
        lease = read_writer_lease(self.paths, str(contract["node_id"]), intent.worktree)
        if lease is None and initial_source is not None:
            lease = observation.lease  # fresh, source-bound evidence checked above
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
        with protected_writer_fence():
            return self._start_owned(contract, worktree)

    def _start_owned(self, contract: dict, worktree: Path) -> RunHandle:
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
        probe_start = False
        if binding_contract_enabled(contract) and not contract.get("continuation"):
            for sequence in (0, 1):
                create = self.store.related_action({
                    "provider": self.provider, "run_id": run_id,
                    "issue_id": binding.issue_id, "role": binding.role,
                    "generation": binding.generation, "sequence": sequence,
                }, "create", required=False)
                if create is None or "write_barrier_version" not in create["request"]:
                    continue
                if create["request"]["write_barrier_version"] != 1:
                    raise ProviderUnavailable("unknown probe write barrier version")
                result = self.store.result(create["action_id"])
                identity = result.get("binding", {}) if isinstance(result, dict) else {}
                if ((identity.get("threadId") or identity.get("task_id")) != binding.task_id
                        or (identity.get("hostId") or identity.get("host")) != binding.host):
                    raise ProviderUnavailable("probe business start identity changed")
                probe_start = True
            path = self._handle_path(handle.run_id)
            if probe_start and path.exists():
                previous = self.store._read(path)
                issued = self.store.related_action({
                    "provider": self.provider, "run_id": run_id,
                    "issue_id": binding.issue_id, "role": binding.role,
                    "generation": binding.generation, "sequence": 0,
                }, "resume")
                if (issued["request"].get("threadId") != binding.task_id
                        or issued["request"].get("hostId") != binding.host
                        or issued["request"].get("authorization_digest") != contract.get("authorization_digest")):
                    raise ProviderUnavailable("probe business action replay changed")
                if any(previous.get(key) != value for key, value in (
                    ("run_id", run_id), ("node_id", binding.issue_id),
                    ("role", binding.role), ("generation", binding.generation),
                    ("task_id", binding.task_id), ("host", binding.host),
                    ("authorization_digest", contract.get("authorization_digest")),
                    ("intent_digest", contract.get("intent_digest")),
                )):
                    raise ProviderUnavailable("probe business start replay changed")
                if "start_owner" in previous:
                    from ..supervisor import SupervisorLease, _read_lease_record
                    current_owner = _read_lease_record(SupervisorLease(self.paths).path(run_id))
                    current_owner = ({key: current_owner.get(key) for key in
                        ("run_id", "lease_epoch", "owner", "released")}
                        if current_owner is not None else None)
                    if previous["start_owner"] != current_owner or (current_owner or {}).get("released"):
                        raise ProviderUnavailable("probe business start owner changed")
                return handle
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
            "intent_digest": contract.get("intent_digest"),
            "node_contract_digest": contract.get("node_contract_digest"),
        }
        from ..supervisor import SupervisorLease, _read_lease_record
        owner = _read_lease_record(SupervisorLease(self.paths).path(run_id))
        # Legacy adopted handles have no immutable start intent and follow
        # their existing wait recovery path. Only intent-bound starts carry
        # the owner fence used by crash-safe start recovery.
        if contract.get("intent_digest"):
            metadata["start_owner"] = ({key: owner.get(key) for key in
                ("run_id", "lease_epoch", "owner", "released")} if owner is not None else None)
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
        if contract.get("continuation") or probe_start:
            request = {
                "threadId": binding.task_id,
                "hostId": binding.host,
                "prompt": self._continuation_prompt(contract, self.paths.root) + self._remote_git_ban(contract) + "\n" + self._consistency_instruction(contract),
            }
            if probe_start and binding.topology == "visible-sdd":
                protocol = contract.get("sdd_protocol")
                if not isinstance(protocol, str) or not protocol.strip():
                    raise ProviderUnavailable("probe business start requires sdd_protocol")
                request["prompt"] = (
                    load_protocol(Path(protocol).stem) + "\n\n" + request["prompt"]
                )
            if route_model is not None:
                request.update({"model": route_model, "thinking": provider_thinking_for(route_reasoning)})
            action = self._action(contract, run_id, "resume", request)
            metadata["pending_action"] = action["action_id"]
            metadata["pending_operation"] = "resume"
        self.store._atomic(self._handle_path(handle.run_id), metadata)
        return handle

    def recover_start(self, run_id, node_id, intent, authorization_digest):
        """Locate an already-issued continuation; never issue a start/resume."""
        from ..supervisor import SupervisorLease, _read_lease_record
        handle = RunHandle(self._handle_id(dict(intent, node_id=node_id), run_id))
        path = self._handle_path(handle.run_id)
        if not path.exists():
            return None
        metadata = self.store._read(path)
        owner = _read_lease_record(SupervisorLease(self.paths).path(run_id))
        owner = ({key: owner.get(key) for key in ("run_id", "lease_epoch", "owner", "released")}
                 if owner is not None else None)
        if (metadata.get("start_owner") != owner or (owner or {}).get("released") is True
                or metadata.get("authorization_digest") != authorization_digest
                or metadata.get("intent_digest") != intent.get("intent_digest")
                or any(metadata.get(key) != value for key, value in (
                    ("handle_id", handle.run_id), ("run_id", run_id), ("node_id", node_id),
                    ("role", intent["role"]), ("generation", intent["generation"]),
                    ("task_id", intent["task_id"])))):
            raise PermissionError("provider start recovery lineage changed")
        if metadata.get("pending_operation") != "resume":
            return None
        action = self.store.action(metadata["pending_action"])
        binding = load_task_binding(self.paths, node_id, intent["role"], run_id=run_id)
        if (binding.provider != self.provider or binding.task_id != intent["task_id"]
                or binding.generation != intent["generation"] or binding.host != metadata["host"]
                or any(action.get(key) != value for key, value in (
                    ("run_id", run_id), ("issue_id", node_id), ("role", intent["role"]),
                    ("generation", intent["generation"]), ("provider", self.provider), ("operation", "resume")))
                or action["request"].get("threadId") != binding.task_id
                or action["request"].get("hostId") != binding.host
                or action["request"].get("authorization_digest") != authorization_digest):
            raise PermissionError("provider start recovery action changed")
        result = self.store.result(action["action_id"])
        return handle if result is not None and result.get("resumed") is True else None

    def send_notice(self, handle: RunHandle, message: str) -> None:
        """Queue a fire-and-forget message to the bound thread (advisory).

        Uses its own ``resume`` action sequence and leaves ``pending_action``
        untouched, so the wait/poll state machine is unaffected.
        """
        metadata = self._validated_handle_metadata(handle)
        from ..supervisor import SupervisorLease, _read_lease_record
        lease = SupervisorLease(self.paths)
        with lease.lock(metadata["run_id"]):
            self._validate_handle_source(handle, metadata)
            record = _read_lease_record(lease.path(metadata["run_id"]))
            owner = ({key: record.get(key) for key in ("run_id", "lease_epoch", "owner", "released")}
                     if record is not None else None)
            if "start_owner" in metadata and (metadata["start_owner"] != owner
                    or (owner or {}).get("released") is True):
                raise PermissionError("provider notice owner fence changed")
            self._send_notice_owned(handle, message, metadata)

    def _send_notice_owned(self, handle, message, metadata):
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

    def _validated_handle_metadata(self, handle: RunHandle):
        metadata = self.store._read(self._handle_path(handle.run_id))
        # A handle's deterministic key pins run/node/role/generation before
        # mutable metadata can choose a source directory or lease lock.
        if (metadata.get("handle_id") != handle.run_id
                or self._handle_id(metadata, metadata["run_id"]) != handle.run_id):
            raise PermissionError("provider handle source identity changed")
        binding = load_task_binding(self.paths, metadata["node_id"], metadata["role"],
            run_id=metadata["run_id"])
        if (binding.task_id != metadata.get("task_id")
                or binding.generation != metadata.get("generation")
                or binding.provider != self.provider
                or binding.host != metadata.get("host")):
            raise PermissionError("provider handle registry lineage changed")
        pending_action = metadata.get("pending_action")
        if pending_action:
            action = self.store.action(pending_action)
            request = action.get("request") or {}
            targets = request.get("targets") or []
            target = targets[0] if len(targets) == 1 else request
            if (any(action.get(key) != expected for key, expected in (
                    ("run_id", metadata["run_id"]), ("issue_id", metadata["node_id"]),
                    ("role", metadata["role"]), ("generation", metadata["generation"]),
                    ("provider", self.provider), ("operation", metadata.get("pending_operation"))))
                    or target.get("threadId") != binding.task_id
                    or target.get("hostId") != binding.host):
                raise PermissionError("provider poll original action lineage changed")
        return metadata

    def poll(self, handle: RunHandle):
        metadata = self._validated_handle_metadata(handle)
        from ..supervisor import SupervisorLease, _read_lease_record
        caller = _RECEIPT_CALLER.get()
        if caller is not None and caller[0] is self:
            expected_owner = caller[1]
        elif "start_owner" in metadata:
            expected_owner = metadata["start_owner"]
        else:
            record = _read_lease_record(SupervisorLease(self.paths).path(metadata["run_id"]))
            expected_owner = ({key: record.get(key) for key in ("run_id", "lease_epoch", "owner", "released")}
                              if record is not None else None)
        with SupervisorLease(self.paths).lock(metadata["run_id"]):
            metadata = self._validated_handle_metadata(handle)
            self._validate_handle_source(handle, metadata)
            self._check_consumption_owner(metadata["run_id"], expected_owner,
                legacy_adopted="start_owner" not in metadata)
            return self._poll_owned(handle, metadata)

    @contextmanager
    def receipt_owner(self, expected_owner, *, lock_held=False):
        """Carry the monitor's fixed call owner, including an explicit absent lease."""
        token = _RECEIPT_CALLER.set((self, expected_owner, lock_held))
        try:
            yield
        finally:
            _RECEIPT_CALLER.reset(token)

    def _poll_owned(self, handle, metadata):
        claims = {"node_id": metadata["node_id"], "role": metadata["role"],
            "task_id": metadata["task_id"], "handle_id": handle.run_id,
            "generation": metadata["generation"]}
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
                return self._wait_result(handle, metadata, claims, result, consumption_locked=True)
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

        action = self._issue_wait_owned(handle, metadata)
        result = self.store.result(action["action_id"])
        if result is None:
            return [
                RunEvent(
                    "visibility_unknown",
                    {**claims, "reason": "provider wait is pending"},
                )
            ]
        return self._wait_result(handle, metadata, claims, result, consumption_locked=True)

    def _check_consumption_owner(self, run_id, expected_owner, *, legacy_adopted=False):
        from ..supervisor import SupervisorLease, _read_lease_record
        record = _read_lease_record(SupervisorLease(self.paths).path(run_id))
        owner = ({key: record.get(key) for key in ("run_id", "lease_epoch", "owner", "released")}
                 if record is not None else None)
        legacy_single_shot = (legacy_adopted and owner == expected_owner
            and (owner or {}).get("lease_epoch") == 0 and (owner or {}).get("owner") is None)
        if owner != expected_owner or ((owner or {}).get("released") is True and not legacy_single_shot):
            raise PermissionError("provider receipt consumption owner changed")

    def _issue_wait(self, handle: RunHandle, metadata: Dict[str, Any]) -> Dict[str, Any]:
        from ..supervisor import SupervisorLease, _read_lease_record
        from ..state import load_events
        lease = SupervisorLease(self.paths)
        with lease.lock(metadata["run_id"]):
            self._validate_handle_source(handle, metadata)
            record = _read_lease_record(lease.path(metadata["run_id"]))
            owner = ({key: record.get(key) for key in ("run_id", "lease_epoch", "owner", "released")}
                     if record is not None else None)
            if "start_owner" in metadata and (metadata["start_owner"] != owner or (owner or {}).get("released") is True):
                raise PermissionError("provider wait owner fence changed")
            return self._issue_wait_owned(handle, metadata)

    def _validate_handle_source(self, handle, metadata):
        from ..state import load_events
        # The immutable event source, not deletable metadata fields,
        # determines whether this is an intent-bound start. Legacy
        # adopted handles have no matching start-intent event.
        starts = [row["data"] for row in load_events(self.paths, metadata["run_id"])
            if row["event"] == "start_intent"
            and self._handle_id(row["data"], metadata["run_id"]) == handle.run_id]
        if starts:
            source = starts[-1]
            from ..state import run_dir
            from ..digests import canonical_json_digest
            dispatch = self.store._read(run_dir(self.paths, metadata["run_id"])
                / "start-dispatches" / (source["intent_id"] + ".json"))
            if (not source.get("intent_digest")
                    or metadata.get("intent_digest") != source["intent_digest"]
                    or "start_owner" not in metadata
                    or any(metadata.get(key) != source.get(key)
                           for key in ("run_id", "node_id", "role", "task_id", "generation"))
                    or dispatch.get("digest") != canonical_json_digest(
                        {key: value for key, value in dispatch.items() if key != "digest"})
                    or dispatch.get("intent", {}).get("intent_digest") != source["intent_digest"]
                    or "owner" not in dispatch
                    or metadata["start_owner"] != dispatch["owner"]):
                raise PermissionError("intent-bound wait fence evidence missing")
        else:
            # Legacy eligibility needs positive, task-bound adoption
            # evidence; an empty/misdirected event lookup is not proof.
            adopted = [row["data"] for row in load_events(self.paths, metadata["run_id"])
                if row["event"] == "orphan_dispatch_adopted"
                and row["data"].get("handle_id") == handle.run_id]
            if len(adopted) != 1 or any(adopted[0].get(key) != metadata.get(key)
                    for key in ("run_id", "node_id", "role", "task_id", "host", "generation")):
                raise PermissionError("legacy wait adoption provenance missing")
            original = self.store.action(adopted[0]["action_id"])
            if (original.get("request_digest") != adopted[0].get("request_digest")
                    or original.get("run_id") != metadata["run_id"]
                    or original.get("issue_id") != metadata["node_id"]
                    or original.get("role") != metadata["role"]
                    or original.get("generation") != metadata["generation"]):
                raise PermissionError("legacy wait original action changed")

    def _issue_wait_owned(self, handle: RunHandle, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Write the next non-blocking (``timeoutMs`` 0) wait request and record it as pending."""
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
        return action

    def _wait_result(
        self,
        handle: RunHandle,
        metadata: Dict[str, Any],
        claims: Dict[str, Any],
        result: Dict[str, Any],
        consumption_locked: bool = False,
    ):
        status = result.get("status")
        cursor = result.get("cursor")
        previous_cursor = metadata.get("cursor")
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
            if status == "timeout" and (cursor is None or cursor == previous_cursor) and all(
                    metadata.get(key) is not None
                    for key in ("node_id", "role", "run_id", "generation", "task_id", "host")):
                # A timeout that moved nothing is not news for the supervisor:
                # queue the next non-blocking wait now, from the same cursor,
                # instead of leaving the node unwatched until the next tick.
                if consumption_locked:
                    self._issue_wait_owned(handle, metadata)
                else:
                    self._issue_wait(handle, metadata)
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
        metadata = self._validated_handle_metadata(handle)
        caller = _RECEIPT_CALLER.get()
        if caller is not None and caller[0] is self and caller[2]:
            self._check_consumption_owner(metadata["run_id"], caller[1])
            return self._stop_owned(handle, metadata)
        from ..supervisor import SupervisorLease
        with SupervisorLease(self.paths).lock(metadata["run_id"]):
            metadata = self._validated_handle_metadata(handle)
            if caller is not None and caller[0] is self:
                self._check_consumption_owner(metadata["run_id"], caller[1])
            return self._stop_owned(handle, metadata)

    def _stop_owned(self, handle, metadata):
        self._validate_handle_source(handle, metadata)
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
        events = self._wait_result(handle, metadata, claims, result, consumption_locked=True)
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
