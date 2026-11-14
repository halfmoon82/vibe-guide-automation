"""Protected same-plan/run contract amendments (r4 P1).

A confirmed plan's business contract can be amended only through a public,
protected path -- never by editing the live artifacts or starting a new
plan/run:

``propose``
    reads the business-only amendment spec, derives the engineering fields
    (unchanged node identities, worktrees, topology, card options), renders
    the diff, the DAG audit and a *candidate* authorization card under
    ``plans/<plan>/amendments/<digest>/`` and writes the immutable
    ``candidate.json``.  Nothing live changes and no task is dispatched.

``show``
    renders the candidate card with its dependency graph and records a
    display receipt bound to the candidate digest, exactly the way
    ``vibe authorize --show-card`` binds plan version + card + graph.

``confirm``
    only with the candidate-bound token (``AMEND <digest>``) and inside one
    protected transaction: re-verify lineage (live card/record/run pointer),
    owner, task bindings, writer leases and pending provider side effects;
    write the ``authorization_reauthorized`` transfer event; then swap the
    plan artifacts in.  A crash before the event restores the complete old
    state; a crash after it idempotently finishes the new projection
    (``recover``).  While the transfer marker is present every execution
    entry point fails closed (``require_no_pending_transfer``).

The same node set, plan version, run id, task identities and cursors are
preserved; old records stay under ``amendments/<digest>/``.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import hashlib
import re
import subprocess

from .adapters.task_provider import ProviderActionStore
from .contracts import RunEvent
from .authorization import (
    AuthorizationCard,
    AuthorizationRecord,
    _canonical_digest,
    authorize,
    refresh_authorization_card,
)
from .authorize_entry import (
    AuthorizationDenied,
    _atomic_json,
    materialize_workflow_evidence,
)
from .dag import (
    INTEGRATION_REVIEW_NODE_ID,
    dependency_graph,
    dependency_graph_digest,
    integration_review_scope,
    is_integration_review_node,
)
from .digests import sha256_bytes, sha256_file as _artifact_hash
from .fsio import JsonCorruptError, JsonMissingError, read_json as _fsio_read_json
from .diagnostics import assert_planning_gate, require_execution_ready
from .models import DAGNode, Plan, node_branch, node_worktree
from .node_spec import (
    complete_node_contracts,
    reject_engineering_fields,
    render_prd_markdown,
)
from .paths import ProjectPaths
from .planner import PRD, DecisionCard, approve_prd
from .state import (
    append_event,
    interprocess_lock,
    load_events,
    load_snapshot,
    read_writer_lease,
    retirement_writer_lease,
    _lease_lock,
    release_writer_lease,
    save_snapshot,
)

AMENDMENTS_DIRNAME = "amendments"
TRANSFER_MARKER = ".amendment-transfer.json"
DISPLAY_RECEIPT_NAME = "amendment-display-receipt.json"
CHANGE_REASON = "plan_amendment"
TOKEN_PREFIX = "AMEND "

# Node-level fields a business amendment may change.  Everything else on a
# node -- engineering contract keys, worker/worktree/branch identities --
# must come back identical after the engineering defaults are re-derived.
_BUSINESS_NODE_KEYS = ("title", "depends_on", "integration_after",
                       "parallel_group")
_BUSINESS_CONTRACT_KEYS = ("input", "output", "error_behavior",
                           "acceptance_example", "files", "dependency_reasons")

#: Snapshot node statuses that mean a live or unproven executor exists.
_ACTIVE_NODE_STATUSES = frozenset({
    "running", "review", "rework", "start_pending", "dispatching",
    "awaiting_supervisor", "blocked_unknown",
})
_TERMINAL_BINDING_STATUSES = frozenset({
    "archived", "complete", "terminated", "closed", "replaced",
})

#: Audit reason written on stale markers for creates that provably were
#: never invoked.  It is deliberately distinct from
#: ``platform_handoff_superseded`` -- this path retires legacy side
#: effects inside the same platform and authorization, not across a
#: handoff.
RETIRE_UNINVOKED_REASON = "retire_uninvoked_legacy"
#: The durable run event proving the retirement boundary.
RETIRE_UNINVOKED_EVENT = "retire_uninvoked_legacy"
_RETIRE_SCHEMA = 1
_RETIRE_KIND = "retire_uninvoked_legacy"
#: Rollout payload types that start a native tool invocation.
_NATIVE_CALL_TYPES = frozenset({
    "function_call", "local_shell_call", "custom_tool_call",
})
#: Rollout payload types that close one.
_NATIVE_RETURN_TYPES = frozenset({
    "function_call_output", "local_shell_call_output",
    "custom_tool_call_output", "tool_call_output",
})
#: A create that must never appear in a retired consumer's own log.
_FORBIDDEN_RETIRE_CALLS = frozenset({
    "create_thread", "codex_app__create_thread",
    "svc__codex_app__create_thread",
    "tools.mcp__codex_app__create_thread",
})
_KNOWN_HOST_ITEM_TYPES = frozenset({
    "CommandExecution", "Reasoning", "AgentMessage", "McpToolCall",
    "FileChange", "FunctionCallOutput", "UserMessage", "ImageView",
    "SubAgentActivity", "CollabAgentToolCall", "ContextCompaction",
})
_BENIGN_HOST_ITEM_TYPES = frozenset({
    "Reasoning", "AgentMessage", "UserMessage", "ImageView",
    "ContextCompaction",
})
_EXECUTING_HOST_ITEM_TYPES = frozenset({
    "CommandExecution", "FileChange", "FunctionCallOutput",
    "SubAgentActivity", "CollabAgentToolCall",
})
_KNOWN_NON_ITEM_EVENT_TYPES = frozenset({
    "token_count", "task_started", "task_complete", "turn_aborted",
    "thread_settings_applied", "turn_context",
})
_KNOWN_RESPONSE_ITEM_TYPES = frozenset(
    set(_NATIVE_CALL_TYPES) | set(_NATIVE_RETURN_TYPES)
    | {"additional_tools", "agent_message", "message", "reasoning"}
)
_KNOWN_ENVELOPE_TYPES = frozenset({
    "compacted", "event_msg", "inter_agent_communication_metadata",
    "item_completed", "item_started", "response_item", "session_meta",
    "token_usage_record", "turn_context", "world_state",
})
#: Invocation shapes embedded inside an exec payload's JavaScript source.
#: Only occurrences *outside* string literals and comments can execute; a
#: cmd-string, prompt or audit-script quoting of the same text is a
#: reference, not a call, and is deliberately not flagged.
_EMBEDDED_CREATE_MARKERS = (
    "svc__codex_app__create_thread(",
    "codex_app__create_thread(",
    "create_thread(",
)


def _js_tokens(source: str) -> List[Any]:
    """Lex data literals without running JavaScript. Unknown executable forms refuse."""
    import ast
    tokens = []
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index)
            index = len(source) if end < 0 else end + 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated JavaScript comment")
            index = end + 2
            continue
        if char == "/" and (not tokens or tokens[-1][1] in {"(", "=", ":", ",", "[", "{", "=>"}
                            or (len(tokens) >= 2 and tokens[-2][1] == "=" and tokens[-1][1] == ">")):
            # Regex literals are inert data. Refuse ambiguous division forms
            # rather than treating a possible native reference as text.
            index += 1
            in_class = False
            while index < len(source):
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == "[":
                    in_class = True
                elif source[index] == "]":
                    in_class = False
                elif source[index] == "/" and not in_class:
                    break
                elif source[index] in "\r\n":
                    raise ValueError("unterminated JavaScript regex")
                index += 1
            if index >= len(source):
                raise ValueError("unterminated JavaScript regex")
            index += 1
            while index < len(source) and source[index].isalpha():
                index += 1
            tokens.append(("regex", "<regex>"))
            continue
        if char in {"'", '"', "`"}:
            quote, start = char, index
            index += 1
            while index < len(source):
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == quote:
                    break
                index += 1
            if index == len(source):
                raise ValueError("unterminated JavaScript literal")
            raw = source[start:index + 1]
            if quote == "`":
                if "${" in raw:
                    # Expressions can execute calls, even when the template
                    # value is discarded. Never count them as quoted text.
                    raise ValueError("template interpolation has unknown native effects")
                value = raw[1:-1]
            else:
                try:
                    value = json.loads(raw) if quote == '"' else ast.literal_eval(raw)
                except (ValueError, SyntaxError) as error:
                    # JS permits identity escapes such as \( and \; that
                    # JSON rejects. Decode those while keeping JSON escapes
                    # and rejecting malformed Unicode or line breaks.
                    if quote != '"':
                        raise ValueError("unknown JavaScript string literal") from error
                    normalized = re.sub(r'\\([^"\\/bfnrtu0-9\r\n])', r'\1', raw)
                    try:
                        value = json.loads(normalized)
                    except ValueError as nested:
                        raise ValueError("unknown JavaScript string literal") from nested
            tokens.append(("string", value))
            index += 1
            continue
        # A backslash outside a literal can escape any part of an identifier
        # (including the host object). Do not split it into harmless tokens.
        if char == "\\":
            raise ValueError("escaped JavaScript identifier has unknown native effects")
        match = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*|[0-9]+", source[index:])
        if match:
            value = match.group(0)
            tokens.append(("id", value))
            index += len(value)
        else:
            tokens.append(("punct", char))
            index += 1
    return tokens


def _static_js_value(tokens, position, constants):
    if position >= len(tokens):
        raise ValueError("missing native call argument")
    kind, value = tokens[position]
    if kind == "string":
        return value, position + 1
    if kind == "id":
        if value in {"true", "false", "null"}:
            return {"true": True, "false": False, "null": None}[value], position + 1
        if value.isdigit():
            return int(value), position + 1
        if value in constants:
            return copy.deepcopy(constants[value]), position + 1
        raise ValueError("native arguments contain an unresolved variable")
    if value not in {"{", "["}:
        raise ValueError("native arguments are not static data")
    is_object = value == "{"
    closing = "}" if is_object else "]"
    result = {} if is_object else []
    position += 1
    while position < len(tokens) and tokens[position][1] != closing:
        if is_object:
            key_kind, key = tokens[position]
            if key_kind not in {"id", "string"} or key in result:
                raise ValueError("unknown/duplicate native argument key")
            position += 1
            if position < len(tokens) and tokens[position][1] == ":":
                item, position = _static_js_value(tokens, position + 1, constants)
            elif key in constants:
                item = copy.deepcopy(constants[key])
            else:
                raise ValueError("unresolved shorthand native argument")
            result[key] = item
        else:
            item, position = _static_js_value(tokens, position, constants)
            result.append(item)
        if position < len(tokens) and tokens[position][1] == ",":
            position += 1
        elif position >= len(tokens) or tokens[position][1] != closing:
            raise ValueError("native arguments contain executable expressions")
    if position >= len(tokens):
        raise ValueError("unterminated native argument")
    return result, position + 1


def _static_tool_invocations(source, names):
    tokens = _js_tokens(source)
    constants, invocations = {}, []
    for index, (kind, value) in enumerate(tokens):
        if kind != "id":
            continue
        # A capability reference can be aliased/computed; refusing it is
        # essential because absence of a literal call is not non-invocation.
        if value == "tools":
            if (index + 3 >= len(tokens) or tokens[index + 1][1] != "."
                    or tokens[index + 2][0] != "id" or tokens[index + 3][1] != "("):
                raise ValueError("indirect host tool access has unknown native effects")
        if value in {"eval", "Function", "Reflect"}:
            raise ValueError("dynamic JavaScript has unknown native effects")
        if value == "const" and index + 3 < len(tokens) and tokens[index + 2][1] == "=":
            try:
                item, end = _static_js_value(tokens, index + 3, constants)
                if end < len(tokens) and tokens[end][1] == ";":
                    constants[tokens[index + 1][1]] = item
            except ValueError:
                pass
        if value in constants and index + 1 < len(tokens) and tokens[index + 1][1] == "=":
            # const reassignment, destructuring and computed evaluation are
            # outside the auditable subset; do not evaluate a guessed value.
            if index == 0 or tokens[index - 1][1] != "const":
                raise ValueError("native argument constant was reassigned")
        if value not in names:
            continue
        if index + 1 >= len(tokens) or tokens[index + 1][1] != "(":
            raise ValueError("aliased native tool reference")
        args, end = _static_js_value(tokens, index + 2, constants)
        if not isinstance(args, dict) or end >= len(tokens) or tokens[end][1] != ")":
            raise ValueError("native invocation arguments are unknown")
        invocations.append((value, args))
    return invocations


def _audit_static_exec(tokens, extra_host_names=None):
    """Accept only straight-line calls to known host functions with static data.

    Member access, arbitrary functions, computed expressions and assignments
    are not a proof of non-invocation. In particular, constructor chains must
    never be interpreted as inert text merely because ``tools`` is quoted.
    """
    host_names = {"exec_command", "mcp__codex_app__create_thread",
                  "svc__codex_app__create_thread", "codex_app__create_thread",
                  "create_thread"} | set(extra_host_names or ())
    # Result bindings never stand for a callable tool or arbitrary expression.
    # Only the plain exec_command output field may be passed to text().
    output_bindings = set()
    bindings = set()
    reserved = host_names | {
        "tools", "text", "Promise", "ALL_TOOLS", "globalThis", "global",
        "window", "self", "eval", "Function", "Reflect", "String",
        "await", "const", "let", "var", "new", "this", "null", "true", "false",
        "break", "case", "catch", "class", "continue", "debugger", "default",
        "delete", "do", "else", "export", "extends", "finally", "for",
        "function", "if", "import", "in", "instanceof", "return", "super",
        "switch", "throw", "try", "typeof", "void", "while", "with", "yield",
        "enum", "implements", "interface", "package", "private", "protected",
        "public", "static", "arguments",
    }

    def expression(pos):
        awaited = False
        if pos < len(tokens) and tokens[pos] == ("id", "await"):
            awaited = True
            pos += 1
        if pos < len(tokens) and tokens[pos][1] == "(":
            pos, origin = expression(pos + 1)
            if pos >= len(tokens) or tokens[pos][1] != ")":
                raise ValueError("unknown grouped JavaScript expression")
            pos += 1
            # Only this result field of the known exec_command call is
            # supported; property chains and computed access remain unknown.
            if tokens[pos:pos + 2] == [("punct", "."), ("id", "output")]:
                if origin != "exec_result":
                    raise ValueError("output projection requires a proven exec result")
                pos += 2
                origin = "output"
            return pos, origin
        if pos >= len(tokens):
            raise ValueError("missing JavaScript expression")
        if (tokens[pos][0] == "regex"
                and tokens[pos + 1:pos + 4] == [("punct", "."), ("id", "test"), ("punct", "(")]):
            if (pos + 5 >= len(tokens) or tokens[pos + 4][0] != "string"
                    or tokens[pos + 5][1] != ")"):
                raise ValueError("unknown regex test argument")
            return pos + 6, "static"
        if tokens[pos][0] in {"string", "regex"}:
            return pos + 1, "static"
        # Only the metadata projection ``ALL_TOOLS.map(x => x.name)`` is
        # accepted. No callback body, invocation, computed member or alias
        # is admitted; the parameter must not shadow a host/builtin name.
        if tokens[pos:pos + 4] == [
                ("id", "ALL_TOOLS"), ("punct", "."),
                ("id", "map"), ("punct", "(")]:
            if (pos + 9 >= len(tokens) or tokens[pos + 4][0] != "id"
                    or tokens[pos + 4][1] in reserved | bindings
                    or tokens[pos + 5:pos + 7] != [("punct", "="), ("punct", ">")]
                    or tokens[pos + 7:pos + 10] != [tokens[pos + 4], ("punct", "."), ("id", "name")]
                    or pos + 10 >= len(tokens) or tokens[pos + 10] != ("punct", ")")):
                raise ValueError("unknown metadata projection")
            return pos + 11, "metadata"
        if tokens[pos:pos + 2] == [("id", "text"), ("punct", "(")]:
            if (pos + 5 < len(tokens) and tokens[pos + 2][0] == "id"
                    and tokens[pos + 2][1] in output_bindings
                    and tokens[pos + 3:pos + 5] == [("punct", "."), ("id", "output")]):
                pos += 5
            else:
                pos, _origin = expression(pos + 2)
            if pos >= len(tokens) or tokens[pos][1] != ")":
                raise ValueError("unknown text argument")
            return pos + 1, "text_result"
        if tokens[pos:pos + 2] == [("id", "tools"), ("punct", ".")]:
            if (pos + 3 >= len(tokens) or tokens[pos + 2][0] != "id"
                    or tokens[pos + 2][1] not in host_names
                    or tokens[pos + 3][1] != "("):
                raise ValueError("unknown host function")
            tool = tokens[pos + 2][1]
            pos += 4
            if pos < len(tokens) and tokens[pos][1] != ")":
                _value, pos = _static_js_value(tokens, pos, {})
            if pos >= len(tokens) or tokens[pos][1] != ")":
                raise ValueError("unknown host call argument")
            return pos + 1, ("exec_result" if awaited and tool == "exec_command"
                             else "host_result")
        raise ValueError("JavaScript expression is outside the static audit subset")

    pos = 0
    while pos < len(tokens):
        if tokens[pos] == ("id", "const"):
            if (pos + 5 >= len(tokens) or tokens[pos + 1][0] != "id"
                    or tokens[pos + 1][1] in reserved | bindings
                    or tokens[pos + 2:pos + 4] != [("punct", "="), ("id", "await")]):
                raise ValueError("unknown JavaScript const declaration")
            name = tokens[pos + 1][1]
            tool = None
            if tokens[pos + 4:pos + 8] == [
                    ("id", "Promise"), ("punct", "."),
                    ("id", "allSettled"), ("punct", "(")]:
                pos += 8
                if pos >= len(tokens) or tokens[pos] != ("punct", "["):
                    raise ValueError("unknown allSettled argument")
                pos += 1
                while pos < len(tokens) and tokens[pos] != ("punct", "]"):
                    if tokens[pos:pos + 2] != [("id", "tools"), ("punct", ".")]:
                        raise ValueError("unknown allSettled item")
                    pos, _origin = expression(pos)
                    if pos < len(tokens) and tokens[pos] == ("punct", ","):
                        pos += 1
                    elif pos >= len(tokens) or tokens[pos] != ("punct", "]"):
                        raise ValueError("unknown allSettled separator")
                if tokens[pos:pos + 2] != [("punct", "]"), ("punct", ")")]:
                    raise ValueError("unterminated allSettled")
                pos += 2
            else:
                if (pos + 7 >= len(tokens)
                        or tokens[pos + 4:pos + 6] != [("id", "tools"), ("punct", ".")]
                        or tokens[pos + 6][0] != "id"
                        or tokens[pos + 6][1] not in host_names
                        or tokens[pos + 7] != ("punct", "(")):
                    raise ValueError("unknown JavaScript const initializer")
                tool = tokens[pos + 6][1]
                pos, _origin = expression(pos + 3)
            bindings.add(name)
            if tool == "exec_command":
                output_bindings.add(name)
        else:
            pos, _origin = expression(pos)
        if pos < len(tokens) and tokens[pos][1] == ";":
            pos += 1
        elif pos < len(tokens):
            raise ValueError("unknown JavaScript statement")


def _embedded_create_invocations(text: str) -> int:
    tokens = _js_tokens(text)
    _audit_static_exec(tokens)
    if any(kind == "id" and value in {"globalThis", "global", "window", "self"}
           for kind, value in tokens):
        raise ValueError("indirect global access has unknown native effects")
    if any(kind == "string" and value in _FORBIDDEN_RETIRE_CALLS | {"svc__codex_app__create_thread", "tools"}
           and index > 0 and tokens[index - 1][1] == "["
           for index, (kind, value) in enumerate(tokens)):
        raise ValueError("computed host access has unknown native effects")
    return len(_static_tool_invocations(text, _FORBIDDEN_RETIRE_CALLS | {
        "mcp__codex_app__create_thread"}))


def project_paths(root: Any) -> ProjectPaths:
    """``ProjectPaths`` for an explicit project root (fixtures and the CLI)."""
    return ProjectPaths(Path(str(root)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _plan_root(paths: ProjectPaths, plan_id: str) -> Path:
    return paths.resolve_vibe_path(Path("plans") / plan_id)


def amendments_dir(paths: ProjectPaths, plan_id: str) -> Path:
    return _plan_root(paths, plan_id) / AMENDMENTS_DIRNAME


def candidate_dir(paths: ProjectPaths, plan_id: str, amendment_digest: str) -> Path:
    return amendments_dir(paths, plan_id) / amendment_digest


def barrier_path(paths: ProjectPaths, plan_id: str) -> Path:
    return _plan_root(paths, plan_id) / TRANSFER_MARKER


def _read_artifact(path: Path) -> Any:
    if not path.is_file() or path.is_symlink():
        raise ValueError("required artifact is missing: " + path.name)
    try:
        return _fsio_read_json(path)
    except (JsonMissingError, JsonCorruptError) as error:
        raise ValueError("required artifact is unreadable: " + path.name) from error


def _plan_data(plan_root: Path) -> Dict[str, Any]:
    data = _read_artifact(plan_root / "plan.json")
    if not isinstance(data, dict):
        raise ValueError("plan.json must be an object")
    return data


def _plan_object(plan_root: Path, data: Dict[str, Any]) -> Plan:
    nodes = [DAGNode.from_dict(dict(item))
             for item in _read_artifact(plan_root / "nodes.json")]
    return Plan(
        data["plan_id"], int(data["version"]), data["prd_path"],
        list(data["node_ids"]), data["status"],
        authorization_required=bool(data.get("authorization_required")),
        decisions=list(data.get("decisions") or []),
        nodes=nodes,
        complexity_band=str(data.get("complexity_band") or ""),
        spec_path=str(data.get("spec_path") or ""),
        integration_contract=dict(data.get("integration_contract") or {}),
        evidence_priority=list(data.get("evidence_priority")
                               or ["verified_fact", "spec", "self_report"]),
    )


def _load_record(plan_root: Path) -> AuthorizationRecord:
    path = plan_root / "authorization.json"
    if not path.is_file():
        raise ValueError(
            "amendment requires an authorized plan: authorization.json missing")
    return AuthorizationRecord.from_dict(_read_artifact(path))


def _load_card(plan_root: Path) -> AuthorizationCard:
    from .cli import _authorization_card  # call-time: cli imports this module
    return _authorization_card(_read_artifact(plan_root / "authorization-card.json"))


def _current_run_id(plan_root: Path) -> Optional[str]:
    pointer = plan_root / "current-run.json"
    if not pointer.is_file() or pointer.is_symlink():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("current-run.json is unreadable")
    run_id = data.get("run_id") if isinstance(data, dict) else None
    if not isinstance(run_id, str) or not run_id.startswith("run-"):
        raise ValueError("current-run.json does not name a run")
    return run_id


def _node_map(plan_root: Path) -> Dict[str, Dict[str, Any]]:
    raw = _read_artifact(plan_root / "nodes.json")
    if not isinstance(raw, list) or not raw:
        raise ValueError("nodes.json must be a non-empty list")
    return {item["id"]: item for item in raw}


def _validate_amendment_spec(
    spec: Any, previous_nodes: Dict[str, Dict[str, Any]],
) -> List[DecisionCard]:
    """Business-only spec over the identical node set; returns its decisions."""
    reject_engineering_fields(spec)
    if not isinstance(spec.get("reason", None), (type(None), str)):
        raise ValueError("amendment reason must be a string")
    nodes = spec.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("amendment spec must carry the full node list")
    offered = {item.get("id") for item in nodes if isinstance(item, dict)}
    if len(offered) != len(nodes) or any(not item for item in offered):
        raise ValueError("every amendment node needs an id")
    if offered != set(previous_nodes):
        raise ValueError(
            "contract amendments keep the node set identical; "
            "nodes cannot be added, removed or renamed")
    for item in nodes:
        contract = item.get("contract")
        if not isinstance(contract, dict):
            raise ValueError("every amendment node needs a contract object")
        allowed = set(_BUSINESS_CONTRACT_KEYS)
        if item.get("id") == INTEGRATION_REVIEW_NODE_ID:
            # The final reviewer owns nothing: its ``files`` is the derived
            # union of the business write scopes, not a business claim.
            allowed.discard("files")
        unknown = set(contract) - allowed
        if unknown:
            raise ValueError(
                "以下字段由系统自动生成，请从产品 spec 中删除：contract."
                + ", contract.".join(sorted(unknown)))
    decisions = [DecisionCard(**item) for item in spec.get("decisions", [])]
    approval = approve_prd(
        PRD(str(spec.get("title", "")).strip(),
            str(spec.get("objective", "")).strip()),
        decisions)
    if not approval.approved:
        raise PermissionError("product decisions remain unresolved")
    return decisions


def _merge_nodes(spec: Dict[str, Any],
                 previous_nodes: Dict[str, Dict[str, Any]]) -> List[DAGNode]:
    """Overlay the business fields onto the published engineering nodes."""
    merged: List[DAGNode] = []
    for item in spec["nodes"]:
        previous = previous_nodes[item["id"]]
        node_dict = copy.deepcopy(previous)
        for key in _BUSINESS_NODE_KEYS:
            if key in item:
                node_dict[key] = item[key]
        contract = node_dict.setdefault("contract", {})
        for key, value in item.get("contract", {}).items():
            contract[key] = value
        # ``worker_profile.allowlist`` is a *derived* projection of
        # ``contract.files`` (node_spec: ``files or ["."]``).  A files
        # amendment must refresh it in place or the write-scope audit
        # unions the stale projection with the new files and dispatch
        # would still bind the old allowlist.  Every other profile field
        # stays byte-identical; a ``files`` value that cannot derive a
        # projection (a non-list truthy value) is left untouched so the
        # engineering re-verification below fails closed.
        old_files = (previous.get("contract") or {}).get("files")
        new_files = contract.get("files")
        derived = _derived_allowlist(new_files)
        if (derived is not None and new_files != old_files
                and isinstance(contract.get("worker_profile"), dict)):
            contract["worker_profile"] = dict(
                contract["worker_profile"], allowlist=derived)
        merged.append(DAGNode.from_dict(node_dict))
    _rederive_integration_review(merged)
    return merged


def _rederive_integration_review(merged: List[DAGNode]) -> None:
    """Re-project the final reviewer's read scope onto the merged nodes.

    Publish derives ``integration-review.contract["files"]`` as
    ``integration_review_scope(business_nodes)`` -- the union of every
    business write scope, since the reviewer reads the whole delivery but
    owns nothing.  A files amendment must re-derive it or the stored
    projection disagrees with the candidate card's ``file_scope`` and
    dispatch would bind the reviewer to the stale union.  The spec cannot
    declare it: ``_validate_amendment_spec`` treats it as an engineering
    field on this node, so the value written here is deterministic.  The
    empty-union case mirrors publish too: ``files=[]`` plus the
    whole-project fallback profile, without which dispatch can never bind
    a reviewer session to the node.
    """
    for node in merged:
        if not is_integration_review_node(node):
            continue
        business = [item for item in merged
                    if not is_integration_review_node(item)]
        scope = integration_review_scope(business)
        contract = node.contract if isinstance(node.contract, dict) else {}
        contract["files"] = list(scope)
        profile = contract.get("worker_profile")
        if profile is not None and not isinstance(profile, dict):
            # A profile that is not a mapping can carry no projection at
            # all; refuse the amendment instead of passing the malformed
            # value through to the candidate card and dispatch.
            raise ValueError(
                "contract amendment cannot keep malformed "
                "contract.worker_profile on node {}".format(node.id))
        if scope:
            if isinstance(profile, dict):
                # A plan published before the scope was derived can carry a
                # stale whole-project profile; narrow it to the union so the
                # stored contract keeps one projection.
                contract["worker_profile"] = dict(profile,
                                                  allowlist=list(scope))
        elif isinstance(profile, dict):
            # The publish-time fallback for an empty union is the
            # whole-project allowlist.  A profile narrowed by an earlier
            # amendment must return to it or the stale entries both block
            # this amendment at re-verification and bind paths no business
            # node still owns to the reviewer.
            contract["worker_profile"] = dict(profile, allowlist=["."])
        else:
            contract["worker_profile"] = {
                "worker": str(contract.get("worker", "worker")),
                "model": "default",
                "reasoning": "normal",
                "fallbacks": [],
                "selection_basis": {
                    "issue_complexity_ref": INTEGRATION_REVIEW_NODE_ID,
                    "complexity_band": "standard",
                    "risk_tags": ["integration", "read-only"],
                    "availability_evidence": "configured",
                },
                "writer": str(contract.get("writer", "worker")),
                "worktree": node_worktree(INTEGRATION_REVIEW_NODE_ID),
                "branch": node_branch(INTEGRATION_REVIEW_NODE_ID),
                "allowlist": ["."],
            }
        return


def _derived_allowlist(files: Any) -> Optional[List[str]]:
    """Mirror the publish-time projection ``contract.files or ["."]``.

    Returns ``None`` when ``files`` is a non-list truthy value: nothing
    legitimate derives from it and the caller must leave the stored
    projection untouched so re-verification rejects the amendment.
    """
    if isinstance(files, list):
        return list(files) or ["."]
    if not files:
        return ["."]
    return None


def _profile_projection_diff(previous: Any,
                             merged: Any) -> Optional[str]:
    """The only engineering difference a files amendment may create.

    ``previous`` is the stored ``worker_profile``; ``merged`` is the
    merged profile plus the node's new ``contract.files`` under the
    ``"files"`` key.  Returns a field path when the profile differs by
    anything other than ``allowlist`` becoming exactly the projection
    derived from those files, ``None`` when the difference is legal.
    """
    if merged == previous:
        return None
    if not isinstance(merged, dict) or not isinstance(previous, dict):
        return "worker_profile"
    expected = _derived_allowlist(merged.get("files"))
    if expected is None:
        return "worker_profile.allowlist"
    if "allowlist" not in merged:
        return "worker_profile.allowlist"
    if merged.get("allowlist") != expected:
        return "worker_profile"
    for key in set(previous) | set(merged):
        if key in ("allowlist", "files"):
            continue
        if previous.get(key) != merged.get(key):
            return "worker_profile." + str(key)
    return None


def _business_diff(old_nodes: Dict[str, Dict[str, Any]],
                   new_nodes: List[DAGNode]) -> Dict[str, Any]:
    diff: Dict[str, Any] = {}
    for node in new_nodes:
        old = old_nodes[node.id]
        changes: Dict[str, Any] = {}
        for key in _BUSINESS_NODE_KEYS:
            if old.get(key) != getattr(node, key):
                changes[key] = {"previous": old.get(key),
                                "next": getattr(node, key)}
        old_contract = old.get("contract") or {}
        for key in _BUSINESS_CONTRACT_KEYS:
            if old_contract.get(key) != node.contract.get(key):
                changes["contract." + key] = {
                    "previous": old_contract.get(key),
                    "next": node.contract.get(key)}
        if changes:
            diff[node.id] = changes
    return diff


def _amendment_revision(plan_root: Path) -> int:
    base = amendments_dir_from_root(plan_root)
    existing = 0
    if base.is_dir():
        for item in base.iterdir():
            if item.is_dir():
                try:
                    existing += 1
                except OSError:
                    pass
    data = _read_artifact(plan_root / "plan.json")
    recorded = data.get("amendment_revision") or 0
    try:
        recorded = int(recorded)
    except (TypeError, ValueError):
        recorded = 0
    return max(existing, recorded) + 1


def amendments_dir_from_root(plan_root: Path) -> Path:
    return plan_root / AMENDMENTS_DIRNAME


def _topology_rulings(paths: ProjectPaths):
    from .cli import _observed_topology_rulings
    return _observed_topology_rulings(paths)


def _candidate_card(paths: ProjectPaths, plan: Plan, nodes: List[DAGNode],
                    previous_card: AuthorizationCard, spec: Dict[str, Any],
                    workflow: Dict[str, Any]) -> AuthorizationCard:
    local_commit = spec.get("local_commit_allowed")
    if local_commit is not None and type(local_commit) is not bool:
        raise ValueError("local_commit_allowed must be true or false")
    # A spec that omits the choice keeps the approved one; explicit false
    # withdraws the exception and must be re-signed.
    flag = previous_card.local_commit_allowed if "local_commit_allowed" not in spec \
        else (True if local_commit is True else None)
    remote = str(spec.get("remote_git_actions") or previous_card.remote_git_actions)
    if remote != previous_card.remote_git_actions:
        raise ValueError(
            "remote_git_actions cannot change through a contract amendment; "
            "the Git remote group is a separate authorization")
    engine_attestation = None
    attestation_path = _plan_root(paths, plan.plan_id) / "engine-attestation.json"
    if attestation_path.is_file() and not attestation_path.is_symlink():
        try:
            engine_attestation = json.loads(
                attestation_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("engine-attestation.json is unreadable")
    return refresh_authorization_card(
        plan, nodes, previous_card,
        workflow=workflow,
        topology_rulings=_topology_rulings(paths),
        local_commit_allowed=flag,
        engine_attestation=engine_attestation,
        heartbeat_interval_options=spec.get("_heartbeat_options"),
        rotate_hard_cap_options=spec.get("_rotate_options"),
    )


def propose(
    paths: ProjectPaths,
    plan_id: str,
    spec: Any,
    reason: str,
) -> Dict[str, Any]:
    """Stage an immutable amendment candidate; nothing live is modified."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("amendment requires a non-empty reason")
    plan_root = _plan_root(paths, plan_id)
    data = _plan_data(plan_root)
    if data.get("plan_id") != plan_id:
        raise ValueError("plan id mismatch for " + str(plan_id))
    record = _load_record(plan_root)
    card = _load_card(plan_root)
    if record.digest != card.digest:
        raise ValueError("live card and authorization record disagree")
    if isinstance(spec, dict) and spec.get("previous_authorization_digest") \
            and spec["previous_authorization_digest"] != record.digest:
        raise ValueError(
            "previous_authorization_digest does not match the live record")
    previous_nodes = _node_map(plan_root)
    if isinstance(spec, dict) and "nodes" not in spec:
        raise ValueError("amendment spec must carry the full node list")
    decisions = _validate_amendment_spec(spec, previous_nodes)
    require_no_pending_transfer(paths, plan_id)
    plan = _plan_object(plan_root, data)
    nodes = _merge_nodes(spec, previous_nodes)
    # Business fields only changed; every engineering field must come back
    # identical after derivation, proving identities are preserved.
    for node in nodes:
        previous = DAGNode.from_dict(previous_nodes[node.id])
        for key, value in previous.contract.items():
            if key in _BUSINESS_CONTRACT_KEYS:
                continue
            if key == "worker_profile":
                merged_profile = node.contract.get("worker_profile")
                if merged_profile == value:
                    continue
                if isinstance(merged_profile, dict):
                    merged_profile = dict(
                        merged_profile, files=node.contract.get("files"))
                drift = _profile_projection_diff(value, merged_profile)
                if drift is not None:
                    raise ValueError(
                        "contract amendment cannot change engineering field "
                        "contract.{} on node {}".format(drift, node.id))
            elif node.contract.get(key) != value:
                raise ValueError(
                    "contract amendment cannot change engineering field "
                    "contract.{} on node {}".format(key, node.id))
    workflow = None
    state = paths.vibe / "state.json"
    if state.is_file():
        from .authorize_entry import load_live_workflow
        workflow = load_live_workflow(paths, plan_id)
    candidate_card = _candidate_card(
        paths, plan, nodes, card, spec, workflow)
    graph = dependency_graph(nodes)
    revision = _amendment_revision(plan_root)
    diff = _business_diff(previous_nodes, nodes)
    candidate = {
        "schema_version": 1,
        "kind": "plan_amendment_candidate",
        "status": "pending_authorization",
        "plan_id": plan_id,
        "plan_version": plan.version,
        "run_id": _current_run_id(plan_root),
        "revision": revision,
        "reason": reason.strip(),
        "proposed_at": _now(),
        "previous_authorization_digest": record.digest,
        "previous_card_digest": card.digest,
        "previous_source_digests": {
            "plan_json": _fingerprint(plan_root / "plan.json"),
            "nodes_json": _fingerprint(plan_root / "nodes.json"),
            "prd_md": _fingerprint(plan_root / "prd.md"),
        },
        "node_ids": sorted(node.id for node in nodes),
        "diff": diff,
        "dag_audit": {"status": "reviewed", "node_count": len(nodes),
                      "plan_revision": str(plan.version),
                      "node_ids": sorted(node.id for node in nodes),
                      "dependency_graph_digest": dependency_graph_digest(graph)},
        "candidate_card": candidate_card.to_dict(),
    }
    # Stage every artifact first: the candidate digest must cover the
    # bytes the transfer will actually install (spec, card, nodes, audit,
    # diff), not just candidate.json's embedded copies.  Tampering any of
    # them later fails the load-time re-check (P1.4/B03).
    staging = Path(tempfile.mkdtemp(
        prefix=".amend-candidate-",
        dir=str(amendments_dir(paths, plan_id).parent)))
    try:
        _atomic_json(staging / "spec.json", spec)
        _atomic_json(staging / "authorization-card.json",
                     candidate_card.to_dict())
        _atomic_json(staging / "nodes.json",
                     [node.to_dict() for node in nodes])
        _atomic_json(staging / "dag-audit.json", candidate["dag_audit"])
        _atomic_json(staging / "diff.json", diff)
        candidate["artifact_digests"] = {
            name: _fingerprint(staging / name)
            for name in ("spec.json", "authorization-card.json",
                         "nodes.json", "dag-audit.json", "diff.json")
        }
        digest = _canonical_digest(candidate)
        candidate["amendment_digest"] = digest
        _atomic_json(staging / "candidate.json", candidate)
        target = candidate_dir(paths, plan_id, digest)
        if target.exists():
            raise FileExistsError(
                "amendment candidate already exists: " + digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(staging), str(target))
    except BaseException:
        shutil.rmtree(str(staging), ignore_errors=True)
        raise
    return candidate


def _fingerprint(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        return ""
    return _artifact_hash(path)


CANDIDATE_ARTIFACTS = ("spec.json", "authorization-card.json", "nodes.json",
                       "dag-audit.json", "diff.json")


def _load_candidate(paths: ProjectPaths, plan_id: str,
                    amendment_digest: str) -> Dict[str, Any]:
    if not isinstance(amendment_digest, str) or len(amendment_digest) != 64:
        raise ValueError("amendment digest must be a 64-char hex string")
    directory = candidate_dir(paths, plan_id, amendment_digest)
    candidate = _read_artifact(directory / "candidate.json")
    if not isinstance(candidate, dict):
        raise ValueError("amendment candidate is not an object")
    if candidate.get("amendment_digest") != amendment_digest:
        raise PermissionError("amendment candidate digest mismatch")
    body = {key: value for key, value in candidate.items()
            if key != "amendment_digest"}
    if _canonical_digest(body) != amendment_digest:
        raise PermissionError("amendment candidate was modified after proposal")
    # The digest bound the installed artifact bytes at proposal time;
    # re-verify every file the transfer (or show) will actually read.
    recorded = candidate.get("artifact_digests")
    if not isinstance(recorded, dict) or sorted(recorded) != sorted(
            CANDIDATE_ARTIFACTS):
        raise PermissionError("amendment candidate artifact ledger is invalid")
    for name, expected in recorded.items():
        if _fingerprint(directory / name) != expected:
            raise PermissionError(
                "amendment candidate artifact was modified: " + name)
    return candidate


def show(paths: ProjectPaths, plan_id: str,
         amendment_digest: str) -> Dict[str, Any]:
    """Render the candidate card + graph and record the bound receipt."""
    candidate = _load_candidate(paths, plan_id, amendment_digest)
    directory = candidate_dir(paths, plan_id, amendment_digest)
    nodes = [DAGNode.from_dict(dict(item))
             for item in _read_artifact(directory / "nodes.json")]
    graph = dependency_graph(nodes)
    receipt = {
        "schema": 1,
        "plan_id": plan_id,
        "plan_version": candidate["plan_version"],
        "amendment_digest": amendment_digest,
        "card_digest": candidate["candidate_card"]["digest"],
        "dependency_graph_digest": dependency_graph_digest(graph),
        "shown_at": _now(),
    }
    _atomic_json(
        _plan_root(paths, plan_id) / DISPLAY_RECEIPT_NAME, receipt)
    return {
        "status": "shown",
        "plan_id": plan_id,
        "amendment_digest": amendment_digest,
        "card": candidate["candidate_card"],
        "diff": candidate["diff"],
        "dag_audit": candidate["dag_audit"],
        "dependency_graph": graph,
        "display_receipt": receipt,
        "confirmation_hint": (
            "产品经理确认新卡后执行：vibe plan-amend confirm --plan {} "
            "--amendment {} --confirm 'AMEND {}'".format(
                plan_id, amendment_digest, amendment_digest)),
    }


def confirmation_token(amendment_digest: str) -> str:
    """The only token that may confirm this candidate."""
    return TOKEN_PREFIX + str(amendment_digest)


def _require_candidate_receipt(plan_root: Path, plan_id: str,
                               candidate: Dict[str, Any]) -> None:
    receipt_path = plan_root / DISPLAY_RECEIPT_NAME
    receipt = None
    if receipt_path.is_file() and not receipt_path.is_symlink():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            receipt = None
    if not isinstance(receipt, dict):
        raise PermissionError(
            "amendment candidate has not been shown: run plan-amend show first")
    if receipt.get("amendment_digest") != candidate["amendment_digest"]:
        raise PermissionError(
            "the display receipt belongs to a different amendment candidate")
    if receipt.get("card_digest") != candidate["candidate_card"]["digest"]:
        raise PermissionError("the displayed card no longer matches")
    if str(receipt.get("plan_version")) != str(candidate["plan_version"]):
        raise PermissionError("the display receipt predates this plan version")


def _live_executors(paths: ProjectPaths, run_id: str) -> List[str]:
    """Every reason the run cannot be proven quiescent; empty means still."""
    problems: List[str] = []
    try:
        snapshot = load_snapshot(paths, run_id)
    except Exception as error:
        return ["run snapshot unreadable: " + str(error)]
    if snapshot.handles:
        problems.append("active task handles: " + ",".join(sorted(snapshot.handles)))
    for node_id, node in snapshot.nodes.items():
        if node.get("status") in _ACTIVE_NODE_STATUSES:
            problems.append("node {} is {}".format(node_id, node.get("status")))
        if node.get("active_task") or node.get("active_role"):
            problems.append("node {} carries an active task".format(node_id))
        if node.get("retryable_action"):
            problems.append("node {} has an unapplied retryable action".format(node_id))
    try:
        store = ProviderActionStore(paths)
        pending = store.pending(run_id)
    except Exception as error:
        problems.append("provider pending scan unreadable: " + str(error))
    else:
        if pending:
            problems.append(
                "pending provider side effects: "
                + ",".join(str(item.get("action_id")) for item in pending))
        # A retired uninvoked create must stay invisible *and* unclaimed:
        # a late result, observation, claim or worktree write after
        # retirement is evidence the "never invoked" proof was wrong, so
        # the run goes back to blocked_unknown instead of consuming it.
        stale_dir = store.root / "stale"
        if stale_dir.is_dir():
            for marker_path in sorted(stale_dir.glob("action-*.json")):
                try:
                    marker = json.loads(
                        marker_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    problems.append(
                        "provider stale marker unreadable: " + marker_path.name)
                    continue
                if (marker.get("run_id") != run_id
                        or marker.get("reason") != RETIRE_UNINVOKED_REASON):
                    continue
                action_id = marker.get("action_id")
                node_id = marker.get("issue_id")
                if (store.root / "results" / marker_path.name).exists():
                    problems.append(
                        "retired action {} gained a late result".format(
                            action_id))
                if (store.root / "observations").is_dir() and any(
                        (store.root / "observations").glob(
                            str(action_id) + "-*.json")):
                    problems.append(
                        "retired action {} gained a late observation".format(
                            action_id))
                claim = _retire_claim_evidence(
                    store, run_id, str(node_id),
                    str(marker.get("amendment_digest") or ""))
                if claim is not None:
                    problems.append(
                        "retired node {} has late claim evidence: {}".format(
                            node_id, claim))
                node = snapshot.nodes.get(node_id) if isinstance(
                    getattr(snapshot, "nodes", None), dict) else None
                base = node.get("worktree_base") if isinstance(node, dict) else None
                wt = node.get("worktree") if isinstance(node, dict) else None
                if isinstance(wt, str) and wt:
                    wt_path = paths.root / wt
                    try:
                        if not isinstance(base, str) or len(base) != 40:
                            raise ValueError("worktree_base missing")
                        head = _git_probe(wt_path, "rev-parse", "HEAD")
                        dirty = _git_probe(
                            wt_path, "status", "--porcelain")
                        if head != base:
                            problems.append(
                                "retired node {} worktree head moved".format(
                                    node_id))
                        if dirty:
                            problems.append(
                                "retired node {} worktree is dirty".format(
                                    node_id))
                    except (ValueError, OSError) as error:
                        problems.append(
                            "retired node {} worktree unverifiable: {}".format(
                                node_id, error))
    try:
        leases_dir = paths.vibe / "leases"
        if leases_dir.is_dir():
            for lease_file in sorted(leases_dir.glob("*.json")):
                try:
                    lease = json.loads(
                        lease_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    problems.append(
                        "writer lease unreadable: " + lease_file.name)
                    continue
                if not isinstance(lease, dict):
                    problems.append(
                        "writer lease malformed: " + lease_file.name)
                    continue
                lease_run = lease.get("run_id")
                status = lease.get("status")
                if lease_run is not None and lease_run != run_id:
                    roots = {(paths.root / str(node.get("worktree"))).resolve()
                             for node in snapshot.nodes.values() if node.get("worktree")}
                    lease_root = lease.get("worktree")
                    if (not isinstance(lease_root, str)
                            or (paths.root / lease_root).resolve() in roots
                            or lease.get("node_id") in snapshot.nodes):
                        problems.append("foreign run writer lease conflicts with this run: " + lease_file.name)
                    continue
                # A released lease is unlinked, so anything on disk is an
                # unproven writer: ``active`` is live, ``quarantined`` is
                # "termination unproven, still owned" (state.py lease
                # lifecycle), and any other/missing status is unknown --
                # none of them proves the run quiescent.
                if status in {"active", "quarantined"}:
                    problems.append(
                        "writer lease {} is {} for node {}".format(
                            lease_file.name, status,
                            lease.get("node_id") or "?"))
                elif not status:
                    problems.append(
                        "writer lease {} has no status".format(
                            lease_file.name))
                else:
                    problems.append(
                        "writer lease {} has unknown status {}".format(
                            lease_file.name, status))
    except OSError as error:
        problems.append("writer leases unreadable: " + str(error))
    try:
        from .task_registry import _all_bindings
        for binding in _all_bindings(paths):
            if getattr(binding, "run_id", None) != run_id:
                continue
            status = str(getattr(binding, "status", "") or "")
            if status and status not in _TERMINAL_BINDING_STATUSES:
                problems.append(
                    "task binding {}:{} is {}".format(
                        getattr(binding, "node_id", "?"),
                        getattr(binding, "role", "?"), status))
    except Exception as error:
        problems.append("task registry unreadable: " + str(error))
    return problems


def _cas_lineage(paths: ProjectPaths, plan_id: str, plan_root: Path,
                 candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Re-verify every value the candidate bound, right before the switch."""
    record = _load_record(plan_root)
    card = _load_card(plan_root)
    if record.digest != candidate["previous_authorization_digest"]:
        raise PermissionError(
            "the live authorization changed since the candidate was proposed")
    if card.digest != candidate["previous_card_digest"] or record.digest != card.digest:
        raise PermissionError("live card and record disagree")
    run_id = _current_run_id(plan_root)
    if run_id != candidate.get("run_id"):
        raise PermissionError(
            "the bound run changed since the candidate was proposed")
    if run_id is None:
        raise PermissionError("the plan has no live run to amend")
    snapshot = load_snapshot(paths, run_id)
    if snapshot.authorization_digest != record.digest:
        raise PermissionError(
            "the run's authorization digest no longer matches the record")
    return {"record": record, "card": card, "run_id": run_id,
            "snapshot": snapshot}


def transfer_pending(paths: ProjectPaths, plan_id: str) -> bool:
    return barrier_path(paths, plan_id).is_file()


def require_no_pending_transfer(paths: ProjectPaths, plan_id: str) -> None:
    """Fail closed while a transfer marker is present.

    Recovery is attempted first: a marker whose committed event already
    exists finishes the projection, and one with no committed event restores
    the old state.  A marker that cannot be resolved -- conflicting digest,
    unreadable artifacts, unknown run -- raises instead of letting any entry
    point consume or dispatch on a half-amended plan.
    """
    marker = barrier_path(paths, plan_id)
    if marker.is_file():
        recover(paths, plan_id)


def _marker_state(paths: ProjectPaths, plan_id: str) -> Optional[Dict[str, Any]]:
    marker = barrier_path(paths, plan_id)
    if not marker.is_file() or marker.is_symlink():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PermissionError("amendment transfer marker is unreadable")
    if not isinstance(data, dict) or "amendment_digest" not in data:
        raise PermissionError("amendment transfer marker is malformed")
    return data


def _event_committed(paths: ProjectPaths, run_id: str,
                     amendment_digest: str) -> bool:
    try:
        for record in load_events(paths, run_id):
            if (record["event"] == "authorization_reauthorized"
                    and record["data"].get("change_reason") == CHANGE_REASON
                    and record["data"].get("amendment_digest") == amendment_digest):
                return True
    except Exception as error:
        raise PermissionError(
            "run event log unreadable during amendment recovery: " + str(error))
    return False


def _apply_staged(plan_root: Path, staged_dir: Path) -> None:
    """Install the rendered new artifacts (each write is os.replace)."""
    for name in ("authorization-card.json", "authorization.json", "plan.json",
                 "nodes.json", "prd.md", "dag-audit.json"):
        staged = staged_dir / "new" / name
        if staged.is_file():
            os.replace(str(staged), str(plan_root / name))
    for sub in ("specs", "issues"):
        staged_sub = staged_dir / "new" / sub
        live_sub = plan_root / sub
        if staged_sub.is_dir():
            if live_sub.is_dir():
                shutil.rmtree(str(live_sub))
            shutil.copytree(str(staged_sub), str(live_sub))


def _restore_previous(plan_root: Path, staged_dir: Path) -> None:
    """Put the pre-swap artifacts back byte for byte."""
    for name in ("authorization-card.json", "authorization.json", "plan.json",
                 "nodes.json", "plan-confirmation.json", "prd.md",
                 "dag-audit.json"):
        backup = staged_dir / "old" / name
        if backup.is_file():
            shutil.copy2(str(backup), str(plan_root / name))
    for sub in ("specs", "issues"):
        backup_sub = staged_dir / "old" / sub
        live_sub = plan_root / sub
        if backup_sub.is_dir():
            if live_sub.is_dir():
                shutil.rmtree(str(live_sub))
            shutil.copytree(str(backup_sub), str(live_sub))


def _write_staging(paths: ProjectPaths, plan_root: Path,
                   candidate: Dict[str, Any], run_id: str) -> Path:
    """Copy old artifacts aside and render the amended set under one dir."""
    plan_id = candidate["plan_id"]
    staging = Path(tempfile.mkdtemp(
        prefix=".amend-staging-", dir=str(plan_root)))
    old_dir = staging / "old"
    new_dir = staging / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    for name in ("authorization-card.json", "authorization.json", "plan.json",
                 "nodes.json", "plan-confirmation.json", "prd.md",
                 "dag-audit.json"):
        source = plan_root / name
        if source.is_file():
            shutil.copy2(str(source), str(old_dir / name))
    for sub in ("specs", "issues"):
        if (plan_root / sub).is_dir():
            shutil.copytree(str(plan_root / sub), str(old_dir / sub))
    # The pre-swap artifacts are history the candidate keeps verbatim: only
    # this staging step still sees them, so archive them now.
    history = candidate_dir(
        paths, plan_id, candidate["amendment_digest"])
    for name, target in (
        ("authorization-card.json", "previous-authorization-card.json"),
        ("authorization.json", "previous-authorization.json"),
    ):
        source = old_dir / name
        if source.is_file() and not (history / target).exists():
            shutil.copy2(str(source), str(history / target))
    cand_dir = candidate_dir(paths, plan_id, candidate["amendment_digest"])
    shutil.copy2(str(cand_dir / "authorization-card.json"),
                 str(new_dir / "authorization-card.json"))
    shutil.copy2(str(cand_dir / "nodes.json"), str(new_dir / "nodes.json"))
    shutil.copy2(str(cand_dir / "dag-audit.json"), str(new_dir / "dag-audit.json"))
    spec = _read_artifact(cand_dir / "spec.json")
    decisions = [DecisionCard(**item) for item in spec.get("decisions", [])]
    plan_data = _plan_data(plan_root)
    amended_plan = dict(plan_data)
    amended_plan["amendment_revision"] = candidate["revision"]
    amended_plan["status"] = "authorized"
    _atomic_json(new_dir / "plan.json", amended_plan)
    # plan-confirmation.json binds run_id + event_sequence, so it cannot be
    # staged before the transfer event exists; _finish_projection writes it.
    (new_dir / "prd.md").write_text(
        render_prd_markdown(spec, decisions,
                            plan_data.get("evidence_priority")), encoding="utf-8")
    (new_dir / "specs").mkdir()
    (new_dir / "issues").mkdir()
    amended_nodes = [DAGNode.from_dict(dict(item))
                     for item in _read_artifact(cand_dir / "nodes.json")]
    for node in amended_nodes:
        contract = node.contract
        (new_dir / "specs" / (node.id + ".md")).write_text(
            "# Spec: {}\n\nnode_id: {}\n状态：published\n审核：reviewed\n\n输入：{}\n\n输出：{}\n\n错误行为：{}\n\n验收示例：{}\n".format(
                node.title, node.id, contract.get("input"),
                contract.get("output"), contract.get("error_behavior"),
                contract.get("acceptance_example")), encoding="utf-8")
        (new_dir / "issues" / (node.id + ".md")).write_text(
            "# Issue: {}\n\nissue_id: {}\n状态：published\n审核：reviewed\n\n并行组：{}\n".format(
                node.title, node.id, node.parallel_group or "none"),
            encoding="utf-8")
    return staging


def _finish_projection(paths: ProjectPaths, plan_id: str, plan_root: Path,
                       candidate: Dict[str, Any], run_id: str,
                       runner: Any = None) -> Dict[str, Any]:
    """Post-event projection, idempotent.

    The artifacts may already be installed (the transfer committed them
    before crashing) or not (recovery after an early exit).  Re-applying
    the staged render is a no-op when they are, so both paths converge.
    """
    staging = _write_staging(paths, plan_root, candidate, run_id)
    try:
        _apply_staged(plan_root, staging)
    finally:
        shutil.rmtree(str(staging), ignore_errors=True)
    candidate_card = _load_card(
        candidate_dir(paths, plan_id, candidate["amendment_digest"]))
    record = authorize(candidate_card, "AUTHORIZE")
    snapshot = load_snapshot(paths, run_id)
    plan_data = _plan_data(plan_root)
    _atomic_json(
        plan_root / "plan-confirmation.json",
        {"status": "confirmed", "plan_id": plan_id,
         "plan_revision": str(plan_data.get("version")),
         "authorization_digest": record.digest,
         "authorization_required": True,
         "run_id": run_id,
         "event_sequence": snapshot.event_sequence,
         "publication": "plan_amendment",
         "amendment_digest": candidate["amendment_digest"]})
    _atomic_json(plan_root / "authorization.json", record.to_dict())
    # Evidence must name the token the user actually supplied; the
    # candidate-bound AMEND token is derived from the verified digest, so
    # it is the same string confirm required -- never a fabricated
    # AUTHORIZE.
    materialize_workflow_evidence(
        paths, plan_id, confirmation_token(candidate["amendment_digest"]))
    return {"record": record, "candidate_card": candidate_card}


def confirm(
    paths: ProjectPaths,
    plan_id: str,
    amendment_digest: str,
    token: str,
    runner: Any = None,
    _fault_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Transfer the same plan/run onto the confirmed amendment.

    ``_fault_at`` exists for the crash-boundary tests: ``before_event`` and
    ``after_event`` raise around the durable event so recovery can be
    exercised deterministically; production callers never pass it.
    """
    if token != confirmation_token(amendment_digest):
        raise PermissionError(
            "authorization requires the exact candidate-bound confirmation")
    candidate = _load_candidate(paths, plan_id, amendment_digest)
    plan_root = _plan_root(paths, plan_id)
    _require_candidate_receipt(plan_root, plan_id, candidate)
    with interprocess_lock(plan_root / ".amendment.lock"):
        marker = _marker_state(paths, plan_id)
        if marker is not None:
            if marker["amendment_digest"] != amendment_digest:
                raise PermissionError(
                    "a different amendment transfer is in progress")
            # An interrupted transfer of this same candidate: a committed
            # event finishes the projection; an uncommitted one is rolled
            # back and the confirm below retries the whole transaction.
            if _event_committed(paths, marker.get("run_id"), amendment_digest):
                return _recover_locked(
                    paths, plan_id, plan_root, runner=runner)
            _rollback_uncommitted(plan_root)
            marker_path = barrier_path(paths, plan_id)
            if marker_path.is_file():
                marker_path.unlink()
        lineage = _cas_lineage(paths, plan_id, plan_root, candidate)
        run_id = lineage["run_id"]
        problems = _live_executors(paths, run_id)
        if problems:
            raise PermissionError(
                "amendment requires a quiescent run: " + "; ".join(problems))
        staging = _write_staging(paths, plan_root, candidate, run_id)
        try:
            _atomic_json(barrier_path(paths, plan_id), {
                "schema": 1,
                "plan_id": plan_id,
                "amendment_digest": amendment_digest,
                "run_id": run_id,
                "started_at": _now(),
            })
            # The marker now blocks every execution entry, but a dispatch
            # that started between the earlier check and this write could
            # still be in flight; re-prove quiescence inside the protected
            # window and refuse instead of consuming or stopping it.
            problems = _live_executors(paths, run_id)
            if problems:
                raise PermissionError(
                    "an executor became active inside the transfer window: "
                    + "; ".join(problems))
            if _fault_at == "before_event":
                raise RuntimeError("fault injected before transfer event")
            _commit_transfer_event(
                paths, plan_id, plan_root, candidate, run_id,
                lineage["record"], lineage["snapshot"], staging,
                runner=runner)
            if _fault_at == "after_event":
                raise RuntimeError("fault injected after transfer event")
            finished = _finish_projection(
                paths, plan_id, plan_root, candidate, run_id, runner=runner)
            barrier_path(paths, plan_id).unlink()
        except BaseException:
            # The committed event decides the state: once it is durable the
            # marker stays and ``recover`` finishes the new projection on
            # the next entry; before it the complete old state goes back.
            if _event_committed(paths, run_id, amendment_digest):
                raise
            _restore_previous(plan_root, staging)
            if barrier_path(paths, plan_id).is_file():
                barrier_path(paths, plan_id).unlink()
            raise
        finally:
            shutil.rmtree(str(staging), ignore_errors=True)
    return {
        "status": "amended", "plan_id": plan_id, "run_id": run_id,
        "amendment_digest": amendment_digest,
        "authorization_digest": finished["record"].digest,
        "previous_authorization_digest":
            candidate["previous_authorization_digest"],
    }


def _commit_transfer_event(paths: ProjectPaths, plan_id: str, plan_root: Path,
                           candidate: Dict[str, Any], run_id: str,
                           record: AuthorizationRecord, snapshot: Any,
                           staging: Path,
                           runner: Any = None) -> None:
    """Publish the durable transfer inside ``Monitor.reauthorize``.

    The monitor validates previous lineage, records the
    ``authorization_reauthorized`` event with ``change_reason``
    ``plan_amendment`` and applies the new projection to the snapshot --
    task identities, cursors and node continuity included.  For the
    amendment reason the monitor refuses active executors and never
    schedules: the amended run only dispatches after the caller's own
    next monitor/resume.
    """
    from .cli import _authorization_card, _public_runner
    from .monitor import Monitor
    from .runners.fake import FakeRunner

    cand_dir = candidate_dir(paths, plan_id, candidate["amendment_digest"])
    new_card = _authorization_card(_read_artifact(cand_dir / "authorization-card.json"))
    new_nodes = [DAGNode.from_dict(dict(item))
                 for item in _read_artifact(cand_dir / "nodes.json")]
    plan_data = _plan_data(plan_root)
    plan = _plan_object(plan_root, plan_data)
    # The monitor must see the amended nodes: its projection binds them.
    plan.nodes = new_nodes
    new_record = authorize(new_card, "AUTHORIZE")
    # Install the new artifacts first so the monitor's digest reads see the
    # amended sources while the event is being committed; the caller's
    # staging still holds the pre-swap bytes for rollback.
    _apply_staged(plan_root, staging)
    monitor = Monitor(paths, plan, new_nodes)
    if runner is None:
        runner = FakeRunner()
    monitor.reauthorize(run_id, new_record, runner, CHANGE_REASON,
                        amendment_digest=candidate["amendment_digest"])


def _rollback_uncommitted(plan_root: Path) -> None:
    """Restore the pre-swap artifacts left behind by a killed transfer.

    The swap inside ``_commit_transfer_event`` runs before the durable
    event, so a process death in that window leaves the new artifacts
    installed with no committed event -- and the only copy of the old
    bytes is the staging directory the dead process never removed.
    """
    staging_dirs = sorted(
        (item for item in plan_root.glob(".amend-staging-*")
         if item.is_dir()),
        key=lambda item: item.stat().st_mtime)
    if staging_dirs:
        _restore_previous(plan_root, staging_dirs[-1])
    for item in staging_dirs:
        shutil.rmtree(str(item), ignore_errors=True)


def recover(paths: ProjectPaths, plan_id: str, runner: Any = None) -> Dict[str, Any]:
    """Resolve a transfer marker: finish the committed side, undo the rest.

    The marker can only be judged under the same ``.amendment.lock`` the
    owning ``confirm`` holds for the whole marker->event->projection
    transaction: a marker visible while that lock is held belongs to a
    live transfer and this call simply waits; once the lock is free any
    remaining marker is proven crash residue.  A lock that cannot be
    acquired in time means the transfer is still in progress -- the
    non-owner path fails closed, it never rolls anything back.
    """
    plan_root = _plan_root(paths, plan_id)
    try:
        with interprocess_lock(plan_root / ".amendment.lock"):
            return _recover_locked(paths, plan_id, plan_root, runner=runner)
    except TimeoutError as error:
        raise PermissionError(
            "amendment transfer is still in progress; this entry cannot "
            "resolve it") from error


def _recover_locked(paths: ProjectPaths, plan_id: str, plan_root: Path,
                    runner: Any = None) -> Dict[str, Any]:
    """``recover`` with ``.amendment.lock`` already held (or provably free)."""
    marker = _marker_state(paths, plan_id)
    if marker is None:
        return {"status": "clean", "plan_id": plan_id}
    digest = marker["amendment_digest"]
    run_id = marker.get("run_id")
    try:
        candidate = _load_candidate(paths, plan_id, digest)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PermissionError(
            "amendment marker names an unreadable candidate: " + str(error))
    if marker.get("run_id") != candidate.get("run_id"):
        raise PermissionError(
            "amendment marker and candidate disagree about the run")
    if _event_committed(paths, run_id, digest):
        finished = _finish_projection(
            paths, plan_id, plan_root, candidate, run_id, runner=runner)
        marker_path = barrier_path(paths, plan_id)
        if marker_path.is_file():
            marker_path.unlink()
        return {
            "status": "completed", "plan_id": plan_id, "run_id": run_id,
            "amendment_digest": digest,
            "authorization_digest": finished["record"].digest,
        }
    _rollback_uncommitted(plan_root)
    marker_path = barrier_path(paths, plan_id)
    if marker_path.is_file():
        marker_path.unlink()
    return {"status": "rolled_back", "plan_id": plan_id,
            "run_id": run_id, "amendment_digest": digest}


# ----------------------------------------------------------------------
# retire_uninvoked_legacy
#
# The narrowest safe close-out for a run whose dispatch-generation creates
# were recorded as durable provider requests but provably never invoked:
# the consuming supervisor shift completed without the create_thread call,
# every known consumer session was archived, a successor shift owns the
# supervisor lease by public CAS, and the contract worktrees are bit-clean
# at their dispatch heads.  Retirement then writes a dedicated stale audit
# marker beside the immutable request (the request is never deleted and
# ``pending()`` simply stops surfacing it), a single
# ``retire_uninvoked_legacy`` run event, and resets the node projection to
# ``planned`` while releasing the matching writer lease.  Holds are not
# touched; this is engineering recovery, not a product or authorization
# decision.
#
# Nothing here fabricates a provider result, deletes a request, edits the
# registry, or declares an unknown consumer safe: every field of the
# evidence bundle is re-verified against live on-disk state inside the
# amendment lock.  A late result/observation that appears afterwards is
# detected by ``_live_executors`` and blocks ``confirm`` -- it is never
# hidden and never consumed.
# ----------------------------------------------------------------------


def _git_probe(worktree: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(worktree)] + list(args),
        capture_output=True, text=True, timeout=15)
    if completed.returncode != 0:
        raise ValueError(
            "git probe failed for {}: {}".format(
                worktree, (completed.stderr or completed.stdout).strip()))
    return completed.stdout.strip()


def _rollout_native_calls(path: Path, only_ids=None) -> Dict[str, Any]:
    """Structured call/return pairs of one rollout log; fail closed on noise.

    Only a real ``function_call``/tool-call payload names a native
    invocation -- a ``tools.mcp__codex_app__create_thread`` string inside an
    exec command body is *not* a call, so textual grep cannot stand in for
    this parse.  Any unparseable line, call without return, or return
    without call raises, because an unreadable log is not proof that the
    create was never issued.
    """
    calls: Dict[str, Dict[str, Any]] = {}
    returns: Dict[str, Dict[str, Any]] = {}
    ordered: List[Dict[str, Any]] = []
    structured_host: List[Dict[str, Any]] = []
    structured_host_ids = set()
    structured_host_issues: List[Dict[str, Any]] = []
    host_item_ids = set()
    source_session = None
    session_meta_count = 0
    host_boundary_seen = False
    skipped_returns = 0
    native_rows = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError as error:
            raise ValueError(
                "rollout {} line {} is not parseable JSON".format(
                    path.name, line_number)) from error
        if not isinstance(entry, dict):
            raise ValueError(
                "rollout {} line {} is not an object".format(
                    path.name, line_number))
        native_rows.append(entry)
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else entry
        kind = payload.get("type")
        entry_type = entry.get("type")
        if entry_type == "response_item":
            if (not isinstance(entry.get("payload"), dict)
                    or not isinstance(kind, str)
                    or kind not in _KNOWN_RESPONSE_ITEM_TYPES):
                structured_host_issues.append({
                    "line": line_number, "reason": "unknown response item type",
                    "kind": kind})
                continue
        elif not isinstance(entry_type, str) or entry_type not in _KNOWN_ENVELOPE_TYPES:
            structured_host_issues.append({
                "line": line_number, "reason": "unknown envelope type",
                "kind": entry_type})
            continue
        if entry.get("type") == "session_meta":
            session_meta_count += 1
            if session_meta_count != 1 or host_boundary_seen:
                structured_host_issues.append({
                    "line": line_number, "reason": "duplicate or late session metadata"})
            source_session = payload.get("id")
        if (entry.get("type") == "event_msg" and isinstance(kind, str)
                and kind not in _KNOWN_NON_ITEM_EVENT_TYPES
                and kind not in {"item_completed", "item_started"}):
            structured_host_issues.append({
                "line": line_number, "reason": "unknown event kind",
                "kind": kind})
            continue
        if entry.get("type") == "event_msg" and (
                "item" in payload or not isinstance(kind, str)):
            if not isinstance(kind, str) or kind not in {"item_completed", "item_started"}:
                structured_host_issues.append({
                    "line": line_number, "reason": "invalid event schema"})
                continue
        # Codex archives also persist the host's structured tool boundary in
        # ``event_msg.item_completed``.  Keep it separate from the outer
        # response-item call until the turn ownership is known below.
        if isinstance(kind, str) and kind.startswith("item_"):
            host_boundary_seen = True
            item = payload.get("item")
            if kind not in {"item_completed", "item_started"}:
                structured_host_issues.append({
                    "line": line_number, "reason": "unknown host event kind"})
                continue
            if not isinstance(item, dict):
                structured_host_issues.append({
                    "line": line_number, "reason": "missing host item"})
                continue
            item_type = item.get("type")
            item_id = item.get("id")
            if (not isinstance(item_id, str) or not item_id
                    or not isinstance(payload.get("thread_id"), str)
                    or not payload.get("thread_id")
                    or not isinstance(payload.get("turn_id"), str)
                    or not payload.get("turn_id")):
                structured_host_issues.append({
                    "line": line_number, "reason": "host item lacks identity",
                    "item": item_id or item_type})
                continue
            if source_session is None or payload.get("thread_id") != source_session:
                structured_host_issues.append({
                    "line": line_number, "reason": "host item session mismatch",
                    "item": item_id})
                continue
            if item_id in host_item_ids:
                structured_host_issues.append({
                    "line": line_number, "reason": "duplicate host item",
                    "item": item_id})
                continue
            host_item_ids.add(item_id)
            if not isinstance(item_type, str) or item_type not in _KNOWN_HOST_ITEM_TYPES:
                structured_host_issues.append({
                    "line": line_number, "reason": "unknown item type",
                    "item": item_type})
                continue
            if kind == "item_started":
                structured_host_issues.append({
                    "line": line_number, "reason": "unfinished host item",
                    "item": item_type})
                continue
            if item_type in _EXECUTING_HOST_ITEM_TYPES:
                structured_host_issues.append({
                    "line": line_number, "reason": "unreconciled host item",
                    "item": item_id or item_type})
                continue
            if item_type in _BENIGN_HOST_ITEM_TYPES and any(
                    key in item for key in ("server", "tool", "arguments", "result", "parent_id")):
                structured_host_issues.append({
                    "line": line_number, "reason": "benign item carries execution fields",
                    "item": item_id})
                continue
            if (item_type in _BENIGN_HOST_ITEM_TYPES and
                    (("status" in item and item.get("status") != "completed")
                     or ("status" not in item and
                         (type(payload.get("started_at_ms")) is not int
                          or type(payload.get("completed_at_ms")) is not int
                          or payload["completed_at_ms"] < payload["started_at_ms"])))):
                structured_host_issues.append({
                    "line": line_number, "reason": "unfinished benign item",
                    "item": item_id})
                continue
            if item_type == "McpToolCall":
                valid_identity = (isinstance(item_id, str) and bool(item_id)
                                  and isinstance(payload.get("thread_id"), str)
                                  and bool(payload.get("thread_id"))
                                  and isinstance(payload.get("turn_id"), str)
                                  and bool(payload.get("turn_id")))
                valid_shape = (isinstance(item.get("server"), str)
                               and bool(item.get("server"))
                               and isinstance(item.get("tool"), str)
                               and bool(item.get("tool")))
                if not valid_identity or not valid_shape:
                    structured_host_issues.append({
                        "line": line_number, "reason": "malformed McpToolCall",
                        "item": item_id})
                    continue
                if item.get("status") != "completed":
                    structured_host_issues.append({
                        "line": line_number, "reason": "unfinished host call",
                        "item": item_id})
                    continue
                result = item.get("result")
                if (not isinstance(item.get("arguments"), dict)
                        or not isinstance(result, dict)
                        or result.get("isError") is not False
                        or not isinstance(result.get("content"), list)):
                    structured_host_issues.append({
                        "line": line_number, "reason": "unreconciled host result",
                        "item": item_id})
                    continue
                if item.get("server") != "codex_app" or item.get("tool") != "create_thread":
                    structured_host_issues.append({
                        "line": line_number, "reason": "unreconciled host call",
                        "item": item_id})
                    continue
                if item.get("server") == "codex_app" and item.get("tool") == "create_thread":
                    if only_ids is None or item_id in only_ids:
                        if item_id in structured_host_ids:
                            raise ValueError(
                                "structured host create repeats item id {}".format(item_id))
                        structured_host_ids.add(item_id)
                        structured_host.append({
                            "call_id": item_id,
                            "line": line_number,
                            "turn_id": payload["turn_id"],
                            "thread_id": payload["thread_id"],
                            "item": item,
                            "returned": isinstance(item.get("arguments"), dict)
                            and isinstance(item.get("result"), dict)
                            and item["result"].get("isError") is not True,
                        })
            continue
        if only_ids is not None and (payload.get("call_id") or payload.get("id")) not in only_ids:
            continue
        if isinstance(kind, str) and "call" in kind and (
                kind not in _NATIVE_CALL_TYPES
                and kind not in _NATIVE_RETURN_TYPES):
            raise ValueError(
                "rollout {} line {} carries an unclassifiable call type {}".format(
                    path.name, line_number, kind))
        if kind in _NATIVE_CALL_TYPES:
            # An exec-style payload can *host* a native invocation: the
            # host runtime executes ``tools.mcp__codex_app__create_thread(...)``
            # inside its argument text.  Only the call payload is scanned
            # for that shape -- tool documentation or prompt text quoting
            # the same signature is not an invocation.
            # Exec payloads are flagged, not rejected, at collection time:
            # the JavaScript-aware classifier above separates an actual
            # embedded invocation from a quoted reference, and
            # _verify_declared_creates decides whether each flagged call is
            # a declared non-legacy invocation or a retired binding.

            name = payload.get("name") or payload.get("tool_name")
            call_id = payload.get("call_id") or payload.get("id")
            if not isinstance(name, str) or not name:
                raise ValueError(
                    "rollout {} line {} carries an unnamed native call".format(
                        path.name, line_number))
            if not isinstance(call_id, str) or not call_id:
                raise ValueError(
                    "rollout {} line {} carries a native call without id".format(
                        path.name, line_number))
            if call_id in calls:
                raise ValueError(
                    "rollout {} line {} repeats native call id {}".format(
                        path.name, line_number, call_id))
            calls[call_id] = {"name": name, "call_id": call_id,
                              "line": line_number}
            calls[call_id]["payload"] = payload
            calls[call_id]["native_create"] = (
                name in _FORBIDDEN_RETIRE_CALLS)
            ordered.append(calls[call_id])
        elif kind in _NATIVE_RETURN_TYPES:
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                # Host records like delegation injections use the output
                # shape but carry only their own fco_/ctco_ id, never a
                # call_id: they are not tool returns and cannot serve as
                # pairing evidence.  They are counted, not trusted.
                skipped_returns += 1
                continue
            # The matching call may live in another consumer's log (the
            # fork-inherited shape): keep every identifiable return and
            # let the caller pair it across the retired consumer set.
            returns[call_id] = {"line": line_number,
                                "payload": payload}
    unpaired = sorted(set(calls) - set(returns))
    # A fork/successor shift inherits the parent's *in-flight* calls into
    # its own log without their returns (the return lives in the parent
    # record).  Those are reported, not silently excused: the caller pairs
    # each one against the owner log's completed call before release.
    returned = {call_id for call_id in calls if call_id in returns}
    host_ledger = _raw_host_ledger(native_rows, source_session) if only_ids is None else None
    for call in ordered:
        if call["call_id"] in returns:
            call["return_line"] = returns[call["call_id"]]["line"]
            call["return_payload"] = returns[call["call_id"]]["payload"]
        source = call["payload"].get("input")
        call["embedded_create"] = False
        if not isinstance(source, str):
            continue
        try:
            call["embedded_create"] = _embedded_create_invocations(source) > 0
        except ValueError:
            ledger_items = [item for item in (host_ledger or {}).get("items", [])
                            if item["outer_call_id"] == call["call_id"]]
            try:
                source_tokens = _js_tokens(source)
            except ValueError:
                source_tokens = []
            host_name = "m" + "cp__codex_app__list_projects"
            compatibility_name = "s" + "vc__codex_app__list_projects"
            accepted = (
                source_tokens in ([('id', 'text'), ('punct', '('), ('id', 'await'),
                    ('id', 'tools'), ('punct', '.'), ('id', name), ('punct', '('),
                    ('punct', '{'), ('punct', '}'), ('punct', ')'), ('punct', ')'),
                    ('punct', ';')] for name in (host_name, compatibility_name))
                and len(ledger_items) == 1
                and not (host_ledger or {}).get("issues")
                and ledger_items[0]["item"].get("server") == "codex_app"
                and ledger_items[0]["item"].get("tool") == "list_projects"
                and ledger_items[0]["item"].get("arguments") == {}
                and ledger_items[0]["item"].get("status") == "completed"
                and ledger_items[0]["invocation_verified"]
                and ledger_items[0]["continuation_call_id"] is None)
            if accepted:
                host_values = _native_return_values({"output": ledger_items[0]["item"].get("result")})
                outer_values = _native_return_values(call.get("return_payload"))
                accepted = len(host_values) == 1 and outer_values == host_values
            if accepted:
                call["host_audit"] = "direct_list_projects"
                continue
            # Only this fully constrained source form proves a raw newline
            # inside a quoted JS string, a lexical error before execution.
            # Error text alone could instead be thrown at runtime. Other
            # malformed or engine-specific forms remain unknown.
            output = (call.get("return_payload") or {}).get("output")
            if not (call["name"] == "exec" and isinstance(output, list)
                    and len(output) == 2 and all(isinstance(x, dict) and x.get("type") == "input_text" for x in output)
                    and re.fullmatch(r"Script failed\nWall time [0-9.]+ seconds\nOutput:\n", str(output[0].get("text", "")))
                    and output[1].get("text") == "Script error:\nSyntaxError: Invalid or unexpected token"
                    and re.fullmatch(
                        r'text\(await tools\.exec_command\(\{cmd:"[A-Za-z0-9 _.-]*\n[A-Za-z0-9 _.-]*"\}\)\);',
                        source)):
                raise
            call["syntax_rejected"] = True
    return {"calls": ordered, "unpaired": unpaired,
            "returned": sorted(returned), "returns": returns,
            "skipped_returns": skipped_returns,
            "structured_host": structured_host,
            "structured_host_issues": structured_host_issues,
            "raw_host_ledger": host_ledger}


def _retire_field(evidence: Dict[str, Any], key: str) -> Any:
    value = evidence.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError("retire evidence requires " + key)
    return value


def _native_return_values(payload):
    """Decode structured native results, including the exec/MCP text envelope.

    A thread id merely mentioned in prose, an error, or an unknown block
    cannot be promoted to a create/termination result.
    """
    def decode(value):
        if isinstance(value, str):
            try:
                return decode(json.loads(value))
            except (ValueError, TypeError):
                if re.fullmatch(r"Script completed\nWall time [^\n]+\nOutput:\n?", value):
                    return []
                raise ValueError("native return contains unstructured text")
        if isinstance(value, list):
            result = []
            for block in value:
                result.extend(decode(block))
            return result
        if not isinstance(value, dict):
            raise ValueError("native return is not structured")
        if value.get("isError") is True or value.get("error") not in (None, False):
            raise ValueError("native return is an error")
        if value.get("type") in {"text", "input_text"} and isinstance(value.get("text"), str):
            return decode(value["text"])
        if "content" in value:
            return decode(value["content"])
        return [value]
    if not isinstance(payload, dict) or "output" not in payload:
        raise ValueError("native return missing output")
    return decode(payload["output"])


def _native_call_arguments(call, tool):
    payload = call.get("payload") or {}
    name = str(call.get("name") or "")
    aliases = {tool, "codex_app__" + tool, "svc__codex_app__" + tool,
               "mcp__codex_app__" + tool, "tools.mcp__codex_app__" + tool}
    if name in aliases:
        args = payload.get("arguments")
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except ValueError as error:
            raise ValueError("native call arguments are not JSON") from error
        if not isinstance(args, dict):
            raise ValueError("native call lacks actual arguments")
        return [args]
    source = payload.get("input")
    if not isinstance(source, str):
        raise ValueError("native call has no static arguments")
    return [args for _name, args in _static_tool_invocations(source, aliases)]


def _native_session_record(session, archived):
    """Resolve one native Codex session record; ambiguity is not proof."""
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", session):
        raise ValueError("invalid native session identity")
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()
    pattern = "rollout-*-{}.jsonl".format(session)
    archive_root, active_root = home / "archived_sessions", home / "sessions"
    if ((archive_root.exists() and (archive_root.is_symlink() or not archive_root.is_dir()))
            or (active_root.exists() and (active_root.is_symlink() or not active_root.is_dir()))):
        raise ValueError("native session roots are not trusted directories")
    archives = list(archive_root.glob(pattern))
    active = list(active_root.glob("*/*/*/" + pattern))
    if archived and active:
        raise ValueError("archived consumer still has an active record")
    candidates = archives if archived else archives + active
    if len(candidates) != 1 or candidates[0].is_symlink() or not candidates[0].is_file():
        raise ValueError("native session record is missing or ambiguous")
    path = candidates[0]
    try:
        path.resolve().relative_to(archive_root if path in archives else active_root)
    except ValueError:
        raise ValueError("native session record escapes provider root")
    data = path.read_bytes()
    if not data or not data.endswith(b"\n"):
        raise ValueError("native session record has an incomplete final entry")
    rows = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
    if (not rows or not all(isinstance(row, dict) for row in rows)
            or rows[0].get("type") != "session_meta"
            or (rows[0].get("payload") or {}).get("id") != session):
        raise ValueError("native session record identity mismatch")
    return path, rows


def _direct_terminal_call_occurs(tokens, index):
    """Recognize only immediately awaited, unconditional top-level calls."""
    if index < 3 or tokens[index - 3:index] != [
            ("id", "await"), ("id", "tools"), ("punct", ".")]:
        return False
    prefix = tokens[:index - 3]
    if any(candidate == ("id", keyword) for keyword in ("throw", "return")
           for candidate in prefix):
        return False
    braces = []
    parens = []
    for pos, token in enumerate(prefix):
        if token == ("punct", "{"):
            braces.append(pos)
        elif token == ("punct", "}"):
            if not braces:
                return False
            braces.pop()
        elif token == ("punct", "("):
            parens.append(pos)
        elif token == ("punct", ")"):
            if not parens:
                return False
            parens.pop()
    if braces or any(pos == 0 or tokens[pos - 1] != ("id", "text") for pos in parens):
        return False
    segments = []
    segment_start = 0
    brace_depth = paren_depth = 0
    for pos, token in enumerate(prefix):
        if token == ("punct", "{"):
            brace_depth += 1
        elif token == ("punct", "}"):
            brace_depth -= 1
        elif token == ("punct", "("):
            paren_depth += 1
        elif token == ("punct", ")"):
            paren_depth -= 1
        elif token == ("punct", ";") and brace_depth == 0 and paren_depth == 0:
            segments.append(prefix[segment_start:pos])
            segment_start = pos + 1
    segments.append(prefix[segment_start:])
    for segment in segments[:-1]:
        if segment and (len(segment) < 2
                        or segment[0] != ("id", "text")
                        or segment[1] != ("punct", "(")):
            return False
        if segment:
            try:
                _audit_static_exec(
                    segment + [("punct", ";")],
                    {"mcp__codex_app__read_thread",
                     "mcp__codex_app__set_thread_archived",
                     "svc__codex_app__read_thread",
                     "svc__codex_app__set_thread_archived"})
            except ValueError:
                return False
    boundary = max((pos for pos, token in enumerate(prefix) if token[1] in {";", "}"}), default=-1)
    statement = prefix[boundary + 1:]
    if statement in ([], [("id", "text"), ("punct", "(")]):
        return True
    return (len(statement) == 3 and statement[0] == ("id", "const")
            and statement[1][0] == "id" and statement[1][1] != "tools"
            and statement[2] == ("punct", "="))


def _raw_host_ledger(rows, session):
    """Retain observed host boundaries; unpaired or ambiguous facts stay unknown."""
    issues, items, starts, returns = [], [], {}, {}
    if (not isinstance(rows, list) or not rows or not isinstance(rows[0], dict)
            or rows[0].get("type") != "session_meta"
            or not isinstance(rows[0].get("payload"), dict)
            or rows[0]["payload"].get("id") != session):
        return {"items": [], "issues": [{"reason": "source session mismatch"}]}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            issues.append({"line": index + 1, "reason": "invalid source row"})
            continue
        payload = row.get("payload")
        if row.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        call_id = payload.get("call_id")
        kind = payload.get("type")
        if kind in _NATIVE_CALL_TYPES:
            target = starts
        elif kind in _NATIVE_RETURN_TYPES:
            target = returns
        else:
            continue
        if not isinstance(call_id, str) or not call_id:
            continue
        target.setdefault(call_id, []).append((index, payload))
    seen = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("type") != "event_msg":
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "item_completed":
            continue
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "McpToolCall":
            continue
        item_id, turn = item.get("id"), payload.get("turn_id")
        if (not isinstance(item_id, str) or not item_id or item_id in seen
                or payload.get("thread_id") != session or not isinstance(turn, str) or not turn
                or not isinstance(item.get("server"), str) or not item["server"]
                or not isinstance(item.get("tool"), str) or not item["tool"]
                or not isinstance(item.get("arguments"), dict)
                or not isinstance(item.get("result"), dict)
                or not isinstance(item["result"].get("content"), list)
                or type(item["result"].get("isError")) is not bool
                or item.get("status") not in {"completed", "failed"}
                or (item["status"] == "completed") == item["result"]["isError"]
                or type(payload.get("started_at_ms")) is not int
                or type(payload.get("completed_at_ms")) is not int
                or payload["completed_at_ms"] < payload["started_at_ms"]):
            issues.append({"line": index + 1, "reason": "invalid or duplicate host item"})
            continue
        seen.add(item_id)
        matches = []
        for call_id, entries in starts.items():
            endings = returns.get(call_id, [])
            if len(entries) != 1 or len(endings) != 1:
                continue
            start, call = entries[0]
            end, result = endings[0]
            meta = result.get("internal_chat_message_metadata_passthrough")
            result_turn = meta.get("turn_id") if isinstance(meta, dict) else None
            if start < index < end and result_turn == turn:
                matches.append((call_id, call, result))
        if len(matches) != 1:
            issues.append({"line": index + 1, "reason": "host outer interval not unique"})
            continue
        call_id, call, result = matches[0]
        continuation = None
        if call.get("name") == "wait":
            try:
                args = call.get("arguments")
                args = json.loads(args) if isinstance(args, str) else args
                cell = args.get("cell_id") if isinstance(args, dict) else None
                if not isinstance(cell, str) or not cell:
                    raise ValueError("wait has no cell identity")
                origins = []
                for origin_id, entries in starts.items():
                    endings = returns.get(origin_id, [])
                    if len(entries) != 1 or len(endings) != 1:
                        continue
                    origin_line, origin = entries[0]
                    end_line, output = endings[0]
                    raw = output.get("output")
                    blocks = raw if isinstance(raw, list) else [{"text": raw}]
                    if (origin.get("name") != "exec" or end_line >= index
                            or not any(isinstance(block, dict)
                                and isinstance(block.get("text"), str)
                                and re.search(r"(?:^|\n)Script running with cell ID "
                                              + re.escape(cell) + r"(?:\n|$)", block["text"])
                                for block in blocks)):
                        continue
                    meta = output.get("internal_chat_message_metadata_passthrough") or {}
                    if meta.get("turn_id") == turn:
                        origins.append((origin_id, origin, output))
                if len(origins) != 1:
                    raise ValueError("wait cell origin not unique")
                continuation = call_id
                call_id, call, result = origins[0]
            except (ValueError, TypeError, AttributeError):
                issues.append({"line": index + 1, "reason": "wait cell origin unknown"})
                continue
        invocation_verified = False
        tool = item["tool"]
        source = call.get("input")
        if call.get("name") == "exec" and isinstance(source, str):
            aliases = {"m" + "cp__codex_app__" + tool,
                       "s" + "vc__codex_app__" + tool}
            try:
                tokens = _js_tokens(source)
                token_candidates = [i for i, token in enumerate(tokens)
                                    if token[0] == "id" and token[1] in aliases]
                if len(token_candidates) == 1:
                    pos = token_candidates[0]
                    if (pos >= 2 and tokens[pos - 2:pos] == [("id", "tools"), ("punct", ".")]
                            and pos + 1 < len(tokens) and tokens[pos + 1][1] == "("):
                        args, args_end = _static_js_value(tokens, pos + 2, {})
                        invocation_verified = (
                            args == item["arguments"] and args_end < len(tokens)
                            and tokens[args_end][1] == ")"
                            and not any(token == ("id", "tools") and (
                                i == 0 or tokens[i - 1][1] in {"const", "let", "var"}
                                or tokens[i + 1:i + 2] != [("punct", ".")]
                                or i + 3 >= len(tokens) or tokens[i + 2][0] != "id"
                                or tokens[i + 3][1] != "(")
                                for i, token in enumerate(tokens)))
            except (ValueError, TypeError, IndexError):
                pass
        outcome_class = "completed" if item["status"] == "completed" else "unknown_failure"
        if item["status"] == "failed" and len(item["result"]["content"]) == 1:
            block = item["result"]["content"][0]
            message = block.get("text") if isinstance(block, dict) else None
            if (isinstance(message, str) and item["tool"] in {
                    "automation_update", "send_message_to_thread", "wait_threads"}
                    and message.startswith(item["tool"] + " received invalid arguments")):
                outcome_class = "validation_error"
            elif (isinstance(message, str) and item["tool"] == "send_message_to_thread"
                    and re.fullmatch(r"session [A-Za-z0-9_.-]+ is archived\. Run .+ to unarchive it first\.", message)):
                outcome_class = "archived_session_error"
            elif (item["tool"] == "automation_update" and message ==
                    "Automation does not exist in the app and could not be updated. "
                    "It may have been deleted manually by the user."):
                outcome_class = "automation_not_found"
        items.append({"host_id": item_id, "line": index + 1, "turn_id": turn,
                      "outer_call_id": call_id, "outer_call": call,
                      "outer_return": result, "continuation_call_id": continuation,
                      "invocation_verified": invocation_verified,
                      "outcome_class": outcome_class, "no_side_effect_proven": False,
                      "item": item})
    return {"items": items, "issues": issues}


def _terminal_read_projection_matches(host_result, outer_result):
    if not isinstance(host_result, dict) or not isinstance(outer_result, dict):
        return False
    host_thread, outer_thread = host_result.get("thread"), outer_result.get("thread")
    host_turns, outer_turns = host_result.get("turns"), outer_result.get("turns")
    if (not isinstance(host_thread, dict) or not isinstance(outer_thread, dict)
            or any(outer_thread.get(key) != host_thread.get(key)
                   for key in ("id", "hostId", "status"))
            or not isinstance(host_turns, list) or not isinstance(outer_turns, list)
            or len(host_turns) != len(outer_turns) or not host_turns
            or host_result.get("page") != outer_result.get("page")):
        return False
    for actual, projected in zip(host_turns, outer_turns):
        if (not isinstance(actual, dict) or not isinstance(projected, dict)
                or any(key not in projected or projected[key] != actual.get(key)
                       for key in ("id", "status", "error"))):
            return False
        if any(value != actual.get(key) for key, value in projected.items()):
            return False
    return True


def _combined_terminal_host_results(rows, call_id, observer_id, session, host):
    """Bind one returned exec interval to its actual terminal-tool items."""
    calls = []
    returns = []
    for index, row in enumerate(rows):
        payload = row.get("payload")
        if row.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        if payload.get("call_id") != call_id:
            continue
        if payload.get("type") in _NATIVE_CALL_TYPES:
            calls.append((index, payload))
        elif payload.get("type") in _NATIVE_RETURN_TYPES:
            returns.append((index, payload))
    if len(calls) != 1 or len(returns) != 1:
        raise ValueError("terminal host outer call/return is missing or ambiguous")
    start, call = calls[0]
    end, result = returns[0]
    turn = (call.get("internal_chat_message_metadata_passthrough") or {}).get("turn_id")
    return_turn = (result.get("internal_chat_message_metadata_passthrough") or {}).get("turn_id")
    if (start >= end or call.get("name") != "exec" or not isinstance(turn, str)
            or not turn or return_turn != turn):
        raise ValueError("terminal host outer call turn/return mismatch")
    source = call.get("input")
    if not isinstance(source, str):
        raise ValueError("terminal host outer call source missing")
    values = _native_return_values(result)
    expected = {}
    tokens = _js_tokens(source)
    # Validate the entire script, including suffix declarations whose lexical
    # scope affects earlier calls and suffix syntax that prevents compilation.
    _audit_static_exec(tokens, {prefix + tool
                               for prefix in ("svc__codex_app__", "mcp__codex_app__")
                               for tool in ("read_thread", "set_thread_archived")})
    for index, token in enumerate(tokens):
        if token == ("id", "tools") and (
                index == 0 or tokens[index - 1][1] in {"const", "let", "var"}
                or tokens[index + 1:index + 2] != [("punct", ".")]
                or index + 3 >= len(tokens)
                or tokens[index + 2][0] != "id" or tokens[index + 3][1] != "("):
            raise ValueError("terminal host access is shadowed or indirect")
    for tool in ("read_thread", "set_thread_archived"):
        candidates = []
        aliases = {"m" + "cp__codex_app__" + tool,
                   "s" + "vc__codex_app__" + tool}
        for index, token in enumerate(tokens):
            if token[0] != "id" or token[1] not in aliases:
                continue
            if (index < 2 or tokens[index - 2:index] != [("id", "tools"), ("punct", ".")]
                    or index + 1 >= len(tokens) or tokens[index + 1][1] != "("
                    or not _direct_terminal_call_occurs(tokens, index)):
                raise ValueError("terminal host invocation is not a direct tool call")
            args, args_end = _static_js_value(tokens, index + 2, {})
            if not isinstance(args, dict) or args_end >= len(tokens) or tokens[args_end][1] != ")":
                raise ValueError("terminal host invocation arguments are not static")
            if (args.get("threadId") == session and args.get("hostId", host) == host
                    and (tool != "set_thread_archived" or args.get("archived") is True)):
                candidates.append(args)
        if len(candidates) != 1:
            raise ValueError("terminal host outer invocation is missing or ambiguous")
        expected[tool] = candidates[0]
    all_ids = set()
    for index in range(start + 1, end):
        row = rows[index]
        if not isinstance(row, dict):
            raise ValueError("terminal host interval contains an invalid envelope")
        entry_type = row.get("type")
        payload = row.get("payload")
        if entry_type == "event_msg":
            if not isinstance(payload, dict):
                raise ValueError("terminal host event payload is missing")
            kind = payload.get("type")
            if kind in _KNOWN_NON_ITEM_EVENT_TYPES and "item" not in payload:
                continue
            if kind == "item_started":
                raise ValueError("terminal host item is unfinished")
            if kind != "item_completed":
                raise ValueError("terminal host event kind is unknown")
            item = payload.get("item")
            if not isinstance(item, dict):
                raise ValueError("terminal host item is missing")
            item_type = item.get("type")
            if item_type in _BENIGN_HOST_ITEM_TYPES:
                item_id = item.get("id")
                if (not isinstance(item_id, str) or not item_id
                        or item_id in all_ids
                        or payload.get("thread_id") != observer_id
                        or payload.get("turn_id") != turn
                        or type(payload.get("started_at_ms")) is not int
                        or type(payload.get("completed_at_ms")) is not int
                        or payload["completed_at_ms"] < payload["started_at_ms"]
                        or any(key in item for key in
                               ("server", "tool", "arguments", "result", "parent_id"))
                        or ("status" in item and item.get("status") != "completed")):
                    raise ValueError("terminal host benign item schema mismatch")
                all_ids.add(item_id)
                continue
            if item_type != "McpToolCall":
                raise ValueError("terminal host item type is unknown or executable")
            continue
        if entry_type in {"token_usage_record", "turn_context", "world_state",
                          "compacted", "inter_agent_communication_metadata"}:
            continue
        raise ValueError("terminal host interval envelope is unexpected")
    items = {}
    for index, row in enumerate(rows):
        payload = row.get("payload")
        if (row.get("type") != "event_msg" or not isinstance(payload, dict)
                or payload.get("type") != "item_completed"):
            continue
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "McpToolCall":
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id or item_id in all_ids:
            raise ValueError("terminal host item identity is missing or duplicated")
        all_ids.add(item_id)
        in_interval = start < index < end
        if not in_interval:
            continue
        tool = item.get("tool")
        args = item.get("arguments")
        if (item.get("server") != "codex_app" or tool not in expected
                or not isinstance(args, dict) or args.get("threadId") != session):
            raise ValueError("terminal host item tool or arguments mismatch")
        if (payload.get("thread_id") != observer_id
                or payload.get("turn_id") != turn or args != expected[tool]
                or type(payload.get("started_at_ms")) is not int
                or type(payload.get("completed_at_ms")) is not int
                or payload["completed_at_ms"] < payload["started_at_ms"]
                or item.get("status") != "completed"):
            raise ValueError("terminal host item source, turn or status mismatch")
        if tool in items:
            raise ValueError("terminal host tool result is ambiguous")
        host_values = _native_return_values({"output": item.get("result")})
        matched = [value for value in values if value == host_values[0]
                   or (tool == "read_thread"
                       and _terminal_read_projection_matches(host_values[0], value))] if len(host_values) == 1 else []
        if len(matched) != 1:
            raise ValueError("terminal host item result is not in returned outer output")
        items[tool] = host_values[0]
    if set(items) != set(expected):
        raise ValueError("terminal host tool item is missing")
    return items


def _verify_terminal_receipt(consumer, plan_id, run_id, host, registry):
    session, terminal = consumer["session_id"], consumer["native_terminal"]
    try:
        receipt = json.loads(Path(terminal["receipt_path"]).read_text(encoding="utf-8"))
        if (not isinstance(receipt, dict) or receipt.get("schema") != 1
                or receipt.get("kind") != "native_consumer_termination_evidence"
                or receipt.get("plan_id") != plan_id or receipt.get("run_id") != run_id
                or not str(receipt.get("source") or "").startswith(
                    "Codex App public read_thread and set_thread_archived")
                or consumer.get("host") != host):
            raise ValueError("native receipt source/plan/run/host mismatch")
        rows = receipt.get("consumers")
        if not isinstance(rows, list):
            raise ValueError("native receipt consumers missing")
        rows = [row for row in rows if isinstance(row, dict) and row.get("thread_id") == session]
        if len(rows) != 1:
            raise ValueError("native receipt does not uniquely bind the consumer")
        row = rows[0]
        observation, archived = row.get("native_observation"), row.get("native_archive_result")
        if not isinstance(observation, dict) or not isinstance(archived, dict):
            raise ValueError("native receipt missing tool results")
        state = observation.get("status")
        if (observation.get("id") != session or observation.get("hostId") != host
                or not isinstance(state, dict) or state.get("type") != terminal["read_thread_status"]
                or state.get("type") not in {"archived", "stopped", "failed", "closed", "terminated", "notLoaded"}
                or archived.get("threadId") != session or archived.get("archived") is not True):
            raise ValueError("native receipt terminal state/session/host mismatch")
        turns = observation.get("latest_turn")
        if (not isinstance(turns, list) or not turns
                or any(not isinstance(turn, dict) or not turn.get("id")
                       or turn.get("status") not in {"completed", "failed", "interrupted"} for turn in turns)):
            raise ValueError("native receipt has no proven terminal turn")
        source = terminal.get("source_rollout_path")
        if not isinstance(source, str) or not source:
            raise ValueError("native receipt requires source rollout binding")
        observer = receipt.get("observer") or {}
        observer_id = observer.get("session_id")
        native_path, _rows = _native_session_record(observer_id, archived=False)
        source_path = Path(source)
        if not source_path.exists() and str(observer.get("session_record") or "") == source:
            # Provider moved the exact named session record on archival.
            if native_path.name != source_path.name:
                raise ValueError("observer archived record identity mismatch")
            source_path = native_path
        if _artifact_hash(source_path) != terminal.get("source_rollout_sha256"):
            raise ValueError("native receipt source digest mismatch")
        if observer.get("session_record") is not None and observer["session_record"] != source:
            raise ValueError("native receipt observer source mismatch")
        entries = [entry for entry in [registry.get("current")] + (registry.get("history") or [])
                   if isinstance(entry, dict) and entry.get("session_id") == observer_id
                   and entry.get("lease_epoch") == observer.get("lease_epoch")
                   and entry.get("provider") == "codex" and entry.get("host") == host]
        if len(entries) != 1 or not entries[0].get("session_record_digest"):
            raise ValueError("observer is not uniquely registered")
        original = Path(str(observer.get("original_session_record") or observer.get("session_record") or ""))
        from .supervisor import _record_digest
        if _record_digest(original) != entries[0]["session_record_digest"]:
            raise ValueError("observer source is not the registered record")
        if native_path != original and native_path.name != original.name:
            raise ValueError("observer original and native archive do not match")
        source_bytes = source_path.read_bytes()
        native_bytes = native_path.read_bytes()
        if not source_bytes or not source_bytes.endswith(b"\n") or not native_bytes.startswith(source_bytes):
            raise ValueError("observer source is not a complete native prefix")
        prefix_rows = [json.loads(line) for line in source_bytes.decode("utf-8").splitlines() if line.strip()]
        if ([row.get("ordinal") for row in prefix_rows] != list(range(len(prefix_rows)))
                or not prefix_rows or prefix_rows[0].get("type") != "session_meta"
                or (prefix_rows[0].get("payload") or {}).get("id") != observer_id):
            raise ValueError("observer prefix identity or sequence mismatch")
        if observer.get("source_prefix_bytes", len(source_bytes)) != len(source_bytes):
            raise ValueError("observer source prefix length mismatch")
        if observer.get("source_prefix_sha256", sha256_bytes(source_bytes)) != sha256_bytes(source_bytes):
            raise ValueError("observer source prefix digest mismatch")
        refs = row.get("native_call_refs") or terminal.get("native_call_refs")
        if not isinstance(refs, dict):
            raise ValueError("native receipt requires tool call references")
        if any(not isinstance(refs.get(tool), str) or not refs[tool]
               for tool in ("read_thread", "set_thread_archived")):
            raise ValueError("native receipt has invalid call references")
        shared = refs["read_thread"] == refs["set_thread_archived"]
        if shared:
            host_values = _combined_terminal_host_results(
                prefix_rows, refs["read_thread"], observer_id, session, host)
        else:
            audit = _rollout_native_calls(source_path, only_ids=set(refs.values()))
        for tool in ("read_thread", "set_thread_archived"):
            if shared:
                value = host_values[tool]
            else:
                [call] = [call for call in audit["calls"] if call["call_id"] == refs.get(tool)]
                args = _native_call_arguments(call, tool)
                if len(args) != 1 or args[0].get("threadId") != session:
                    raise ValueError("native receipt source call targets another consumer")
                if tool == "set_thread_archived" and args[0].get("archived") is not True:
                    raise ValueError("native receipt source did not archive")
                values = _native_return_values(call.get("return_payload"))
                if len(values) != 1:
                    raise ValueError("native receipt source return is ambiguous")
                value = values[0]
            if tool == "set_thread_archived":
                if value.get("threadId") != session or value.get("archived") is not True:
                    raise ValueError("native archive source return mismatch")
            else:
                thread = value.get("thread", value)
                actual_turns = value.get("turns", value.get("latest_turn"))
                if (thread.get("id") != session or thread.get("hostId") != host
                        or thread.get("status") != observation["status"]
                        or not isinstance(actual_turns, list)):
                    raise ValueError("native read source return mismatch")
                for turn in turns:
                    if not any(all(other.get(k) == turn.get(k) for k in turn)
                               for other in actual_turns if isinstance(other, dict)):
                        raise ValueError("native source terminal turn mismatch")
                if shared:
                    native_read = value
        if shared:
            return native_read
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PermissionError("consumer {} native terminal proof invalid: {}".format(session, error)) from error


def _retire_consumer_proof(consumer: Dict[str, Any], index: int,
                           plan_id: str, run_id: str, host: str, registry: Dict[str, Any]) -> Dict[str, Any]:
    """Re-verify one archived consumer's rollout inside the lock."""
    session = consumer.get("session_id")
    if not isinstance(session, str) or not session:
        raise ValueError(
            "retire evidence consumer {} requires session_id".format(index))
    terminal = consumer.get("native_terminal")
    if not isinstance(terminal, dict):
        raise ValueError(
            "consumer {} requires a native_terminal receipt object".format(
                session))
    if terminal.get("archive_status") != "archived":
        raise PermissionError(
            "consumer {} archive receipt is not archived".format(session))
    if terminal.get("read_thread_status") not in {
            "archived", "stopped", "failed", "closed", "terminated",
            "notLoaded"}:
        raise PermissionError(
            "consumer {} read_thread state is not terminal".format(session))
    receipt = terminal.get("receipt_path")
    if not isinstance(receipt, str) or not receipt:
        raise ValueError(
            "consumer {} requires native_terminal.receipt_path".format(
                session))
    try:
        if (_artifact_hash(Path(receipt))
                != terminal.get("receipt_sha256")):
            raise PermissionError(
                "consumer {} receipt digest drifted".format(session))
    except (OSError, ValueError) as error:
        raise PermissionError(
            "consumer {} receipt unreadable: {}".format(session, error))
    native_read = _verify_terminal_receipt(consumer, plan_id, run_id, host, registry)
    rollout = consumer.get("rollout_path")
    if not isinstance(rollout, str) or not rollout:
        raise ValueError(
            "consumer {} requires rollout_path".format(session))
    rollout_path = Path(rollout)
    try:
        actual = _artifact_hash(rollout_path)
    except (OSError, ValueError) as error:
        raise PermissionError(
            "consumer {} rollout unreadable: {}".format(session, error))
    if actual != consumer.get("rollout_sha256"):
        raise PermissionError(
            "consumer {} rollout digest drifted".format(session))
    try:
        native_path, native_rows = _native_session_record(session, archived=True)
        if rollout_path.read_bytes() != native_path.read_bytes():
            raise ValueError("consumer source is not the complete native record")
        ordinals = [row.get("ordinal") for row in native_rows]
        if ordinals != list(range(len(native_rows))):
            raise ValueError("consumer native record has missing or reordered entries")
        receipt_data = json.loads(Path(receipt).read_text(encoding="utf-8"))
        [row] = [row for row in receipt_data["consumers"] if row.get("thread_id") == session]
        turns = row["native_observation"]["latest_turn"]
        tail = native_rows[-1].get("payload") or {}
        last = turns[-1]
        expected = {"completed": "task_complete", "interrupted": "turn_aborted", "failed": "turn_aborted"}
        terminal_kind = expected.get(last.get("status"))
        if last.get("status") == "failed" and isinstance(native_read, dict):
            actual = [turn for turn in native_read.get("turns", [])
                      if isinstance(turn, dict) and turn.get("id") == last.get("id")]
            error = actual[0].get("error") if len(actual) == 1 else None
            tail_error = tail.get("error")
            if (tail.get("type") == "task_complete" and isinstance(error, dict)
                    and isinstance(tail_error, dict) and isinstance(error.get("message"), str)
                    and error["message"] and error["message"] == tail_error.get("message")):
                terminal_kind = "task_complete"
        if (native_rows[-1].get("type") != "event_msg" or tail.get("type") != terminal_kind
                or tail.get("turn_id") != last.get("id")):
            raise ValueError("consumer native record does not reach its terminal turn")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise PermissionError("consumer {} native archive invalid: {}".format(session, error)) from error
    try:
        metadata = [json.loads(line).get("payload") for line in rollout_path.read_text(encoding="utf-8").splitlines()
                    if line.strip() and json.loads(line).get("type") == "session_meta"]
    except (OSError, ValueError, AttributeError) as error:
        raise PermissionError("consumer rollout metadata unreadable") from error
    if len(metadata) != 1 or not isinstance(metadata[0], dict) or metadata[0].get("id") != session:
        raise PermissionError("consumer rollout does not bind its session")
    try:
        audit = _rollout_native_calls(rollout_path)
    except (OSError, ValueError) as error:
        raise PermissionError(
            "consumer {} rollout cannot prove quiescence: {}".format(
                session, error))
    # Host records provide positive evidence of calls, not a coverage
    # guarantee.  Until an independently verifiable complete host boundary
    # exists, any structured create requires reconciliation and blocks this
    # no-invocation retirement path, even if the outer source looked inert.
    if audit["structured_host"] or audit.get("structured_host_issues"):
        item = (audit["structured_host"] or audit["structured_host_issues"])[0]
        raise PermissionError(
            "consumer {} has structured host boundary at line {}; "
            "host coverage and action binding require reconciliation".format(
                session, item["line"]))
    calls = audit["calls"]
    # A native or embedded create is flagged, not auto-fatal: a retired
    # supervisor legitimately creates its own successor.  The binding check
    # in _verify_declared_creates runs after request pairing and refuses
    # every flagged call that is not declared with its returned threads.
    declared = consumer.get("native_calls")
    if not isinstance(declared, list):
        raise ValueError(
            "consumer {} requires a native_calls list".format(session))
    observed = sorted(
        (item["name"], item["call_id"]) for item in calls)
    expected = sorted(
        (item.get("name"), item.get("call_id")) for item in declared
        if isinstance(item, dict))
    if observed != expected or any(
            item.get("returned") is not True for item in declared
            if isinstance(item, dict)) or len(declared) != len(calls):
        raise PermissionError(
            "consumer {} declared native calls disagree with its own log".format(
                session))
    # An in-flight call whose return lives in the *parent* log is the
    # inherited-history shape (fork/successor shifts carry the parent's
    # open calls).  It is only excused when the evidence names it and
    # another retired consumer's own log holds the matching return --
    # verified by the caller once every consumer was audited.
    inherited = consumer.get("inherited_unreturned") or []
    if not isinstance(inherited, list):
        raise ValueError(
            "consumer {} inherited_unreturned must be a list".format(session))
    inherited_ids = set()
    for item in inherited:
        if not isinstance(item, dict) or not isinstance(
                item.get("call_id"), str) or not item["call_id"]:
            raise ValueError(
                "consumer {} inherited_unreturned entries require "
                "call_id".format(session))
        inherited_ids.add(item["call_id"])
    if inherited_ids != set(audit["unpaired"]):
        raise PermissionError(
            "consumer {} has unreturned native calls not declared as "
            "inherited: {}".format(
                session, ", ".join(sorted(set(audit["unpaired"])
                                          - inherited_ids)) or "none"))
    audit["session_id"] = session
    audit["rollout_sha256"] = consumer["rollout_sha256"]
    return audit


def _retire_request_problems(
        store: ProviderActionStore, evidence: Dict[str, Any],
        run_id: str) -> List[Dict[str, Any]]:
    """Validated (action, request-path) pairs; the first defect aborts all."""
    declared = evidence.get("requests")
    if not isinstance(declared, list) or not declared:
        raise ValueError("retire evidence requires a non-empty requests list")
    pairs = []
    seen = set()
    request_dir = store.root / "requests"
    for index, item in enumerate(declared):
        if not isinstance(item, dict):
            raise ValueError("retire request {} must be an object".format(index))
        action_id = item.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            raise ValueError("retire request {} requires action_id".format(index))
        if action_id in seen:
            raise ValueError("retire request {} duplicated".format(action_id))
        seen.add(action_id)
        path = request_dir / (action_id + ".json")
        if not path.is_file():
            raise PermissionError(
                "provider request {} is not on disk".format(action_id))
        if _artifact_hash(path) != item.get("request_sha256"):
            raise PermissionError(
                "provider request {} digest drifted".format(action_id))
        try:
            action = store.action(action_id)
        except (OSError, ValueError, KeyError) as error:
            raise PermissionError(
                "provider request {} unreadable: {}".format(action_id, error))
        envelope = action.get("request")
        if not isinstance(envelope, dict):
            raise PermissionError(
                "provider request {} has no bound request envelope".format(
                    action_id))
        checks = (
            ("run_id", action.get("run_id"), run_id),
            ("operation", action.get("operation"), "create"),
            ("role", action.get("role"), item.get("role")),
            ("issue_id", action.get("issue_id"), item.get("issue_id")),
            ("generation", action.get("generation"), item.get("generation")),
            ("request_digest", action.get("request_digest"),
             item.get("request_digest")),
            ("authorization_digest", envelope.get("authorization_digest"),
             _retire_field(evidence, "authorization_digest")),
        )
        for field, actual, expected in checks:
            if actual != expected:
                raise PermissionError(
                    "provider request {} {} disagrees with the bound "
                    "evidence".format(action_id, field))
        if store.result(action_id) is not None:
            raise PermissionError(
                "provider request {} already carries a result".format(action_id))
        stale = _retire_stale_marker(store, action_id)
        if stale is not None and stale.get("reason") != RETIRE_UNINVOKED_REASON:
            raise PermissionError(
                "provider request {} carries a foreign stale marker".format(
                    action_id))
        if stale is not None and (
                stale.get("amendment_digest")
                != _retire_field(evidence, "amendment_digest")):
            raise PermissionError(
                "provider request {} was retired under a different "
                "amendment".format(action_id))
        claim = _retire_claim_evidence(
            store, run_id, str(action.get("issue_id")),
            _retire_field(evidence, "amendment_digest"))
        # Legacy/unmarked creates remain UNKNOWN to ordinary callers.  This
        # retirement path has already verified the complete consumer/native
        # terminal evidence chain above; only that exact proof may discharge
        # the legacy exposure marker. Controlled creates and every other
        # evidence kind remain fail-closed.
        legacy_retirement_proven = (
            claim == "unknown_create_exposure"
            and store._read_create_state(action) is None
        )
        if claim is not None and not legacy_retirement_proven:
            raise PermissionError(
                "provider request {} node already has durable claim "
                "evidence: {}".format(action_id, claim))
        pairs.append((action_id, path, stale is not None,
                        action))
    return pairs


def _verify_base_correction(
        correction: Dict[str, Any], node: Dict[str, Any],
        item: Dict[str, Any], node_id: str, wt_path: Path,
        audits: List[Dict[str, Any]]) -> str:
    """Re-verify a dispatch-head correction inside the retire lock."""
    if not isinstance(correction, dict):
        raise ValueError(
            "node {} base_correction must be an object".format(node_id))
    for key in ("old_snapshot_base", "corrected_base",
                "creation_rollout_sha256", "creation_call_id",
                "creation_command_digest", "output_digest",
                "reflog_digest", "actual_output"):
        if not isinstance(correction.get(key), str) or not correction[key]:
            raise ValueError(
                "node {} base_correction requires {}".format(node_id, key))
    old_base = correction["old_snapshot_base"]
    new_base = correction["corrected_base"]
    for label, base in (("old_snapshot_base", old_base),
                        ("corrected_base", new_base)):
        if len(base) != 40 or any(
                ch not in "0123456789abcdef" for ch in base.lower()):
            raise PermissionError(
                "node {} {} is not a commit digest".format(node_id, label))
    return _verify_correction_chain(
        correction, node, item, node_id, wt_path, audits, new_base)


def _verify_correction_chain(
        correction, node, item, node_id, wt_path, audits, new_base):
    """Bind corrected_base to reflog, the creation call and its output."""
    try:
        reflog = _git_probe(
            wt_path, "reflog", "show", str(node.get("branch")),
            "--format=%H %gs")
    except ValueError as error:
        raise PermissionError(
            "node {} reflog unreadable: {}".format(node_id, error))
    entries = [line for line in reflog.splitlines() if line.strip()]
    # The branch must have exactly one reflog entry: the initial Created
    # from <full sha> line written by git worktree add.  Any later
    # mutation -- a reset, commit or checkout -- produces a second entry
    # or a different subject, and the node then has un-audited history.
    created = ("{} branch: Created from {}".format(new_base, new_base))
    if entries != [created]:
        raise PermissionError(
            "node {} branch reflog is not the single create entry: "
            "{}".format(node_id, entries))
    if hashlib.sha256(reflog.encode("utf-8")).hexdigest() != (
            correction["reflog_digest"]):
        raise PermissionError(
            "node {} reflog digest drifted".format(node_id))
    call = None
    for audit in audits:
        if audit.get("rollout_sha256") == correction[
                "creation_rollout_sha256"]:
            for candidate in audit["calls"]:
                if candidate["call_id"] == correction["creation_call_id"]:
                    call = candidate
    if call is None:
        raise PermissionError(
            "node {} creation call is not inside an audited "
            "rollout".format(node_id))
    payload_text = json.dumps(
        call.get("payload") or {}, ensure_ascii=False, sort_keys=True)
    if hashlib.sha256(payload_text.encode("utf-8")).hexdigest() != (
            correction["creation_command_digest"]):
        raise PermissionError(
            "node {} creation command digest drifted".format(node_id))
    output = correction["actual_output"]
    if hashlib.sha256(output.encode("utf-8")).hexdigest() != (
            correction["output_digest"]):
        raise PermissionError(
            "node {} creation output digest drifted".format(node_id))
    # The declared output is not trusted on its own: it must also be the
    # exact payload the audited rollout recorded for this call's return.
    return_payload = call.get("return_payload")
    if not isinstance(return_payload, dict):
        raise PermissionError(
            "node {} creation call has no audited return payload".format(
                node_id))
    real_output = return_payload.get("output")
    real_text = None
    if isinstance(real_output, str):
        real_text = real_output
    elif isinstance(real_output, list):
        parts = [
            block.get("text") for block in real_output
            if isinstance(block, dict) and isinstance(block.get("text"), str)]
        if parts:
            real_text = chr(10).join(parts)
    if real_text is None:
        raise PermissionError(
            "node {} creation call return carries no textual output".format(
                node_id))
    if output != real_text:
        raise PermissionError(
            "node {} declared actual_output does not match the audited "
            "call return".format(node_id))
    worktree = str(node.get("worktree"))
    branch = str(node.get("branch"))
    expected_add = ("git worktree add {} -b {} {}".format(
        worktree, branch, new_base))
    if expected_add not in payload_text:
        raise PermissionError(
            "node {} creation call does not run the exact worktree add "
            "for this node".format(node_id))
    if ("Preparing worktree (new branch '{}')".format(branch)
            not in real_text):
        raise PermissionError(
            "node {} creation output does not prove the branch "
            "add".format(node_id))
    match = re.search(r"HEAD is now at ([0-9a-f]{6,40})", real_text)
    if match is None or not new_base.startswith(match.group(1)):
        raise PermissionError(
            "node {} creation output does not bind HEAD to the "
            "corrected base".format(node_id))
    return new_base


def _retire_intent_path(store: ProviderActionStore,
                        amendment_digest: str) -> Path:
    return store.root / "locks" / (
        "retire-{}.intent".format(amendment_digest[:16]))


def _retire_intent(store: ProviderActionStore, run_id: str,
                   amendment_digest: str) -> Optional[Dict[str, Any]]:
    """This amendment's own retire intent record, or None.

    The two-phase record distinguishes our crash residue from a foreign
    claim: phase 'intent' was written before any lock was acquired, phase
    'locked' names the exact lock-file SHAs this attempt held.  A record
    of another amendment or run, or an unreadable one, is foreign
    evidence -- callers fail closed on it.
    """
    path = _retire_intent_path(store, amendment_digest)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PermissionError(
            "retire intent record unreadable: {}".format(error))
    if not isinstance(data, dict):
        raise PermissionError("retire intent record is not an object")
    if (data.get("kind") != "retire_uninvoked_legacy_intent"
            or data.get("amendment_digest") != amendment_digest
            or data.get("run_id") != run_id):
        raise PermissionError(
            "a different retire attempt's intent record is present")
    if data.get("phase") not in {"intent", "locked", "complete"}:
        raise PermissionError("retire intent record phase is invalid")
    return data


def _retire_own_locks(store: ProviderActionStore, run_id: str,
                      amendment_digest: str) -> Optional[set]:
    """Action ids whose residual lock files belong to our own retire.

    None means no intent record exists; a set (possibly empty) means the
    listed locks are ours.  The lock SHA binds the residue to the intent,
    so a foreign completion lock written after our crash is still seen
    as foreign.
    """
    intent = _retire_intent(store, run_id, amendment_digest)
    if intent is None:
        return None
    action_ids = intent.get("action_ids")
    if not isinstance(action_ids, list):
        raise PermissionError("retire intent record action_ids invalid")
    if intent["phase"] == "intent":
        # Written before the first lock acquisition: any matching lock
        # file can only be our own residue (or a foreign claim; either
        # way the caller re-acquires and the in-lock result check
        # decides).
        return {str(item) for item in action_ids
                if isinstance(item, str)}
    lock_shas = intent.get("lock_shas")
    if not isinstance(lock_shas, dict):
        raise PermissionError("retire intent record lock_shas invalid")
    own = set()
    locks_dir = store.root / "locks"
    for action_id in action_ids:
        lock_path = locks_dir / (str(action_id) + ".lock")
        if not lock_path.is_file():
            continue
        try:
            actual = _artifact_hash(lock_path)
        except (OSError, ValueError):
            continue
        if actual == lock_shas.get(action_id):
            own.add(str(action_id))
    return own


def _retire_claim_evidence(
        store: ProviderActionStore, run_id: str, issue_id: str,
        amendment_digest: str) -> Optional[str]:
    """claim_evidence that recognizes this retire path's own lock residue.

    ProviderActionStore.claim_evidence counts any locks/<action>.lock as
    a claim mark.  retire holds the same name to fence complete() and
    records exactly which locks it owns in the two-phase intent record;
    a crash between marker writes and the event leaves residue that a
    rerun converges instead of deadlocking on.  Any other lock --
    foreign, historical, or a different amendment's -- stays 'claimed'
    (fail closed), and every other evidence kind mirrors claim_evidence
    exactly.
    """
    own_locks = _retire_own_locks(store, run_id, amendment_digest)
    try:
        action_ids = set()
        exposed_create = False
        request_dir = store.root / "requests"
        if request_dir.is_dir():
            for path in sorted(request_dir.glob("action-*.json")):
                action = store._read(path)
                if (action.get("run_id") == run_id
                        and action.get("issue_id") == issue_id):
                    action_ids.add(path.stem)
                    if action.get("operation") == "create":
                        exposed_create = True
        for action_id in sorted(action_ids):
            if ((store.root / "locks" / (action_id + ".lock")).is_file()
                    and (own_locks is None
                         or action_id not in own_locks)):
                return "lock"
            if (store.root / "results" / (action_id + ".json")).exists():
                return "result"
            observations = store.root / "observations"
            if observations.is_dir() and any(
                    observations.glob(action_id + "-*.json")):
                return "observation"
        handles = store.root / "handles"
        if handles.is_dir():
            for path in sorted(handles.glob("*.json")):
                meta = store._read(path)
                if (meta.get("run_id") == run_id
                        and meta.get("node_id") == issue_id):
                    return "handle"
        sessions = store.root / "claude-bg-sessions"
        if sessions.is_dir():
            for path in sorted(sessions.glob("*.json")):
                claims = store._read(path).get("claims")
                if (isinstance(claims, dict)
                        and claims.get("run_id") == run_id
                        and claims.get("node_id") == issue_id):
                    return "session"
        name = re.compile(
            "^" + re.escape(run_id) + "-" + re.escape(issue_id)
            + r"-(developer|reviewer)-g" + chr(92) + "d+" + chr(92)
            + ".(json|md)$")
        for directory in ("deliveries", "prompts"):
            folder = store.root / directory
            if folder.is_dir() and any(
                    name.match(item.name) for item in folder.iterdir()):
                return directory
    except (OSError, ValueError, TypeError, AttributeError):
        return "unreadable"
    return "unknown_create_exposure" if exposed_create else None


def _retire_stale_marker(store: ProviderActionStore, action_id: str):
    marker = store.root / "stale" / (action_id + ".json")
    if not marker.is_file():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PermissionError(
            "provider stale marker {} unreadable".format(action_id))
    return data if isinstance(data, dict) else None


def _retirement_event(paths, run_id, amendment_digest, evidence):
    """History is authoritative only for this exact proof and request set."""
    matches = [event for event in load_events(paths, run_id)
               if event["event"] == RETIRE_UNINVOKED_EVENT
               and event["data"].get("amendment_digest") == amendment_digest]
    if not matches:
        return None
    if len(matches) != 1:
        raise PermissionError("multiple retirement boundary events")
    event = matches[0]
    data = event["data"]
    if (data.get("plan_id") != evidence.get("plan_id")
            or data.get("authorization_digest") != evidence.get("authorization_digest")
            or data.get("retirement_proof", {}).get("evidence_sha256") != sha256_bytes(
                json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            or set(data.get("superseded_creates", [])) != {r["action_id"] for r in evidence["requests"]}):
        raise PermissionError("retirement event does not bind this evidence")
    return event


def _verify_retire_evidence(
        paths: ProjectPaths, plan_id: str, amendment_digest: str,
        evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Fail closed unless the whole uninvoked-legacy chain verifies.

    The return value is the validated context: run id, live snapshot, the
    bound request pairs and the takeover facts the event will cite.
    """
    if not isinstance(evidence, dict):
        raise ValueError("retire evidence must be an object")
    if evidence.get("schema_version") != _RETIRE_SCHEMA:
        raise ValueError("retire evidence schema_version must be {}".format(_RETIRE_SCHEMA))
    if evidence.get("kind") != _RETIRE_KIND:
        raise ValueError("retire evidence kind must be " + _RETIRE_KIND)
    run_id = _retire_field(evidence, "run_id")
    if _retire_field(evidence, "plan_id") != plan_id:
        raise PermissionError("retire evidence plan_id does not match")
    if _retire_field(evidence, "amendment_digest") != amendment_digest:
        raise PermissionError(
            "retire evidence is bound to a different amendment")
    plan_root = _plan_root(paths, plan_id)
    candidate = _load_candidate(paths, plan_id, amendment_digest)
    lineage = _cas_lineage(paths, plan_id, plan_root, candidate)
    if lineage["run_id"] != run_id:
        raise PermissionError("retire evidence run is not the pinned run")
    if (_retire_field(evidence, "authorization_digest")
            != lineage["record"].digest):
        raise PermissionError(
            "retire evidence authorization does not bind the live record")
    snapshot = lineage["snapshot"]
    committed = _retirement_event(paths, run_id, amendment_digest, evidence)
    takeover = evidence.get("owner_takeover")
    if not isinstance(takeover, dict):
        raise ValueError("retire evidence requires owner_takeover")
    for key in ("old_session_id", "new_session_id"):
        if not isinstance(takeover.get(key), str) or not takeover[key]:
            raise ValueError("owner_takeover requires " + key)
    for key in ("old_lease_epoch", "new_lease_epoch"):
        value = takeover.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("owner_takeover requires integer " + key)
    if takeover["new_lease_epoch"] <= takeover["old_lease_epoch"]:
        raise PermissionError("owner takeover epoch did not advance")
    registry_path = paths.vibe / "runs" / run_id / "supervisor-registry.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PermissionError("supervisor registry unreadable: " + str(error))
    current = registry.get("current")
    if not isinstance(current, dict) or current.get("session_id") != takeover["new_session_id"]:
        raise PermissionError(
            "the registered supervisor is not the takeover session")
    from .supervisor import read_lease_epoch
    if read_lease_epoch(paths, run_id) != takeover["new_lease_epoch"]:
        raise PermissionError("supervisor lease epoch does not bind the takeover")
    retired = evidence.get("retired_consumers")
    if not isinstance(retired, list) or not retired:
        raise ValueError("retire evidence requires retired_consumers")
    retired_ids = set()
    audits = []
    for index, consumer in enumerate(retired):
        if not isinstance(consumer, dict):
            raise ValueError("retired consumer {} must be an object".format(index))
        audits.append(_retire_consumer_proof(consumer, index, plan_id, run_id, current.get("host"), registry))
        retired_ids.add(consumer["session_id"])
    for audit in audits:
        for call_id in audit["unpaired"]:
            paired = any(
                call_id in other["returned"]
                for other in audits
                if other["session_id"] != audit["session_id"])
            if not paired:
                raise PermissionError(
                    "consumer {} has an unreturned native call {} with no "
                    "paired return in the retired consumer set".format(
                        audit["session_id"], call_id))
    if takeover["old_session_id"] not in retired_ids:
        raise PermissionError("the outgoing supervisor is not proven retired")
    history = registry.get("history")
    if isinstance(history, list):
        uncovered = [
            item.get("session_id") for item in history
            if isinstance(item, dict)
            and item.get("session_id") not in retired_ids]
        if uncovered:
            raise PermissionError(
                "registry history consumers are not proven retired: "
                + ", ".join(str(item) for item in uncovered))
    store = ProviderActionStore(paths)
    pairs = _retire_request_problems(store, evidence, run_id)
    creates = _verify_declared_creates(paths, registry, pairs, retired, audits)
    worktrees = evidence.get("worktrees")
    if not isinstance(worktrees, list) or len(worktrees) != len(pairs):
        raise ValueError(
            "retire evidence requires one worktree entry per retired node")
    clean_nodes = {}
    for index, item in enumerate(worktrees):
        if not isinstance(item, dict):
            raise ValueError("worktree evidence {} must be an object".format(index))
        node_id = item.get("node_id")
        node = snapshot.nodes.get(node_id)
        if node is None:
            raise PermissionError(
                "retire evidence names unknown node {}".format(node_id))
        if node.get("active_task") or node.get("active_role") or node_id in snapshot.handles:
            raise PermissionError(
                "node {} still carries a live task".format(node_id))
        retry = node.get("retryable_action")
        node_clean = (node.get("status") == "planned" and retry is None)
        pending_shape = (
            node.get("status") in {"running", "retry_pending"}
            and isinstance(retry, dict)
            and retry.get("role") == "developer"
            and retry.get("last_observation_ref") == "provider-pending"
            and retry.get("successor") is not True)
        if not (node_clean or pending_shape):
            raise PermissionError(
                "node {} is not the uninvoked-pending shape".format(node_id))
        try:
            from .task_registry import _all_bindings
            live_bindings = [
                binding for binding in _all_bindings(paths)
                if getattr(binding, "run_id", None) == run_id
                and getattr(binding, "node_id", None) == node_id
                and str(getattr(binding, "status", "") or "")
                not in _TERMINAL_BINDING_STATUSES]
        except Exception as error:
            raise PermissionError(
                "task registry unreadable for {}: {}".format(node_id, error))
        if live_bindings:
            raise PermissionError(
                "node {} has non-terminal task bindings".format(node_id))
        worktree = item.get("worktree")
        branch = item.get("branch")
        head = item.get("head")
        if worktree != node.get("worktree") or branch != node.get("branch"):
            raise PermissionError(
                "node {} worktree/branch evidence does not bind".format(node_id))
        lease = retirement_writer_lease(paths, node_id, str(worktree), run_id)
        if lease is None and committed is None:
            raise PermissionError("writer lease missing before retirement boundary")
        request = next((a for _id, _p, _old, a in pairs if a.get("issue_id") == node_id), None)
        if request is None:
            raise PermissionError("worktree has no matching retired request")
        if lease is not None:
            record = lease["record"]
            from .monitor import Monitor
            intent = (node.get("start_intent") or {}).get("intent_digest") or Monitor._prospective_intent_digest(
                snapshot, node_id, request["role"], request["generation"], node)
            if (record.get("role") != request["role"]
                    or record.get("generation") != request["generation"]
                    or record.get("intent_digest") != intent):
                raise PermissionError("writer lease does not bind the retired action intent")
            if committed is not None:
                recorded = committed["data"]["retirement_proof"].get("leases", [])
                if not any(row.get("lease_id") == record["lease_id"] for row in recorded):
                    raise PermissionError("writer lease changed after retirement event")
        wt_path = paths.root / str(worktree)
        if not wt_path.is_dir():
            raise PermissionError(
                "node {} worktree {} is missing".format(node_id, worktree))
        try:
            actual_head = _git_probe(wt_path, "rev-parse", "HEAD")
            actual_branch = _git_probe(
                wt_path, "rev-parse", "--abbrev-ref", "HEAD")
            dirty = _git_probe(wt_path, "status", "--porcelain")
        except ValueError as error:
            raise PermissionError(str(error))
        correction = item.get("base_correction")
        corrected_base = None
        if correction is not None:
            corrected_base = _verify_base_correction(
                correction, node, item, node_id, wt_path, audits)
        if actual_head != head:
            raise PermissionError(
                "node {} worktree head is not the bound head".format(node_id))
        stored_base = node.get("worktree_base")
        if corrected_base is not None:
            correction_committed = (committed is not None and
                committed["data"].get("base_corrections", {}).get(node_id) == {
                    "old_snapshot_digest": correction.get("old_snapshot_base"),
                    "corrected_base_digest": corrected_base})
            if (stored_base != correction.get("old_snapshot_base")
                    and not (correction_committed and stored_base == corrected_base)):
                raise PermissionError(
                    "node {} stored worktree_base does not match the "
                    "correction premise".format(node_id))
            if actual_head != corrected_base:
                raise PermissionError(
                    "node {} worktree head is not the corrected base".format(
                        node_id))
        elif actual_head != stored_base:
            raise PermissionError(
                "node {} worktree head is not the frozen dispatch "
                "head".format(node_id))
        if actual_branch != branch:
            raise PermissionError(
                "node {} worktree is on the wrong branch".format(node_id))
        if dirty:
            raise PermissionError(
                "node {} worktree is not clean".format(node_id))
        clean_nodes[node_id] = {
            "worktree": worktree, "head": actual_head,
            "corrected_base": corrected_base, "lease": lease}
    request_nodes = {
        str(item.get("issue_id")) for item in evidence["requests"]}
    if set(clean_nodes) != request_nodes:
        raise PermissionError(
            "worktree evidence does not cover every retired node")
    return {
        "run_id": run_id,
        "snapshot": snapshot,
        "pairs": pairs,
        "takeover": takeover,
        "retired_consumers": retired,
        "creates": creates,
        "clean_nodes": clean_nodes,
        "store": store,
        "committed": committed,
    }


def _call_return_text(payload: Any) -> Optional[str]:
    """Text of an audited return payload, or None when none exists."""
    if not isinstance(payload, dict):
        return None
    output = payload.get("output")
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts = [
            block.get("text") for block in output
            if isinstance(block, dict) and isinstance(block.get("text"), str)]
        if parts:
            return chr(10).join(parts)
    return None


def _verify_declared_creates(
        paths: ProjectPaths, registry: Dict[str, Any], pairs: List[Any], consumers: List[Dict[str, Any]],
        audits: List[Dict[str, Any]]) -> None:
    """Only a demonstrably registered supervisor successor is excluded."""
    bindings = []
    create_proofs = []
    for action_id, _path, _already, action in pairs:
        marks = {str(action_id)}
        digest = action.get("request_digest")
        if isinstance(digest, str) and digest:
            marks.add(digest)
        envelope = action.get("request")
        if isinstance(envelope, dict):
            prompt = envelope.get("prompt")
            if isinstance(prompt, str) and prompt:
                marks.add(prompt)
        bindings.append((str(action.get("issue_id")), marks))
    by_session = {
        consumer.get("session_id"): consumer
        for consumer in consumers if isinstance(consumer, dict)}
    all_returns = {}
    for audit in audits:
        for call_id, entry in (audit.get("returns") or {}).items():
            all_returns.setdefault(call_id, entry)
    for audit in audits:
        session = audit["session_id"]
        consumer = by_session.get(session) or {}
        declared = consumer.get("declared_creates")
        if declared is None:
            declared = []
        if not isinstance(declared, list):
            raise ValueError(
                "consumer {} declared_creates must be a list".format(session))
        declared_map = {}
        for index, item in enumerate(declared):
            if not isinstance(item, dict):
                raise ValueError(
                    "consumer {} declared_creates[{}] must be an object".format(
                        session, index))
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError(
                    "consumer {} declared_creates[{}] requires call_id".format(
                        session, index))
            if call_id in declared_map:
                raise ValueError("duplicate declared create call")
            declared_map[call_id] = item
        flagged = [
            call for call in audit["calls"]
            if call.get("native_create") or call.get("embedded_create")]
        for call in flagged:
            payload_text = json.dumps(
                call.get("payload") or {}, ensure_ascii=False)
            for node_id, marks in bindings:
                if any(mark in payload_text for mark in marks):
                    raise PermissionError(
                        "consumer {} create call {} at line {} carries the "
                        "identity of retired node {}".format(
                            session, call["call_id"], call["line"],
                            node_id))
            declaration = declared_map.pop(call["call_id"], None)
            if declaration is None:
                raise PermissionError(
                    "consumer {} has an undeclared create invocation {} at "
                    "line {}".format(session, call["call_id"], call["line"]))
            args_list = _native_call_arguments(call, "create_thread")
            thread_ids = declaration.get("returned_thread_ids")
            if (not isinstance(thread_ids, list) or len(thread_ids) != len(args_list)
                    or not thread_ids or len(set(thread_ids)) != len(thread_ids)
                    or any(not isinstance(item, str) or not item
                           for item in thread_ids)):
                raise ValueError(
                    "consumer {} declared create {} requires "
                    "returned_thread_ids".format(session, call["call_id"]))
            ret_entry = all_returns.get(call["call_id"])
            values = _native_return_values(ret_entry.get("payload") if isinstance(ret_entry, dict) else None)
            if len(values) != len(args_list):
                raise PermissionError("create return count does not bind invocations")
            record_path = declaration.get("successor_record_path")
            if len(args_list) != 1 or not isinstance(record_path, str) or not record_path:
                raise PermissionError("create successor source is ambiguous")
            source = Path(record_path)
            if _artifact_hash(source) != declaration.get("successor_record_sha256"):
                raise PermissionError("successor source digest mismatch")
            from .supervisor import _record_digest
            thread_id, args, value = thread_ids[0], args_list[0], values[0]
            entries = [entry for entry in [registry.get("current")] + (registry.get("history") or [])
                       if isinstance(entry, dict) and entry.get("session_id") == thread_id]
            if (len(entries) != 1 or entries[0].get("provider") != "codex"
                    or entries[0].get("session_record_digest") != _record_digest(source)):
                raise PermissionError("create result is not a registered supervisor successor")
            target = args.get("target")
            expected_targets = [(action.get("request") or {}).get("target") for _, _, _, action in pairs]
            if (not isinstance(target, dict) or target.get("type") != "project"
                    or not isinstance(target.get("projectId"), str) or not target["projectId"]
                    or any(not isinstance(t, dict) or target.get("projectId") != t.get("projectId")
                           for t in expected_targets)
                    or not isinstance(args.get("prompt"), str) or not args["prompt"]
                    or value.get("threadId") != thread_id
                    or value.get("hostId") != entries[0].get("host")):
                raise PermissionError("create arguments, target or structured return mismatch")
            if "target" in value and value["target"] != target:
                raise PermissionError("create return target mismatch")
            meta = None
            user_texts = []
            for raw in source.read_text(encoding="utf-8").splitlines():
                item = json.loads(raw)
                if item.get("type") == "session_meta":
                    if meta is not None:
                        raise PermissionError("duplicate successor session metadata")
                    meta = item.get("payload")
                payload = item.get("payload") or {}
                if item.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                    for block in payload.get("content") or []:
                        if isinstance(block, dict) and isinstance(block.get("text"), str):
                            user_texts.append(block["text"])
            if not isinstance(meta, dict) or meta.get("id") != thread_id or not user_texts or args["prompt"] != user_texts[0]:
                raise PermissionError("successor session does not bind create prompt")
            create_proofs.append({"session_id": session, "call_id": call["call_id"],
                                  "successor_session_id": thread_id,
                                  "source_rollout_sha256": declaration["successor_record_sha256"],
                                  "parameters_digest": _canonical_digest(args),
                                  "return_digest": _canonical_digest(value)})
        if declared_map:
            raise PermissionError(
                "consumer {} declares creates absent from its own log: "
                "{}".format(session, ", ".join(sorted(declared_map))))
    return create_proofs


def retire_uninvoked_legacy(
        paths: ProjectPaths, plan_id: str, amendment_digest: str,
        evidence: Dict[str, Any],
        _fault_at: Optional[str] = None) -> Dict[str, Any]:
    """Retire provider creates that provably were never invoked, publicly.

    Preconditions are re-verified inside the same ``.amendment.lock`` the
    transfer uses; the durable ``retire_uninvoked_legacy`` event separates
    rollback-allowed state from converge-only state.  The per-action
    ``complete()`` locks are held from the stale-marker write through the
    event, snapshot projection and lease release, so a provider callback
    can never interleave with the boundary.  A two-phase intent record
    (``locks/retire-<digest>.intent``) marks our own lock residue: a crash
    before the event leaves intent+locks+markers that a rerun converges,
    and a crash after it leaves a coalesced event plus the same residue --
    either way the next call finishes the boundary without a twin event.
    The completed intent remains as provenance for stable lock inodes.
    """
    if not isinstance(amendment_digest, str) or not amendment_digest:
        raise ValueError("retire requires the bound amendment digest")
    plan_root = _plan_root(paths, plan_id)
    with interprocess_lock(plan_root / ".amendment.lock"), interprocess_lock(_lease_lock(paths)):
        if _marker_state(paths, plan_id) is not None:
            raise PermissionError(
                "an amendment transfer is in progress; retire cannot "
                "interleave with it")
        context = _verify_retire_evidence(
            paths, plan_id, amendment_digest, evidence)
        run_id = context["run_id"]
        snapshot = context["snapshot"]
        store = context["store"]
        pairs = context["pairs"]
        action_ids = [item[0] for item in pairs]
        stale_dir = store.root / "stale"
        from contextlib import ExitStack
        evidence_sha = sha256_bytes(json.dumps(
            evidence, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        locks_dir = store._directory("locks")
        intent_path = _retire_intent_path(store, amendment_digest)
        intent_base = {
            "kind": "retire_uninvoked_legacy_intent",
            "run_id": run_id,
            "plan_id": plan_id,
            "amendment_digest": amendment_digest,
            "action_ids": sorted(action_ids),
            "evidence_sha256": evidence_sha,
        }
        # Phase one -- written before the first lock is taken so that a
        # crash leaves an explainable residue, never an anonymous lock.
        _atomic_json(intent_path, dict(intent_base, phase="intent"))
        with ExitStack() as stack:
            for action_id in sorted(action_ids):
                stack.enter_context(interprocess_lock(
                    locks_dir / (action_id + ".lock")))
            # Phase two -- bind each held lock's payload SHA so a foreign
            # lock written after a crash is still foreign on rerun.
            _atomic_json(intent_path, dict(
                intent_base,
                phase="locked",
                lock_shas={
                    action_id: _artifact_hash(
                        locks_dir / (action_id + ".lock"))
                    for action_id in sorted(action_ids)}))
            # A callback may have completed between preflight and acquiring
            # the action locks. Check every claim before writing any stale
            # marker; callback observations now use these same locks.
            for _action_id, _path, _already, action in pairs:
                claim = _retire_claim_evidence(
                    store, run_id, str(action.get("issue_id")), amendment_digest)
                legacy_retirement_proven = (
                    claim == "unknown_create_exposure"
                    and store._read_create_state(action) is None
                )
                if claim is not None and not legacy_retirement_proven:
                    raise PermissionError("retirement gained claim evidence: " + claim)
            for action_id, _path, already, _action in pairs:
                if store.result(action_id) is not None:
                    raise PermissionError(
                        "provider request {} gained a result inside the "
                        "retire window".format(action_id))
                if already:
                    continue
                action = store.action(action_id)
                marker = {
                    "schema_version": _RETIRE_SCHEMA,
                    "status": "stale",
                    "reason": RETIRE_UNINVOKED_REASON,
                    "action_id": action_id,
                    "request_digest": action.get("request_digest"),
                    "run_id": run_id,
                    "issue_id": action.get("issue_id"),
                    "role": action.get("role"),
                    "generation": action.get("generation"),
                    "amendment_digest": amendment_digest,
                    "plan_id": plan_id,
                    "evidence_sha256": evidence_sha,
                }
                _atomic_json(stale_dir / (action_id + ".json"), marker)
                if _fault_at == "after_stale:" + action_id:
                    raise RuntimeError("retire fault injected after stale marker")
            changed = snapshot.nodes
            worktree_items = {
                str(item.get("node_id")): item
                for item in evidence.get("worktrees") or []
                if isinstance(item, dict)}
            for item in evidence["requests"]:
                node_id = item["issue_id"]
                node = snapshot.nodes[node_id]
                if (node.get("status") != "planned"
                        or node.get("retryable_action") is not None):
                    node["status"] = "planned"
                    node["retryable_action"] = None
                    node["old_task_reconciled"] = True
                    node.pop("reason", None)
                    node["user_status"] = "准备中"
                corrected = context["clean_nodes"].get(node_id, {}).get(
                    "corrected_base")
                if (corrected is not None
                        and node.get("worktree_base") != corrected):
                    node["worktree_base"] = corrected
                changed[node_id] = node
            provenance = {
                "role": "system",
                "task_id": None,
                "handle_id": None,
                "generation": 0,
                "authorization_digest": snapshot.authorization_digest,
                "node_contract_digest": snapshot.node_contract_digest,
            }
            if _fault_at == "before_event":
                raise RuntimeError("retire fault injected before event")
            retirement_event = RunEvent(RETIRE_UNINVOKED_EVENT, {
                    "run_id": run_id,
                    "plan_id": plan_id,
                    "amendment_digest": amendment_digest,
                    "authorization_digest": _retire_field(
                        evidence, "authorization_digest"),
                    "superseded_creates": action_ids,
                    "node_ids": sorted(context["clean_nodes"]),
                    "status": "retired",
                    "reason": RETIRE_UNINVOKED_REASON,
                    "reason_class": "engineering",
                    # Correction digests live at data level: everything
                    # under the 'evidence' key is provider-text redacted
                    # on write, which would mask the very SHAs the
                    # correction exists to record.
                    "base_corrections": {
                        node_id: {
                            "old_snapshot_digest": (
                                (worktree_items.get(node_id) or {})
                                .get("base_correction", {})
                                .get("old_snapshot_base")),
                            "corrected_base_digest": context[
                                "clean_nodes"][node_id]["corrected_base"],
                        }
                        for node_id in sorted(context["clean_nodes"])
                        if context["clean_nodes"][node_id][
                            "corrected_base"] is not None},
                    "retirement_proof": {
                        "evidence_sha256": evidence_sha,
                        "creates": context["creates"],
                        "requests": [{"action_id": action_id,
                                      "request_digest": action["request_digest"],
                                      "request_sha256": _artifact_hash(path)}
                                     for action_id, path, _old, action in pairs],
                        "leases": [dict(item["lease"]["record"])
                                   for item in context["clean_nodes"].values()
                                   if item["lease"] is not None],
                        "owner_takeover": context["takeover"],
                        "retired_consumers": [
                            {"session_id": item["session_id"],
                             "rollout_sha256": item["rollout_sha256"],
                             "receipt_sha256": item["native_terminal"]["receipt_sha256"],
                             "source_rollout_sha256": item["native_terminal"].get("source_rollout_sha256"),
                             "host": item.get("host", "local")}
                            for item in context["retired_consumers"]],
                    },
                })
            if context["committed"] is None:
                snapshot.event_sequence = append_event(paths, retirement_event, provenance)
            else:
                snapshot.event_sequence = max(snapshot.event_sequence, context["committed"]["sequence"])
            if _fault_at == "after_event":
                raise RuntimeError("retire fault injected after event")
            save_snapshot(paths, snapshot)
            if _fault_at == "after_snapshot":
                raise RuntimeError("retire fault injected after snapshot")
            for node_id in sorted(context["clean_nodes"]):
                lease = context["clean_nodes"][node_id]["lease"]
                if lease is None:
                    continue
                released = release_writer_lease(
                    paths, node_id,
                    str(context["clean_nodes"][node_id]["worktree"]),
                    run_id, expected=lease, _locked=True)
                if not released:
                    raise PermissionError("writer lease CAS release failed")
                if _fault_at == "after_lease:" + node_id:
                    raise RuntimeError("retire fault injected after lease release")
            # Never unlink shared flock paths: queued waiters retain their
            # inode. The durable intent distinguishes our mutex metadata
            # from a provider claim, including after cleanup/replay.
            _atomic_json(intent_path, dict(
                intent_base, phase="complete", lock_shas={
                    action_id: _artifact_hash(locks_dir / (action_id + ".lock"))
                    for action_id in sorted(action_ids)}))
            if _fault_at == "after_cleanup":
                raise RuntimeError("retire fault injected after cleanup")
        return {
            "status": "retired",
            "plan_id": plan_id,
            "run_id": run_id,
            "amendment_digest": amendment_digest,
            "action_ids": action_ids,
            "node_ids": sorted(context["clean_nodes"]),
            "converged": all(item[2] for item in pairs),
        }
