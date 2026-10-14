# bg-live-acceptance 真机验收记录（2026-10-14）

计划 session-8756d959f797ff54 / 运行 run-85e33b17 第 4 节点。环境：Claude Code 2.1.260，vibe 5.0.10 主干 + 本 PR 修复，模型 `claude-sonnet-5-5`，凭据走用户级 `claude_bg.settings_path`（apiKeyHelper 运行时从 `~/.claude/settings.json` 读密钥，任何文件和日志都不落密钥）。

## 结论

真实链路通过：后台派发 → worker 交付 → 独立审查提出 1 个缺陷 → 返工续接**同一会话** → 复审通过，全程无人工点击。用修复后的代码连跑 3 次都通过，每次约 30 秒。

| 次 | 会话 | 交付 g1 | 审查 g1 | 交付 g2 | 审查 g2 | 返工前后本目录会话 |
|---|---|---|---|---|---|---|
| 1 | fd464490-176c-49d3-b36f-cdaa8b7f2da9 | 14s | 1 个缺陷 | 34s | 无 | 同一个 |
| 2 | e7f944a4-bf34-4171-82a7-fe45ec11d891 | 14s | 1 个缺陷 | 34s | 无 | 同一个 |
| 3 | 4015ecc1-bc0c-4bea-af08-a6cc2b9086a3 | 12s | 1 个缺陷 | 29s | 无 | 仅 1 个，等于派发会话 |

复跑命令（默认跳过）：`VIBE_LIVE_CLAUDE_BG=1 python3 -m unittest tests.test_claude_bg_live -v`

说明：
- 审查缺陷是预设的（`greet("")` 应返回 `Hello, stranger`），用来检验返工链路，不是检验 worker 能力。
- 交付通过 worker 在 worktree 内写 `.vibe/provider-actions/deliveries/<run>-<node>-developer-g<N>.json` 完成，由 `ClaudeBgRunner` 读取；没有走 `vibe worker-deliver`，因为临时项目不是真实 vibe 运行。

## 真机才暴露的 4 个缺陷（均已修，均有失败先行的单测）

1. **从桌面版派发的后台会话全部"未登录"**。桌面宿主注入的 `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST` 被继承后，会话忽略 `--settings` 里的 apiKeyHelper。逐变量排查后确认只有这一个变量起作用：所有非 CLAUDE 变量合起来 → done；只带这一个 → blocked。修复：`subprocess_executor` 去掉这个变量。
2. **返工复制出新会话**。`claude stop` 返回后几秒内会话仍算"运行中"，立刻续接会得到 `already running … started a copy`。实测：等 0 秒 → 复制，等 5 秒 → 续接原会话。状态字段在这段时间内一直是 `done`，不能当信号。修复：每次续接前分别等 5/10/20 秒；遇到 "already running" 就停掉并删除副本后重试，最多 3 次。"带了参数"导致的复制不重试。副本会立即"未登录"（没有继承原会话保存的凭据），不会造成双写。
3. **任务说明以 `-` 开头时被当成命令行选项**，会话 failed。修复：提示词和返工消息前加 `--`。实测 `--` 不影响续接原会话（`woke session … with its saved options`）。
4. **启动超时会留下孤儿会话**：0.3 秒超时后，后台仍有一个免确认权限的会话在跑（`83ea1bea working`）。修复：启动失败时，按名称 + 同目录 + 本次启动之后的 `startedAt` 找到它并 stop，把结果写进报错。修复后复测这一次没有出现孤儿，所以清理分支在真机上没有被触发，只有单测覆盖。

## 遗留审查项的真机结论

| 项 | 结论 |
|---|---|
| `claude` 命令不存在 | 报 unknown，不留会话 ✓ |
| `--` 分隔 | 需要，已加（见缺陷 3） |
| 大任务说明 | 64KB、200KB 正常；600KB 时后台服务超时（报 unknown，失败关闭）。真实派发的 prompt 约 16KB |
| 启动超时留孤儿 | 属实，已修（见缺陷 4） |
| 续接不复制 | 需要等待，已修（见缺陷 2） |
| 主目录 vs worktree | 主目录派发 2/2 被另开到 `.claude/worktrees/<名>`，worktree 派发 2/2 正常写入。vibe 按节点 worktree 派发，不受影响；README 已写明 |

## 未验证

- 长任务、休眠或 daemon 退出后的恢复；`--session-id`；`-w` 与 `--bg` 合用；桌面内置 CLI 下的 `--bg`。
- 通过真实 vibe 运行（plan → authorize → monitor）端到端派发后台节点。本次只驱动了 `ClaudeBgLauncher` + `ClaudeBgRunner`。
- 孤儿清理分支没有在真机上触发（见缺陷 4）。

## 清理

验收会话全部 stop + rm。探针产生的会话（929d3c43、1f10384e、281555f5、ba2644eb、eba70222）及其临时 worktree 已删除。不属于本次验收的会话（如 4f31def4）未动。
