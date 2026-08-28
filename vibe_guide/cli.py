"""Thin CLI wiring over the scanner, planner, authorization and monitor APIs."""

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .authorization import (
    AuthorizationCard,
    AuthorizationRecord,
    authorize,
    build_authorization_card,
    refresh_authorization_card,
)
from .deploy import (
    DeployManifest,
    DeployState,
    authorize_deploy,
    deploy_manifest_digest,
    is_deploy_authorization_valid,
    plan_deploy,
    prepare_deploy,
    start_deploy,
    verify_deploy,
)
from .adapters.base import Environment
from .adapters.registry import AdapterRegistry
from .adapters.task_provider import ProviderActionStore
from .dag import render_plan_artifacts, validate_dag
from .doctor import doctor
from .initializer import init_project
from .models import AgentCapabilities, DAGNode, Plan
from .monitor import Monitor
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
from .state import load_snapshot
from .runners.provider_action import ProviderActionRunner


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
        choices=("scan", "init", "doctor", "plan", "monitor", "deploy", "status", "resume"),
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--request")
    parser.add_argument("--plan-id")
    parser.add_argument("--plan")
    parser.add_argument("--run-id")
    parser.add_argument("--s1")
    parser.add_argument("--node-spec")
    parser.add_argument("--authorize")
    parser.add_argument("--authorization-token", dest="legacy_authorization")
    parser.add_argument("--deploy-authorize")
    parser.add_argument("--deploy-observations")
    return parser


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
    capabilities = ProviderActionStore(paths).capabilities()
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
    nodes = [DAGNode.from_dict(item) for item in source.get("nodes", [])]
    if not nodes:
        raise ValueError("node spec must contain at least one node")
    validation = validate_dag(nodes)
    if not validation.valid:
        raise ValueError("DAG is invalid: " + "; ".join(validation.errors))

    destination = _plan_root(paths, plan_id)
    deploy_manifest = None
    if source.get("deploy") is not None:
        deploy_manifest = DeployManifest.from_dict(source["deploy"])
    plan = Plan(
        plan_id,
        1,
        str(Path(".vibe") / "plans" / plan_id / "prd.md"),
        [node.id for node in nodes],
        "draft",
        decisions=[asdict(item) for item in decisions],
        deploy=deploy_manifest.to_dict() if deploy_manifest else None,
    )
    capabilities = AgentCapabilities.from_dict(source.get("capabilities", {}))
    try:
        capabilities = _observed_adapter(paths, capabilities.agent_id).capabilities
    except (FileNotFoundError, OSError, TypeError, ValueError):
        # Planning remains usable for explicitly injected/background test paths;
        # the public monitor rechecks live observed capability before execution.
        pass
    card = build_authorization_card(
        plan,
        nodes,
        capabilities,
        active_pair_limit=source.get("active_pair_limit"),
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
            "# {}\n\n状态：approved\n\n目标：{}\n\n## 已批准产品决策\n\n{}\n\n"
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
                "# Spec: {}\n\n输入：{}\n\n输出：{}\n\n错误行为：{}\n\n验收示例：{}\n".format(
                    node.title,
                    contract.get("input"),
                    contract.get("output"),
                    contract.get("error_behavior"),
                    contract.get("acceptance_example"),
                ),
                encoding="utf-8",
            )
            (staging / "issues" / (node.id + ".md")).write_text(
                "# Issue: {}\n\n状态：planned\n\n并行组：{}\n".format(
                    node.title, node.parallel_group or "none"
                ),
                encoding="utf-8",
            )
        _atomic_json(staging / "plan.json", plan.to_dict())
        _atomic_json(staging / "nodes.json", [node.to_dict() for node in nodes])
        _atomic_json(staging / "authorization-card.json", card.to_dict())
        if deploy_manifest is not None:
            _atomic_json(staging / "deploy-manifest.json", deploy_manifest.to_dict())
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
    nodes = [DAGNode.from_dict(item) for item in nodes_data]
    card = _authorization_card(_read_json(directory / "authorization-card.json"))
    if plan.plan_id != plan_id or card.plan_id != plan_id:
        raise ValueError("plan identity does not match its directory")
    return directory, plan, nodes, card


def _snapshot_result(command: str, snapshot: Any, as_json: bool) -> CLIResult:
    payload = {
        "command": command,
        "status": snapshot.status,
        "run_id": snapshot.run_id,
        "nodes": snapshot.nodes,
    }
    if snapshot.status == "blocked_unknown":
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
        "{}：运行 {}，状态 {}".format(command, snapshot.run_id, snapshot.status),
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
        payload = {
            "command": "doctor",
            "status": "ok" if ready else "blocked",
            "ok": ready,
            "issues": issues,
            "facts": report.facts,
            "provider_bridge": bridge_payload,
        }
        text = (
            "环境检查通过"
            if ready
            else "环境检查发现问题：" + "；".join(issues)
        )
        return _result(
            SUCCESS if ready else BLOCKED, payload, text, args.as_json
        )

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
        if args.authorize != "AUTHORIZE":
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
        try:
            directory, plan, nodes, card = _load_plan(paths, args.plan)
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
            else:
                record = authorize(card, args.authorize)
                if runner is None:
                    runner = _public_runner(paths, card, nodes)
                snapshot = Monitor(paths, plan, nodes).start(record, runner)
            _atomic_json(directory / "authorization.json", record.to_dict())
            _atomic_json(
                _current_run_path(directory), {"run_id": snapshot.run_id}
            )
            if invalidation_to_clear is not None:
                invalidation_to_clear.unlink()
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

    if args.command == "deploy":
        if not args.plan or args.deploy_authorize != "AUTHORIZE_DEPLOY":
            return _result(
                BLOCKED,
                {"command": "deploy", "status": "blocked_deploy", "reason": "separate Deploy authorization required"},
                "Deploy 已暂停：需要独立 AUTHORIZE_DEPLOY 授权",
                args.as_json,
            )
        directory = None
        try:
            directory, plan, _nodes, _card = _load_plan(paths, args.plan)
            manifest_path = directory / "deploy-manifest.json"
            if not manifest_path.exists():
                raise PermissionError("Deploy was not selected for this plan")
            manifest = DeployManifest.from_dict(_read_json(manifest_path))
            run_id = _run_id(directory, None)
            snapshot = load_snapshot(paths, run_id)
            if snapshot.status != "complete":
                raise PermissionError("independent acceptance is required before Deploy")
            record = authorize_deploy(manifest, args.deploy_authorize)
            if args.deploy_observations:
                state = prepare_deploy(manifest, plan_deploy(manifest, "accepted"), record)
                observation_path = paths.resolve_relative(args.deploy_observations)
                observations = _read_json(observation_path)
                running = start_deploy(manifest, state, record)
                verified = verify_deploy(manifest, observations)
                evidence = dict(verified.evidence)
                evidence.setdefault("authorization_digest", running.evidence.get("authorization_digest"))
                state = DeployState(verified.status, verified.manifest, verified.reason, evidence)
            else:
                # A deploy command without observable health/version evidence
                # must fail closed.  Do not persist the intermediate ready
                # state as if an unobserved deployment had succeeded.
                unknown = verify_deploy(manifest, {})
                evidence = dict(unknown.evidence)
                evidence["authorization_digest"] = record.digest
                state = DeployState(unknown.status, unknown.manifest, unknown.reason, evidence)
            _atomic_json(directory / "deploy-authorization.json", record.to_dict())
            _atomic_json(directory / "deploy-state.json", state.to_dict())
        except PermissionError as error:
            return _result(
                BLOCKED,
                {"command": "deploy", "status": "blocked_deploy", "reason": str(error)},
                "Deploy 已阻塞：" + str(error),
                args.as_json,
            )
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as error:
            return _result(
                UNKNOWN,
                {"command": "deploy", "status": "blocked_unknown", "reason": str(error)},
                "Deploy 状态未知：" + str(error),
                args.as_json,
            )
        payload = {
            "command": "deploy",
            "status": state.status,
            "manifest_digest": deploy_manifest_digest(manifest),
            "reason": state.reason,
            "evidence": state.evidence,
        }
        code = UNKNOWN if state.status == "blocked_unknown" else (BLOCKED if state.status == "blocked_deploy" else SUCCESS)
        return _result(code, payload, "Deploy 状态：" + state.status, args.as_json)

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
        directory = None
        try:
            directory, plan, nodes, _card = _load_plan(paths, args.plan)
            run_id = _run_id(directory, args.run_id)
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
                snapshot = monitor.resume(run_id, runner)
                snapshot = monitor.tick(run_id, runner)
        except PermissionError as error:
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
