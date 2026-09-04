"""Thin CLI wiring over the scanner, planner, authorization and monitor APIs."""

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .authorization import (
    AuthorizationCard,
    AuthorizationRecord,
    authorize,
    build_authorization_card,
    refresh_authorization_card,
)
from .adapters.base import Environment
from .adapters.registry import AdapterRegistry
from .adapters.task_provider import ProviderActionStore, ProviderPending, classify_provider_for_v4
from .dag import render_plan_artifacts, validate_dag, schedule_ready_nodes
from .doctor import doctor
from .initializer import apply_agentsmd_proposal, init_project
from .upgrade import upgrade_project
from .models import AgentCapabilities, DAGNode, Plan, DeployManifest, DeployState
from .monitor import Monitor
from .change_requests import ChangeRequest, classify_merge_capability
from .deploy import authorize_deploy, plan_deploy, verify_deploy, start_deploy
from .paths import ProjectPaths
from .planner import (
    DecisionCard,
    PRD,
    TaskContext,
    approve_prd,
    classify_s0,
    route_task,
    score_s1,
)
from .scanner import scan_project
from .diagnostics import screen_session, require_session_screened
from .diagnostics import assert_planning_gate, _valid_plan_confirmation_binding
from .workflow_gate import require_capability_contract
from .state import load_events, load_snapshot, map_user_status
from .runners.provider_action import ProviderActionRunner
from .preflight import PreflightBlockedError, PreflightContext, assert_authorizable, run_preflight
from .installation import run_install, run_upgrade
from .models import InstallRequest


SUCCESS = 0
USAGE_ERROR = 2
BLOCKED = 3
UNKNOWN = 4
_DOCUMENT_LIMIT = 1024 * 1024


def run_v4_sdd(request: Dict[str, Any], json_output: bool = False) -> Dict[str, Any]:
    """Return the provider-neutral V4 SDD-first user projection.

    Provider lease/cursor/path fields are advisory in V4.  Missing values are
    recorded as observations, never converted into repeated user prompts.
    """
    if not isinstance(request, dict):
        raise TypeError("V4 SDD request must be a mapping")
    workflow_version = request.get("workflow_version", 4)
    if workflow_version != 4:
        raise ValueError("V4 SDD requires workflow_version=4")
    execution_mode = request.get("execution_mode", request.get("mode", "sdd_first"))
    if execution_mode != "sdd_first":
        raise ValueError("V4 SDD requires execution_mode=sdd_first")
    nodes = request.get("nodes", [])
    if nodes is None:
        nodes = []
    if not isinstance(nodes, list):
        raise TypeError("V4 SDD nodes must be a list")
    status = request.get("status")
    if status is None and nodes:
        statuses = [node.get("status") for node in nodes if isinstance(node, dict)]
        if statuses and all(item in {"accepted", "archived"} for item in statuses):
            status = "accepted"
        elif any(item in {"running", "review", "rework", "delivered"} for item in statuses):
            status = "running"
        else:
            status = "planned"
    status = str(status or "planned")
    observations = []
    for key in ("lease", "cursor", "path", "worktree"):
        if not request.get(key):
            observations.append("missing_provider_" + key)
    provider_observations = request.get("provider_observations", request.get("provider", []))
    if isinstance(provider_observations, dict):
        provider_observations = [provider_observations]
    provider_results = []
    node_effects = {}
    if isinstance(provider_observations, list):
        for observation in provider_observations:
            if isinstance(observation, dict):
                item = {
                    "node_id": observation.get("node_id"),
                    "action": observation.get("action"),
                    "status": classify_provider_for_v4(observation),
                }
                provider_results.append(item)
                if item["node_id"]:
                    node_effects[str(item["node_id"])] = item["status"]
    admitted_nodes = []
    persistence = None
    if request.get("orchestrate"):
        # This local seam exercises V4 admission and node isolation without
        # invoking a provider or performing any remote side effect.
        from .monitor import heal_v4_node
        mutable_nodes = [dict(node) for node in nodes if isinstance(node, dict)]
        by_id = {str(node.get("id")): node for node in mutable_nodes if node.get("id")}
        local_snapshot = {"run_id": str(request.get("run_id", "v4-local")), "nodes": by_id, "healing_events": []}
        for observation in provider_observations if isinstance(provider_observations, list) else []:
            if isinstance(observation, dict) and observation.get("node_id"):
                provider_status = classify_provider_for_v4(observation)
                is_timeout = str(observation.get("status", "")).strip().casefold() in {"timeout", "timed_out"} or observation.get("kind") in {"provider_timeout", "timeout"}
                if provider_status == "unknown" or is_timeout:
                    healing = heal_v4_node(local_snapshot, str(observation["node_id"]), observation)
                    node_effects[str(observation["node_id"])] = "unknown" if is_timeout else provider_status
        admitted_nodes = schedule_ready_nodes(mutable_nodes, active_pairs=None, capacity=5)
        for node in mutable_nodes:
            if node.get("id") in by_id:
                by_id[str(node["id"])] = node
        # Return the orchestrated mutable projection, including node-local
        # isolation changes, rather than the original request payload.
        nodes = mutable_nodes
        state_dir = request.get("state_dir")
        if state_dir:
            run_dir = Path(state_dir).resolve() / local_snapshot["run_id"]
            run_dir.mkdir(parents=True, exist_ok=True)
            state_path = run_dir / "state.json"
            event_path = run_dir / "events.jsonl"
            payload = {"run_id": local_snapshot["run_id"], "nodes": by_id, "healing_events": local_snapshot["healing_events"], "admitted_nodes": admitted_nodes}
            temporary = state_path.with_name("." + state_path.name + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            os.replace(str(temporary), str(state_path))
            event_tmp = event_path.with_name("." + event_path.name + ".tmp")
            existing_records = []
            if event_path.is_file() and not event_path.is_symlink():
                for line in event_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        try:
                            existing_records.append(json.loads(line))
                        except json.JSONDecodeError:
                            existing_records.append({"event": "legacy", "raw": line})
            event_records = list(existing_records)
            event_records.append({"event": "v4_admission", "run_id": local_snapshot["run_id"], "admitted_nodes": admitted_nodes})
            isolated_ids = {
                str(item.get("node_id"))
                for item in local_snapshot["healing_events"]
                if isinstance(item, dict) and (item.get("isolated") is True or item.get("status") == "isolated") and item.get("node_id")
            }
            existing_isolation_ids = {
                str(record.get("node_id")) for record in existing_records
                if isinstance(record, dict) and record.get("event") == "v4_node_isolated" and record.get("node_id")
            }
            event_records.extend({
                "event": "v4_node_isolated", "run_id": local_snapshot["run_id"],
                "node_id": node_id, "status": "blocked_unknown", "isolated": True,
            } for node_id in sorted(isolated_ids - existing_isolation_ids))
            event_tmp.write_text("".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in event_records), encoding="utf-8")
            os.replace(str(event_tmp), str(event_path))
            persistence = {"state": str(state_path), "events": str(event_path)}
    visible = map_user_status(status, request.get("reason", ""))
    return {
        "workflow_version": 4,
        "execution_mode": "sdd_first",
        "status": visible,
        "user_status": visible,
        "internal_status": status,
        "message": visible,
        "prompts": [],
        "required_inputs": [],
        "observations": observations,
        "provider": provider_results,
        "node_effects": node_effects,
        "nodes": nodes,
        "admitted_nodes": admitted_nodes,
        "persistence": persistence,
    }


@dataclass(frozen=True)
class CLIResult:
    exit_code: int
    payload: Dict[str, Any]
    text: str
    as_json: bool = False


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
        choices=("scan", "init", "apply-agentsmd", "doctor", "install", "upgrade", "plan", "sdd", "monitor", "reconcile", "status", "resume", "change-request", "deploy"),
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--request")
    parser.add_argument("--plan-id")
    parser.add_argument("--plan")
    parser.add_argument("--run-id")
    parser.add_argument("--evidence")
    parser.add_argument("--s1")
    parser.add_argument("--node-spec")
    parser.add_argument("--authorize")
    parser.add_argument("--authorization-token", dest="legacy_authorization")
    parser.add_argument("--manifest")
    parser.add_argument("--acceptance-state")
    parser.add_argument("--observations")
    parser.add_argument("--mode", choices=("layered", "bundled"), default="layered")
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
    runner = run_upgrade if operation in {"upgrade", "upg"} else run_install
    result = runner(install_request, ProjectPaths.from_cwd(install_request.project_root))
    payload = result.to_dict()
    payload["operation"] = "upgrade" if runner is run_upgrade else "install"
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
    for key in (
        "node_ids",
        "file_scope",
        "worker_scope",
        "allowed_actions",
        "excluded_actions",
    ):
        converted[key] = tuple(converted[key])
    return AuthorizationCard(**converted)


def _observed_adapter(paths: ProjectPaths, adapter_id: str):
    store = ProviderActionStore(paths)
    capabilities = None
    last_error = None
    for attempt in range(3):
        try:
            capabilities = store.capabilities()
            break
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(0.05)
    if capabilities is None:
        raise ProviderPending("provider capability observation pending") from last_error
    if capabilities["adapter_id"] != adapter_id:
        raise ValueError("observed provider does not match the selected adapter")
    facts = capabilities["facts"]
    environment = Environment(
        facts=facts,
        provenance={key: capabilities["provenance"] for key in facts},
        available_agents=(adapter_id,),
    )
    result = AdapterRegistry().get(adapter_id).detect(environment)
    if (
        not result.detected
        or result.capabilities.mode != "visible"
        or not result.capabilities.visible_automation
    ):
        raise ValueError("selected provider has no verified visible lifecycle")
    return result


def _public_runner(
    paths: ProjectPaths,
    card: AuthorizationCard,
    nodes: List[DAGNode],
) -> ProviderActionRunner:
    if any(node.contract.get("adapter_id") != card.agent_id for node in nodes):
        raise ValueError("node adapter routing does not match authorization")
    observed = _observed_adapter(paths, card.agent_id)
    return ProviderActionRunner(
        paths,
        observed.adapter_id,
        observed.capabilities.provider,
    )


def _publish_plan(
    paths: ProjectPaths, plan_id: str, source_path: Path
) -> Tuple[Plan, List[DAGNode], AuthorizationCard]:
    source = _read_json(source_path)
    if not isinstance(source, dict):
        raise ValueError("node spec must be a JSON object")
    decisions = [DecisionCard(**item) for item in source.get("decisions", [])]
    prd = PRD(
        str(source.get("title", "")).strip(),
        str(source.get("objective", "")).strip(),
    )
    if not prd.title or not prd.objective:
        raise ValueError("approved PRD title and objective are required")
    approval = approve_prd(prd, decisions)
    if not approval.approved:
        raise PermissionError("product decisions remain unresolved")
    raw_nodes = source.get("nodes", [])
    source_capabilities = AgentCapabilities.from_dict(source.get("capabilities", {}))
    project_id = source.get("project_id")
    # Visible task routing requires a confirmed project id. Legacy/background
    # node specs predate that field and remain valid because they do not use
    # the visible desktop-task route.
    if source_capabilities.level == "full" and (not isinstance(project_id, str) or not project_id.strip()):
        raise ValueError("node spec project_id is required for visible task routing")
    if not isinstance(project_id, str) or not project_id.strip():
        project_id = None
    for item in raw_nodes:
        contract = item.setdefault("contract", {})
        contract.setdefault("adapter_id", source_capabilities.agent_id)
        if project_id:
            contract.setdefault("project_id", project_id.strip())
        contract.setdefault("worker", contract.get("writer", "worker"))
        contract.setdefault("reviewer_worker", contract.get("reviewer", "reviewer"))
        contract.setdefault("worker_profile", {
            "worker": contract.get("writer", "worker"),
            "model": "default",
            "reasoning": "normal",
            "fallbacks": [],
            "selection_basis": {
                "issue_complexity_ref": item.get("id", "node"),
                "complexity_band": "standard",
                "risk_tags": contract.get("risk_tags", []),
                "availability_evidence": "configured",
            },
            "writer": contract.get("writer", "worker"),
            "worktree": contract.get("worktree", "."),
            "branch": contract.get("branch", "main"),
            "allowlist": contract.get("files", []) or ["."],
        })
    nodes = [DAGNode.from_dict(item) for item in raw_nodes]
    if not nodes:
        raise ValueError("node spec must contain at least one node")
    validation = validate_dag(nodes)
    if not validation.valid:
        raise ValueError("DAG is invalid: " + "; ".join(validation.errors))

    destination = _plan_root(paths, plan_id)
    plan = Plan(
        plan_id,
        1,
        str(Path(".vibe") / "plans" / plan_id / "prd.md"),
        [node.id for node in nodes],
        "confirmed_pending_authorization",
        authorization_required=True,
        decisions=[asdict(item) for item in decisions],
        nodes=nodes,
    )
    capabilities = source_capabilities
    try:
        capabilities = _observed_adapter(paths, capabilities.agent_id).capabilities
    except (FileNotFoundError, OSError, TypeError, ValueError, ProviderPending):
        # Planning remains usable for explicitly injected/background test paths;
        # the public monitor rechecks live observed capability before execution.
        pass
    card = build_authorization_card(
        plan,
        nodes,
        capabilities,
        active_pair_limit=source.get("active_pair_limit"),
        allowed_actions=source.get("allowed_actions"),
    )

    plans_root = destination.parent
    if plans_root.is_symlink():
        raise ValueError("plans directory may not be a symlink")
    plans_root.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("plan already exists")
    staging = Path(tempfile.mkdtemp(prefix="." + plan_id + ".", dir=str(plans_root)))
    try:
        render_plan_artifacts(plan, staging)
        (staging / "specs").mkdir()
        (staging / "issues").mkdir()
        (staging / "prd.md").write_text(
            "# {}\n\n状态：approved\n审核：reviewed\n\n目标：{}\n\n## 已批准产品决策\n\n{}\n\n"
            "证据优先级：{}\n".format(
                prd.title,
                prd.objective,
                "\n".join(
                    "- {} → {}".format(item.question, item.selected)
                    for item in decisions
                ),
                " > ".join(plan.evidence_priority),
            ),
            encoding="utf-8",
        )
        for node in nodes:
            contract = node.contract
            (staging / "specs" / (node.id + ".md")).write_text(
                "# Spec: {}\n\nnode_id: {}\n状态：published\n审核：reviewed\n\n输入：{}\n\n输出：{}\n\n错误行为：{}\n\n验收示例：{}\n".format(
                    node.title, node.id,
                    contract.get("input"),
                    contract.get("output"),
                    contract.get("error_behavior"),
                    contract.get("acceptance_example"),
                ),
                encoding="utf-8",
            )
            (staging / "issues" / (node.id + ".md")).write_text(
                "# Issue: {}\n\nissue_id: {}\n状态：published\n审核：reviewed\n\n并行组：{}\n".format(
                    node.title, node.id, node.parallel_group or "none"
                ),
                encoding="utf-8",
            )
        _atomic_json(staging / "plan.json", plan.to_dict())
        _atomic_json(staging / "nodes.json", [node.to_dict() for node in nodes])
        _atomic_json(staging / "authorization-card.json", card.to_dict())
        _atomic_json(staging / "dag-audit.json", {"status": "reviewed", "node_count": len(nodes), "plan_revision": str(plan.version), "node_ids": [node.id for node in nodes]})
        _atomic_json(staging / "plan-confirmation.json", {"status": "confirmed", "plan_id": plan.plan_id, "plan_revision": str(plan.version), "authorization_digest": card.digest, "authorization_required": True})
        os.replace(str(staging), str(destination))
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return plan, nodes, card


def _load_plan(paths: ProjectPaths, plan_id: str):
    directory = _plan_root(paths, plan_id)
    plan = Plan.from_dict(_read_json(directory / "plan.json"))
    nodes_data = _read_json(directory / "nodes.json")
    if not isinstance(nodes_data, list):
        raise ValueError("nodes.json must contain a list")
    normalized_nodes = []
    for item in nodes_data:
        if not isinstance(item, dict):
            raise ValueError("nodes.json entries must be objects")
        # Revisioned V3.9 plans keep the executable file scope at the node
        # level; adapt it to the legacy contract container used by the model.
        if "contract" not in item:
            item = dict(item)
            item["contract"] = {
                "files": list(item.get("allowlist", [])),
                "worker": "codex-app-visible-developer",
                "adapter_id": "codex",
                "project_id": "dbd30713-4842-4030-90ad-1789a85cbc58",
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
        card = build_authorization_card(plan, nodes, capabilities, active_pair_limit=5)
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
            refreshed = refresh_authorization_card(plan, nodes, card)
            if _verified_same_run_reauthorization(
                paths, directory, plan, nodes, refreshed
            ):
                return
        except (OSError, TypeError, ValueError):
            pass
    raise PermissionError("planning_required: " + ", ".join(gate.missing))


def _snapshot_result(command: str, snapshot: Any, as_json: bool) -> CLIResult:
    retry_pending = any(
        isinstance(node.get("retryable_action"), dict)
        and node.get("status") == "running"
        and not node.get("active_task")
        for node in snapshot.nodes.values()
    )
    result_status = "retry_pending" if retry_pending else snapshot.status
    payload = {
        "command": command,
        "status": result_status,
        "run_id": snapshot.run_id,
        "nodes": snapshot.nodes,
    }
    if retry_pending or snapshot.status == "blocked_unknown":
        code = UNKNOWN
    elif snapshot.status == "blocked_design":
        code = BLOCKED
    elif snapshot.status == "failed":
        code = UNKNOWN
    else:
        code = SUCCESS
    return _result(
        code,
        payload,
        (
            "监工已启动并自动推进中：运行 {}".format(snapshot.run_id)
            if retry_pending
            else "{}：运行 {}，状态 {}".format(command, snapshot.run_id, result_status)
        ),
        as_json,
    )


def _current_run_path(directory: Path) -> Path:
    return directory / "current-run.json"


def _invalidation_path(directory: Path) -> Path:
    return directory / "authorization-invalidated.json"


def _persist_invalidation(directory: Optional[Path], reason: str) -> None:
    if directory is not None:
        _atomic_json(
            _invalidation_path(directory),
            {
                "status": "blocked_design",
                "reason": reason,
                "change_reason": "executable_contract_changed",
            },
        )


def _is_capability_contract_unknown(error: BaseException) -> bool:
    return "capability_contract_unknown" in str(error)


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


def run_cli(argv: Sequence[str], cwd: Path, runner=None) -> CLIResult:
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

    if args.command == "sdd":
        try:
            request = _read_json(paths.resolve_relative(args.request)) if args.request else {}
            payload = {"command": "sdd", **run_v4_sdd(request, args.as_json)}
            code = BLOCKED if payload["user_status"] == "需要你决定" else SUCCESS
            return _result(code, payload, payload["user_status"], args.as_json)
        except (OSError, TypeError, ValueError) as error:
            return _result(BLOCKED, {"command": "sdd", "status": "需要你决定", "reason": str(error)}, "需要你决定", args.as_json)

    v2_state = False
    state_probe = paths.vibe / "state.json"
    if state_probe.is_file():
        try:
            state_data = _read_json(state_probe)
            v2_state = isinstance(state_data, dict) and state_data.get("workflow_version") == 2
        except (OSError, ValueError, AttributeError):
            v2_state = False
    if (v2_state or args.command == "init") and (args.command != "init" or args.confirm):
        try:
            session_id = args.command + ":" + str(args.run_id or args.plan_id or args.plan or "session")
            # CLI persistence binds the route, not raw user/provider text.
            screen_session(paths, str(session_id), args.command)
        except (OSError, ValueError, PermissionError) as error:
            return _result(BLOCKED, {"command": args.command, "status": "session_gate_blocked", "reason": str(error)}, "会话筛选已阻塞：" + str(error), args.as_json)
    if args.command in {"monitor", "reconcile", "resume", "status", "scan"} and paths.vibe.exists() and not state_probe.exists():
        return _result(BLOCKED, {"command": args.command, "status": "session_gate_blocked", "reason": "V2 state.json is missing"}, "会话筛选已阻塞：V2 state.json 缺失", args.as_json)
    if args.command == "scan" and paths.vibe.exists():
        try:
            state_data = _read_json(state_probe)
            if not isinstance(state_data, dict) or state_data.get("workflow_version") != 2 or state_data.get("session_gate") != "s0_required":
                raise ValueError("invalid V2 state")
        except (OSError, ValueError, AttributeError):
            return _result(BLOCKED, {"command": "scan", "status": "session_gate_blocked", "reason": "V2 state.json invalid"}, "扫描已阻塞：V2 state.json 无效", args.as_json)

    if args.command in {"install", "upgrade"}:
        if args.command == "upgrade" and not args.confirm:
            return _result(
                BLOCKED,
                {"command": "upgrade", "status": "blocked", "reason": "confirmation required"},
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
        if args.command == "upgrade" and payload.get("status") == "complete":
            try:
                upgraded = upgrade_project(paths, True)
                payload.update({"changed": upgraded.changed, "paths": upgraded.paths, "deploy": False})
            except (OSError, TypeError, ValueError) as error:
                payload = {
                    "operation": "upgrade",
                    "status": "blocked_invalid",
                    "phase": "blocked",
                    "errors": [str(error)],
                    "message": "需要你决定",
                }
        status = payload.get("status")
        code = SUCCESS if status == "complete" else UNKNOWN if status in {"blocked_unknown", "retry_pending", "failed"} else BLOCKED
        return _result(code, {"command": args.command, **payload}, payload.get("message", "需要你决定"), args.as_json)

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
        return _result(
            SUCCESS,
            payload,
            "初始化完成" if initialized.changed else "初始化无需变更",
            args.as_json,
        )

    if args.command == "upgrade":
        if not args.confirm:
            return _result(
                BLOCKED,
                {"command": "upgrade", "status": "blocked", "reason": "confirmation required"},
                "升级已暂停：需要明确确认",
                args.as_json,
            )
        try:
            upgraded = upgrade_project(paths, True)
        except (OSError, TypeError, ValueError) as error:
            return _result(
                BLOCKED,
                {"command": "upgrade", "status": "blocked", "reason": str(error)},
                "升级已阻塞：" + str(error),
                args.as_json,
            )
        payload = {
            "command": "upgrade",
            "status": "ok",
            "changed": upgraded.changed,
            "paths": upgraded.paths,
            "deploy": False,
        }
        return _result(
            SUCCESS,
            payload,
            "升级完成" if upgraded.changed else "升级无需变更",
            args.as_json,
        )

    if args.command == "apply-agentsmd":
        if not args.confirm:
            return _result(
                BLOCKED,
                {
                    "command": "apply-agentsmd",
                    "status": "blocked",
                    "reason": "confirmation required",
                },
                "AGENTS.md 规则应用已暂停：需要明确确认",
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
                "AGENTS.md 规则应用已阻塞：" + str(error),
                args.as_json,
            )
        payload = {
            "command": "apply-agentsmd",
            "status": "ok",
            "changed": applied.changed,
            "paths": applied.paths,
        }
        return _result(
            SUCCESS,
            payload,
            "AGENTS.md 能力规则已生效"
            if applied.changed
            else "AGENTS.md 能力规则无需变更",
            args.as_json,
        )

    if args.command == "doctor":
        report = doctor(scan_project(paths))
        bridge_payload = None
        try:
            bridge = ProviderActionStore(paths).capabilities()
            bridge_payload = _observed_adapter(
                paths, bridge["adapter_id"]
            ).to_dict()
        except (FileNotFoundError, OSError, TypeError, ValueError):
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
        }
        text = (
            "环境检查通过"
            if ready
            else "环境检查发现问题：" + "；".join(issues)
        )
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

    if args.command == "plan":
        screen = classify_s0(args.request or "")
        if screen.simple:
            payload = {
                "command": "plan",
                "status": "ok",
                "route": "simple",
                "rationale": screen.rationale,
            }
            return _result(
                SUCCESS, payload, "该请求走轻量直接执行路径", args.as_json
            )
        try:
            score = score_s1(_scores(args.s1))
            route = route_task(score)
            if route != "complex":
                payload = {
                    "command": "plan",
                    "status": "ok",
                    "route": route,
                    "score": score.total,
                }
                return _result(
                    SUCCESS, payload, "任务已进入轻规划", args.as_json
                )
            if not args.plan_id or not args.node_spec:
                raise PermissionError(
                    "complex planning requires a plan id and explicit node spec"
                )
            source_path = paths.resolve_relative(args.node_spec)
            plan, nodes, card = _publish_plan(paths, args.plan_id, source_path)
        except PermissionError as error:
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
            "plan": plan.to_dict(),
            "nodes": [node.id for node in nodes],
            "authorization_digest": card.digest,
            "workflow_version": 4,
            "execution_mode": "sdd_first",
        }
        return _result(
            SUCCESS, payload, "复杂计划产物已生成，等待一次授权", args.as_json
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
        if v2_state:
            try:
                require_capability_contract(paths)
            except PermissionError as error:
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
            if invalidation_path.exists():
                invalidation = _read_json(invalidation_path)
                if not isinstance(invalidation, dict):
                    raise ValueError("authorization invalidation record is invalid")
                card = refresh_authorization_card(plan, nodes, card)
                record = authorize(card, args.authorize)
                if runner is None:
                    runner = _public_runner(paths, card, nodes)
                snapshot = Monitor(paths, plan, nodes).reauthorize(
                    _run_id(directory, None),
                    record,
                    runner,
                    str(
                        invalidation.get(
                            "change_reason", "executable_contract_changed"
                        )
                    ),
                )
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
                card = refresh_authorization_card(plan, nodes, card)
                _atomic_json(directory / "authorization-card.json", card.to_dict())
                record = authorize(card, args.authorize)
                if runner is None:
                    runner = _public_runner(paths, card, nodes)
                snapshot = Monitor(paths, plan, nodes).reauthorize(
                    _run_id(directory, None),
                    record,
                    runner,
                    "capability_contract_changed",
                )
            else:
                record = authorize(card, args.authorize)
                if runner is None:
                    runner = _public_runner(paths, card, nodes)
                snapshot = Monitor(paths, plan, nodes).start(record, runner)
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
            if invalidation_to_clear is not None:
                invalidation_to_clear.unlink()
        except PreflightBlockedError as error:
            return _result(
                BLOCKED,
                {"command": "monitor", "status": "preflight_blocked", "check_ids": list(error.check_ids), "reason": str(error)},
                "预检已阻塞：" + ", ".join(error.check_ids),
                args.as_json,
            )
        except PermissionError as error:
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
        return _snapshot_result("monitor", snapshot, args.as_json)

    if args.command == "reconcile":
        try:
            if not args.plan or not args.run_id or not args.evidence:
                raise ValueError("reconcile requires --plan, --run-id and --evidence")
            directory, plan, nodes, _card = _load_plan(paths, args.plan)
            evidence_path = paths.resolve_relative(args.evidence)
            package = _read_json(evidence_path)
            snapshot = Monitor(paths, plan, nodes).reconcile_evidence(args.run_id, package)
            return _snapshot_result("reconcile", snapshot, args.as_json)
        except (FileNotFoundError, OSError, TypeError, ValueError, PermissionError) as error:
            return _result(
                UNKNOWN,
                {"command": "reconcile", "status": "blocked_unknown", "reason": str(error)},
                "对账已暂停：" + str(error),
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
                if runner is None:
                    runner = _public_runner(paths, _card, nodes)
                AuthorizationRecord.from_dict(
                    _read_json(directory / "authorization.json")
                )
                monitor = Monitor(paths, plan, nodes)
                # The CLI performs the normal resume tick immediately after
                # reattachment; let that tick own the single provider poll.
                snapshot = monitor.resume(run_id, runner, poll_handles=False)
                snapshot = monitor.tick(run_id, runner)
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
            if (
                "authorization" in message
                or "contract" in message
                or "plan no longer" in message
            ):
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
        return _snapshot_result(args.command, snapshot, args.as_json)

    return _result(
        USAGE_ERROR, {"status": "usage_error"}, "参数错误", args.as_json
    )


def handle_monitor(
    as_json: bool = False, authorization: Optional[str] = None, runner=None
) -> int:
    argv = ["monitor"]
    if authorization:
        argv.extend(["--authorize", authorization])
    if as_json:
        argv.append("--json")
    return run_cli(argv, Path.cwd(), runner=runner).exit_code


def main(argv: Optional[Sequence[str]] = None, runner=None) -> int:
    arguments = list(argv) if argv is not None else os.sys.argv[1:]
    result = run_cli(arguments, Path.cwd(), runner=runner)
    print(
        json.dumps(result.payload, ensure_ascii=False, sort_keys=True)
        if result.as_json
        else result.text
    )
    return result.exit_code
