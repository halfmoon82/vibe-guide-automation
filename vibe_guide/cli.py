"""Small, dependency-free public CLI for receiving agents.

The delivery package intentionally keeps the receiving-agent surface local and
deterministic.  Real provider capabilities remain unknown until a provider
adapter supplies structured evidence; the fake runner is only a local smoke
fixture and never represents remote authority.
"""

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, Optional, Sequence

from . import __version__

SUCCESS, USAGE_ERROR, BLOCKED, UNKNOWN = 0, 2, 3, 4


@dataclass(frozen=True)
class CLIResult:
    exit_code: int
    payload: Dict[str, Any]
    text: str


def _json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("persistence path may not be a symlink")
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _scan(root: Path) -> Dict[str, Any]:
    return {
        "root": str(root),
        "python_version": ".".join(str(item) for item in sys.version_info[:3]),
        "git_version": _git(root, "--version"),
        "git_root": _git(root, "rev-parse", "--show-toplevel"),
        "git_remote": _git(root, "config", "--get", "remote.origin.url"),
        "agentsmd_exists": (root / "AGENTS.md").is_file(),
        "knowledge_exists": (root / ".vibe" / "knowledge").is_dir(),
        "vibe_exists": (root / ".vibe").is_dir(),
        "writes": [],
    }


def _doctor(root: Path) -> Dict[str, Any]:
    commands = {name: bool(shutil.which(name)) for name in ("python3", "git", "pip")}
    contract = _read_json(root / ".vibe" / "session-contract.json")
    capability_status = "unknown" if not contract else contract.get("status", "unknown")
    return {
        "version": __version__,
        "python": sys.executable,
        "python_version": ".".join(str(item) for item in sys.version_info[:3]),
        "commands": commands,
        "provider_lifecycle": capability_status,
        "real_provider": "unknown",
    }


def _init(root: Path, confirm: bool) -> CLIResult:
    vibe = root / ".vibe"
    if not confirm:
        payload = {"command": "init", "status": "confirmation_required", "changed": False}
        return CLIResult(BLOCKED, payload, "需要确认：vibe init --confirm")
    created = []
    for relative in ("plans", "runs", "knowledge", "proposals/agentsmd"):
        path = vibe / relative
        if not path.exists():
            path.mkdir(parents=True)
            created.append(relative)
    state = vibe / "state.json"
    if not state.exists():
        _json_write(state, {"workflow_version": 2, "session_gate": "s0_required", "status": "initialized"})
        created.append("state.json")
    contract = vibe / "session-contract.json"
    if not contract.exists():
        _json_write(contract, {"version": 1, "status": "unknown", "evidence_ref": "init"})
        created.append("session-contract.json")
    payload = {"command": "init", "status": "initialized", "changed": bool(created), "created": created}
    return CLIResult(SUCCESS, payload, "初始化完成" if created else "已初始化，无变化")


def _plan(root: Path, request: str, plan_id: str) -> CLIResult:
    if not request.strip():
        raise ValueError("request is required")
    plan_id = plan_id or "local-plan"
    if not all(character.isalnum() or character in "_.-" for character in plan_id):
        raise ValueError("plan id must be a simple identifier")
    destination = root / ".vibe" / "plans" / plan_id
    if destination.exists():
        payload = _read_json(destination / "plan.json") or {}
        return CLIResult(SUCCESS, {"command": "plan", "status": "existing", "plan": payload}, "计划已存在")
    destination.mkdir(parents=True, exist_ok=True)
    payload = {
        "plan_id": plan_id,
        "version": 1,
        "request": request,
        "status": "authorized",
        "nodes": [{"id": "local", "title": request, "status": "planned", "runner": "fake/local"}],
        "authorization_digest": hashlib.sha256((plan_id + request).encode("utf-8")).hexdigest(),
    }
    _json_write(destination / "plan.json", payload)
    return CLIResult(SUCCESS, {"command": "plan", "status": "authorized", "plan": payload}, "计划已生成：" + plan_id)


def _plan_file(root: Path, plan_id: Optional[str]) -> Optional[Path]:
    if not plan_id:
        return None
    return root / ".vibe" / "plans" / plan_id / "plan.json"


def _monitor(root: Path, plan_id: Optional[str], token: Optional[str]) -> CLIResult:
    if token != "AUTHORIZE":
        return CLIResult(BLOCKED, {"command": "monitor", "status": "blocked", "reason": "authorization_required"}, "需要精确授权")
    path = _plan_file(root, plan_id)
    plan = _read_json(path) if path else None
    if not plan:
        return CLIResult(UNKNOWN, {"command": "monitor", "status": "blocked_unknown", "reason": "plan_unknown"}, "计划状态未知")
    plan["status"] = "delivered"
    plan["developer"] = {"status": "delivered", "runner": "fake/local"}
    _json_write(path, plan)
    return CLIResult(SUCCESS, {"command": "monitor", "status": "delivered", "plan": plan}, "本地 fake runner 已交付")


def _status(root: Path, plan_id: Optional[str]) -> CLIResult:
    plan = _read_json(_plan_file(root, plan_id)) if plan_id else None
    if not plan:
        return CLIResult(UNKNOWN, {"command": "status", "status": "unknown", "reason": "plan_unknown"}, "状态未知")
    return CLIResult(SUCCESS, {"command": "status", "status": plan.get("status", "unknown"), "plan": plan}, "状态：" + str(plan.get("status")))


def _resume(root: Path, plan_id: Optional[str]) -> CLIResult:
    path = _plan_file(root, plan_id)
    plan = _read_json(path) if path else None
    if not plan:
        return CLIResult(UNKNOWN, {"command": "resume", "status": "unknown", "reason": "plan_unknown"}, "状态未知")
    if plan.get("status") == "delivered":
        plan["status"] = "accepted"
        plan["reviewer"] = {"status": "accepted", "independent": True}
        _json_write(path, plan)
    return CLIResult(SUCCESS, {"command": "resume", "status": plan.get("status"), "plan": plan}, "已恢复：" + str(plan.get("status")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vibe", description="Vibe Coding 辅助开发向导")
    parser.add_argument("command", nargs="?", choices=("scan", "init", "doctor", "plan", "monitor", "status", "resume"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--request")
    parser.add_argument("--plan-id")
    parser.add_argument("--plan")
    parser.add_argument("--authorize")
    return parser


def run_cli(argv: Optional[Sequence[str]] = None, root: Optional[Path] = None) -> CLIResult:
    parser = _parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as error:
        code = int(error.code or 0)
        return CLIResult(USAGE_ERROR if code else SUCCESS, {"status": "usage_error"}, parser.format_help())
    project_root = Path(root or Path.cwd()).resolve()
    try:
        if args.command is None:
            payload = {"command": "help", "status": "ok"}
            return CLIResult(SUCCESS, payload, parser.format_help())
        if args.command == "scan":
            payload = {"command": "scan", "status": "ok", "report": _scan(project_root)}
            return CLIResult(SUCCESS, payload, "扫描完成：" + str(project_root))
        if args.command == "doctor":
            return CLIResult(SUCCESS, {"command": "doctor", "status": "ok", "report": _doctor(project_root)}, "环境诊断完成")
        if args.command == "init":
            return _init(project_root, args.confirm)
        if args.command == "plan":
            return _plan(project_root, args.request or "", args.plan_id or "local-plan")
        if args.command == "monitor":
            return _monitor(project_root, args.plan or args.plan_id, args.authorize)
        if args.command == "status":
            return _status(project_root, args.plan or args.plan_id)
        if args.command == "resume":
            return _resume(project_root, args.plan or args.plan_id)
    except (OSError, ValueError) as error:
        return CLIResult(USAGE_ERROR, {"status": "error", "reason": str(error)}, str(error))
    return CLIResult(USAGE_ERROR, {"status": "usage_error"}, parser.format_help())


def main(argv: Optional[Sequence[str]] = None) -> int:
    result = run_cli(argv)
    if argv is not None and "--json" in argv:
        print(json.dumps(result.payload, ensure_ascii=False, sort_keys=True))
    elif argv is None and "--json" in sys.argv[1:]:
        print(json.dumps(result.payload, ensure_ascii=False, sort_keys=True))
    else:
        print(result.text)
    return result.exit_code

