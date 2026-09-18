# Claude Code 可见任务派发：真实实测记录

日期：2026-09-18　　基底：main `5a7ccbe`　　执行者：Claude Code 桌面会话（本机）

这份记录写的是**实际发生过的一次派发**，不是设计意图。fake 信箱服务的端到端测试
（`tests/test_pm_path_end_to_end.py`）已经绿了很久，但它证明不了桌面工具的真实行为，
所以有了这一次。

## 走通的链路

探针项目（临时目录，`vibe init --confirm` 起）：

```
attest --adapter claude-code  →  plan --from-prd（复杂路由）
  →  authorize --authorize AUTHORIZE  →  monitor --authorize AUTHORIZE
  →  信箱出现两个 pending create 请求（两个并行节点各一个）
  →  按合同建 worktree  →  调 ccd_session__spawn_task 创建可见会话
  →  会话在自己的分支上提交  →  complete() 回写绑定  →  resume 接受
```

五个操作都用真实桌面工具跑过一遍，与 `NATIVE_TOOL_MAP` 登记的名字一致：

| 操作 | 工具 | 实测结果 |
|---|---|---|
| `create` | `ccd_session__spawn_task` | 会话创建成功，但**行为与契约设想不同**，见下 |
| `locate` | `ccd_window__open_session_in` | 未单独验证（会话已在前台） |
| `visibility` | `ccd_session_mgmt__get_session` | 返回 `cwd`/`isRunning`/`model`，可用于可见性判定 |
| `resume` | `ccd_session_mgmt__send_message` | 未单独验证（本次任务一轮完成） |
| `wait` | `ccd_session_mgmt__list_events` | 返回事件流，cursor 是 `before_uuid`（形如 `c_a076d1ef…`） |

## 与契约设想不符的一处：`create` 是建议式的

阶段 5.5 的契约假设 `create` 调用后直接返回 `{"binding":{"sessionId","hostId"}}`。
实际不是：`ccd_session__spawn_task` **只提议一个任务**，在用户界面显示一张卡片，
由用户点击才真正创建会话。调用方拿不到 `sessionId`，返回的是一个 `task_id`
（形如 `task_1b72b080`）加一句"chip is showing for the user"。

本次是用户点了卡片之后，再用 `ccd_session_mgmt__list_sessions` 查到真实
`sessionId` 的。也就是说，`create` 在真实环境里是**两步、且中间需要人**：

1. 调 `spawn_task` → 得到 `task_id`，用户看到卡片
2. 用户点击 → 会话创建 → 需要另一次查询才能拿到 `sessionId` 绑定

**影响**：任何假设 `create` 同步返回绑定的实现都会在这里断。监工要么等用户点，
要么把 `task_id → sessionId` 的解析做成一次独立的查询步骤。这也意味着
"全自动派发"在当前桌面工具下做不到——`create` 天然带一个人工确认点。

## 替代路径：`claude --bg` 能无人值守创建（实测）

`spawn_task` 的人工确认点与 vibeguide 的设计目标直接冲突——授权卡的意义就是
"一次授权、后面不用盯屏幕"。CLI 有一条不需要点击的路：

```
$ claude --bg --permission-mode acceptEdits "<任务>"
Starting background service…
backgrounded · 4f31def4
  claude agents             list sessions
  claude attach 4f31def4    open in this terminal
  claude logs 4f31def4      show recent output
  claude stop 4f31def4      stop this session
```

一条命令直接返回会话 id，零人工介入。五个操作都有对应物，且比 ccd 工具更贴合派发：

| vibe 操作 | `claude` CLI | 说明 |
|---|---|---|
| `create` | `claude --bg "<prompt>"` | 直接返回 id，无确认点 |
| `locate` | `claude attach <id>` | 需要时才进前台 |
| `visibility` | `claude agents --json` | 返回 sessionId/status/cwd |
| `resume` | `claude --bg --resume <id>` | 同 id 后台续接 |
| `wait` | `claude logs <id>` | 读输出 |

额外好处：`--allowedTools` 与 `--permission-mode` 可以把授权卡的 allowlist
**翻译成进程级权限约束**，而不是靠被派发会话自觉遵守合同。这比 `spawn_task`
的信任模型强。

**一个真实障碍**：后台会话不继承桌面应用的 OAuth 认证，实测报
`Not logged in · Please run /login`，所以没干活。`claude --help` 写明无人值守
场景的认证途径是 `ANTHROPIC_API_KEY` 或 `apiKeyHelper`。要跑全自动派发必须配
其中之一，计费与桌面订阅是两笔——这是个需要产品决定的取舍，不是技术障碍。

（`spawn_task` 的确认点本身不是设计缺陷：派发会真的创建会话并消耗额度，
有人把关是合理的。它只是不适合"授权后无人值守"这个目标。）

## 节点隔离在真实派发里的验证

PR #40 修的就是这件事，这次拿到了真实证据。两个并行节点的 `child_binding`：

```
node=date-range-filter  worktree=.worktrees/date-range-filter-60466cae
                        branch=node/date-range-filter-60466cae
                        allowlist=['src/components/DateRange.tsx']
node=export-button      worktree=.worktrees/export-button-b474cfc3
                        branch=node/export-button-b474cfc3
                        allowlist=['src/pages/policy/view.tsx', 'src/utils/pdf.ts']
```

各自一棵树、一个分支，无一落在项目根或 `main`（#40 之前两者都是 `.` 和 `main`）。

被派发的会话实际落点，与合同逐字一致：

```
sessionId: local_c3f86730-431e-4745-993c-e6ff65a97cdb
cwd:       /tmp/mbox.acp6/.worktrees/date-range-filter-60466cae
branch:    node/date-range-filter-60466cae
commit:    91ca1df feat: date range filter skeleton
            src/components/DateRange.tsx | 12 +++++++++++-
            1 file changed, 11 insertions(+), 1 deletion(-)
```

独立复核（在派发方而非被派发方跑）：

- 主 worktree 仍在 `main`，HEAD 是 `scaffold probe files`，**不含**节点提交
- 节点提交只碰了 allowlist 里那一个文件，没有溢出
- 另一个节点的 worktree 从未被创建（它的请求仍 pending）——正确，没有越权预建

## 信箱回写

```python
store.complete(action_id, {"binding": {"sessionId": "local_c3f86730-…",
                                       "hostId": "macmini"}})
```

pending 从 2 降到 1，`resume` 返回 `retry_pending` 继续等第二个节点——正确行为，
不是失败。

## 被派发会话报告的两个真实问题

这两条不是链路问题，但会影响真实项目，记在这里备查：

1. **git 身份**：这台机器没配全局 `user.name`/`user.email`，提交作者被推导成
   `Macmini <macmini@MacminideMac-mini.local>`。探针项目无所谓，真实项目里
   作者信息会不对。
2. **commit message 的附加行**：被派发会话按其会话规范给提交附了
   `Co-Authored-By` 行。如果监工要逐字比对 commit message，这一行会算作差异。

## 尚未验证

- `locate`（`open_session_in`）与 `resume`（`send_message`）没有单独跑过
- 多节点并发派发（本次只派了一个节点，第二个留在 pending）
- reviewer 角色的派发（本次只有 developer）
- 一个 run 走到 `complete`（本次停在两节点其一）
