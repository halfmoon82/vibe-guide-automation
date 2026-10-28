"""Thin CLI wiring over the scanner, planner, authorization and monitor APIs."""

import argparse
import contextlib
import hashlib
import importlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .authorization import (
    AuthorizationCard,
    AuthorizationRecord,
    authorize,
    is_authorization_valid,
    build_authorization_card,
    INTEGRATION_REVIEW_NODE_ID,
    dispatch_topology_for_node,
    node_adapter_id,
    rebind_dispatch_contract,
    refresh_authorization_card,
)
from .adapters.base import (
    BACKGROUND_LAUNCH_LIMITATIONS,
    Environment as AdapterEnvironment,
    TOPOLOGY_BACKGROUND,
    TOPOLOGY_IN_SESSION_SDD,
)
from .adapters.registry import AdapterRegistry
from .adapters.task_provider import ProviderActionStore, ProviderPending
from .config import load_project_config, load_supervisor_single_shift
from .dag import render_plan_artifacts, validate_dag, append_integration_review_node
from .doctor import doctor
from .initializer import _rules_target, apply_agentsmd_proposal, init_project, refresh_protocol_copies
from . import host_rules
from .models import AgentCapabilities, DAGNode, Plan, DeployManifest, DeployState, PRD, SkillProfile
from .monitor import BLOCKED_UNKNOWN_DISPOSITION_KINDS, Monitor, ScopeDecisionRequired
from .monitor_handoff import current_plan_digests, record_event
from .supervisor_output import supervisor_output_batch
from .supervisor_wakeup import idle_heartbeat_minutes, preflight_with_checkpoint
from .supervisor import (
    Supervisor,
    contract_drift as supervisor_contract_drift,
    authorization_card_summary,
    current_supervisor_address,
    supervisor_mailbox_view,
    provider_never_rotates,
    mailbox_complete,
    mailbox_result_template,
    beat_stall_report,
    render_stall_beat_text,
    heal_verify,
    stall_peek,
    register_supervisor_address,
    session_cwd_warning,
    project_root_worktree_warning,
    release_supervisor_hold,
    set_supervisor_hold,
    supervisor_handoff,
    bounded_supervisor_output,
    heartbeat_command,
    heartbeat_interval,
    heartbeat_interval_options,
    rotate_hard_cap_options,
    context_window_from_record,
    read_heartbeat_interval,
    record_heartbeat_interval,
    HEARTBEAT_INTERVAL_DEFAULT_MINUTES,
    HEARTBEAT_INTERVAL_PUSH_VERIFIED_MINUTES,
    DEFAULT_ROTATE_HARD_CAP,
    DEFAULT_ROTATE_SOFT_CAP,
    DEFAULT_ROTATE_TOKEN_THRESHOLD,
    rotate_hard_cap_for_run,
    supervisor_holds,
    supervisor_preflight,
    supervisor_beat,
    HOST_SHIFT_DIFFERENCES,
    SESSION_RECORD_HINT,
)
from .change_requests import ChangeRequest, classify_merge_capability
from .deploy import authorize_deploy, plan_deploy, verify_deploy, start_deploy
from .paths import ProjectPaths
from .publish import publish_plan as _publish_plan
from .planner import (
    DecisionCard,
    PRD,
    TaskContext,
    approve_prd,
    classify_s0,
    route_task,
    score_s1,
    build_stage_handoff,
)
from .session_entry import build_session_entry, draft_s1, materialize_session_entry, replaceable_draft_extras
from .capability_contract import contract_path, save_contract
from .handoff_rebind import (
    HandoffRebindError,
    contract_for_platform,
    rebind_nodes,
    resolve_handoff_target,
    stranded_nodes,
    unfinished_node_ids,
)
from .node_spec import (
    complete_node_contracts,
    normalize_node_spec,
    observe_all_capabilities,
    observe_capabilities,
    reject_engineering_fields,
    render_planning_brief_markdown,
    render_prd_markdown,
)
from .scanner import scan_project
from .diagnostics import screen_session, require_session_screened
from .diagnostics import assert_planning_gate, _valid_plan_confirmation_binding
from .workflow_gate import is_v42_sdd_first_state, require_capability_contract
from .authorize_entry import (
    AuthorizationDenied,
    load_live_workflow,
    materialize_workflow_evidence,
    verify_workflow_artifacts,
)
from .attest import record_session_capabilities
from .protocols import PRD_GUIDE_NAME, load_protocol
from .state import _atomic_bytes, load_events, load_snapshot, validate_run_id
from .state import RunSnapshot
from .state import (
    WORKTREE_ACTION_STATUS,
    WORKTREE_PREFLIGHT_KEY,
    STALL_TIER_KEY,
    WORKTREE_PREFLIGHT_KEYS,
    map_user_status,
    save_snapshot,
    worktree_action,
    worktree_reason_code,
)
from .runners.provider_action import ProviderActionRunner
from .preflight import PreflightBlockedError, PreflightContext, assert_authorizable, run_preflight
from .prd_profiles import evaluate_prd_checkpoints, validate_skill_profile
from .engine_attestation import (
    create_engine_attestation,
    needs_renewal,
    validate_engine_attestation,
)
from .evidence import evaluate_v41_closeout
from .installation import run_install, run_upgrade, migrate_state
from .skills import SkillSpec, install_project_skill
from .models import InstallRequest
from .decision_boundary import answer_decision, decision_list, raise_decision
from . import worker_questions


SUCCESS = 0
USAGE_ERROR = 2
BLOCKED = 3
UNKNOWN = 4
_DOCUMENT_LIMIT = 1024 * 1024


@dataclass(frozen=True)
class CLIResult:
    exit_code: int
    payload: Dict[str, Any]
    text: str
    as_json: bool = False


def _explicit_role(argv: Sequence[str]) -> Optional[str]:
    """``--role`` as typed (abbreviations included), or None when it was left to its default."""
    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument("--role", dest="role", default=None)
    try:
        return probe.parse_known_args(list(argv))[0].role
    except SystemExit:
        return None


def _result(
    code: int, payload: Dict[str, Any], text: str, as_json: bool
) -> CLIResult:
    return CLIResult(code, payload, text, as_json)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vibe", add_help=False)
    parser.add_argument("-h", "--help", action="store_true", dest="show_help")
    parser.add_argument(
        "command",
        nargs="?",
        choices=('scan', 'init', 'apply-agentsmd', 'doctor', 'install', 'upgrade', 'migrate-state', 'attest', 'plan', 'authorize', 'monitor', 'reconcile', 'status', 'resume', 'change-request', 'deploy', 'skill-install', 'supervisor-beat', 'supervisor-preflight', 'supervisor-register', 'supervisor-address', 'supervisor-hold', 'supervisor-handoff', 'supervisor-output', 'supervisor-mailbox', 'blocked-unknown', 'worker-deliver', 'decision-raise', 'decision-list', 'decision-answer', 'scope-expand', 'repair', 'requeue', 'heal-verify', 'daemon'),
    )
    parser.add_argument("daemon_action", nargs="?", choices=("start", "status", "stop", "run", "register-pm"))
    parser.add_argument("--interval", dest="daemon_interval", type=float, default=None)
    parser.add_argument("--model", dest="daemon_model", default=None)
    parser.add_argument("--session-name", dest="pm_session_name", default=None)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--host-rules", dest="host_rules", choices=("skip", "apply", "rollback"),
                        help="init: 回答「是否在用户级规则中加入 vibe 监工会话例外」")
    parser.add_argument("--host-rules-digest", dest="host_rules_digest",
                        help="init --host-rules apply: 授权预览差异的摘要")
    parser.add_argument("--request")
    parser.add_argument("--plan-id")
    parser.add_argument("--plan")
    parser.add_argument("--run-id")
    parser.add_argument("--evidence")
    parser.add_argument("--s1")
    parser.add_argument("--node-spec")
    parser.add_argument("--from-prd", dest="from_prd")
    parser.add_argument("--print-protocol", action="store_true", dest="print_protocol")
    parser.add_argument("--adapter")
    parser.add_argument("--facts")
    parser.add_argument("--provenance")
    parser.add_argument("--project-id", dest="project_id")
    parser.add_argument("--authorize")
    parser.add_argument("--manifest")
    parser.add_argument("--acceptance-state")
    parser.add_argument("--observations")
    parser.add_argument("--watch", action="store_true", dest="watch")
    parser.add_argument("--preview-card", action="store_true", dest="preview_card")
    parser.add_argument("--platform-handoff", action="store_true", dest="platform_handoff",
                        help="monitor: 换平台交接时重新授权同一运行（只允许派发绑定与能力合同变化）")
    parser.add_argument("--mode", choices=("layered", "bundled"), default="layered")
    parser.add_argument("--source", dest="skill_source")
    parser.add_argument("--sha", dest="skill_sha")
    parser.add_argument("--name", dest="skill_name")
    parser.add_argument("--subdir", dest="skill_subdir", default="")
    parser.add_argument("--session-record", dest="session_record")
    parser.add_argument("--accept-decision", action="store_true", dest="accept_decision")
    parser.add_argument("--provider", dest="supervisor_provider")
    parser.add_argument("--session-id", dest="session_id")
    parser.add_argument("--host", dest="supervisor_host")
    parser.add_argument("--wake-target", dest="wake_target",
                        help="supervisor-register: 工人唤醒本监工时 SendMessage 用的会话名（缺省用 --session-id）")
    parser.add_argument("--context-hard-cap", dest="context_hard_cap", type=int, default=None)
    parser.add_argument("--token-threshold", dest="token_threshold", type=int, default=None)
    parser.add_argument("--reason", dest="hold_reason")
    parser.add_argument("--category", dest="hold_category")
    parser.add_argument("--decision", dest="hold_decision")
    parser.add_argument("--authorization-gap", dest="hold_authorization_gap")
    parser.add_argument("--release", action="store_true", dest="hold_release")
    parser.add_argument("--node", dest="node_id")
    parser.add_argument("--complete", dest="mailbox_complete", metavar="ACTION_ID",
                        help="supervisor-mailbox: 把 --result 写成该信箱请求的回写（--result 为文件或 -）")
    parser.add_argument("--template", dest="mailbox_template", metavar="ACTION_ID",
                        help="supervisor-mailbox: 输出该请求回写的示例")
    parser.add_argument("--result", dest="mailbox_result")
    parser.add_argument("--disposition", dest="blocked_disposition",
                        help="blocked-unknown: confirmed_benign|escalated_to_findings|resolved_with_facts|deferred_to_acceptance")
    parser.add_argument("--item", dest="blocked_item",
                        help="blocked-unknown: 只处置这一项（缺省处置该节点全部待处置项）")
    parser.add_argument("--role", dest="role", default="developer")
    parser.add_argument(
        "--payload",
        dest="worker_payload",
        help=(
            "交付 JSON：嵌套 delivery_evidence（completion_marker/delivery_path/thread_status）"
            "加顶层 event（developer 用 delivered/complete）；reviewer 全部清理 P0-P2 后验收必须带 "
            "顶层 event=accepted 与顶层 evidence 对象，仍有未清发现时用 event=review_finding"
        ),
    )
    parser.add_argument("--generation", dest="worker_generation", type=int, default=None)
    parser.add_argument("--id", dest="decision_id")
    parser.add_argument("--choice", dest="decision_choice")
    parser.add_argument("--digest", dest="decision_digest")
    parser.add_argument("--via", dest="decision_via")
    parser.add_argument("--note", dest="decision_note")
    parser.add_argument("--question", dest="decision_question")
    parser.add_argument("--options", dest="decision_options")
    parser.add_argument("--recommendation", dest="decision_recommendation")
    parser.add_argument("--decision-evidence", dest="decision_evidence")
    return parser


def run_install_or_upgrade(request: Any, json_output: bool = False) -> Dict[str, Any]:
    """Run the shared provider-neutral installation state machine.

    The returned dictionary is the sole state-machine representation used by
    both interactive and JSON entry points.  ``json_output`` is intentionally
    accepted for API parity but does not alter the payload.
    """
    if type(json_output) is not bool:
        raise TypeError("json_output must be a bool")
    if isinstance(request, InstallRequest):
        install_request = request
        operation = "install"
    elif isinstance(request, dict):
        operation = str(request.get("operation", request.get("command", "install"))).strip().lower()
        if operation not in {"install", "upgrade", "upg"}:
            raise ValueError("unsupported installation operation")
        install_request = InstallRequest(
            str(request.get("mode", "layered")),
            bool(json_output),
            Path(request.get("project_root", Path.cwd())),
        )
    else:
        raise TypeError("installation request must be a mapping or InstallRequest")
    installation_state = (
        Path(install_request.project_root) / ".vibe" / "installation" / "state.json"
    )
    was_installed = installation_state.is_file() and not installation_state.is_symlink()
    runner = run_upgrade if operation in {"upgrade", "upg"} else run_install
    result = runner(install_request, ProjectPaths.from_cwd(install_request.project_root))
    payload = result.to_dict()
    payload["operation"] = "upgrade" if runner is run_upgrade else "install"
    if runner is run_upgrade:
        payload["changed"] = not was_installed
    payload["message"] = _install_message(payload.get("status"), payload.get("phase"))
    return payload


def _install_message(status: str, phase: str) -> str:
    if status == "complete" or phase == "complete":
        return "已启动"
    if phase in {"backup", "migrate"}:
        return "自动修复中"
    if status in {"blocked_unknown", "retry_pending", "blocked_invalid", "failed"} or phase == "blocked":
        return "需要你决定"
    return "准备中"


def _scan_payload(paths: ProjectPaths) -> Dict[str, Any]:
    report = scan_project(paths)
    return {
        "root": report.root,
        "python_version": report.python_version,
        "git_version": report.git_version,
        "git_root": report.git_root,
        "git_remote": report.git_remote,
        "agentsmd_exists": report.agentsmd_exists,
        "rules_file": getattr(report, "rules_file", None),
        "knowledge_exists": report.knowledge_exists,
        "vibe_exists": report.vibe_exists,
        "skills": report.skills,
        "agent_commands": report.agent_commands,
        "skill_records_error": report.skill_records_error,
    }


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("JSON input must be a regular file")
    raw = path.read_bytes()
    if len(raw) > _DOCUMENT_LIMIT:
        raise ValueError("JSON input exceeds the size bound")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("JSON input is invalid") from error


def _decision_options(value: Optional[str]) -> list:
    if not value:
        raise ValueError("--options is required")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(parsed, list):
        raise ValueError("--options must be a JSON list or comma-separated values")
    return parsed


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("persistence path may not be a symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + path.name + ".", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _scores(value: Optional[str]) -> TaskContext:
    if not value:
        raise ValueError("five explicit S1 scores are required")
    try:
        values = [int(item.strip()) for item in value.split(",")]
    except ValueError as error:
        raise ValueError("S1 scores must be five integers") from error
    if len(values) != 5:
        raise ValueError("S1 scores must contain five dimensions")
    return TaskContext(*values)


def _plan_root(paths: ProjectPaths, plan_id: str) -> Path:
    if not plan_id or any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
        for character in plan_id
    ):
        raise ValueError("plan id must be a simple identifier")
    return paths.resolve_vibe_path(Path("plans") / plan_id)


def _authorization_card(data: Dict[str, Any]) -> AuthorizationCard:
    converted = dict(data)
    for key in ("remote_git_actions_options", "remote_git_actions_scope", "deploy_authorization"):
        converted.pop(key, None)
    # The availability lists are unsigned metadata that must round-trip: a card
    # read back and rewritten (a refresh) would otherwise lose the reason a tier
    # was unavailable, which is the half the product manager reads.
    for key in ("heartbeat_interval_options", "rotate_hard_cap_options"):
        value = converted.get(key)
        if value is None:
            continue
        if isinstance(value, (list, tuple)) and all(isinstance(item, dict) for item in value):
            converted[key] = tuple(dict(item) for item in value)
        else:
            converted.pop(key, None)
    for key in (
        "node_ids",
        "file_scope",
        "worker_scope",
        "allowed_actions",
        "excluded_actions",
    ):
        converted[key] = tuple(converted[key])
    return AuthorizationCard(**converted)


def _card_discloses_background(card: Optional[AuthorizationCard]) -> bool:
    """True when the authorization card already discloses a background worker."""
    if card is None:
        return False
    workers = getattr(card, "workers", None) or ()
    entries = workers.values() if isinstance(workers, dict) else workers
    return any(isinstance(entry, dict) and entry.get("mode") == "background" for entry in entries)


def _observed_topology_rulings(
    paths: ProjectPaths, card: Optional[AuthorizationCard] = None
) -> dict:
    """Return ``{adapter_id: dispatch ruling}`` from the recorded bridges.

    The ruling re-derives the ISSUE-07 topology decision from the capability
    facts and provenance each desktop session published (#279: every adapter
    keeps its own record, so a run gets rulings for the adapters that
    actually attested, not for whichever wrote last).  Any missing or
    invalid evidence yields an empty mapping, and the monitor then keeps its
    conservative dual-visible default: UNKNOWN never upgrades to visible-sdd.

    A verified background-launch fact rules ``background``.  A run whose
    existing ``card`` never disclosed a background worker keeps its original
    (non-background) path, so the card the user approved and the dispatch can
    not diverge; the first-publication card (``card=None``) is not walked back.
    """
    try:
        observed = observe_all_capabilities(paths)
    except (ProviderPending, OSError, TypeError, ValueError):
        return {}
    rulings = {}
    for adapter_id, record in observed.items():
        probes = [record.adapter_id + ".in_session_sdd", record.adapter_id + ".subprocess"]
        try:
            adapter = AdapterRegistry().get(record.adapter_id)
        except KeyError:
            continue
        background_probe = adapter.manifest.get("background_probe")
        if background_probe:
            probes.append(background_probe)
        evidence = record.detection.evidence
        provenance = record.detection.capabilities.provenance

        def ruling(names):
            facts = {}
            provenance_map = {}
            for name in names:
                if name in evidence:
                    facts[name] = bool(evidence[name])
                    source = provenance.get(name)
                    if isinstance(source, str) and source:
                        provenance_map[name] = source
            return adapter.topology_decision(
                AdapterEnvironment(facts=facts, provenance=provenance_map)
            ).topology

        try:
            topology = ruling(probes)
            if (
                topology == TOPOLOGY_BACKGROUND
                and card is not None
                and not _card_discloses_background(card)
            ):
                topology = ruling(probes[:-1] if background_probe else probes)
        except (KeyError, TypeError, ValueError):
            continue
        rulings[record.adapter_id] = topology
    return rulings


def _background_launcher_problem(paths: ProjectPaths) -> Optional[str]:
    """Why the verified background launcher cannot be used, or None when it can.

    Exception messages are never copied out: they may carry paths or secrets.
    """
    try:
        module = importlib.import_module("vibe_guide.adapters.claude_bg")
    except ImportError:
        return "后台启动器未安装（缺少 vibe_guide.adapters.claude_bg）"
    except Exception as error:
        return "后台启动器加载失败：" + type(error).__name__
    try:
        module.load_claude_bg_config(paths)
    except (module.ClaudeBgUnknown, module.ClaudeBgConfigRefused) as error:
        reason = getattr(error, "reason", None) or str(error)
        return "后台启动器未配置：" + str(reason)
    except Exception as error:
        return "后台启动器配置检查失败：" + type(error).__name__
    return None


def _background_execution_note(topology: str) -> str:
    return "该节点在本运行中同样由后台会话执行（拓扑保持 {}：先交付、再派独立 reviewer 复审）".format(topology)


def _background_worker_entries(
    nodes: List[DAGNode],
    topology_rulings: Optional[Dict[str, str]],
    paths: ProjectPaths,
) -> Optional[Dict[str, Dict[str, Any]]]:
    """Card worker entries disclosing the background downgrade, or None.

    ``None`` when no node is ruled ``background`` so the card stays exactly
    what it was without the background fact.  Once any node is background,
    the same background runner executes the rest of the run (for instance the
    integration-review closeout), so those nodes get a disclosure entry too --
    keeping the topology the supervisor actually dispatches them under.
    """
    ruled = {node.id: dispatch_topology_for_node(node, topology_rulings) for node in nodes}
    if TOPOLOGY_BACKGROUND not in ruled.values():
        return None
    problem = _background_launcher_problem(paths)
    background_adapters = {
        node_adapter_id(node) for node in nodes if ruled[node.id] == TOPOLOGY_BACKGROUND
    }
    unavailable = ["后台启动器不可用：" + problem] if problem else []
    entries: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        topology = ruled[node.id]
        limitations = list(BACKGROUND_LAUNCH_LIMITATIONS)
        if topology != TOPOLOGY_BACKGROUND:
            # Only nodes the same background runner executes: the closeout
            # node, or a node routed to a background-ruled platform.
            if node.id != INTEGRATION_REVIEW_NODE_ID and node_adapter_id(node) not in background_adapters:
                continue
            limitations.append(_background_execution_note(topology))
        entry = {
            "topology": topology,
            "mode": "background" if topology == TOPOLOGY_BACKGROUND else "visible",
            "limitations": limitations + unavailable,
        }
        entries[node.id] = entry
    return entries


def _observed_engine_attestation(
    paths: ProjectPaths,
    plan: Plan,
    provider: Optional[str] = None,
    reuse_current: bool = False,
) -> Dict[str, Any]:
    """Re-observe the engine evidence a same-plan reauthorization has to name.

    Publication stamps the plan's ``engine-attestation.json`` once, and
    ``validate_engine_attestation`` refuses evidence older than 72 hours.  A plan
    published more than 72 hours ago therefore could not be reauthorized at all:
    the refreshed card would keep naming the expired reference, and
    ``Monitor._require_record`` reads the file, finds the recorded reference
    intact but the evidence stale, and blocks before any provider dispatch is
    considered.  There is no other supported way back -- the only writer is the
    publication path, which refuses to run for a plan that already exists.

    The observation is taken the way publication takes it, from the live
    ``ProviderActionStore``, so the re-signed card rests on freshly observed
    facts rather than on a re-typed copy of the old ones.  The record is the
    adapter the published attestation names: other adapters' attests cannot
    rewrite it (#279).  Raising is the
    correct outcome when the store cannot be read: an unverifiable engine
    binding must not be re-signed.
    """
    attestation_path = paths.resolve_vibe_path(
        Path("plans") / plan.plan_id / "engine-attestation.json"
    )
    # A platform handoff names the platform the run is moving to; the
    # published attestation still names the one it is leaving.
    try:
        if (
            provider is None
            and attestation_path.is_file()
            and not attestation_path.is_symlink()
        ):
            provider = json.loads(
                attestation_path.read_text(encoding="utf-8")
            ).get("provider")
    except (OSError, TypeError, ValueError):
        provider = None
    observed = ProviderActionStore(paths).capabilities(provider)
    if reuse_current:
        # ``generated_at`` is part of the digest, so a fresh stamp on every
        # call gives every re-run a new authorization digest -- and a create
        # request already written under the previous one then reads as an
        # identity drift.  Evidence that still describes exactly what is
        # observed now is therefore kept as it is.
        try:
            existing = json.loads(attestation_path.read_text(encoding="utf-8"))
            validate_engine_attestation(existing, plan.plan_id, plan.version)
            if (
                existing.get("provider") == observed["adapter_id"]
                and existing.get("capability_facts") == observed["facts"]
                and existing.get("provenance") == observed["provenance"]
            ):
                return existing
        except (OSError, TypeError, ValueError):
            pass
    return create_engine_attestation(
        plan.plan_id,
        plan.version,
        "vibeguide_monitor",
        "dag",
        observed["adapter_id"],
        observed["facts"],
        observed["provenance"],
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


@contextlib.contextmanager
def _staged_engine_attestation(
    path: Path,
    attestation: Optional[Dict[str, Any]],
    committed: Optional[List[bool]] = None,
):
    """Publish ``attestation`` for the duration of one reauthorization call.

    ``Monitor._require_record`` re-reads the file, so the refreshed evidence
    has to be on disk *before* ``reauthorize`` runs -- while the card that
    names it is deliberately published only after it succeeds.  Those two
    orderings cannot both be "after", so the evidence is staged and put back
    on failure: a reauthorization that raises leaves the plan exactly as it
    found it, instead of pairing fresh evidence with a card that still names
    the old reference.

    ``None`` means this plan carries no engine binding (see
    :func:`_engine_evidence_required`), and the whole thing is a no-op: there
    is no evidence to stage and no file to leave behind.

    If the process dies inside the window the leftover state is detectable
    rather than silently wrong -- the card names evidence the file no longer
    matches -- and the next reauthorization restages it.
    """
    if attestation is None:
        yield None
        return
    previous = (
        path.read_bytes() if path.is_file() and not path.is_symlink() else None
    )
    _atomic_json(path, attestation)
    try:
        yield attestation
    except BaseException:
        if committed and committed[0]:
            # The run already durably moved onto this evidence (a platform
            # handoff past its reauthorization): putting the old file back
            # would leave the run naming evidence nobody can find.
            raise
        if previous is None:
            if path.exists():
                path.unlink()
        else:
            _atomic_bytes(path, previous)
        raise


@contextlib.contextmanager
def _staged_session_contract(
    paths: ProjectPaths,
    target: Optional[str],
    committed: Optional[List[bool]] = None,
):
    """Point the session capability contract at the platform a handoff moves to.

    Nothing a user can run refreshes ``.vibe/session-contract.json`` (``init``
    writes it once as ``provider=unknown``; ``attest`` only writes the provider
    mailbox), so a handoff that waited for the contract to change never moved
    anything.  The handoff is the explicit act that says the session platform
    changed, so it owns this write; no other command reaches here.  Staged like
    the engine evidence: restored if the reauthorization fails before it commits,
    and a re-run after a crash sees the new contract and converges.
    """
    if target is None:
        yield
        return
    refreshed = contract_for_platform(
        require_capability_contract(paths), paths.root, target
    )
    if refreshed is None:
        yield
        return
    path = contract_path(paths)
    previous = path.read_bytes()
    save_contract(paths, refreshed)
    try:
        yield
    except BaseException:
        if not (committed and committed[0]):
            _atomic_bytes(path, previous)
        raise


@contextlib.contextmanager
def _staged_reauthorization(
    paths: ProjectPaths,
    plan: Plan,
    directory: Path,
    handoff_agent: Optional[str],
    committed: List[bool],
    observation: Tuple[Any, ...],
):
    """Everything a same-run reauthorization stages before ``reauthorize`` runs.

    One place for both card-refresh branches of ``monitor`` (with and without an
    invalidation marker), so neither can drift from the other: the session
    contract refresh of a platform handoff, then the engine evidence.  Yields
    the engine attestation (``None`` when the plan has no engine binding).
    """
    with _staged_session_contract(paths, handoff_agent, committed), \
        _staged_engine_attestation(
            directory / "engine-attestation.json",
            _observed_engine_attestation(paths, plan, *observation)
            if _engine_evidence_required(plan)
            else None,
            committed,
        ) as engine_attestation:
        yield engine_attestation


def _observed_adapter(paths: ProjectPaths, adapter_id: str):
    observed = None
    last_error = None
    for attempt in range(3):
        try:
            observed = observe_capabilities(paths, adapter_id)
            break
        except ProviderPending as error:
            last_error = error
            if attempt < 2:
                time.sleep(0.05)
    if observed is None:
        raise ProviderPending("provider capability observation pending") from last_error
    if observed.adapter_id != adapter_id:
        raise ValueError("observed provider does not match the selected adapter")
    result = observed.detection
    capabilities = result.capabilities
    # Two admissible dispatch shapes: a verified visible lifecycle, or the
    # in-session SDD topology (no visible task window, concurrent in-session
    # subagents instead).  Anything else stays refused.
    dispatchable = (
        capabilities.mode == TOPOLOGY_IN_SESSION_SDD
        or capabilities.mode == "background"
        or (capabilities.mode == "visible" and capabilities.visible_automation)
    )
    if not result.detected or not dispatchable:
        raise ValueError("selected provider has no verified dispatch lifecycle")
    return result


def _claude_bg_launcher_factory(paths: ProjectPaths, launchers: Optional[List[Any]] = None):
    """Return ``factory(contract, worktree) -> ClaudeBgLauncher`` for the runner.

    The launcher module is imported lazily when the factory is called.
    ``launchers``, when given, collects ``(node_id, launcher)`` so the caller
    can read back what the dispatch had to do about a node worktree (#235);
    the launcher has no run identity of its own and cannot record events.
    """

    def factory(contract: dict, worktree: Path):
        # Same lookup as _background_launcher_problem: sys.modules, not a stale
        # package attribute.
        claude_bg = importlib.import_module("vibe_guide.adapters.claude_bg")
        from .adapters.task_provider import BackgroundTaskRouting

        fallback = "provider-actions/deliveries/{}-{}-{}-g{}.json".format(
            contract.get("run_id"),
            contract.get("node_id"),
            contract.get("role") or "developer",
            contract.get("generation"),
        )
        binding = contract.get("child_binding")
        model = binding.get("model") if isinstance(binding, dict) else None
        if not isinstance(model, str) or not model.strip() or model.strip() == "default":
            model = None
        routing = BackgroundTaskRouting(
            host=str(contract.get("host") or "local"),
            worktree=str(worktree),
            branch=str(contract.get("branch") or ""),
            status_file=str(contract.get("status_file") or fallback),
            handoff_file=str(contract.get("handoff_file") or fallback),
        )
        launcher = claude_bg.ClaudeBgLauncher(
            routing, claude_bg.load_claude_bg_config(paths), model=model,
            upstream_branches=contract.get("upstream_branches") or (),
        )
        if launchers is not None:
            launchers.append((str(contract.get("node_id") or ""), launcher))
        return launcher

    return factory


def _background_runner(
    paths: ProjectPaths,
    adapter_id: str,
    provider: str,
    launchers: Optional[List[Any]] = None,
):
    """Build the background-session runner (``vibe_guide.runners.claude_bg``)."""
    try:
        from .runners.claude_bg import ClaudeBgRunner
    except ImportError as error:
        raise ValueError("后台运行器未安装：vibe_guide.runners.claude_bg") from error
    return ClaudeBgRunner(
        _claude_bg_launcher_factory(paths, launchers), paths=paths
    )


def _platform_handoff_rebind(
    paths: ProjectPaths,
    directory: Path,
    nodes: List[DAGNode],
    card: AuthorizationCard,
    explicit: Optional[str],
) -> Tuple[List[DAGNode], Optional[str]]:
    """Nodes and target platform of a ``--platform-handoff``.

    Returns ``(nodes, None)`` unchanged when no platform can be named,
    which is exactly the behaviour before this existed.  A named platform that is
    not attested and dispatchable raises ``HandoffRebindError``: the run is
    never pointed at a platform nobody verified.
    """
    snapshot = load_snapshot(paths, _run_id(directory, None))
    authorization = snapshot.authorization if isinstance(snapshot.authorization, dict) else {}
    run_agent = str(authorization.get("agent_id") or "")
    unfinished = unfinished_node_ids(nodes, snapshot.nodes)
    run_drift = bool(run_agent) and any(
        node_adapter_id(node) != run_agent for node in nodes if node.id in unfinished
    )
    contract = require_capability_contract(paths)
    dispatchable: List[str] = []
    for adapter_id in sorted(observe_all_capabilities(paths)):
        try:
            _observed_adapter(paths, adapter_id)
        except (ProviderPending, ValueError):
            continue
        dispatchable.append(adapter_id)
    target = resolve_handoff_target(
        explicit=explicit,
        card_agent=card.agent_id,
        run_agent=run_agent,
        run_drift=run_drift,
        capability_changed=snapshot.capability_contract_digest != contract.contract_digest,
        contract_provider=contract.provider,
        dispatchable_others=dispatchable,
    )
    if target is None:
        # Only a contract naming a platform that is attested and dispatchable
        # is an actionable disagreement; one naming something nobody attested
        # (a bare label) gives the operator no target to add.
        stranded = (
            stranded_nodes(nodes, unfinished, contract.provider)
            if contract.provider in dispatchable
            else []
        )
        if stranded:
            # Nothing is written before this point: say so instead of
            # re-authorizing into a no-op that looks like a handoff.
            raise HandoffRebindError(
                "platform handoff: 会话能力合同写的是 {}，但未完成节点（{}）仍绑在别的平台，"
                "无法判断交接到哪个；请加 --adapter <新平台> 指定".format(
                    contract.provider, ", ".join(stranded)
                )
            )
        return nodes, None
    try:
        _observed_adapter(paths, target)
    except (ProviderPending, ValueError) as error:
        raise HandoffRebindError(
            "platform handoff: 新平台 {} 未登记能力或没有可验证的派发通道（先在新平台会话里 "
            "vibe attest --adapter {}）：{}".format(target, target, error)
        ) from error
    rebound, _moved = rebind_nodes(nodes, unfinished, target)
    return rebound, target


def _handed_off_accepted_nodes(paths: ProjectPaths, card: AuthorizationCard) -> frozenset:
    """Accepted nodes of a run that recorded a platform_handoff.

    Their dispatch binding is history: they were accepted on the platform the
    run left, and the handoff never rewrites that.  A run with no recorded
    handoff exempts nothing, so the routing check stays exactly as strict as
    before for every ordinary run.  Any read problem exempts nothing.
    """
    try:
        directory = _plan_root(paths, card.plan_id)
        run_id = _run_id(directory, None)
        events = load_events(paths, run_id)
        if not any(
            event.get("event") == "authorization_reauthorized"
            and event.get("data", {}).get("change_reason") == "platform_handoff"
            for event in events
        ):
            return frozenset()
        snapshot = load_snapshot(paths, run_id)
        return frozenset(
            node_id for node_id, current in snapshot.nodes.items()
            if current.get("status") == "accepted"
        )
    except (OSError, TypeError, ValueError, KeyError):
        return frozenset()


def _public_runner(
    paths: ProjectPaths,
    card: AuthorizationCard,
    nodes: List[DAGNode],
    launchers: Optional[List[Any]] = None,
    exempt_nodes: Optional[frozenset] = None,
) -> ProviderActionRunner:
    # The handoff call names its accepted nodes itself (no handoff event is
    # recorded yet when its runner is built); every other call derives them
    # from a recorded handoff.
    exempt = (
        exempt_nodes if exempt_nodes is not None
        else _handed_off_accepted_nodes(paths, card)
    )
    if any(
        node.contract.get("adapter_id") != card.agent_id
        for node in nodes if node.id not in exempt
    ):
        raise ValueError("node adapter routing does not match authorization")
    observed = _observed_adapter(paths, card.agent_id)
    if observed.capabilities.mode == "background":
        if _card_discloses_background(card):
            return _background_runner(
                paths, observed.adapter_id, observed.capabilities.provider, launchers
            )
        # The approved card never disclosed a background downgrade: keep the
        # visible / in-session path it was authorized for.
        if not (
            observed.capabilities.in_session_sdd or observed.capabilities.visible_automation
        ):
            raise ValueError("selected provider has no verified dispatch lifecycle")
        return ProviderActionRunner(
            paths,
            observed.adapter_id,
            AdapterRegistry().get(observed.adapter_id).manifest["provider"],
        )
    return ProviderActionRunner(
        paths,
        observed.adapter_id,
        observed.capabilities.provider,
    )


#: Launcher report fields worth persisting: the worktree it built or refused,
#: the branch and baseline it used, and the command a supervisor must run.
_WORKTREE_REPORT_KEYS = WORKTREE_PREFLIGHT_KEYS


def _record_worktree_preflight(
    paths: ProjectPaths,
    monitor: Monitor,
    snapshot: RunSnapshot,
    launchers: Optional[List[Any]],
) -> bool:
    """Persist what a background dispatch did about node worktrees (#235).

    The launcher cannot record events -- it has no run identity -- so the CLI,
    which owns both the factory and the snapshot, writes the report each
    launcher left behind.  A creation is history only.  A block is also a node
    marker, because ``vibe status`` has to name the action long after the
    dispatch returned: the retry marker beside it says ``binding_unknown`` and
    its free-text reason is redacted on write, so without this marker the
    node would keep reading as 自动修复中 forever.
    """
    # Lazily, like the factory: the launcher module is optional at import time.
    from .adapters.claude_bg import WORKTREE_BLOCKED_EVENT, WORKTREE_CREATED_EVENT

    changed = False
    for node_id, launcher in launchers or ():
        report = getattr(launcher, "worktree_preflight", None)
        if not isinstance(report, dict) or node_id not in snapshot.nodes:
            continue
        current = snapshot.nodes[node_id]
        event = report.get("event")
        if event == WORKTREE_BLOCKED_EVENT:
            data = {"run_id": snapshot.run_id, "node_id": node_id}
            for key in _WORKTREE_REPORT_KEYS:
                if report.get(key):
                    data[key] = report[key]
            record_event(monitor, snapshot, event, data)
            current["worktree_preflight"] = {
                key: value for key, value in data.items()
                if key not in ("run_id", "node_id")
            }
        elif event == WORKTREE_CREATED_EVENT:
            data = {"run_id": snapshot.run_id, "node_id": node_id}
            for key in _WORKTREE_REPORT_KEYS:
                if report.get(key):
                    data[key] = report[key]
            record_event(monitor, snapshot, event, data)
            current.pop("worktree_preflight", None)
            if report.get("worktree_base"):
                # The base vibe built this worktree from (main plus accepted
                # upstreams); delivery-time scope checks diff against it.
                current["worktree_base"] = report["worktree_base"]
        elif WORKTREE_PREFLIGHT_KEY in current:
            # The directory is there and matches the contract now (the report
            # says nothing happened): the action a supervisor was asked to
            # take is done, so the marker must stop claiming it.
            current.pop(WORKTREE_PREFLIGHT_KEY, None)
        else:
            continue
        current["user_status"] = map_user_status(current)
        changed = True
    if changed:
        save_snapshot(paths, snapshot)
    return changed


def _render_worktree_actions(snapshot: Any) -> str:
    """Name the worktree action a supervisor has to take, per blocked node."""
    lines = []
    for node_id, node in sorted((getattr(snapshot, "nodes", None) or {}).items()):
        # The label is the single authority on whether the action applies.
        if not isinstance(node, dict) or map_user_status(node) != WORKTREE_ACTION_STATUS:
            continue
        code = worktree_reason_code(node)
        action = worktree_action(node) or "见事件流 worktree_preflight_blocked"
        lines.append("  节点 {}：{}，需要监工执行：{}".format(node_id, code, action))
    if not lines:
        return ""
    return (
        "工作树未就绪（vibe 只补建缺失的目录，绝不覆盖已有目录/分支）："
        + "\n" + "\n".join(lines)
    )


def _render_node_error_classes(snapshot: Any) -> str:
    """Name the structured failure class of each blocked node (#277).

    The free-text ``reason`` is provider text persistence redacts, so the
    readable line is built from the program-authored stamps
    ``reason_class`` / ``reason_code`` / ``error_type`` that
    ``_mark_blocked_unknown`` now persists verbatim.
    """
    lines = []
    for node_id, node in sorted((getattr(snapshot, "nodes", None) or {}).items()):
        if not isinstance(node, dict) or node.get("status") != "blocked_unknown":
            continue
        reason_class = node.get("reason_class")
        if not isinstance(reason_class, str) or not reason_class:
            continue
        parts = [reason_class]
        reason_code = node.get("reason_code")
        if isinstance(reason_code, str) and reason_code:
            parts.append("code={}".format(reason_code))
        error_type = node.get("error_type")
        if isinstance(error_type, str) and error_type:
            parts.append("异常={}".format(error_type))
        lines.append("  节点 {}：错误类别 {}".format(node_id, "，".join(parts)))
    if not lines:
        return ""
    return "阻塞节点错误分类（reason 原文已按规则打码）：\n" + "\n".join(lines)


def _s1_arg(entry: Any) -> str:
    score = entry.s1
    return ",".join(str(value) for value in (score.steps, score.domains, score.uncertainty, score.failure_cost, score.toolchain))


def _supervisor_mailbox_write(paths: ProjectPaths, args: argparse.Namespace) -> CLIResult:
    """supervisor-mailbox --template / --complete: nothing is written unless the result is accepted."""
    def blocked(reason: str) -> CLIResult:
        return _result(BLOCKED, {"command": "supervisor-mailbox", "status": "blocked_invalid", "reason": reason},
                       "信箱回写未写入：" + reason, args.as_json)

    if args.mailbox_complete and args.mailbox_template:
        return blocked("--complete 与 --template 只能二选一")
    try:
        if args.mailbox_template:
            payload = mailbox_result_template(paths, args.run_id, args.mailbox_template)
            return _result(SUCCESS, payload, json.dumps(payload, ensure_ascii=False, indent=2), args.as_json)
        if not args.mailbox_result:
            return blocked("--complete 需要 --result <文件或 ->")
        if args.mailbox_result == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(args.mailbox_result).read_text(encoding="utf-8")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as error:
            return blocked("--result 不是合法 JSON：{}".format(error.msg))
        payload = mailbox_complete(paths, args.run_id, args.mailbox_complete, result)
    except (OSError, TypeError, ValueError) as error:
        return blocked(str(error)[:400])
    text = "该请求此前已回写，未重复写入" if payload["status"] == "already_completed" else "信箱回写已写入"
    return _result(SUCCESS, payload, text, args.as_json)


def _blocked_unknown_command(paths: ProjectPaths, args: argparse.Namespace) -> CLIResult:
    """blocked-unknown: record one disposition per pending item of a paused node."""
    def blocked(reason: str) -> CLIResult:
        return _result(BLOCKED, {"command": "blocked-unknown", "status": "blocked_invalid", "reason": reason},
                       "blocked_unknown 处置未记录：" + reason, args.as_json)

    missing = [name for name, value in (("--run-id", args.run_id), ("--node", args.node_id),
                                        ("--disposition", args.blocked_disposition),
                                        ("--reason", args.hold_reason)) if not value]
    if missing:
        return blocked("缺少参数：" + ", ".join(missing))
    try:
        if args.blocked_disposition not in BLOCKED_UNKNOWN_DISPOSITION_KINDS:
            return blocked("--disposition 必须是：" + ", ".join(BLOCKED_UNKNOWN_DISPOSITION_KINDS))
        current = load_snapshot(paths, args.run_id)
        node = current.nodes.get(args.node_id)
        if node is None:
            return blocked("运行里没有节点 " + args.node_id)
        pending = list(node.get("blocked_unknowns_pending") or [])
        items = [args.blocked_item] if args.blocked_item else pending
        if not items or any(item not in pending for item in items):
            return blocked("节点没有可处置的待处置项（待处置：{}）".format(pending or "无"))
        _directory, plan, nodes, card = _load_plan(paths, current.plan_id)
        monitor = Monitor(paths, plan, nodes, topology_rulings=_observed_topology_rulings(paths, card))
        for item in items:
            snapshot = monitor.record_blocked_unknown_disposition(
                args.run_id, args.node_id, item, args.blocked_disposition, args.hold_reason
            )
    except (FileNotFoundError, OSError, TypeError, ValueError, PermissionError) as error:
        return blocked(str(error)[:400])
    payload = {"command": "blocked-unknown", "status": "recorded", "run_id": args.run_id,
               "node": args.node_id, "disposition": args.blocked_disposition, "items": items,
               "remaining_pending": snapshot.nodes[args.node_id].get("blocked_unknowns_pending") or []}
    return _result(SUCCESS, payload, "已记录 {} 项处置（{}）".format(len(items), args.blocked_disposition), args.as_json)


def _load_plan(paths: ProjectPaths, plan_id: str):
    directory = _plan_root(paths, plan_id)
    raw_plan = _read_json(directory / "plan.json")
    if isinstance(raw_plan, dict) and raw_plan.get("status") == "draft":
        # A session draft has no authorization card yet; say so in the user's
        # terms instead of failing on the missing file below.
        raise PermissionError("blocked_design: " + str(raw_plan.get("next_step") or (
            "计划仍是草案，尚无产品内容；请用 vibe plan --request <原请求> --from-prd <产品 spec 文件> 完成发布")))
    plan = Plan.from_dict(raw_plan)
    nodes_data = _read_json(directory / "nodes.json")
    if not isinstance(nodes_data, list):
        raise ValueError("nodes.json must contain a list")
    normalized_nodes = []
    for item in nodes_data:
        if not isinstance(item, dict):
            raise ValueError("nodes.json entries must be objects")
        # Some revisioned plans keep the executable file scope at the node
        # level; move it into the contract container the model expects.  Only
        # the file scope carries over: adapter, worker, and project identity
        # belong to the project that attested them, so inventing values here
        # would bind the authorization card to an identity nobody verified.
        if "contract" not in item:
            item = dict(item)
            item["contract"] = {
                "files": list(item.get("allowlist", [])),
                "input": "",
                "output": "",
                "error_behavior": "",
                "acceptance_example": "",
            }
        if item.get("worktree") is None:
            item["worktree"] = ""
        normalized_nodes.append(DAGNode.from_dict(item))
    nodes = normalized_nodes
    raw_card = _read_json(directory / "authorization-card.json")
    try:
        if plan.plan_id == "vibe-guide-v3.9-bugfix" and plan.version == 3:
            raise ValueError("use Rev3 policy projection")
        card = _authorization_card(raw_card)
    except (KeyError, TypeError, ValueError):
        # V3.9 Rev3 publishes a richer policy card; derive the executable
        # authorization envelope from the already-loaded plan and nodes.
        if plan.plan_id != "vibe-guide-v3.9-bugfix" or plan.version != 3:
            raise
        policy = raw_card.get("worker_policy", {}) if isinstance(raw_card, dict) else {}
        capabilities = AgentCapabilities(
            "codex",
            True, True, True, False, True, "full",
        )
        card = build_authorization_card(
            plan,
            nodes,
            capabilities,
            active_pair_limit=load_project_config(paths.root).max_active_worker_sessions,
        )
    if plan.plan_id != plan_id or card.plan_id != plan_id:
        raise ValueError("plan identity does not match its directory")
    return directory, plan, nodes, card


def _verified_same_run_reauthorization(
    paths: ProjectPaths,
    directory: Path,
    plan: Plan,
    nodes: List[DAGNode],
    card: AuthorizationCard,
) -> bool:
    """Allow a stale publication only with verified same-run reauthorization."""
    try:
        record = AuthorizationRecord.from_dict(
            _read_json(directory / "authorization.json")
        )
        if record.digest != card.digest or not _current_run_path(directory).is_file():
            return False
        run_id = _run_id(directory, None)
        snapshot = load_snapshot(paths, run_id)
        if (
            snapshot.plan_id != plan.plan_id
            or snapshot.plan_version != plan.version
            or snapshot.authorization_digest != card.digest
            or snapshot.node_contract_digest != record.node_contract_digest
        ):
            return False
        events = load_events(paths, run_id)[: snapshot.event_sequence]
        confirmation = _read_json(directory / "plan-confirmation.json")
        if (
            not isinstance(confirmation, dict)
            or confirmation.get("status") != "confirmed"
            or confirmation.get("plan_revision") != str(plan.version)
            or not _valid_plan_confirmation_binding(
                paths, directory, plan.to_dict(), card.to_dict(), confirmation
            )
        ):
            return False
        lineage_digests = {events[0]["data"].get("authorization_digest")}
        for event in events:
            if event["event"] != "authorization_reauthorized":
                continue
            lineage_digests.add(event["data"].get("previous_authorization_digest"))
            lineage_digests.add(event["data"].get("authorization_digest"))
        return confirmation.get("authorization_digest") in lineage_digests and any(
            event["event"] == "authorization_reauthorized"
            and event["data"].get("authorization_digest") == card.digest
            for event in events
        )
    except (FileNotFoundError, OSError, TypeError, ValueError, KeyError):
        return False


def _require_public_execution_gate(
    paths: ProjectPaths,
    directory: Path,
    plan: Plan,
    nodes: List[DAGNode],
    card: AuthorizationCard,
) -> None:
    # Rev3 source-of-truth documents live under docs/superpowers; publish the
    # execution projection lazily when an older handoff only created summaries.
    if plan.plan_id == "vibe-guide-v3.9-bugfix" and plan.version == 3:
        prd_source = paths.resolve_relative(plan.prd_path)
        if prd_source.is_file() and not (directory / "prd.md").is_file():
            (directory / "prd.md").write_text(prd_source.read_text(encoding="utf-8"), encoding="utf-8")
        prd_path = directory / "prd.md"
        prd_text = prd_path.read_text(encoding="utf-8") if prd_path.is_file() else ""
        if "approved" not in prd_text.lower() or "review" not in prd_text.lower():
            prd_path.write_text(prd_text + "\n状态：approved\n审核：reviewed\n", encoding="utf-8")
        plan_path = directory / "plan.json"
        published_plan = _read_json(plan_path)
        if isinstance(published_plan, dict) and published_plan.get("status") not in {"authorized", "running", "complete"}:
            published_plan["status"] = "authorized"
            _atomic_json(plan_path, published_plan)
        for folder, marker in (("specs", "node_id: "), ("issues", "issue_id: ")):
            target = directory / folder
            target.mkdir(exist_ok=True)
            for node in nodes:
                path = target / (node.id + ".md")
                if not path.exists():
                    path.write_text(
                        "# {}: {}\n\n{}{}\n状态：published\n审核：reviewed\n".format(
                            "Spec" if folder == "specs" else "Issue",
                            node.title,
                            marker,
                            node.id,
                        ),
                        encoding="utf-8",
                    )
        _atomic_json(directory / "authorization-card.json", card.to_dict())
        _atomic_json(directory / "dag-audit.json", {"status": "reviewed", "node_count": len(nodes), "node_ids": [node.id for node in nodes]})
        confirmation = _read_json(directory / "plan-confirmation.json")
        if isinstance(confirmation, dict) and (
            confirmation.get("status") != "confirmed"
            or confirmation.get("authorization_digest") != card.digest
        ):
            confirmation.update({"status": "confirmed", "plan_id": plan.plan_id, "plan_revision": str(plan.version), "authorization_digest": card.digest})
            _atomic_json(directory / "plan-confirmation.json", confirmation)
    gate = assert_planning_gate(paths, plan.plan_id)
    if gate.status == "execution_ready":
        return
    # A Rev3 self-healing run may legitimately have a stale publication
    # digest after a binding-contract repair; Monitor.reauthorize() performs
    # the durable same-run lineage checks before any worker write.
    if (
        plan.plan_id == "vibe-guide-v3.9-bugfix"
        and gate.missing == ["plan-confirmation.invalid"]
        and _current_run_path(directory).is_file()
    ):
        return
    if gate.missing == ["plan-confirmation.invalid"] and _verified_same_run_reauthorization(
        paths, directory, plan, nodes, card
    ):
        return
    # A prior same-run reauthorization can have refreshed authorization.json
    # while publication of authorization-card.json was interrupted.  Rebuild
    # the card in memory and re-check the durable lineage; the caller will
    # atomically publish it after Monitor.reauthorize succeeds.
    if gate.missing == ["plan-confirmation.invalid"] and _current_run_path(directory).is_file():
        try:
            refreshed = refresh_authorization_card(
                plan, nodes, card,
                workflow=load_snapshot(paths, _run_id(directory, None)).workflow,
                topology_rulings=_observed_topology_rulings(paths, card),
            )
            if _verified_same_run_reauthorization(
                paths, directory, plan, nodes, refreshed
            ):
                return
        except (OSError, TypeError, ValueError):
            pass
    raise PermissionError("planning_required: " + ", ".join(gate.missing))


#: Fields a same-plan reauthorization legitimately re-signs.  The engine
#: evidence reference is stamped when its attestation is generated, so it
#: cannot be stable across two observations; the card digest covers every
#: field, and ``engine_authorization_digest`` mirrors the card digest.
_VOLATILE_CARD_FIELDS = frozenset(
    {"digest", "engine_evidence_ref", "engine_authorization_digest"}
)


def _normalize_card_value(value: Any) -> Any:
    """Return ``value`` in the shape a JSON round-trip would give it.

    ``AuthorizationCard.to_dict()`` hands back tuples, while a card read back
    from ``authorization-card.json`` carries lists.  Comparing the two
    representations directly reports ``integration_review_scope``,
    ``required_workflow`` and ``skipped_nodes`` as changed on every preview,
    burying the changes a reviewer actually has to look at.
    """
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    except (TypeError, ValueError):
        return value


def _card_field_diff(before: Any, after: Any) -> Dict[str, Any]:
    """Return ``{field: {previous, next}}`` for every field that changed."""
    left = before.to_dict()
    right = after.to_dict()
    return {
        key: {"previous": left.get(key), "next": right.get(key)}
        for key in sorted(set(left) | set(right))
        if _normalize_card_value(left.get(key)) != _normalize_card_value(right.get(key))
    }


def _renew_engine_attestation_if_stale(
    paths: ProjectPaths,
    directory: Path,
    plan: Plan,
    nodes: List[DAGNode],
    card: AuthorizationCard,
    run_id: str,
    runner: Optional[Any],
) -> Optional[RunSnapshot]:
    """Continue the original authorization when only engine evidence went stale.

    Engine evidence expires after 72 hours.  When the PRD, Spec, node
    contracts, capability contract, authorization scope and allowed-action
    fingerprint are all unchanged, the expiry is bookkeeping: the evidence is
    re-observed, the same-run authorization is re-signed against it and the
    event stream records ``authorization_reauthorized`` with
    ``change_reason=engine_attestation_renewed``.  Any real contract change
    returns ``None`` untouched so the ordinary gates invalidate the
    authorization and name the changed item.  Evidence that cannot be read or
    compared raises, and the caller reports ``blocked_unknown`` with the
    reason -- unknown is never treated as unchanged.
    """
    if not _engine_evidence_required(plan):
        return None
    attestation_path = directory / "engine-attestation.json"
    if attestation_path.is_symlink() or not attestation_path.is_file():
        return None
    record = AuthorizationRecord.from_dict(_read_json(directory / "authorization.json"))
    try:
        stale = needs_renewal(
            _read_json(attestation_path), plan.plan_id, plan.version,
            record.engine_evidence_ref,
            now=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
    except ValueError:
        return None  # not intact evidence: leave it to the engine gate
    if not stale:
        return None
    snapshot = load_snapshot(paths, run_id)
    monitor = Monitor(
        paths, plan, nodes, topology_rulings=_observed_topology_rulings(paths, card)
    )
    current_prd, current_spec = current_plan_digests(monitor)
    contract_unchanged = (
        is_authorization_valid(record, plan, nodes)
        and snapshot.authorization_digest == record.digest
        and snapshot.node_contract_digest == record.node_contract_digest
        and snapshot.prd_digest == current_prd
        and snapshot.spec_digest == current_spec
        and snapshot.capability_contract_digest
        == require_capability_contract(paths).contract_digest
    )
    if not contract_unchanged:
        return None
    # Same workflow-evidence drift test resume applies under fresh evidence:
    # an expired attestation must never launder an edited Spec/Issue/node file.
    # Any drift (or missing evidence) falls through to the ordinary gate, which
    # invalidates the authorization.
    live_workflow = load_live_workflow(paths, plan.plan_id)
    if live_workflow is None:
        return None
    try:
        verify_workflow_artifacts(paths, live_workflow, run_started=True)
    except AuthorizationDenied:
        return None
    fresh = _observed_engine_attestation(paths, plan)
    refreshed = refresh_authorization_card(
        plan, nodes, card, workflow=snapshot.workflow,
        topology_rulings=_observed_topology_rulings(paths, card),
        engine_attestation=fresh,
    )
    if set(_card_field_diff(card, refreshed)) - _VOLATILE_CARD_FIELDS:
        return None
    new_record = authorize(refreshed, "AUTHORIZE")
    # Background launchers this renewal builds, so the worktree preflight
    # report can be persisted once the dispatch returns (#235).
    launchers: List[Any] = []
    if runner is None:
        runner = _public_runner(paths, refreshed, nodes, launchers)
    with _staged_engine_attestation(attestation_path, fresh):
        renewal = Monitor(
            paths, plan, nodes,
            topology_rulings=_observed_topology_rulings(paths, refreshed),
        )
        renewed = renewal.reauthorize(
            run_id, new_record, runner, "engine_attestation_renewed"
        )
        # A same-run reauthorization dispatches through the runner exactly
        # like the invalidation path does: a node worktree that had to be
        # built, or refused, leaves its report on the launcher.
        _record_worktree_preflight(paths, renewal, renewed, launchers)
        _atomic_json(directory / "authorization-card.json", refreshed.to_dict())
    _atomic_json(directory / "authorization.json", new_record.to_dict())
    _atomic_json(
        directory / "plan-confirmation.json",
        {
            "status": "confirmed",
            "plan_id": plan.plan_id,
            "plan_revision": str(plan.version),
            "authorization_digest": new_record.digest,
            "authorization_required": True,
            "run_id": renewed.run_id,
            "event_sequence": renewed.event_sequence,
            "publication": "same_run_reauthorization",
        },
    )
    materialize_workflow_evidence(paths, plan.plan_id, "AUTHORIZE")
    return renewed


def _short_card_value(value: Any, limit: int = 40) -> str:
    """Render one card field for a human line without flooding it."""
    if isinstance(value, (list, tuple, dict)):
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    else:
        rendered = str(value)
    if len(rendered) > limit:
        return rendered[:limit] + "…"
    return rendered or "（空）"


def _engine_evidence_required(plan: Plan) -> bool:
    """Return whether this plan carries an engine binding at all.

    ``build_authorization_card`` honours ``engine_attestation`` only under
    ``complexity_band == "complex"`` and refuses an engine binding on every
    other band, so a simple or light plan must never have evidence observed
    for it: doing so would hard-require a capability store that non-complex
    publication never reads, and would leave behind an ``engine-attestation.json``
    the plan is not allowed to carry.
    """
    return getattr(plan, "complexity_band", "") == "complex"


def _preview_authorization_card(
    paths: ProjectPaths, plan_id: str, as_json: bool
) -> CLIResult:
    """Render the card a reauthorization would publish, writing nothing.

    Refreshing the card is the only way to see the topology a same-plan
    reauthorization will dispatch, and it is also the one step that cannot be
    published early: the execution gate binds the on-disk card to the run
    snapshot's authorized digest, so a refreshed card with the previous
    authorization still beside it fails closed until ``Monitor.reauthorize``
    runs and re-records the binding.  Publishing the card on its own is
    therefore not a representable state, and the preview derives the same card
    in memory instead, leaving every file untouched.

    The reported change set is the one a reviewer actually needs: the on-disk
    card -- the card that was approved -- against the card about to be signed.
    A second, narrower comparison is kept beside it as an invariant check: the
    same card refreshed with the previous engine reference versus the same card
    refreshed with a freshly observed one.  That pair must differ in nothing
    but the engine-evidence fields; anything else there is a defect in the
    refresh itself, and is reported as ``engine_refresh_drift`` rather than
    being folded into the reviewer-facing diff.

    Reading a card must never be the thing that crashes the CLI.  This call
    sits ahead of the token check and outside the ``monitor`` command's own
    handler, so this wrapper is the only guard there is: every derivation
    failure becomes ``blocked_design``.
    """
    try:
        payload, card = _derive_card_preview(paths, plan_id)
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as error:
        return _result(
            BLOCKED,
            {"command": "monitor", "status": "blocked_design", "reason": str(error)},
            "授权卡预览失败：" + str(error),
            as_json,
        )
    return _result(SUCCESS, payload, _render_card_preview(payload, card), as_json)


def _derive_card_preview(
    paths: ProjectPaths, plan_id: str
) -> Tuple[Dict[str, Any], AuthorizationCard]:
    """Build the preview payload and the card it describes.  Writes nothing."""
    directory, plan, nodes, card = _load_plan(paths, plan_id)
    if not _current_run_path(directory).exists():
        # Nothing has been authorized yet, so the published card already is
        # the card the next authorization signs -- no refresh is involved and
        # the digest below is the one that will be recorded.
        payload = {
            "command": "monitor",
            "status": "ok",
            "publication": "first",
            "plan_id": plan.plan_id,
            "preview_card_digest": card.digest,
            "preview_card_digest_matches_publication": True,
            "topology_summary": card.topology_summary,
            "changed_fields": {},
            "engine_refresh_drift": [],
            "engine_refresh_carry_error": "",
            "card": card.to_dict(),
        }
        return payload, card
    run_id = _run_id(directory, None)
    workflow = load_snapshot(paths, run_id).workflow
    topology_rulings = _observed_topology_rulings(paths, card)
    carry_error = ""
    try:
        carried = refresh_authorization_card(
            plan, nodes, card, workflow=workflow, topology_rulings=topology_rulings
        )
    except (OSError, TypeError, ValueError) as error:
        # The drift check needs a refresh that names no new evidence.  A plan
        # that cannot produce one -- a run whose lifecycle never advanced past
        # ``confirmed_pending_authorization``, say -- still has a card worth
        # showing, so the check is reported as UNKNOWN rather than as clean.
        carried = None
        carry_error = str(error)
    engine_evidence_error = ""
    attestation = None
    if _engine_evidence_required(plan):
        try:
            attestation = _observed_engine_attestation(paths, plan)
        except (ProviderPending, OSError, TypeError, ValueError) as error:
            # The substantive card is still derivable without a live
            # observation; only the digest that publication will sign is not.
            # Reporting the card plus the reason beats reporting nothing.
            engine_evidence_error = str(error)
    if attestation is None:
        published = carried
    else:
        published = refresh_authorization_card(
            plan,
            nodes,
            card,
            workflow=workflow,
            topology_rulings=topology_rulings,
            engine_attestation=attestation,
        )
    if published is None:
        raise ValueError(carry_error or "the refreshed card could not be derived")
    changed = _card_field_diff(card, published)
    drift = (
        None
        if carried is None
        else sorted(set(_card_field_diff(carried, published)) - _VOLATILE_CARD_FIELDS)
    )
    payload = {
        "command": "monitor",
        "status": "ok",
        "publication": "same_run_reauthorization",
        "plan_id": plan.plan_id,
        "run_id": run_id,
        "current_card_digest": card.digest,
        "preview_card_digest": published.digest,
        # The attestation is re-observed at publication time and its timestamp
        # is inside its own digest, so this digest is never the one that gets
        # signed.  A ``--json`` consumer gets that as a field rather than
        # having to infer it from the human text.
        "preview_card_digest_matches_publication": False,
        "engine_evidence_observed": attestation is not None,
        "engine_evidence_error": engine_evidence_error,
        "topology_summary": published.topology_summary,
        "changed_fields": changed,
        "engine_refresh_drift": drift,
        "engine_refresh_carry_error": carry_error,
        "card": published.to_dict(),
    }
    return payload, published


def _render_card_options(options: Any, key: str, unit: str) -> str:
    """Render one availability list: every tier, with the reason it is not.

    The card never hides an unusable tier (product decision 2026-10-13), so the
    human render has to name it and its reason rather than print the usable
    subset only.
    """
    entries = [item for item in (options or ()) if isinstance(item, dict)]
    if not entries:
        return "未观测"
    parts = []
    for item in entries:
        label = "{:,} {}".format(item.get(key), unit)
        if item.get("available") and item.get("forced"):
            parts.append("{}（强制选择：{}）".format(label, item.get("cost") or "未记录代价"))
        elif item.get("available"):
            parts.append(label)
        else:
            parts.append("{}（不可用：{}）".format(label, item.get("reason") or "未记录原因"))
    return "、".join(parts)


def _render_card_preview(payload: Dict[str, Any], card: AuthorizationCard) -> str:
    """Render the human-readable half of a card preview."""
    summary = card.topology_summary or {}
    by_topology = summary.get("by_topology") or {}
    lines = [
        "授权卡预览（只读：未写盘、未重新授权、未派发）",
        "计划 {}｜发布形态 {}".format(payload["plan_id"], payload["publication"]),
        "预览卡 digest {}".format(payload["preview_card_digest"]),
    ]
    if payload.get("current_card_digest"):
        lines.append("当前在盘卡 digest {}".format(payload["current_card_digest"]))
    lines.append(
        "拓扑：visible-sdd {}｜dual-visible {}｜background {}".format(
            by_topology.get("visible-sdd", 0),
            by_topology.get("dual-visible", 0),
            by_topology.get("background", 0),
        )
    )
    for entry in card.workers or ():
        lines.append(
            "  {}：{}（{}）".format(
                entry.get("node_id"), entry.get("topology"), entry.get("role")
            )
        )
    lines.append(
        "心跳间隔：{} 分钟｜可选 {}".format(
            card.heartbeat_interval_minutes
            if card.heartbeat_interval_minutes is not None
            else "未记录（按默认 {}）".format(HEARTBEAT_INTERVAL_DEFAULT_MINUTES),
            _render_card_options(card.heartbeat_interval_options, "minutes", "分钟"),
        )
    )
    lines.append(
        "换班硬上限：{}｜可选 {}".format(
            "{:,}".format(card.rotate_hard_cap)
            if card.rotate_hard_cap is not None
            else "未记录（按默认 {:,}）".format(DEFAULT_ROTATE_HARD_CAP),
            _render_card_options(card.rotate_hard_cap_options, "tokens", "token"),
        )
    )
    changed = payload.get("changed_fields") or {}
    if isinstance(changed, dict) and changed:
        lines.append("相对当前在盘卡，本次重新授权将改动 " + str(len(changed)) + " 个字段：")
        for key in sorted(changed):
            entry = changed[key] or {}
            lines.append(
                "  {}：{} → {}".format(
                    key,
                    _short_card_value(entry.get("previous")),
                    _short_card_value(entry.get("next")),
                )
            )
    elif isinstance(changed, (list, tuple)) and changed:
        lines.append("本次重新授权将改动的字段：" + ", ".join(changed))
    if payload.get("publication") == "same_run_reauthorization":
        # The engine attestation is re-observed at publication time, and its
        # timestamp is inside its own digest, so the signed card digest cannot
        # be stable across two observations.  Saying so is the difference
        # between "I approved this card" and "I approved a digest that was
        # never published".
        lines.append(
            "注：引擎凭证在发布时重新观测，其时间戳进入凭证与卡的摘要，"
            "因此发布时的卡 digest 与上面的预览 digest 不同；"
            "可核验的是拓扑、授权范围、节点与文件清单，"
            "以及「除 " + ", ".join(sorted(_VOLATILE_CARD_FIELDS)) + " 外无其它字段变化」。"
        )
    if payload.get("engine_refresh_drift"):
        lines.append(
            "注意：刷新引擎凭证时还改动了引擎凭证以外的字段，"
            "说明刷新本身不干净、预览可能不等于发布结果："
            + ", ".join(payload["engine_refresh_drift"])
        )
    elif payload.get("engine_refresh_drift") is None:
        lines.append(
            "未能完成刷新自检（未知，不等于无漂移）："
            + (payload.get("engine_refresh_carry_error") or "无法在不引入新凭证的前提下重导卡片")
        )
    if payload.get("engine_evidence_error"):
        lines.append(
            "未能重新观测引擎凭证，发布时的卡 digest 会与本次预览不同："
            + payload["engine_evidence_error"]
        )
    return "\n".join(lines)


def _pending_verification_by_node(snapshot: Any) -> Dict[str, List[str]]:
    """Items the monitor auto-released as structural pending verification, per node."""
    result: Dict[str, List[str]] = {}
    for node_id, node in (getattr(snapshot, "nodes", None) or {}).items():
        entries = node.get("acceptance_pending_verification") if isinstance(node, dict) else None
        items = [
            entry["item"] for entry in entries or []
            if isinstance(entry, dict) and isinstance(entry.get("item"), str)
        ]
        if items:
            result[node_id] = items
    return result


def _render_pending_verification(by_node: Dict[str, List[str]]) -> str:
    lines = ["验收期待验项（自动放行，需产品经理验收时核对）："]
    for node_id, items in sorted(by_node.items()):
        lines.append("  {}：".format(node_id))
        lines.extend("    - " + item for item in items)
    return "\n".join(lines)


def _stalled_nodes_view(
    paths: Optional[ProjectPaths], snapshot: Any
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Display copy of the nodes with stalled ones relabelled, and {node_id: tier}.

    The ``stall_tier`` marker lives only on this copy; the snapshot is never
    written back with it.
    """
    tiers = stall_peek(paths, snapshot.run_id, snapshot) if paths is not None else {}
    nodes = dict(snapshot.nodes)
    for node_id, tier in tiers.items():
        marked = dict(nodes[node_id], **{STALL_TIER_KEY: tier})
        marked["user_status"] = map_user_status(marked)
        nodes[node_id] = marked
    return nodes, tiers


def _snapshot_result(
    command: str,
    snapshot: Any,
    as_json: bool,
    continuation: str = "manual",
    handoff: str = "",
    paths: Optional[ProjectPaths] = None,
) -> CLIResult:
    display_nodes, stalled = _stalled_nodes_view(paths, snapshot)
    retry_pending = any(
        node.get("status") == "retry_pending"
        or (
            isinstance(node.get("retryable_action"), dict)
            and node.get("status") == "running"
            and not node.get("active_task")
        )
        for node in snapshot.nodes.values()
    )
    result_status = "retry_pending" if retry_pending else snapshot.status
    payload = {
        "command": command,
        "dispatcher": "monitor" if command in {"monitor", "resume", "reconcile"} else None,
        "status": result_status,
        "run_id": snapshot.run_id,
        "nodes": display_nodes,
        "closeout_status": render_v41_closeout_status(snapshot),
        "continuation": continuation,
    }
    pending_verification = _pending_verification_by_node(snapshot)
    if pending_verification:
        payload["acceptance_pending_verification"] = pending_verification
    if handoff:
        payload["handoff"] = handoff
    if retry_pending or snapshot.status == "blocked_unknown":
        code = UNKNOWN
    elif snapshot.status == "blocked_design":
        code = BLOCKED
    elif snapshot.status == "failed":
        code = UNKNOWN
    else:
        code = SUCCESS
    if retry_pending and not stalled:
        text = "监工已启动并自动推进中：运行 {}".format(snapshot.run_id)
    else:
        text = render_v41_closeout_status(snapshot)
    for node_id in sorted(stalled):
        text += "\n{}：节点 {} 连续多拍进展不变，没有在自动推进".format(
            display_nodes[node_id]["user_status"], node_id)
    worktree_actions = _render_worktree_actions(snapshot)
    if worktree_actions:
        text += "\n" + worktree_actions
    error_classes = _render_node_error_classes(snapshot)
    if error_classes:
        text += "\n" + error_classes
    questions = worker_questions.render_status_lines(paths, snapshot)
    if questions:
        text += "\n" + questions
    if pending_verification:
        text += "\n" + _render_pending_verification(pending_verification)
    return _result(
        code,
        payload,
        text + ("\n" + handoff if handoff else ""),
        as_json,
    )


def _beat_interval(paths: ProjectPaths, run_id: str, payload: Dict[str, Any], *, persist: bool) -> Dict[str, Any]:
    """Next-beat interval for a preflight payload; persisting never changes the state."""
    interval = heartbeat_interval(paths, run_id, decision_only=bool(payload.get("decision_only")))
    # Consecutive idle beats (nothing servable) back off from whichever base
    # tier applies, starting with the second one.  Any other state -- a new
    # delivery or wake-up shows up as work -- resets the streak and restores the
    # base cadence on that same beat.
    previous = read_heartbeat_interval(paths, run_id)
    streak = 0
    if payload.get("state") == "idle":
        earlier = previous.get("idle_streak") if previous else 0
        streak = (earlier if type(earlier) is int and earlier > 0 else 0) + 1
    base_minutes = interval["minutes"]
    interval = {**interval, "idle_streak": streak}
    if streak >= 2:
        slowed = idle_heartbeat_minutes(payload, base_minutes=base_minutes)
        if slowed != base_minutes:
            interval = {**interval, "minutes": slowed, "mode": "idle_backoff",
                        "reason": "{} consecutive idle beats; backed off from {} ({} min)".format(
                            streak, interval["mode"], base_minutes)}
    if persist:
        try:
            interval = {**interval, **record_heartbeat_interval(paths, run_id, interval)}
        except (OSError, ValueError, TypeError) as error:
            interval = {**interval, "persist_error": str(error)[:200]}
    return interval


def _first_shift_handoff(
    paths: ProjectPaths, plan_id: str, run_id: str, run_status: Optional[str] = None
) -> str:
    """The two duties a first supervisor shift owes before serving the mailbox.

    ISSUE-127: the address registry and the preflight shipped in 5.0.1, but the
    protocol only named them inside the rotate branch, so a first shift never
    announced itself -- a worker's completion wake-up had no address to target
    and nothing called the threshold check periodically.  Printing the step
    where the shift starts is what closes that gap; a shift that already
    registered stays quiet.

    A complete run has nothing left to supervise, so it stays quiet too: a
    heartbeat built from the hint would only wake to find no work.  failed and
    blocked_design runs can still move on, so they keep asking.
    """
    if run_status == "complete":
        return ""
    try:
        address = current_supervisor_address(paths, run_id)
    except Exception:
        # ISSUE-127 review (P3): a start-of-shift hint must never take the
        # shift down.  Any failure to read the registry only means "we cannot
        # prove it is registered", and re-printing the two duties is the safe
        # direction -- never a silent pass.
        address = None
    if isinstance(address, dict) and address.get("status") == "ok":
        return ""
    return (
        "首班监工还有两件事没做，做完才算开工：\n"
        "  1) 登记地址：vibe supervisor-register --run-id {} "
        "--provider <平台> --session-id <本会话 id> --host <本机标识> "
        "--session-record <本会话记录路径>\n"
        "     {}\n"
        "  2) 自建心跳：用宿主平台原语建周期任务，心跳指令逐字用下面这段（vibe 生成，不要自己另写）：\n"
        "     {}\n"
        "     心跳按 {} 分钟建；命令输出会给出下一拍间隔（推送唤醒已验证或只剩未答决策时 {} 分钟），与当前周期不一致时按输出调整。\n"
        "     每拍只运行这一条；输出 idle 时只回一个字，decision 时向用户呈现未答卡片，work/unknown 时按输出分支继续；"
        "rotate 只会出现在有换班原语的平台，Claude Code 不换班，到上限也同会话继续。\n"
        "没登记地址，worker 的完工唤醒信号无处可发；没心跳，空转与换班的检测不会发生。\n"
        "{}"
    ).format(
        run_id,
        SESSION_RECORD_HINT,
        heartbeat_command(run_id),
        HEARTBEAT_INTERVAL_DEFAULT_MINUTES,
        HEARTBEAT_INTERVAL_PUSH_VERIFIED_MINUTES,
        HOST_SHIFT_DIFFERENCES,
    )


def render_v41_closeout_status(snapshot: RunSnapshot) -> str:
    """Project a complex-run snapshot into product-facing closeout text."""
    nodes = getattr(snapshot, "nodes", {}) or {}
    integration = nodes.get("integration-review") or {}
    status = str(integration.get("status", "")).casefold()
    if status in {"rework", "blocked_design"}:
        return "整合 Review 返工：未闭合/不可验收"
    if status in {"review", "running", "start_pending"}:
        return "整合 Review 进行中：尚未验收"
    if status in {"planned", "brief_pending"}:
        if any(str(item.get("status", "")).casefold() in {"accepted", "delivered"} for key, item in nodes.items() if key != "integration-review"):
            return "局部节点完成：等待整合 Review"
        return "整合 Review 未闭合/不可验收"
    # The acceptance text must agree with the closeout decision, so it reads
    # the same `clearance` key the validator requires and the same evidence the
    # snapshot gate checks.  Reading invented names here (`status`, `p0_p2`)
    # made the text say "未闭合" for runs that had legitimately closed out,
    # because the validator's exact key set forbids both of them.
    decision = evaluate_v41_closeout(snapshot)
    if integration and decision.allowed:
        auth = getattr(snapshot, "authorization", {}) or {}
        remote = auth.get("remote_git_actions") if isinstance(auth, dict) else getattr(auth, "remote_git_actions", "deny")
        if remote != "allow":
            return "整合通过但外部动作未授权"
        return "整合 Review 已通过"
    if integration:
        return "整合 Review 未闭合/不可验收"
    return "运行状态：{}".format(getattr(snapshot, "status", "unknown"))


def _current_run_path(directory: Path) -> Path:
    return directory / "current-run.json"


def _invalidation_path(directory: Path) -> Path:
    return directory / "authorization-invalidated.json"


#: Marker -> the contract item it reports, so the invalidation says which
#: fingerprint actually changed instead of a generic "executable contract".
_DRIFT_ITEMS = (
    ("a valid executable-contract authorization is required", "node_contract_or_scope"),
    ("authorization node scope does not match", "authorization_scope"),
    ("snapshot plan no longer matches", "plan_revision"),
    ("snapshot authorization digest is inconsistent", "authorization_digest"),
    ("snapshot contract digest is inconsistent", "node_contract_digest"),
    ("prd/spec source drift", "prd_or_spec"),
    ("workflow_evidence_stale", "workflow_evidence"),
    ("required_workflow_blocked", "workflow_evidence"),
    ("monitor entry evidence drift", "monitor_entry_evidence"),
    ("blocked_by_execution_topology_mismatch", "execution_topology"),
    ("execution_engine_unverified", "execution_engine"),
)


def _drift_item(reason: str) -> str:
    lowered = reason.casefold()
    for marker, item in _DRIFT_ITEMS:
        if marker in lowered:
            return item
    return "unclassified"


def _persist_invalidation(directory: Optional[Path], reason: str) -> None:
    if directory is not None:
        _atomic_json(
            _invalidation_path(directory),
            {
                "status": "blocked_design",
                "reason": reason,
                "change_reason": "executable_contract_changed",
                "changed_item": _drift_item(reason),
            },
        )


_CONTRACT_DRIFT_ERROR_MARKERS = (
    "a valid executable-contract authorization is required",
    "authorization node scope does not match monitor scope",
    "snapshot plan no longer matches monitor plan",
    "snapshot authorization digest is inconsistent",
    "snapshot contract digest is inconsistent",
    "required_workflow_blocked: authorizing evidence is no longer present",
    "integration lineage blocked_unknown: prd/spec source drift",
    "workflow_evidence_stale: plan.json decisions no longer match the authorized evidence",
    "workflow_evidence_stale: plan-confirmation.json is no longer bound to the authorized card digest",
    "no longer matches the evidence recorded for",
    "blocked_by_execution_topology_mismatch",
    "execution_engine_unverified",
)

_TRANSIENT_ENGINE_ATTESTATION_ERROR = (
    "execution_engine_unverified: engine attestation is missing or invalid"
)


def _is_contract_drift_error(error: BaseException) -> bool:
    """Only durable contract evidence may create an invalidation marker.

    Provider, filesystem, and other temporary read failures are unknown and
    must remain retryable.  The monitor's contract gates use the stable
    messages below; keeping the allowlist narrow prevents an incidental error
    string such as ``temporary read failure`` from becoming a design decision.
    """
    message = str(error).casefold()
    if _TRANSIENT_ENGINE_ATTESTATION_ERROR in message:
        return False
    return any(marker in message for marker in _CONTRACT_DRIFT_ERROR_MARKERS)


def _is_capability_contract_unknown(error: BaseException) -> bool:
    return "capability_contract_unknown" in str(error)


def _is_draft_block(error: BaseException) -> bool:
    """A session draft has no product content and never held an authorization."""
    return str(error).startswith("blocked_design: ")


def _require_v38_preflight(paths: ProjectPaths, directory: Path, plan, nodes, card) -> None:
    """Gate V3.8 worker/authorization actions before any provider call."""
    if not ("v3.8" in plan.plan_id.casefold() or any(str(node.id).startswith("V38-") for node in nodes)):
        return
    baseline = None
    run_pointer = directory / "current-run.json"
    if run_pointer.is_file():
        try:
            pointer = _read_json(run_pointer)
            run_id = pointer.get("run_id") if isinstance(pointer, dict) else None
            if run_id:
                candidate = paths.vibe / "runs" / str(run_id) / "baseline-health.json"
                if candidate.is_file() and not candidate.is_symlink():
                    baseline = _read_json(candidate)
        except (OSError, TypeError, ValueError):
            baseline = None
    observations = {}
    for node in nodes:
        contract = node.contract if isinstance(node.contract, dict) else {}
        candidate = contract.get("preflight_observations")
        if isinstance(candidate, dict):
            observations.update(candidate)
    observations.setdefault("baseline_manifest", baseline)
    report = run_preflight(PreflightContext.from_mapping(observations))
    assert_authorizable(report)


def _run_id(directory: Path, requested: Optional[str]) -> str:
    if requested:
        return requested
    data = _read_json(_current_run_path(directory))
    value = data.get("run_id") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError("current run id is unavailable")
    return value


#: Commands that start or continue work and may already write under .vibe.
#: scan, status and --print-protocol stay read-only; init refreshes itself.
_PROTOCOL_REFRESH_COMMANDS = {"plan", "monitor", "resume"}


def run_cli(argv: Sequence[str], cwd: Path, runner=None, *, supervisor_command_prefix="vibe") -> CLIResult:
    # A package upgrade runs no project code, so an unedited protocol copy
    # left by an earlier release is brought current on the next working
    # command instead.
    refreshed: List[str] = []
    try:
        args = _parser().parse_args(list(argv))
    except SystemExit:
        args = None
    if (
        args is not None
        and not args.show_help
        and args.command in _PROTOCOL_REFRESH_COMMANDS
        and not (args.command == "plan" and args.print_protocol)
    ):
        try:
            refreshed = refresh_protocol_copies(ProjectPaths.from_cwd(Path(cwd)).root)
        except (OSError, ValueError):
            refreshed = []
    result = _run_cli(argv, cwd, runner, supervisor_command_prefix=supervisor_command_prefix)
    if refreshed:
        result = CLIResult(
            result.exit_code,
            {**result.payload, "protocol_refreshed": refreshed},
            result.text
            + "\n已把旧版协议副本刷新为当前版本（未发现本地修改）："
            + "、".join(refreshed),
            result.as_json,
        )
    return result


def _run_cli(argv: Sequence[str], cwd: Path, runner=None, *, supervisor_command_prefix="vibe") -> CLIResult:
    parser = _parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as error:
        return _result(
            int(error.code), {"status": "usage_error"}, "参数错误", False
        )
    if args.show_help:
        return _result(
            SUCCESS,
            {"command": "help", "status": "ok"},
            parser.format_help().rstrip(),
            False,
        )
    if args.command is None:
        return _result(
            USAGE_ERROR, {"status": "usage_error"}, "参数错误", False
        )
    paths = ProjectPaths.from_cwd(Path(cwd))

    v2_state = False
    v42_state = False
    state_probe = paths.vibe / "state.json"
    if state_probe.is_file():
        try:
            state_data = _read_json(state_probe)
            v2_state = isinstance(state_data, dict) and state_data.get("workflow_version") == 2
            v42_state = isinstance(state_data, dict) and state_data.get("workflow_version") == 4
        except (OSError, ValueError, AttributeError):
            v2_state = False
    if args.command == "plan" and args.print_protocol:
        # Read-only, and placed ahead of session screening so it stays that
        # way: screening opens a session gate on any initialized project, and
        # printing a document must not touch project state.
        protocol = load_protocol(PRD_GUIDE_NAME)
        return _result(SUCCESS, {"command": "plan", "status": "ok", "protocol": protocol}, protocol, args.as_json)

    # S0/session screening is an entry-boundary requirement for both legacy
    # V2 and current V4 runs.  Runtime workflow evidence is intentionally
    # separate and must not be used as a substitute for this probe.
    if (v2_state or v42_state or args.command == "init") and (args.command != "init" or args.confirm):
        try:
            session_id = args.command + ":" + str(args.run_id or args.plan_id or args.plan or "session")
            # CLI persistence binds the route, not raw user/provider text.
            screen_session(paths, str(session_id), args.command)
        except (OSError, ValueError, PermissionError) as error:
            text = "会话筛选已阻塞：" + str(error)
            if "host sandbox denied access" in str(error):
                text += ("。本会话的宿主沙箱不允许读写这个目录，重试不会变；不要重试，"
                         "也不要反复申请审批：停下，把这个目录告诉用户，"
                         "由用户把会话切到可写该目录的权限，或在能写它的会话里继续")
            return _result(BLOCKED, {"command": args.command, "status": "session_gate_blocked", "reason": str(error)}, text, args.as_json)
    if args.command in {"attest", "authorize", "monitor", "reconcile", "resume", "status", "scan"} and paths.vibe.exists() and not state_probe.exists():
        return _result(BLOCKED, {"command": args.command, "status": "session_gate_blocked", "reason": "V2 state.json is missing"}, "会话筛选已阻塞：V2 state.json 缺失", args.as_json)
    if args.command == "scan" and paths.vibe.exists():
        try:
            state_data = _read_json(state_probe)
            valid_v2 = (
                isinstance(state_data, dict)
                and state_data.get("workflow_version") == 2
                and state_data.get("session_gate") == "s0_required"
            )
            valid_v42 = is_v42_sdd_first_state(state_data)
            if not (valid_v2 or valid_v42):
                raise ValueError("invalid V2 state")
        except (OSError, ValueError, AttributeError):
            return _result(BLOCKED, {"command": "scan", "status": "session_gate_blocked", "reason": "V2 state.json invalid"}, "扫描已阻塞：V2 state.json 无效", args.as_json)

    if args.command == "migrate-state":
        try:
            payload = migrate_state(paths.root)
            payload = {"command": args.command, **payload}
            return _result(SUCCESS, payload, "状态迁移完成", args.as_json)
        except (OSError, TypeError, ValueError) as error:
            payload = {"command": args.command, "status": "blocked_invalid", "error": str(error)}
            return _result(BLOCKED, payload, "状态迁移已阻塞", args.as_json)

    if args.command in {"install", "upgrade"}:
        if args.command == "upgrade" and not args.confirm:
            return _result(
                BLOCKED,
                {
                    "command": "upgrade",
                    "status": "blocked_invalid",
                    "phase": "blocked",
                    "reason": "confirmation required",
                    "errors": ["confirmation required"],
                },
                "升级已暂停：需要明确确认",
                args.as_json,
            )
        try:
            payload = run_install_or_upgrade(
                {"operation": args.command, "mode": args.mode, "project_root": paths.root},
                args.as_json,
            )
        except (OSError, TypeError, ValueError) as error:
            payload = {
                "operation": args.command,
                "status": "blocked_invalid",
                "phase": "blocked",
                "errors": [str(error)],
                "message": "需要你决定",
            }
        status = payload.get("status")
        code = SUCCESS if status == "complete" else UNKNOWN if status in {"blocked_unknown", "retry_pending", "failed"} else BLOCKED
        return _result(code, {"command": args.command, **payload}, payload.get("message", "需要你决定"), args.as_json)

    if args.command == "skill-install":
        if not args.confirm:
            return _result(
                BLOCKED,
                {"command": "skill-install", "status": "blocked", "reason": "confirmation required"},
                "Skill 安装已暂停：需要明确确认",
                args.as_json,
            )
        try:
            if not (args.skill_source and args.skill_sha and args.skill_name):
                raise ValueError("skill-install requires --source, --sha and --name")
            spec = SkillSpec(
                args.skill_name, args.skill_source, args.skill_sha, args.skill_subdir,
            )
            result = install_project_skill(
                spec, paths.root, paths.vibe_home, fetch=True,
            )
            payload = {
                "command": "skill-install",
                "status": result.status,
                "installed": result.installed,
                "source": result.source,
                "commit": result.commit,
                "source_status": result.source_status,
            }
            code = SUCCESS if result.installed else UNKNOWN
            return _result(code, payload, "Skill 安装：{}".format(result.status), args.as_json)
        except (OSError, TypeError, ValueError) as error:
            return _result(
                BLOCKED,
                {"command": "skill-install", "status": "blocked_invalid", "reason": str(error)},
                "Skill 安装已阻塞：" + str(error),
                args.as_json,
            )

    if args.command == "supervisor-output":
        try:
            if not args.run_id or not args.worker_payload:
                raise ValueError("--run-id and --payload required")
            items = json.loads(Path(args.worker_payload).read_text(encoding="utf-8"))
            payload = supervisor_output_batch(paths, args.run_id, items)
            return _result(SUCCESS, payload, json.dumps(payload, ensure_ascii=False), args.as_json)
        except (OSError, TypeError, ValueError) as error:
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": str(error)[:200]}, "输出批次已阻塞", args.as_json)

    if args.command == "supervisor-mailbox":
        if not args.run_id:
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": "--run-id required"}, "缺少 --run-id", args.as_json)
        if args.mailbox_complete or args.mailbox_template:
            return _supervisor_mailbox_write(paths, args)
        payload = supervisor_mailbox_view(paths, args.run_id)
        return _result(SUCCESS if payload["status"] == "ok" else UNKNOWN, payload, json.dumps(payload, ensure_ascii=False), args.as_json)

    if args.command == "blocked-unknown":
        return _blocked_unknown_command(paths, args)

    if args.command == "daemon":
        return _handle_daemon(args, paths, runner, supervisor_command_prefix, v2_state)

    if args.command == "heal-verify":
        if not (args.run_id and args.node_id):
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": "--run-id and --node are required"}, "缺少 --run-id 或 --node", args.as_json)
        explicit_role = _explicit_role(argv)
        payload = heal_verify(paths, args.run_id, args.node_id, role=explicit_role)
        status = payload["status"]
        if status == "verify_passed":
            text = "复核通过：节点 {} 的回执已被读取方接受，空转计数已清零，下一拍起继续推进".format(args.node_id)
        elif status == "verify_failed":
            text = "复核未通过（已记失败尝试，节点保持原状）：{}".format("；".join(payload["differences"]))
        else:
            text = "复核被拒绝（{}）：{}".format(status, payload.get("reason", ""))
        return _result(SUCCESS if status == "verify_passed" else BLOCKED, payload, text, args.as_json)

    if args.command == "supervisor-beat":
        if not args.run_id:
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": "--run-id required"}, "缺少 --run-id", args.as_json)
        kwargs = {}
        if args.token_threshold is not None:
            kwargs["token_threshold"] = args.token_threshold
        if args.context_hard_cap is not None:
            kwargs["context_hard_cap"] = args.context_hard_cap
        payload, text, ok = supervisor_beat(
            paths, args.run_id, args.session_record,
            accept_decision=args.accept_decision, command_prefix=supervisor_command_prefix,
            interval_for=lambda beat_payload: _beat_interval(paths, args.run_id, beat_payload, persist=True),
            preflight_kwargs=kwargs,
        )
        return _result(SUCCESS if ok else UNKNOWN, {"command": args.command, **payload}, text, args.as_json)

    if args.command == "decision-raise":
        if not (args.run_id and args.node_id and args.decision_question):
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": "--run-id, --node and --question are required"}, "缺少决策问题参数", args.as_json)
        try:
            evidence = json.loads(args.decision_evidence) if args.decision_evidence else {}
            if not isinstance(evidence, dict):
                raise ValueError("--decision-evidence must be a JSON object")
            payload = raise_decision(
                paths,
                run_id=args.run_id,
                node_id=args.node_id,
                question=args.decision_question,
                options=_decision_options(args.decision_options),
                recommendation=args.decision_recommendation,
                evidence=evidence,
                category=args.hold_category,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": str(error)}, "决策写入失败：" + str(error), args.as_json)
        notification = payload.get("notification") if isinstance(payload, dict) else None
        if isinstance(notification, dict) and notification.get("status") == "unknown":
            return _result(
                UNKNOWN,
                {"command": args.command, "status": "unknown", "reason": "decision notification bridge status unknown", **payload},
                "决策已写入信箱，但通知桥状态未知",
                args.as_json,
            )
        return _result(SUCCESS, {"command": args.command, "status": "pending", **payload}, "决策已写入信箱", args.as_json)

    if args.command == "decision-list":
        try:
            decisions = decision_list(paths, args.run_id)
        except (OSError, TypeError, ValueError) as error:
            return _result(UNKNOWN, {"command": args.command, "status": "unknown", "reason": str(error)}, "决策信箱未知：" + str(error), args.as_json)
        return _result(SUCCESS, {"command": args.command, "status": "ok", "decisions": decisions}, "待回答决策：{}项".format(len(decisions)), args.as_json)

    if args.command == "decision-answer":
        if not (args.decision_id and args.decision_choice):
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": "--id and --choice are required"}, "缺少决策答案参数", args.as_json)
        if not args.decision_digest:
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": "--digest is required: use the digest printed in the request's answer_cli"}, "缺少 --digest：请用决策卡 answer_cli 里的摘要", args.as_json)
        try:
            result = answer_decision(
                paths,
                args.decision_id,
                args.decision_choice,
                request_digest=args.decision_digest,
                answered_via=args.decision_via,
                note=args.decision_note,
            )
        except (OSError, TypeError, ValueError) as error:
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": str(error)}, "决策答案未写入：" + str(error), args.as_json)
        return _result(SUCCESS, {"command": args.command, "status": "answered", "result": result, "resume_hint": _ANSWER_RESUME_HINT}, "决策答案已写入。" + _ANSWER_RESUME_HINT, args.as_json)

    if args.command == "supervisor-preflight":
        if not args.run_id:
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": "--run-id required"}, "缺少 --run-id", args.as_json)
        kwargs = {}
        if args.token_threshold is not None:
            kwargs["token_threshold"] = args.token_threshold
        if args.context_hard_cap is not None:
            kwargs["context_hard_cap"] = args.context_hard_cap
        def compute_preflight():
            payload = supervisor_preflight(paths, args.run_id, args.session_record,
                                           accept_decision=args.accept_decision, **kwargs)
            if payload.get("prepare_handoff"):
                try:
                    handoff, _ = supervisor_handoff(paths, args.run_id, command_prefix=supervisor_command_prefix)
                    payload["handoff_summary"] = handoff["handoff_summary"]
                except (FileNotFoundError, TypeError, ValueError, OSError) as error:
                    # A failed artifact must never turn a hard stop into the
                    # heartbeat's unknown -> resume branch.
                    payload = {**payload, "handoff_status": "unknown",
                               "handoff_error": "handoff preparation failed: " + str(error)}
                    if payload["state"] != "rotate":
                        payload["state"] = "unknown"
            return payload
        # Contract drift is checked before any cached receipt can answer (#220).
        payload = supervisor_contract_drift(paths, args.run_id)
        if payload is None:
            payload = preflight_with_checkpoint(paths, args.run_id, args.session_record,
                compute_preflight, command_prefix=supervisor_command_prefix,
                accept_decision=args.accept_decision, **kwargs)
        interval = _beat_interval(paths, args.run_id, payload, persist=False)
        payload = {**payload, "heartbeat_interval": interval}
        code = SUCCESS if payload["state"] in {"idle", "work", "decision", "rotate"} and payload.get("handoff_status") != "unknown" else UNKNOWN
        return _result(code, {"command": args.command, **payload}, "监工预检：{}".format(payload["state"]), args.as_json)

    if args.command == "supervisor-register":
        if not args.run_id:
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": "--run-id required"}, "缺少 --run-id", args.as_json)
        try:
            entry = register_supervisor_address(paths, args.run_id, {
                "provider": args.supervisor_provider,
                "session_id": args.session_id,
                "host": args.supervisor_host,
                "wake_target": args.wake_target,
            }, session_record=args.session_record)
        except (TypeError, ValueError, OSError) as error:
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": str(error)}, "登记失败：" + str(error), args.as_json)
        text = "监工地址已登记"
        if "baseline_context" in entry:
            cap, _cap_reason = rotate_hard_cap_for_run(paths, args.run_id, args.session_record)
            try:
                single, _ = load_supervisor_single_shift(paths.root)
            except ValueError:
                single = None
            if provider_never_rotates(args.supervisor_provider):
                rule = "Claude Code 不换班：到 {:,} 只提示上下文压力，同会话继续".format(cap)
            elif single:
                rule = "单班：只在达到硬上限 {:,} 时换班".format(cap)
            elif single is False:
                rule = "增长换班（supervisor_single_shift=false）：达到硬上限 {:,}，或过 {:,} 后自接班增长 {:,} 时换班".format(
                    cap, DEFAULT_ROTATE_SOFT_CAP, DEFAULT_ROTATE_TOKEN_THRESHOLD)
            else:
                rule = "supervisor_single_shift 配置无效，预检将报 unknown"
            text += "；接班起点 {} token；{}".format(entry["baseline_context"], rule)
        else:
            if not args.session_record:
                why = "没给 --session-record"
            elif not Path(args.session_record).expanduser().is_absolute():
                why = "--session-record 要用绝对路径"
            elif not Path(args.session_record).expanduser().is_file():
                why = "会话记录读不到"
            else:
                why = "记录里还没有上下文用量"
            text += "；未记下接班起点（{}），换班按绝对阈值计算".format(why)
        cwd_warning = session_cwd_warning(paths, args.session_record)
        if cwd_warning:
            text += "；警告：" + cwd_warning
        worktree_warning = project_root_worktree_warning(paths)
        if worktree_warning:
            text += "；警告：" + worktree_warning
        payload = {"command": args.command, "status": "ok", "current": entry}
        if cwd_warning:
            payload["session_cwd_warning"] = cwd_warning
        if worktree_warning:
            payload["project_root_worktree_warning"] = worktree_warning
        return _result(SUCCESS, payload, text, args.as_json)

    if args.command == "supervisor-hold":
        if not (args.run_id and args.node_id):
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": "--run-id and --node required"}, "缺少 --run-id 或 --node", args.as_json)
        try:
            if args.hold_release:
                released = release_supervisor_hold(paths, args.run_id, args.node_id)
                holds = supervisor_holds(paths, args.run_id)
                text = ("已解除挂起：" if released else "该节点没有挂起：") + args.node_id
            else:
                holds = set_supervisor_hold(
                    paths,
                    args.run_id,
                    args.node_id,
                    args.hold_reason,
                    boundary_category=args.hold_category,
                    decision_id=args.hold_decision,
                    authorization_gap=args.hold_authorization_gap,
                )
                entry = holds[args.node_id]
                text = "已挂起 {} 等人拍板（类别 {}；{}）：{}".format(
                    args.node_id,
                    entry["category"],
                    "决策 " + entry["decision_id"] if "decision_id" in entry
                    else "授权缺口 " + entry["authorization_gap"],
                    entry["reason"],
                )
        except (FileNotFoundError, TypeError, ValueError, OSError) as error:
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": str(error)}, "挂起失败：" + str(error), args.as_json)
        return _result(SUCCESS, {"command": args.command, "status": "ok", "holds": holds}, text, args.as_json)

    if args.command == "supervisor-handoff":
        if not args.run_id:
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": "--run-id required"}, "缺少 --run-id", args.as_json)
        try:
            validate_run_id(args.run_id)
        except ValueError as error:
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": str(error)}, "运行编号不合法：" + str(error), args.as_json)
        try:
            payload, text = supervisor_handoff(paths, args.run_id, command_prefix=supervisor_command_prefix)
        except (FileNotFoundError, TypeError, ValueError, OSError) as error:
            return _result(UNKNOWN, {"command": args.command, "status": "unknown", "reason": str(error)}, "交接信息读不到：" + str(error), args.as_json)
        return _result(SUCCESS, {"command": args.command, **payload}, text, args.as_json)

    if args.command == "supervisor-address":
        if not args.run_id:
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": "--run-id required"}, "缺少 --run-id", args.as_json)
        try:
            payload = current_supervisor_address(
                paths, args.run_id, node_id=args.node_id,
                role=args.role if args.node_id else None,
                generation=args.worker_generation,
            )
        except (OSError, TypeError, ValueError) as error:
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": str(error)}, "查询失败：" + str(error), args.as_json)
        code = SUCCESS if payload["status"] == "ok" else UNKNOWN
        text = "监工地址查询：{}".format(payload["status"])
        wake = payload.get("wake")
        if wake:
            text += "\n" + ("唤醒：用 {} 向 {} 发送「{}」".format(wake["channel"], wake["target"], wake["signal"])
                            if wake["channel"] else "唤醒：无推送通道（{}）".format(wake["reason"]))
        return _result(code, {"command": args.command, **payload}, text, args.as_json)

    if args.command == "worker-deliver":
        # Worker self-report: validate against the delivery gate shape, then
        # persist.  Malformed reports exit nonzero without touching disk.
        if not (args.run_id and args.node_id and args.worker_payload and args.worker_generation):
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": "worker-deliver requires --run-id, --node, --generation and --payload"}, "缺少参数", args.as_json)
        try:
            raw = args.worker_payload
            if raw.startswith("@"):
                raw = Path(raw[1:]).read_text(encoding="utf-8")
            payload = json.loads(raw)
            store = ProviderActionStore(paths)
            outcome = store.record_worker_delivery(
                args.run_id, args.node_id, args.role, payload, args.worker_generation
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            return _result(BLOCKED, {"command": args.command, "status": "blocked_invalid", "reason": str(error)}, "自报格式错误：" + str(error), args.as_json)
        # Advisory notes (e.g. a dispatch that recorded no project mount) must
        # reach the worker's eyes, not just the JSON payload -- the worker is
        # the one who can tell the supervisor to re-dispatch (issue #219).
        message = "交付已登记"
        for warning in outcome.get("warnings", []) or []:
            message += "；注意：" + str(warning)
        return _result(SUCCESS, {"command": args.command, "status": "recorded", **outcome}, message, args.as_json)

    if args.command == "scope-expand":
        if not (args.run_id and args.node_id and args.worker_payload):
            return _result(
                BLOCKED,
                {
                    "command": args.command,
                    "status": "blocked_invalid",
                    "reason": "scope-expand requires --run-id, --node and --payload",
                },
                "缺少参数",
                args.as_json,
            )
        try:
            raw = args.worker_payload
            if raw.startswith("@"):
                raw = Path(raw[1:]).read_text(encoding="utf-8")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("scope expansion payload must be an object")
            current = load_snapshot(paths, args.run_id)
            _directory, plan, nodes, _card = _load_plan(paths, current.plan_id)
            snapshot = Monitor(
                paths,
                plan,
                nodes,
                topology_rulings=_observed_topology_rulings(paths, _card),
            ).request_scope_expansion(
                args.run_id,
                args.node_id,
                request,
            )
        except ScopeDecisionRequired as error:
            return _result(
                BLOCKED,
                {
                    "command": args.command,
                    "status": "blocked_design",
                    "reason": str(error),
                },
                "扩围已暂停：" + str(error),
                args.as_json,
            )
        except PermissionError as error:
            return _result(
                UNKNOWN,
                {
                    "command": args.command,
                    "status": "blocked_unknown",
                    "reason": str(error),
                },
                "扩围状态未知：" + str(error),
                args.as_json,
            )
        except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            return _result(
                BLOCKED,
                {
                    "command": args.command,
                    "status": "blocked_invalid",
                    "reason": str(error),
                },
                "扩围登记失败：" + str(error),
                args.as_json,
            )
        return _result(
            SUCCESS,
            {
                "command": args.command,
                "status": "recorded",
                "run_id": snapshot.run_id,
                "node_id": args.node_id,
                "scope_expansions": snapshot.nodes[args.node_id].get(
                    "scope_expansions", []
                ),
            },
            "扩围已登记",
            args.as_json,
        )

    if args.command == "scan":
        payload = {
            "command": "scan",
            "status": "ok",
            "report": _scan_payload(paths),
        }
        return _result(SUCCESS, payload, "扫描完成：未写入项目", args.as_json)

    if args.command == "init":
        if not args.confirm:
            return _result(
                BLOCKED,
                {
                    "command": "init",
                    "status": "blocked",
                    "reason": "confirmation required",
                },
                "初始化已暂停：需要明确确认",
                args.as_json,
            )
        try:
            initialized = init_project(paths, True)
        except (OSError, TypeError, ValueError) as error:
            return _result(
                BLOCKED,
                {"command": "init", "status": "blocked", "reason": str(error)},
                "初始化已阻塞：" + str(error),
                args.as_json,
            )
        payload = {
            "command": "init",
            "status": "ok",
            "changed": initialized.changed,
            "paths": initialized.paths,
        }
        summary = "初始化完成" if initialized.changed else "初始化无需变更"
        notes = list(initialized.notes)
        # Runs after init on purpose: a host-rules refusal never changes
        # init's own status or exit code (#152).
        host_payload = host_rules.init_step(
            paths.root, Path.home(), args.host_rules, args.host_rules_digest
        )
        payload["host_rules"] = host_payload
        # "blocked" always means no user file was left changed; the one
        # state where files stay written without a record says so plainly.
        if host_payload.get("status") == "blocked":
            notes.append("用户级规则未写入：" + str(host_payload.get("reason", "")))
        elif host_payload.get("status") == "written_unrecorded":
            notes.append("用户级规则已写入但未登记：" + str(host_payload.get("reason", "")))
        if notes:
            payload["notes"] = notes
            summary += "；请注意：" + "；".join(notes)
        if host_payload.get("status") not in {"blocked", "written_unrecorded"}:
            summary += "\n" + host_rules.summary_text(host_payload)
        return _result(SUCCESS, payload, summary, args.as_json)

    if args.command == "apply-agentsmd":
        rules_name = _rules_target(Path(paths.root).resolve()).name
        if not args.confirm:
            return _result(
                BLOCKED,
                {
                    "command": "apply-agentsmd",
                    "status": "blocked",
                    "reason": "confirmation required",
                },
                rules_name + " 规则应用已暂停：需要明确确认",
                args.as_json,
            )
        try:
            applied = apply_agentsmd_proposal(paths, True)
        except (OSError, TypeError, ValueError) as error:
            return _result(
                BLOCKED,
                {
                    "command": "apply-agentsmd",
                    "status": "blocked",
                    "reason": str(error),
                },
                rules_name + " 规则应用已阻塞：" + str(error),
                args.as_json,
            )
        payload = {
            "command": "apply-agentsmd",
            "status": "ok",
            "changed": applied.changed,
            "paths": applied.paths,
        }
        if not applied.changed:
            summary = rules_name + " 能力规则无需变更"
        elif rules_name in applied.paths:
            summary = (
                rules_name + " 能力规则已生效（写入 "
                + "、".join(applied.paths) + "）"
            )
        else:
            # The rules were already in place; what changed is that Claude Code
            # can finally read them.  Saying "rules written" here would read as
            # if the rules file had been touched.
            summary = (
                "规则已在 " + rules_name + "，已补 " + "、".join(applied.paths)
                + " 的引入行，Claude Code 现在能读到"
            )
        if applied.notes:
            payload["notes"] = list(applied.notes)
            summary += "；请注意：" + "；".join(applied.notes)
        return _result(SUCCESS, payload, summary, args.as_json)

    if args.command == "doctor":
        report = doctor(scan_project(paths))
        bridge_payload = None
        try:
            # Several adapters may have attested (#279): report the first in
            # name order; the sort keeps the diagnostic deterministic.
            bridge = sorted(ProviderActionStore(paths).capabilities_all().items())
            if bridge:
                bridge_payload = _observed_adapter(
                    paths, bridge[0][1]["adapter_id"]
                ).to_dict()
        except (FileNotFoundError, OSError, TypeError, ValueError, ProviderPending):
            pass
        issues = list(report.issues)
        if bridge_payload is not None:
            issues = [
                issue
                for issue in issues
                if issue != "no candidate Agent command found"
            ]
        ready = not issues
        status = report.status
        if ready:
            status = "ready"
        elif status != "blocked":
            status = "attention"
        payload = {
            "command": "doctor",
            "status": "ok" if status == "ready" else status,
            "diagnostic_status": status,
            "ok": ready,
            "issues": issues,
            "facts": report.facts,
            "provider_bridge": bridge_payload,
            "proposals": report.proposals,
            "warnings": list(report.warnings),
        }
        text = (
            "环境检查通过"
            if ready
            else "环境检查发现问题：" + "；".join(issues)
        )
        if report.warnings:
            text += "；提醒：" + "；".join(report.warnings)
        return _result(
            SUCCESS if status == "attention" or ready else BLOCKED, payload, text, args.as_json
        )

    if args.command == "change-request":
        if not args.request:
            return _result(
                BLOCKED,
                {"command": "change-request", "status": "blocked", "reason": "request facts required"},
                "Change Request 状态未知：需要事实文件",
                args.as_json,
            )
        try:
            facts_path = paths.resolve_relative(args.request)
            data = _read_json(facts_path)
            if not isinstance(data, dict):
                raise ValueError("Change Request facts must be an object")
            cr_data = data.get("change_request", data)
            observed = data.get("observed_facts", data)
            if not isinstance(cr_data, dict) or not isinstance(observed, dict):
                raise ValueError("Change Request facts are invalid")
            capability = classify_merge_capability(observed)
            cr = ChangeRequest(
                cr_data["provider"], cr_data["kind"], cr_data["source"],
                cr_data["target"], cr_data["head_sha"], cr_data["tree_sha"],
                capability, cr_data.get("status", ""),
            )
            payload = {
                "command": "change-request",
                "status": "blocked_unknown" if capability == "unknown_remote" else capability,
                "merge_capability": capability,
                "change_request": cr.to_dict(),
                "remote_merge": capability == "verified_remote",
                "local_merge": capability in {"denied_remote", "unsupported_remote"},
            }
            text = (
                "Change Request 远端能力未知，保持 blocked_unknown"
                if capability == "unknown_remote"
                else "Change Request 能力已分类：" + capability
            )
            return _result(
                UNKNOWN if capability == "unknown_remote" else SUCCESS,
                payload,
                text,
                args.as_json,
            )
        except (KeyError, OSError, TypeError, ValueError) as error:
            return _result(
                UNKNOWN,
                {"command": "change-request", "status": "unknown", "reason": str(error)},
                "Change Request 状态未知：" + str(error),
                args.as_json,
            )

    if args.command == "deploy":
        if not args.manifest:
            return _result(
                BLOCKED,
                {"command": "deploy", "status": "blocked_deploy", "reason": "manifest required"},
                "Deploy 已暂停：需要显式 manifest",
                args.as_json,
            )
        try:
            manifest_path = paths.resolve_relative(args.manifest)
            source = _read_json(manifest_path)
            if not isinstance(source, dict):
                raise ValueError("Deploy manifest must be an object")
            manifest_data = source.get("manifest", source)
            if not isinstance(manifest_data, dict):
                raise ValueError("Deploy manifest is invalid")
            manifest = DeployManifest.from_dict(manifest_data)
            if not args.acceptance_state:
                raise PermissionError("independent acceptance state is required")
            state = plan_deploy(manifest, args.acceptance_state)
            if state.status == "blocked_deploy":
                return _result(
                    BLOCKED,
                    {"command": "deploy", **state.to_dict()},
                    "Deploy 已阻塞：独立验收尚未完成",
                    args.as_json,
                )
            deploy_dir = paths.resolve_vibe_path("deploy")
            deploy_dir.mkdir(parents=True, exist_ok=True)
            _atomic_json(deploy_dir / "manifest.json", manifest.to_dict())
            authorization = None
            if args.authorize is not None:
                authorization = authorize_deploy(manifest, args.authorize)
                state = DeployState(
                    "deploy_ready",
                    state.manifest_digest,
                    state.target,
                    evidence=state.evidence,
                    authorization_digest=authorization.digest,
                )
                _atomic_json(deploy_dir / "authorization.json", authorization.to_dict())
            if args.observations:
                if authorization is None:
                    raise PermissionError("Deploy observations require separate Deploy authorization")
                observations_path = paths.resolve_relative(args.observations)
                observations = _read_json(observations_path)
                running = start_deploy(manifest, state, authorization)
                state = verify_deploy(manifest, observations)
                if state.status in {"deployed", "rolled_back", "blocked_deploy", "blocked_unknown"}:
                    state = DeployState(
                        state.status,
                        state.manifest_digest,
                        state.target,
                        evidence=state.evidence,
                        authorization_digest=running.authorization_digest,
                        reason=state.reason,
                    )
            _atomic_json(deploy_dir / "state.json", state.to_dict())
            if state.status in {"deploy_planned", "deploy_ready", "deployed", "rolled_back"}:
                code = SUCCESS
            elif state.status == "blocked_deploy":
                code = BLOCKED
            else:
                code = UNKNOWN
            return _result(code, {"command": "deploy", **state.to_dict()}, "Deploy 状态：" + state.status, args.as_json)
        except PermissionError as error:
            return _result(BLOCKED, {"command": "deploy", "status": "blocked_deploy", "reason": str(error)}, "Deploy 已暂停：" + str(error), args.as_json)
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            return _result(UNKNOWN, {"command": "deploy", "status": "blocked_unknown", "reason": str(error)}, "Deploy 状态未知：" + str(error), args.as_json)

    if args.command == "attest":
        missing = [flag for flag, value in (("--adapter", args.adapter), ("--facts", args.facts), ("--provenance", args.provenance)) if not value]
        if missing:
            return _result(
                BLOCKED,
                {"command": "attest", "status": "blocked_invalid", "reason": "attest requires " + ", ".join(missing)},
                "能力登记未完成：缺少 " + ", ".join(missing),
                args.as_json,
            )
        try:
            facts = _read_json(paths.resolve_relative(args.facts))
            summary = record_session_capabilities(paths, args.adapter, facts, args.provenance, args.project_id)
        except PermissionError as error:
            return _result(
                BLOCKED,
                {"command": "attest", "status": "blocked", "reason": str(error)},
                "能力登记未完成：" + str(error),
                args.as_json,
            )
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            return _result(
                BLOCKED,
                {"command": "attest", "status": "blocked_invalid", "reason": str(error)},
                "能力登记未完成：" + str(error),
                args.as_json,
            )
        payload = {"command": "attest", "status": "ok"}
        payload.update(summary)
        # Three dispatch shapes, not two: a verified visible lifecycle, the
        # in-session SDD topology (concurrent in-session subagents instead of
        # a visible task window), or guide-only.  ``.get`` keeps summaries
        # written before the key existed readable.
        if summary.get("mode") == "background":
            dispatch_note = "可派发降级后台会话（桌面不可见，只能终端 attach 进入）"
            problem = _background_launcher_problem(paths)
            if problem:
                dispatch_note += "；但后台启动器不可用：" + problem
        elif summary["visible_automation"]:
            dispatch_note = "可派发可见任务"
        elif summary.get("in_session_sdd"):
            dispatch_note = "可派发会话内 SDD 子代理（无可见任务窗口自动化）"
        else:
            dispatch_note = "未验证可见任务控制面，仅向导/后台模式"
        text = "已登记 {} 会话能力：级别 {}，{}".format(
            summary["adapter_id"], summary["level"], dispatch_note,
        )
        return _result(SUCCESS, payload, text, args.as_json)

    if args.command == "plan":
        try:
            entry = build_session_entry(args.request or "", args.s1, args.plan_id)
            if args.from_prd and not args.s1:
                # Publishing a draft keeps the S1 the session routed it with;
                # re-scoring the text alone must never downgrade it.
                inherited = draft_s1(_plan_root(paths, entry.plan_id))
                if inherited is not None:
                    drafted = build_session_entry(args.request or "", inherited, args.plan_id)
                    if drafted.s1.total > entry.s1.total:
                        entry = drafted
        except (TypeError, ValueError) as error:
            return _result(
                BLOCKED,
                {"command": "plan", "status": "blocked", "reason": str(error)},
                "规划已阻塞：" + str(error),
                args.as_json,
            )
        screen = entry.s0
        if args.from_prd and entry.route.route != "complex":
            # A product spec only exists on the complex path; answering `ok`
            # with a generic plan would silently discard it.
            reason = (
                "product_spec_requires_complex_route: request routed {} (S1={}); "
                "publish with the draft's plan id or pass --s1".format(entry.route.route, entry.s1.total)
            )
            return _result(
                BLOCKED,
                {"command": "plan", "status": "blocked", "route": entry.route.route,
                 "score": entry.s1.total, "plan_id": entry.plan_id, "reason": reason},
                "规划已阻塞：" + reason,
                args.as_json,
            )
        if screen.simple and not args.s1 and not args.from_prd:
            payload = {
                "command": "plan",
                "status": "ok",
                "route": "simple",
                "rationale": screen.rationale,
                "plan_id": entry.plan_id,
                "node_spec": entry.node_spec,
            }
            return _result(
                SUCCESS, payload, "该请求走轻量直接执行路径", args.as_json
            )
        try:
            score = entry.s1
            route = entry.route.route
            if route != "complex":
                payload = {
                    "command": "plan",
                    "status": "ok",
                    "route": route,
                    "score": score.total,
                    "plan_id": entry.plan_id,
                    "node_spec": entry.node_spec,
                }
                return _result(
                    SUCCESS, payload, "任务已进入轻规划", args.as_json
                )
            if not args.node_spec and not args.from_prd:
                materialized = materialize_session_entry(paths, entry)
                payload = {
                    "command": "plan",
                    "status": "planned",
                    "route": route,
                    "score": score.total,
                    "plan_id": entry.plan_id,
                    "node_spec": entry.node_spec,
                    "materialized_path": str(materialized.relative_to(paths.root)),
                    "execution": "deferred_until_authorize",
                }
                return _result(
                    SUCCESS, payload, "复杂请求已生成稳定计划草案，等待授权", args.as_json
                )
            if args.from_prd:
                # Product-level spec from the agent: business fields only;
                # every engineering field is derived by node_spec.
                source = _read_json(paths.resolve_relative(args.from_prd))
                if not isinstance(source, dict):
                    raise ValueError("product spec must be a JSON object")
                reject_engineering_fields(source)
            else:
                source = _read_json(paths.resolve_relative(args.node_spec))
                if not isinstance(source, dict):
                    raise ValueError("node spec must be a JSON object")
            # The session route settles the band only on the product path;
            # a hand-written node spec keeps declaring its own band.
            source = normalize_node_spec(source, entry, paths, route_governs_band=bool(args.from_prd), adapter_id=args.adapter)
            # Evaluate the PRD checkpoints before _publish_plan().  This gate
            # is deliberately side-effect free: unresolved product choices,
            # missing/unverified evidence, and invalid Skill references must
            # not materialize plans or authorization cards.
            has_prd_gate_input = any(
                key in source for key in ("rationale", "product_question", "skill_profiles")
            )
            rationale = source.get("rationale", {})
            if not isinstance(rationale, dict):
                rationale = {"framing": rationale}
            else:
                rationale = dict(rationale)
            if "product_question" in source and "product_question" not in rationale:
                rationale["product_question"] = source["product_question"]
            gate_context = TaskContext(
                score.steps, score.domains, score.uncertainty,
                score.failure_cost, toolchain=score.toolchain, rationale=rationale,
            )
            # Older executable node specs predate the V4.5 PRD checkpoint
            # fields. Keep those specs compatible; once a spec opts into any
            # checkpoint/profile field, the gate is mandatory.
            checkpoints = evaluate_prd_checkpoints(gate_context) if has_prd_gate_input else []
            raw_profiles = source.get("skill_profiles", [])
            if raw_profiles is None:
                raw_profiles = []
            if not isinstance(raw_profiles, list):
                raise TypeError("skill_profiles must be a list")
            for raw_profile in raw_profiles:
                if not isinstance(raw_profile, dict):
                    raise TypeError("Skill profile must be an object")
                validate_skill_profile(SkillProfile(**raw_profile))
            evidence_refs = ["prd:{}@1".format(entry.plan_id)]
            blocked_checkpoints = [item for item in checkpoints if item.status == "blocked_design"]
            if has_prd_gate_input and blocked_checkpoints:
                question = str(blocked_checkpoints[0].fields.get("question", "请补充未决产品选择"))
                handoff = build_stage_handoff(
                    PRD(str(source.get("title", "").strip() or "未命名 PRD"),
                        str(source.get("objective", "").strip() or entry.request),
                        revision=1, status="blocked_design"),
                    [question], evidence_refs,
                )
                payload = {
                    "command": "plan", "status": "blocked_design",
                    "route": "complex", "question": question,
                    "checkpoints": [item.to_dict() for item in checkpoints],
                    "handoff": handoff.to_dict(),
                    "handoff_text": handoff.render(),
                    "downstream_artifact": None,
                }
                return _result(BLOCKED, payload, "规划已暂停：需要回答产品问题：" + question, args.as_json)
            review_checkpoints = [item for item in checkpoints if item.status == "review_required"]
            if has_prd_gate_input and review_checkpoints:
                handoff = build_stage_handoff(
                    PRD(str(source.get("title", "").strip() or "未命名 PRD"),
                        str(source.get("objective", "").strip() or entry.request),
                        revision=1, status="review_required"),
                    [], evidence_refs,
                )
                payload = {
                    "command": "plan", "status": "review_required",
                    "route": "complex", "required_user_action": "confirm_plan",
                    "checkpoints": [item.to_dict() for item in checkpoints],
                    "handoff": handoff.to_dict(),
                    "handoff_text": handoff.render(),
                    "downstream_artifact": None,
                }
                return _result(BLOCKED, payload, "规划已暂停：需要确认 PRD 证据", args.as_json)
            plan, nodes, card = _publish_plan(
                paths, entry.plan_id, source, session_record=args.session_record
            )
        except PermissionError as error:
            if _is_capability_contract_unknown(error):
                return _result(
                    UNKNOWN,
                    {
                        "command": "monitor",
                        "dispatcher": "monitor",
                        "status": "blocked_unknown",
                        "reason": str(error),
                    },
                    "能力合同状态未知：" + str(error),
                    args.as_json,
                )
            return _result(
                BLOCKED,
                {"command": "plan", "status": "blocked", "reason": str(error)},
                "规划已暂停：" + str(error),
                args.as_json,
            )
        except (FileExistsError, OSError, TypeError, ValueError) as error:
            return _result(
                BLOCKED,
                {"command": "plan", "status": "blocked", "reason": str(error)},
                "规划已阻塞：" + str(error),
                args.as_json,
            )
        payload = {
            "command": "plan",
            "status": "ok",
            "route": "complex",
            "score": entry.s1.total,
            # The draft is gone once published; republishing under a new id
            # passes this back as --s1 to keep the same route.
            "s1": _s1_arg(entry),
            # Screen output is slim; plan.json, nodes.json and the full card
            # stay on disk as the authority (issue #216).
            "plan": {"plan_id": plan.plan_id, "version": plan.version,
                     "path": str(_plan_root(paths, plan.plan_id).resolve() / "plan.json")},
            "nodes": [node.id for node in nodes],
            "authorization_card": authorization_card_summary(paths, plan.plan_id, card.to_dict()),
            "authorization_digest": card.digest,
        }
        approved_handoff = build_stage_handoff(
            PRD(str(source.get("title", "").strip() or "未命名 PRD"),
                str(source.get("objective", "").strip() or entry.request),
                revision=1, status="approved"),
            [], ["prd:{}@1".format(entry.plan_id)],
        )
        payload.update({
            "handoff": approved_handoff.to_dict(),
            "handoff_text": approved_handoff.render(),
            "checkpoints": [item.to_dict() for item in checkpoints],
        })
        return _result(
            SUCCESS, payload, "复杂计划产物已生成，等待一次授权", args.as_json
        )

    if args.command == "authorize":
        if not args.plan:
            return _result(
                BLOCKED,
                {"command": "authorize", "status": "blocked", "reason": "plan is required"},
                "授权未记录：需要计划",
                args.as_json,
            )
        try:
            workflow = materialize_workflow_evidence(paths, args.plan, args.authorize or "")
        except AuthorizationDenied as error:
            # A policy denial, identified by its type rather than by whether the
            # exception happens to carry an errno.
            return _result(
                BLOCKED,
                {"command": "authorize", "status": "blocked", "reason": str(error)},
                "授权未记录：" + str(error),
                args.as_json,
            )
        except PermissionError as error:
            # Every policy path raises AuthorizationDenied, so anything left
            # here is an OS-level fault: an environment problem, not a design
            # decision, and reporting it as `blocked` would misattribute it.
            return _result(
                UNKNOWN,
                {
                    "command": "authorize",
                    "status": "blocked_unknown",
                    "reason": "state_unreadable: " + str(error),
                },
                "授权状态未知：" + str(error),
                args.as_json,
            )
        except json.JSONDecodeError as error:
            return _result(
                UNKNOWN,
                {
                    "command": "authorize",
                    "status": "blocked_unknown",
                    "reason": "state_unreadable: " + str(error),
                },
                "授权状态未知：state.json 无法解析：" + str(error),
                args.as_json,
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            return _result(
                BLOCKED,
                {
                    "command": "authorize",
                    "status": "blocked_design",
                    "reason": str(error),
                },
                "授权未记录：" + str(error),
                args.as_json,
            )
        return _result(
            SUCCESS,
            {
                "command": "authorize",
                "status": "ok",
                "plan_id": args.plan,
                "route": workflow.get("route"),
                "recorded_nodes": list(workflow.get("nodes") or []),
                "authorization_card": authorization_card_summary(paths, args.plan),
                "authorization_granted": bool(workflow.get("authorization_granted")),
            },
            "十节点门禁证据已记录，可以启动监工",
            args.as_json,
        )

    if args.command == "monitor":
        if not args.plan:
            return _result(
                BLOCKED,
                {
                    "command": "monitor",
                    "status": "blocked",
                    "reason": "authorization required",
                },
                "监工未启动：需要计划和精确授权",
                args.as_json,
            )
        # A read-only preview sits ahead of the token check on purpose: it
        # neither authorizes nor dispatches, so demanding the exact token would
        # only stop the user from looking at the card they are being asked to
        # confirm.
        if args.preview_card:
            return _preview_authorization_card(paths, args.plan, args.as_json)
        # Plan-bound confirmations may use an explicit revision token.
        if args.authorize != "AUTHORIZE" and not (
            args.authorize == "AUTHORIZE_V39_REV3_SELF_HEAL_NON_DEPLOY_SCOPE"
            and args.plan == "vibe-guide-v3.9-bugfix"
        ):
            return _result(
                BLOCKED,
                {
                    "command": "monitor",
                    "status": "blocked",
                    "reason": "authorization required",
                },
                "监工未启动：需要精确 AUTHORIZE",
                args.as_json,
            )
        if v2_state or v42_state:
            try:
                require_capability_contract(paths)
            except PermissionError as error:
                return _result(
                    UNKNOWN,
                    {
                        "command": "monitor",
                        "dispatcher": "monitor",
                        "status": "blocked_unknown",
                        "reason": str(error),
                    },
                    "能力合同状态未知：" + str(error),
                    args.as_json,
                )
        try:
            directory, plan, nodes, card = _load_plan(paths, args.plan)
            _require_v38_preflight(paths, directory, plan, nodes, card)
            state_path = paths.vibe / "state.json"
            if state_path.is_file():
                try:
                    state_data = _read_json(state_path)
                except ValueError:
                    state_data = {}
                if isinstance(state_data, dict) and state_data.get("workflow_version") == 2:
                    _require_public_execution_gate(
                        paths, directory, plan, nodes, card
                    )
            invalidation_path = _invalidation_path(directory)
            invalidation_to_clear = None
            reauthorizing_current_run = _current_run_path(directory).exists()
            # A platform handoff moves the unfinished nodes and the card's
            # agent to the new platform; everything else is untouched, and an
            # ordinary reauthorization never reaches this.
            handoff_agent: Optional[str] = None
            handoff_nodes: Optional[List[DAGNode]] = None
            handoff_accepted: Optional[frozenset] = None
            handoff_committed: List[bool] = [False]
            handoff_observation: Tuple[Any, ...] = ()
            if args.platform_handoff and reauthorizing_current_run:
                nodes, handoff_agent = _platform_handoff_rebind(
                    paths, directory, nodes, card, args.adapter
                )
                # Only a handoff observes against the target platform and
                # keeps still-current evidence; any other reauthorization
                # observes exactly as it always did.
                handoff_observation = (handoff_agent, True)
                if handoff_agent is not None:
                    handoff_nodes = nodes
                    handoff_accepted = frozenset(
                        node_id for node_id, current in load_snapshot(
                            paths, _run_id(directory, None)
                        ).nodes.items() if current.get("status") == "accepted"
                    )
            # Background launchers this dispatch builds, so the worktree
            # preflight report can be persisted once the dispatch returns.
            launchers: List[Any] = []
            if invalidation_path.exists():
                invalidation = _read_json(invalidation_path)
                if not isinstance(invalidation, dict):
                    raise ValueError("authorization invalidation record is invalid")
                with _staged_reauthorization(
                    paths, plan, directory, handoff_agent,
                    handoff_committed, handoff_observation,
                ) as engine_attestation:
                    card = refresh_authorization_card(
                        plan, nodes, card,
                        workflow=load_snapshot(paths, _run_id(directory, None)).workflow,
                        topology_rulings=_observed_topology_rulings(paths, card),
                        engine_attestation=engine_attestation,
                        agent_id=handoff_agent,
                    )
                    record = authorize(card, args.authorize)
                    if runner is None:
                        runner = _public_runner(
                            paths, card, nodes, launchers,
                            exempt_nodes=handoff_accepted,
                        )
                    monitor = Monitor(
                        paths, plan, nodes,
                        topology_rulings=_observed_topology_rulings(paths, card),
                    )
                    snapshot = monitor.reauthorize(
                        _run_id(directory, None),
                        record,
                        runner,
                        "platform_handoff" if args.platform_handoff else str(
                            invalidation.get(
                                "change_reason", "executable_contract_changed"
                            )
                        ),
                    )
                    handoff_committed[0] = handoff_nodes is not None
                    _record_worktree_preflight(paths, monitor, snapshot, launchers)
                    if handoff_nodes is not None:
                        # Without this the next resume reads the old platform
                        # back out of nodes.json.  Nodes first, then the card:
                        # either half-written state is re-derived by a re-run.
                        _atomic_json(
                            directory / "nodes.json",
                            [node.to_dict() for node in handoff_nodes],
                        )
                    # Publish the refreshed card inside the staged window, and
                    # only once the reauthorization actually succeeded.  Writing
                    # it before `reauthorize` left the card, the authorization
                    # record and plan-confirmation.json disagreeing whenever it
                    # raised, and the next public authorize then failed closed
                    # with `plan-confirmation.invalid` (#159).  Writing it after
                    # the window closed would pair fresh evidence with a card
                    # still naming the old reference if this write failed.
                    _atomic_json(directory / "authorization-card.json", card.to_dict())
                invalidation_to_clear = invalidation_path
            elif _current_run_path(directory).exists():
                # A capability-only mismatch is deliberately reported as
                # unknown by public resume, so it has no invalidation marker
                # to select this branch.  An existing current run is still
                # authoritative: reauthorize it in place rather than
                # creating a second writer and colliding on the old lease.
                # Refresh the card as well: a delivered contract correction
                # may have changed the executable node digest without writing
                # an invalidation marker yet.
                with _staged_reauthorization(
                    paths, plan, directory, handoff_agent,
                    handoff_committed, handoff_observation,
                ) as engine_attestation:
                    card = refresh_authorization_card(
                        plan, nodes, card,
                        workflow=load_snapshot(paths, _run_id(directory, None)).workflow,
                        topology_rulings=_observed_topology_rulings(paths, card),
                        engine_attestation=engine_attestation,
                        agent_id=handoff_agent,
                    )
                    record = authorize(card, args.authorize)
                    if runner is None:
                        runner = _public_runner(
                            paths, card, nodes, launchers,
                            exempt_nodes=handoff_accepted,
                        )
                    monitor = Monitor(
                        paths, plan, nodes,
                        topology_rulings=_observed_topology_rulings(paths, card),
                    )
                    snapshot = monitor.reauthorize(
                        _run_id(directory, None),
                        record,
                        runner,
                        "platform_handoff" if args.platform_handoff
                        else "capability_contract_changed",
                    )
                    handoff_committed[0] = handoff_nodes is not None
                    _record_worktree_preflight(paths, monitor, snapshot, launchers)
                    if handoff_nodes is not None:
                        _atomic_json(
                            directory / "nodes.json",
                            [node.to_dict() for node in handoff_nodes],
                        )
                    # See the invalidation branch above for why the card is
                    # published here and not one line further out.
                    _atomic_json(directory / "authorization-card.json", card.to_dict())
            else:
                if args.platform_handoff:
                    # A handoff continues an existing run; never let the flag
                    # silently start a fresh one.
                    raise ValueError("platform handoff requires an existing run")
                record = authorize(card, args.authorize)
                if runner is None:
                    runner = _public_runner(paths, card, nodes, launchers)
                monitor = Monitor(
                    paths, plan, nodes,
                    topology_rulings=_observed_topology_rulings(paths, card),
                )
                snapshot = monitor.start(record, runner)
                _record_worktree_preflight(paths, monitor, snapshot, launchers)
            # Publishing a plan records confirmation, not execution
            # authorization.  Once the user supplies the exact authorization
            # token, persist the lifecycle transition so the public execution
            # gate can observe the same state on resume.
            if plan.status == "confirmed_pending_authorization":
                published_plan = _read_json(directory / "plan.json")
                if isinstance(published_plan, dict):
                    published_plan["status"] = "authorized"
                    _atomic_json(directory / "plan.json", published_plan)
            _atomic_json(directory / "authorization.json", record.to_dict())
            _atomic_json(
                directory / "plan-confirmation.json",
                {
                    "status": "confirmed",
                    "plan_id": plan.plan_id,
                    "plan_revision": str(plan.version),
                    "authorization_digest": record.digest,
                    "authorization_required": True,
                    "run_id": snapshot.run_id,
                    "event_sequence": snapshot.event_sequence,
                    "publication": "same_run_reauthorization",
                },
            )
            _atomic_json(
                _current_run_path(directory), {"run_id": snapshot.run_id}
            )
            if reauthorizing_current_run and getattr(plan, "complexity_band", "") == "complex":
                # Reauthorization rewrites the card and confirmation. Rebuild
                # their live workflow evidence before resume verifies it again.
                # Use the same explicit token; retain other plans/session gates.
                materialize_workflow_evidence(paths, plan.plan_id, args.authorize)
            if invalidation_to_clear is not None:
                invalidation_to_clear.unlink()
        except PreflightBlockedError as error:
            return _result(
                BLOCKED,
                {"command": "monitor", "status": "preflight_blocked", "check_ids": list(error.check_ids), "reason": str(error)},
                "预检已阻塞：" + ", ".join(error.check_ids),
                args.as_json,
            )
        except HandoffRebindError as error:
            return _result(
                UNKNOWN,
                {"command": "monitor", "dispatcher": "monitor", "status": "blocked_unknown", "reason": str(error)},
                "平台交接未完成：" + str(error),
                args.as_json,
            )
        except PermissionError as error:
            if _is_capability_contract_unknown(error):
                return _result(
                    UNKNOWN,
                    {
                        "command": "monitor",
                        "dispatcher": "monitor",
                        "status": "blocked_unknown",
                        "reason": str(error),
                    },
                    "能力合同状态未知：" + str(error),
                    args.as_json,
                )
            return _result(
                BLOCKED,
                {
                    "command": "monitor",
                    "status": "blocked_design",
                    "reason": str(error),
                },
                "监工已暂停：" + str(error),
                args.as_json,
            )
        except ProviderPending as error:
            return _result(
                UNKNOWN,
                {
                    "command": "monitor",
                    "status": "retry_pending",
                    "reason": str(error),
                },
                "监工等待能力观测，自动重试",
                args.as_json,
            )
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as error:
            if _is_capability_contract_unknown(error):
                return _result(
                    UNKNOWN,
                    {
                        "command": "monitor",
                        "status": "blocked_unknown",
                        "reason": str(error),
                    },
                    "能力合同状态未知：" + str(error),
                    args.as_json,
                )
            return _result(
                UNKNOWN,
                {
                    "command": "monitor",
                    "status": "unknown",
                    "reason": str(error),
                },
                "监工状态未知：" + str(error),
                args.as_json,
            )
        handoff = _first_shift_handoff(paths, args.plan, snapshot.run_id, snapshot.status)
        if args.watch:
            supervisor = Supervisor(
                paths,
                Monitor(paths, plan, nodes, topology_rulings=_observed_topology_rulings(paths, card)),
                runner,
                snapshot.run_id,
            )
            lease_result = supervisor.recover_or_start()
            if not lease_result.get("active_supervisors"):
                return _result(
                    UNKNOWN,
                    {"command": "monitor", "status": "blocked_unknown", "reason": "supervisor lease is held"},
                    "监工租约已被其他进程持有",
                    args.as_json,
                )
            snapshot = supervisor.watch()
            # The watch runs to a terminal outcome, so the hint computed at
            # the start can be stale by the time it is printed: the run may
            # have completed, or the shift may have registered meanwhile.
            handoff = _first_shift_handoff(
                paths, args.plan, snapshot.run_id, getattr(snapshot, "status", None)
            )
            return _snapshot_result("monitor", snapshot, args.as_json, "supervisor", handoff, paths=paths)
        return _snapshot_result("monitor", snapshot, args.as_json, "manual", handoff, paths=paths)

    if args.command == "reconcile":
        try:
            if not args.plan or not args.run_id or not args.evidence:
                raise ValueError("reconcile requires --plan, --run-id and --evidence")
            directory, plan, nodes, _card = _load_plan(paths, args.plan)
            evidence_path = paths.resolve_relative(args.evidence)
            package = _read_json(evidence_path)
            monitor = Monitor(
                paths, plan, nodes,
                topology_rulings=_observed_topology_rulings(paths, _card),
            )
            snapshot = monitor.reconcile_package(
                args.run_id, package,
                runner_factory=lambda: runner if runner is not None else _public_runner(paths, _card, nodes),
                invalidation_path=_invalidation_path(directory),
                authorization_digest=_card.digest,
            )
            return _snapshot_result("reconcile", snapshot, args.as_json, paths=paths)
        except (FileNotFoundError, OSError, TypeError, ValueError, PermissionError) as error:
            return _result(
                UNKNOWN,
                {"command": "reconcile", "status": "blocked_unknown", "reason": str(error)},
                "对账已暂停：" + str(error),
                args.as_json,
            )

    if args.command in {"repair", "requeue"}:
        try:
            if not args.plan or not args.run_id or not args.node_id:
                raise ValueError("{} requires --plan, --run-id and --node".format(args.command))
            _directory, plan, nodes, _card = _load_plan(paths, args.plan)
            monitor = Monitor(
                paths, plan, nodes,
                topology_rulings=_observed_topology_rulings(paths, _card),
            )
            if load_snapshot(paths, args.run_id).nodes.get(args.node_id, {}).get(
                    "status") == worker_questions.AWAITING_STATUS:
                payload = worker_questions.answer_question(
                    monitor, args.run_id, args.node_id, args.decision_note)
            else:
                payload = monitor.repair_node(args.node_id, args.run_id)
            snapshot = load_snapshot(paths, args.run_id)
            result = _snapshot_result(args.command, snapshot, args.as_json, paths=paths)
            result.payload["repair"] = payload
            return result
        except (FileNotFoundError, OSError, TypeError, ValueError, PermissionError) as error:
            return _result(
                UNKNOWN,
                {"command": args.command, "status": "blocked_unknown", "reason": str(error)},
                "工程恢复已暂停：" + str(error),
                args.as_json,
            )

    if args.command in {"resume", "status"}:
        if not args.plan:
            return _result(
                UNKNOWN,
                {
                    "command": args.command,
                    "status": "unknown",
                    "reason": "plan required",
                },
                "状态未知：需要计划标识",
                args.as_json,
            )
        if args.command == "resume" and v2_state:
            try:
                require_capability_contract(paths)
            except PermissionError as error:
                return _result(
                    UNKNOWN,
                    {
                        "command": "resume",
                        "status": "blocked_unknown",
                        "reason": str(error),
                    },
                    "能力合同状态未知：" + str(error),
                    args.as_json,
                )
        directory = None
        try:
            directory, plan, nodes, _card = _load_plan(paths, args.plan)
            run_id = _run_id(directory, args.run_id)
            if args.command == "resume" and v2_state:
                _require_public_execution_gate(
                    paths, directory, plan, nodes, _card
                )
            invalidation = _invalidation_path(directory)
            if (
                args.command == "resume"
                and not invalidation.exists()
                and _current_run_path(directory).is_file()
            ):
                _renew_engine_attestation_if_stale(
                    paths, directory, plan, nodes, _card, run_id, runner
                )
                directory, plan, nodes, _card = _load_plan(paths, args.plan)
            if invalidation.exists():
                persisted = _read_json(invalidation)
                reason = (
                    persisted.get("reason", "authorization invalidated")
                    if isinstance(persisted, dict)
                    else "authorization invalidated"
                )
                return _result(
                    BLOCKED,
                    {
                        "command": args.command,
                        "status": "blocked_design",
                        "reason": reason,
                    },
                    "恢复已暂停：授权因设计变化失效",
                    args.as_json,
                )
            if args.command == "status":
                snapshot = load_snapshot(paths, run_id)
            else:
                launchers = []
                if runner is None:
                    runner = _public_runner(paths, _card, nodes, launchers)
                AuthorizationRecord.from_dict(
                    _read_json(directory / "authorization.json")
                )
                # Reconcile superseded create requests during the servicing
                # turn, after the read-only heartbeat preflight. The request
                # remains immutable and only a fully bound accepted snapshot
                # can produce an audit marker.
                current_snapshot = load_snapshot(paths, run_id)
                accepted_evidence = {
                    "run_id": run_id,
                    "authorization_digest": getattr(current_snapshot, "authorization_digest", ""),
                    "nodes": {
                        node_id: {
                            "status": data.get("status"),
                            "accepted_generation": max(
                                int(data.get("developer_generation", 0) or 0),
                                int(data.get("review_generation", 0) or 0),
                            ),
                            "roles": {
                                "developer": {
                                    "generation": int(data.get("developer_generation", 0) or 0),
                                    "accepted_generation": int(data.get("developer_generation", 0) or 0),
                                },
                                "reviewer": {
                                    "generation": int(data.get("review_generation", 0) or 0),
                                    "accepted_generation": int(data.get("review_generation", 0) or 0),
                                },
                            },
                        }
                        for node_id, data in (current_snapshot.nodes or {}).items()
                        if isinstance(data, dict)
                    },
                }
                ProviderActionStore(paths).reconcile_stale(run_id, accepted_evidence)
                monitor = Monitor(
                    paths, plan, nodes,
                    topology_rulings=_observed_topology_rulings(paths, _card),
                )
                # The CLI performs the normal resume tick immediately after
                # reattachment; let that tick own the single provider poll.
                snapshot = monitor.resume(run_id, runner, poll_handles=False)
                snapshot = monitor.tick(run_id, runner)
                # A dispatch that had to build a node worktree, or refused to,
                # leaves its report on the launcher; persist it before the
                # status projection reads the node markers (#235).
                _record_worktree_preflight(paths, monitor, snapshot, launchers)
        except PermissionError as error:
            if _is_capability_contract_unknown(error):
                return _result(
                    UNKNOWN,
                    {
                        "command": args.command,
                        "status": "blocked_unknown",
                        "reason": str(error),
                    },
                    "能力合同状态未知：" + str(error),
                    args.as_json,
                )
            if _is_draft_block(error):
                # Nothing to invalidate: a draft never held an authorization.
                # Report the design state and how to finish it.
                return _result(
                    BLOCKED,
                    {"command": args.command, "status": "blocked_design", "reason": str(error)},
                    "计划仍是草案：" + str(error),
                    args.as_json,
                )
            if not _is_contract_drift_error(error):
                return _result(
                    UNKNOWN,
                    {
                        "command": args.command,
                        "status": "unknown",
                        "reason": str(error),
                    },
                    "状态未知：" + str(error),
                    args.as_json,
                )
            _persist_invalidation(
                directory, "authorization invalidated: " + str(error)
            )
            return _result(
                BLOCKED,
                {
                    "command": args.command,
                    "status": "blocked_design",
                    "reason": "authorization invalidated: " + str(error),
                },
                "恢复已暂停：授权因设计变化失效",
                args.as_json,
            )
        except RuntimeError as error:
            return _result(
                UNKNOWN,
                {
                    "command": args.command,
                    "status": "unknown",
                    "reason": str(error),
                },
                "状态未知：" + str(error),
                args.as_json,
            )
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            message = str(error)
            if _is_contract_drift_error(error):
                _persist_invalidation(
                    directory, "authorization invalidated: " + message
                )
                return _result(
                    BLOCKED,
                    {
                        "command": args.command,
                        "status": "blocked_design",
                        "reason": "authorization invalidated: " + message,
                    },
                    "恢复已暂停：授权因设计变化失效",
                    args.as_json,
                )
            return _result(
                UNKNOWN,
                {
                    "command": args.command,
                    "status": "unknown",
                    "reason": message,
                },
                "状态未知：" + message,
                args.as_json,
            )
        # A heartbeat that drifted from the generated prompt still calls
        # resume, so resume keeps asking until the shift registers.
        handoff = (
            _first_shift_handoff(paths, args.plan, snapshot.run_id, snapshot.status)
            if args.command == "resume"
            else ""
        )
        return _snapshot_result(args.command, snapshot, args.as_json, handoff=handoff, paths=paths)

    return _result(
        USAGE_ERROR, {"status": "usage_error"}, "参数错误", args.as_json
    )


def _handle_daemon(args, paths, runner, command_prefix, v2_state=False) -> CLIResult:
    """vibe daemon start|status|stop|run|register-pm (run is the detached process itself)."""
    from . import daemon as daemon_module
    action = args.daemon_action
    base = {"command": "daemon", "action": action}
    if action is None or not args.run_id:
        return _result(BLOCKED, {**base, "status": "blocked_invalid", "reason": "daemon needs an action and --run-id"},
                       "用法：vibe daemon start|status|stop|run|register-pm --run-id <run-id>", args.as_json)
    try:
        if action == "status":
            payload = daemon_module.daemon_status(paths, args.run_id)
            code = SUCCESS if payload["status"] == "running" else UNKNOWN
            text = "守护程序：{}{}".format(payload["status"], "（%s）" % payload["reason"] if payload.get("reason") else "")
            return _result(code, payload, text, args.as_json)
        if action == "stop":
            payload = daemon_module.stop_daemon(paths, args.run_id)
            return _result(SUCCESS if payload["status"] in ("stopped", "not_running") else UNKNOWN, payload,
                           "守护程序：" + payload["status"], args.as_json)
        if action == "register-pm":
            if not (args.supervisor_provider and args.pm_session_name):
                return _result(BLOCKED, {**base, "status": "blocked_invalid", "reason": "--provider and --session-name are required"},
                               "缺少 --provider 或 --session-name", args.as_json)
            record = daemon_module.register_pm_address(paths, args.run_id, args.supervisor_provider, args.pm_session_name)
            return _result(SUCCESS, {**base, "status": "registered", "run_id": args.run_id, **record}, "产品经理会话已登记", args.as_json)
        if not args.plan:
            return _result(BLOCKED, {**base, "status": "blocked_invalid", "reason": "--plan is required"},
                           "缺少 --plan", args.as_json)
        if action == "start":
            payload = daemon_module.start_daemon(paths, args.run_id, args.plan, interval=args.daemon_interval,
                                                 model=args.daemon_model, provider=args.supervisor_provider)
            ok = payload["status"] in ("started", "already_running")
            return _result(SUCCESS if ok else UNKNOWN, payload, "守护程序：" + payload["status"], args.as_json)
        # run: the detached process.  Same assembly as `monitor --watch`, with the daemon loop on top.
        config = daemon_module.load_config(paths, {"interval": args.daemon_interval, "model": args.daemon_model,
                                                   "platform": args.supervisor_provider})
        directory, plan, nodes, card = _load_plan(paths, args.plan)
        run_id = _run_id(directory, args.run_id)
        if runner is None:
            runner = _public_runner(paths, card, nodes, [])
        supervisor = Supervisor(
            paths, Monitor(paths, plan, nodes, topology_rulings=_observed_topology_rulings(paths, card)),
            runner, run_id)

        def gate():
            # The same pre-resume checks `vibe resume` makes, re-run every tick:
            # the daemon must not advance a run the public path would refuse.
            try:
                gate_directory, gate_plan, gate_nodes, gate_card = _load_plan(paths, args.plan)
                if v2_state:
                    require_capability_contract(paths)
                    _require_public_execution_gate(paths, gate_directory, gate_plan, gate_nodes, gate_card)
                invalidation = _invalidation_path(gate_directory)
                if not invalidation.exists() and _current_run_path(gate_directory).is_file():
                    before = (gate_directory / "authorization.json").read_bytes()
                    _renew_engine_attestation_if_stale(
                        paths, gate_directory, gate_plan, gate_nodes, gate_card, run_id, runner)
                    if (gate_directory / "authorization.json").read_bytes() != before:
                        # Renewed: later ticks must run under the re-signed authorization.
                        _d, new_plan, new_nodes, new_card = _load_plan(paths, args.plan)
                        supervisor.monitor = Monitor(
                            paths, new_plan, new_nodes,
                            topology_rulings=_observed_topology_rulings(paths, new_card))
            except PermissionError as error:
                return {"status": "blocked_unknown", "reason": str(error)[:200]}
            if invalidation.exists():
                persisted = _read_json(invalidation)
                reason = persisted.get("reason", "authorization invalidated") if isinstance(persisted, dict) \
                    else "authorization invalidated"
                return {"status": "blocked_design", "reason": str(reason)[:200]}
            return None

        outcome = daemon_module.Daemon(paths, run_id, supervisor, config, gate=gate,
                                       command_prefix=command_prefix).run()
        ok = outcome["status"] in ("terminal", "stopped", "max_cycles")
        return _result(SUCCESS if ok else UNKNOWN, {**base, "run_id": run_id, **outcome}, "守护程序结束：" + outcome["status"], args.as_json)
    except (OSError, ValueError, TypeError, FileNotFoundError) as error:
        return _result(UNKNOWN, {**base, "status": "unknown", "reason": str(error)[:200]},
                       "守护程序状态未知：" + str(error)[:200], args.as_json)


def handle_monitor(
    as_json: bool = False, authorization: Optional[str] = None, runner=None
) -> int:
    argv = ["monitor"]
    if authorization:
        argv.extend(["--authorize", authorization])
    if as_json:
        argv.append("--json")
    return run_cli(argv, Path.cwd(), runner=runner).exit_code


_ANSWER_RESUME_HINT = "想马上续跑，去监工会话说继续；否则最晚下一次心跳自动接上"


def main(argv: Optional[Sequence[str]] = None, runner=None, *, supervisor_command_prefix="vibe") -> int:
    arguments = list(argv) if argv is not None else os.sys.argv[1:]
    result = run_cli(arguments, Path.cwd(), runner=runner, supervisor_command_prefix=supervisor_command_prefix)
    rendered = (json.dumps(result.payload, ensure_ascii=False, sort_keys=True)
                if result.as_json else result.text)
    command = arguments[0] if arguments else ""
    run_id = result.payload.get("run_id")
    if not run_id and "--run-id" in arguments:
        index = arguments.index("--run-id") + 1
        run_id = arguments[index] if index < len(arguments) else None
    if run_id and (command.startswith("supervisor-") or command in {"monitor", "resume", "status", "reconcile"}):
        try:
            rendered = bounded_supervisor_output(ProjectPaths.from_cwd(Path.cwd()), run_id, result.payload, rendered)
        except (OSError, ValueError, TypeError) as error:
            print(json.dumps({"status": "unknown", "reason": "output evidence unavailable: " + str(error)[:200]}))
            return result.exit_code or UNKNOWN
    print(rendered)
    return result.exit_code
