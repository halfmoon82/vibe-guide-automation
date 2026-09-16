"""Publish a real complex plan for the authorize-entry tests.

The fixture goes through `vibe init` and `vibe plan` rather than hand-writing
artifacts, so the tests exercise the same files a user would actually have.
"""
import json
import tempfile
from pathlib import Path

from vibe_guide.cli import run_cli

_CAPABILITIES = {
    "schema_version": 1,
    "adapter_id": "claude-code",
    "facts": {
        "claude-code.agent": True,
        "claude-code.shell": True,
        "claude-code.subprocess": True,
        "claude-code.worktree": True,
        "claude-code.visible_task.create": True,
        "claude-code.visible_task.enter": True,
        "claude-code.visible_task.resume": True,
        "claude-code.visible_task.wait": True,
    },
    "provenance": "test fixture: native visible-task tools observed in this session",
}

_NODE_SPEC = {
    "title": "授权入口探针",
    "objective": "verified_fact: 验证 authorize 子命令能否把已发布工件投影为门禁证据",
    "project_id": "authorizeprobe",
    "complexity_band": "complex",
    "spec_path": ".vibe/plans/probe-plan/prd.md",
    "remote_git_actions": "deny",
    "capabilities": {
        "agent_id": "claude-code",
        "shell": True,
        "subprocess": True,
        "worktree": True,
        "background": False,
        "session_resume": True,
        "level": "full",
    },
    "integration_contract": {
        "iteration_context": {
            "kind": "iteration",
            "baseline": "verified_fact: 探针项目为空仓库",
            "target": "verified_fact: authorize 记录十节点证据后 monitor 可建 run",
        },
        "compatibility_scope": [
            "verified_fact: 仅本探针项目",
            "verified_fact: 不触碰任何既有受管项目",
        ],
        "agentsmd_acceptance_refs": ["docs/probe-acceptance.md"],
        "integration_acceptance_contract": {
            "criteria": "verified_fact: events.jsonl 出现 run_started",
            "observation": "verified_fact: 读取 run 事件日志",
        },
        "unverified_or_excluded": [
            "verified_fact: 部署与发布不在范围",
            "verified_fact: 生产写入、凭据与外部通信不在范围",
        ],
    },
    "decisions": [
        {
            "question": "如何验证授权入口",
            "options": ["实测发布后授权再派发", "只读代码"],
            "impact": "verified_fact: 决定是否产生可观测的 run_started 证据",
            "recommendation": "实测发布后授权再派发",
            "status": "approved",
            "selected": "实测发布后授权再派发",
            "field": "verification_method",
        }
    ],
    "nodes": [
        {
            "id": "probe-node-a",
            "title": "探针节点 A",
            "depends_on": [],
            "integration_after": [],
            "parallel_group": "probe-group",
            "status": "planned",
            "contract": {
                "input": "verified_fact: 空仓库",
                "output": "verified_fact: 写一行 README",
                "error_behavior": "保留 blocked_unknown 并提供可恢复状态",
                "acceptance_example": "verified_fact: README 含一行文本",
            },
        }
    ],
}

_REQUEST = "设计并实现探针功能，集成测试与部署 workflow pipeline 迁移"


def publish_complex_probe(case) -> Path:
    """Return a project root holding a freshly published complex plan."""
    root = Path(tempfile.mkdtemp(prefix="v45-authorize-"))
    case.addCleanup(_cleanup, root)

    init = run_cli(["init", "--confirm", "--json"], root)
    assert init.payload.get("status") == "ok", init.payload

    store = root / ".vibe" / "provider-actions"
    store.mkdir(parents=True, exist_ok=True)
    (store / "capabilities.json").write_text(
        json.dumps(_CAPABILITIES, ensure_ascii=False), encoding="utf-8"
    )

    spec_path = root / "node-spec.json"
    spec_path.write_text(json.dumps(_NODE_SPEC, ensure_ascii=False), encoding="utf-8")
    published = run_cli(
        [
            "plan", "--json", "--request", _REQUEST, "--s1", "5,5,5,5,5",
            "--plan-id", "probe-plan", "--node-spec", "node-spec.json",
        ],
        root,
    )
    assert published.payload.get("status") == "ok", published.payload
    return root


def _cleanup(root: Path) -> None:
    import shutil
    shutil.rmtree(root, ignore_errors=True)
