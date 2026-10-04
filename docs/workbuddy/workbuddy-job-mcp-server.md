# workbuddy_job：WorkBuddy 会话侧的派发桥（v5.0.2 起随包发布）

## 1. 它补的是哪一环

vibe 的监工自己**不执行**任何桌面动作。它把请求写进
`.vibe/provider-actions/requests/action-<digest>.json`，里面写明"要调用的原生工具名"，
真正去调的是**桌面会话**。`vibe_guide/runners/provider_action.py` 给
`workbuddy-visible` 映射的五个名字是：

| vibe 动作 | 原生工具名 |
|---|---|
| create | `workbuddy_job__create` |
| locate | `workbuddy_job__get` |
| visibility | `workbuddy_job__list` |
| resume | `workbuddy_job__reply` |
| wait | `workbuddy_job__wait` |

在 v5.0.2 之前，这五个名字**在 WorkBuddy 会话里根本不存在**——映射表登记了，
但没有执行体，所以请求永远躺在信箱里。本模块就是那个执行体：
`vibe_guide/mcp_servers/workbuddy_jobs.py`，一个纯标准库的 stdio MCP server，
server 名 `workbuddy_job`，暴露上面五个工具。

## 2. 注册（一次性）

写进宿主 MCP 配置 `~/.workbuddy-ai/mcp.json`：

```json
{
  "mcpServers": {
    "workbuddy_job": {
      "command": "<python3 绝对路径>",
      "args": ["-m", "vibe_guide.mcp_servers.workbuddy_jobs"],
      "env": {"PYTHONPATH": "<vibe-guide 仓库或安装目录>"}
    }
  }
}
```

写完后**不会自动生效**：到连接器管理页右上角的"自定义连接器"入口，对
`workbuddy_job` 点"信任"。之后会话里就能看到 `mcp__workbuddy_job__create` 等五个工具。

`PYTHONPATH` 是为了让 `import vibe_guide` 找得到包；若已 pip 安装到同一个解释器，
这一项可以省掉。

## 3. 网关：怎么拿到 endpoint 与 token

工具最终打的是 CodeBuddy Code 的 HTTP 网关 `/api/v1/*`。两种接法：

| 方式 | 做法 |
|---|---|
| 复用已有网关 | 设 `WORKBUDDY_JOB_ENDPOINT` 与 `WORKBUDDY_JOB_TOKEN`，直接连 |
| 自动拉起（默认） | 首次调用时自己起一个 `codebuddy --serve --host 127.0.0.1 --port <自动选的空闲口>`，从启动横幅里取 endpoint 与 password，随 MCP 进程退出而停止 |

可调环境变量：

- `WORKBUDDY_CLI`：codebuddy 可执行文件路径（否则依次找 PATH 上的
  `codebuddy` / `cbc` / `workbuddy`，最后回落到 WorkBuddy App 内置的 CLI）
- `WORKBUDDY_JOB_AUTOSTART=0`：禁止自动拉起，只允许连已有网关（拿不到就报错，不猜）

自检：`python3 -m vibe_guide.mcp_servers.workbuddy_jobs --selfcheck`
（打印 `gateway ok: N jobs` 即通；`--print-tools` 打印工具定义）

## 4. 实测（macOS + 内置 CLI 2.147.0，2026-10-03）

以下全部是**真跑出来**的，不是照抄 Windows 那台机器的记录：

| 端点 | 返回 |
|---|---|
| `POST /api/v1/jobs` | `{"data": {<job>}}`，job 含 `id`/`sessionId`/`state`/`kind`/`cwd` |
| `GET /api/v1/jobs/{id}` | `{"data": {"job": {<job>}}}`（**多一层 `job`**，与 create 不同） |
| `GET /api/v1/jobs` | `{"data": {"jobs": [...]}}`——**只列未结束的作业** |
| `GET /api/v1/jobs?all=1` | 追加已完成作业（`?all=true` 同效） |
| `POST /api/v1/jobs/{id}/reply` | 已结束的作业回 `{"delivered":false,"saved":true,"notice":"Reply saved — it will be sent when this session starts again"}` |
| `DELETE /api/v1/jobs/{id}` | `{"data":{"deleted":true}}` |

- 鉴权：`Authorization: Bearer <password>` **且**必带 `X-CodeBuddy-Request: 1`；
  缺 token 回 401 `AUTH_REQUIRED`，未知 id 回 404 `JOB_NOT_FOUND`。
- 启动横幅**带 ANSI 颜色码**（`\x1b[38;5;241mEndpoint\x1b[39m`），解析前必须先剥掉转义，
  否则 `Endpoint\s+` 匹配不上。
- 横幅只在**显式给了 `--port`** 时才打印；stdout 是管道时也可能不打印，故实现里
  用临时文件接输出、端口由自己挑空闲口。
- 启动横幅**携带网关口令**，所以解析失败时也**不回显原文**：错误里只报「抓到多少字节、
  `Endpoint`/`Password` 行是否出现」。这正是最需要克制的场景——格式漂移恰恰意味着口令
  正则失配，此时若把尾部原文拼进错误，明文口令会随 `tools/call` 的 `isError` 文本回到宿主。
  失败路径同时保证把已拉起的网关进程停掉，不留孤儿进程。

## 5. 已知阻断（宿主侧，不是本模块的问题）

在本机当前状态下，`POST /api/v1/jobs` 会回：

```
HTTP 500 [safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]
{"count":50,"threshold":50,"scope":"turn","targets":["~/.workbuddy-ai/jobs/.locks/<id>.state.lock.<pid>.<uuid>.candidate"],"targetCount":1}
```

关于原因，先说一个**被实测推翻的旧判断**：本节早先写作"`~/.workbuddy-ai/settings.json`
的沙箱文件规则把 `delete` 设为 `ask`"。逐条枚举该文件 `sandbox.orderedRules.file.rules`
的 106 条规则后确认：**没有任何一条匹配 `~/.workbuddy-ai/jobs/`**（规则覆盖的是
`~/.workbuddy/…`、`~/.codebuddy/…`、`~/.ssh/`、`~/Library/Keychains/` 等路径）。
所以这不是用户可调的安全设置，改 settings 也修不好。

真实机制是宿主的 **safe-delete 批量守卫**，由 shell 环境注入、与 settings 无关：

| 项 | 值 |
|---|---|
| 守卫程序 | `…/cli/vendor/shim/safe-delete-bulk-guard.cjs`（环境变量 `CODEBUDDY_SAFE_DELETE_BULK_GUARD`） |
| 阈值 | `CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD`，**本机为 50**；程序内默认 `DEFAULT_THRESHOLD = 20` |
| 状态根 | `CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR` = `$TMPDIR/codebuddy-safe-delete-bulk` |
| 状态路径 | `<状态根>/<sha256(sessionId)>/state.json` |
| 计数键 | `state.json` → `requests[<conversationRequestId>].count`，**即「一个回合」** |
| 触发条件 | `count + targetCount >= threshold` |
| 状态 TTL | `TURN_STATE_TTL_MS = 7 天` |

本机实测到的 `state.json`（三个会话，计数 1/4/14/18/49 不等，证明键就是回合）：

```json
{"requests": {
  "e5f8819cd1bc4de6849b29bc47c74255": {"count": 49},
  "01a102305ce7714395278badb5145ef4": {"count": 14}
}, "toolApprovals": {}, "requestRejections": {}}
```

据此，报错里的 `count:50` 不是"已经删了 50 个"，而是 **`已用 49 + 本次 1 = 50` 正好撞线**：

- 被拦的是**本回合额度用尽**，不是本次操作体量大——同报文 `targetCount` 只有 **1**，
  要删的只是建作业时清理的一个 `.locks/<id>.state.lock.<pid>.<uuid>.candidate`。
- **跨进程持久，重启网关不清零**：连起两个全新 `codebuddy --serve`，各自首次
  `POST /api/v1/jobs` 都回 `count:50`（4 次复测 4/4 一致）——因为状态按 session 落盘，
  与网关进程无关。"重开网关再试"无效。
- **换回合即归零**：键是 `conversationRequestId`，新回合 = 新键 = 计数从 0 起。
  本机本回合之所以打满，是因为**在同一个回合里跑了全量测试套件**——测试大量创建/删除
  临时目录，是删除额度的主要消耗方。

- 本模块不擅自改宿主的删除策略，只如实把 500 的原文抛给调用方
  （`isError: true`，不伪装成功）。
- 可选的下一步：**在新回合里重试**（额度归零），或让该回合的删除量降下来。
  **没有**"改一条 settings 规则"这个选项——已证伪。
- 注意：同一台机器上早前的同类调用是成功的（作业 `9a60d10a`、`beb2e65b`、`e55a4e01`
  均正常创建并 settle），所以**链路本身已验证可用**，这个 500 是回合内删除量累积触发的。

## 6. 还没验证的

- **端到端闭环**：`vibe attest` → `vibe plan` → `vibe authorize` → `vibe monitor` →
  信箱请求被取走 → 结果回写。§5 的阻断挡住最后一步，因此本轮只验证到
  "五个工具在会话里可调用、且真实打到控制面"。注意 §5 的阻断**按回合归零**，
  所以这一步应当安排在**没跑过全量测试的新回合**里做，否则额度会被测试吃掉。
- 真实 agent 作业（非 `bash:true` 的 echo 探针）的 `wait`/`reply` 语义。
- 宿主连接器管理页对 `workbuddy_job` 点"信任"之后，`mcp__workbuddy_job__*` 五个名字
  是否真的出现在会话的工具表里（本轮是直接走 MCP 协议调的，未验证宿主暴露）。
