import hashlib
import json
from pathlib import Path
from setuptools import find_namespace_packages, setup

DEFAULT_SOURCE_REF = "https://github.com/halfmoon82/vibe-guide-automation"
DEFAULT_SOURCE_COMMIT = "0c4bb712f7b344f1996de1c760388bcfe7b03d4d"


def _ensure_runtime_assets():
    """Create deterministic, credential-free manifest assets at build time."""
    root = Path(__file__).parent
    manifest_dir = root / "vibe_guide" / "adapters" / "manifests"
    guidance_dir = root / "vibe_guide" / "guidance"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    guidance_dir.mkdir(parents=True, exist_ok=True)
    adapters = ("codex", "claude-code", "cursor", "grok", "workbuddy", "kimi-code", "deepseek-harness")
    providers = {name: ("codex-app-visible" if name == "codex" else name + "-visible") for name in adapters}
    for adapter in adapters:
        target = manifest_dir / (adapter + ".yaml")
        if target.exists():
            continue
        probes = [{"kind": "command", "name": adapter + ".agent"}]
        probes.extend({"kind": "fact", "name": adapter + "." + suffix} for suffix in ("shell", "subprocess", "worktree", "visible_task.create", "visible_task.enter", "visible_task.resume", "visible_task.wait"))
        target.write_text(json.dumps({"id": adapter, "display_name": adapter, "provider": providers[adapter], "background_provider": adapter + "-background", "background_fallback": False, "agent_probe": adapter + ".agent", "source": {"ref": DEFAULT_SOURCE_REF, "commit": DEFAULT_SOURCE_COMMIT}, "probes": probes, "session_prompt": "请{trigger}，计划 {plan_id}。"}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    contract_target = guidance_dir / "canonical-contract.json"
    if not contract_target.exists():
        payload = {"version": "v3", "stages": ["prd_approved", "spec_issue_dag", "development_plan_confirmation", "authorization", "monitor"], "actions": ["continue_planning", "confirm_plan", "authorize_execution"], "statuses": ["planning_required", "governance_pending", "retry_pending", "blocked_unknown"], "stage_handoff_required": ["stage", "status", "plan_id", "plan_revision", "evidence_refs", "required_user_action", "forbidden_automatic_actions", "open_questions"], "forbidden_automatic_actions": ["create_worker", "monitor", "commit", "push", "create_mr", "merge", "deploy"], "s1_complex_rules": {"requires": ["independent_supervisor", "developer_worker", "independent_reviewer", "unique_writer"], "parallel_ready": True}, "authorization_defaults": {"commit": "allowed", "push": "allowed", "create_mr": "allowed", "merge": "allowed", "deploy": "excluded"}}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload["contract_hash"] = hashlib.sha256(encoded).hexdigest()
        contract_target.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


_ensure_runtime_assets()

setup(
    name="vibe-guide",
    version="0.1.0",
    packages=find_namespace_packages(include=["vibe_guide", "vibe_guide.*"]),
    include_package_data=True,
    package_data={"vibe_guide": ["adapters/manifests/*.yaml", "guidance/*.json"]},
    python_requires=">=3.9",
    entry_points={"console_scripts": ["vibe=vibe_guide.cli:main"]},
)
