# V2 能力合同收口验证报告

日期：2026-08-26
分支：`codex/v2-capability-contract`

## 交付范围

- `vibe init --confirm` 生成 `.vibe/session-contract.json`，并将 `.vibe/state.json` 标记为 `workflow_version=2`、`session_gate=s0_required`、`capability_contract_required=true`。
- 监工入口、worker/provider action 共用同一份能力合同；合同缺失、损坏或状态无法核验时统一保持 `blocked_unknown`，过期能力在提示中降为 `stale`。
- `unknown_timeout` 不转成能力不存在；监工或 worker 的自然语言自报、README、记忆和“工具未被提及”不构成能力证据。
- 不直接修改既有 `AGENTS.md`；初始化仅生成 `.vibe/proposals/agentsmd/proposal.md` 建议。

## 本次验证

| 命令 | 结果 |
| --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_capability_contract tests.test_v2_diagnostics tests.test_initializer -v` | 26/26 通过 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_cli tests.test_adapters tests.test_end_to_end -v` | 43/45 通过；2 项既有端到端基线失败 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v` | 203/206 通过；2 项既有端到端基线失败，1 项既有导入错误 |

定向能力合同测试覆盖摘要、原子写入、过期、symlink、`unknown_timeout`、初始化幂等、`AGENTS.md` 提案隔离、入口阻断和 prompt 脱敏。CLI 回归验证也确认：monitor/resume 的 V2 执行路径强制读取能力合同；scan/status 和未授权 monitor 保持既有参数与只读行为；旧 V2 状态缺少新标志时，实际执行仍会进入 `blocked_unknown`，不会绕过合同。

## 未解决但不属于本功能的基线问题

- `test_public_provider_reauthorization_reconciles_terminal_wait_after_retry`：第二次 retry 的既有断言仍得到退出码 `0`，期望 `4`。
- `test_public_reauthorization_restarts_accepted_node_when_contract_changes`：既有断言要求 reauthorization 非 `0`，实际为 `0`。
- `tests.test_model_router` 无法导入 `vibe_guide.model_router`（`ModuleNotFoundError`）。

这些失败未通过修改无关模块掩盖；应由原对应 V2 任务单独处理。

## 外部能力边界

本报告不把本地单元/端到端夹具视为真实 Agent 能力证明。Codex、Claude Code、Cursor、Grok、WorkBuddy、Kimi Code、DeepSeek Harness 的登录、权限、任务可见性、创建/续接和真实平台故障处理仍未在真实平台核验，当前结论仅覆盖本地合同与 fail-closed 路径。

## Git 与合并边界

- 本分支仅用于 V2 验证；未执行 push、MR、merge 或 deploy。
- `.vibe/` 运行态、凭据和无关基线文件不纳入本功能提交。
- 待监工独立验收通过后，再按项目授权将本分支合并回目标分支。
