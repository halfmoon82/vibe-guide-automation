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

## 7. 第二条阻断：`.guard` 目录删不掉（2026-10-05 实测，与 §5 不同）

§5 记的是**额度撞线**。2026-10-05 复测时拿到的是**另一条**，报文不同，别混：

```
POST /api/v1/jobs failed: HTTP 500
{"error":{"code":"INTERNAL_ERROR",
 "message":"[safe-delete] broker denied delete: path=/Users/macmini/.workbuddy-ai/jobs/.locks/<id>.state.lock.guard"}}
```

**两条的区分判据**（一眼可辨，别互相套用结论）：

| | §5 额度撞线 | §7 本条 |
|---|---|---|
| 报文关键词 | `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]` | `[safe-delete] broker denied delete` |
| 是否带 `count`/`threshold` | 带 | **不带** |
| 目标 | `.state.lock.<pid>.<uuid>.candidate`（**文件**） | `.state.lock.guard`（**目录**） |
| 换回合能否绕过 | 能（计数按回合归零） | **不能**（与计数无关） |

**实测证据**（2026-10-05 19:27–19:34，`vibe_guide/mcp_servers/workbuddy_jobs.py` 直连 stdio）：

- `create` 连续 4 次失败，每次新 job id（`b54319c0` / `f57b3f01` / `555a0f20` / `50c78e9e`），
  报文逐字一致；**沙箱内与沙箱外结果相同**，所以不是 agent 沙箱造成的假红。
- 同会话同回合的删除计数当时是 **38**（阈值 50，见
  `$CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR/<sha256(session)>/state.json`），
  **额度并未耗尽** → 本条与 §5 的额度机制无关。
- job 目录 `~/.workbuddy-ai/jobs/<id>/` 其实**建出来了**（含 `state.json`、`tmp/`），
  失败点在服务清理自己的锁守卫目录时被拒。每次尝试都会多留一个空的 `.guard` 目录。
- `vibe_guide/` 全目录对宿主的 safe-delete 机制**零感知**（`grep SAFE_DELETE|safe_delete` 无命中）。

**尚未定论**：`broker denied delete` 这个字符串**不在** `safe-delete-bulk-guard.cjs` 里
（该脚本只有 `SAFE_DELETE_BULK_CONFIRM_REQUIRED` / `_GUARD_ERROR` / `_REJECTED` /
`_STATE_DIR` / `_THRESHOLD`），所以拒绝来自守卫之外的另一层 broker；
读取 App 内部实现被沙箱拦下，未继续。

### 7.1 归属判断（2026-10-05 修订：vibeguide 侧确有修复项，已实施）

**修订说明**：本条初版结论是「归属在宿主、vibeguide 侧没有可指认的回归、不新立修复计划」。
该结论**前提正确、推论错误**——它回答了「谁拒绝的」，但没有回答「我们能不能不撞上」。
产品经理要求把这个 bug 修好，继续深挖后拿到了**可复现的最小实验**，结论翻到 vibeguide 侧。

#### 7.1.1 决定性实验（可复现，10 秒内出结果）

只创建/删除探针自己造的目录，不碰任何既有条目：

| 臂 | 环境 | 在 `~/.workbuddy-ai/jobs/.locks/` 下 `fs.rmdirSync` | 结果 |
|---|---|---|---|
| A | 完整宿主环境 | **FAIL** `[safe-delete] broker denied delete` | 复现 §7 的 500 |
| B | 剥掉 broker 三件套 | ok | 目录被删掉 |
| C | broker socket 指向不存在路径 | ok | 目录被删掉 |

A 臂失败后**目录残留**，与 §7 记的失败期空 `.guard` 残留**完全同构**。

#### 7.1.2 根因链条（代码级，非推断）

1. WorkBuddy 把 safe-delete 注入每个它启动的进程：`BASH_ENV` → `safe-bin` 包装
   `rm`/`rmdir`；`PATH` 首位 `brokered-bin`；`PYTHONPATH` → `sitecustomize.py`；
   `NODE_OPTIONS` → `node-language-shim.cjs`；
   `CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS` → `$TMPDIR/cbb-XXXX/broker.sock`。
2. Node shim 的删除走 `tryTrash()` → `tryBrokerDelete()`（`node-safe-delete-shim.cjs:590`）。
   broker **不可用**时返回 `unavailable` → **降级到回收站，成功**。
3. 但 `classifyDeleteResult()`（`safe-delete-broker-delete.cjs:51`）是 **fail-closed 兜底**：

   ```js
   if (response.ok === true)            return 'ok';
   if (response.decision === 'sandbox') return 'unavailable';
   return 'denied';                     // ← 其余一切都算拒绝
   ```

   broker **回复了但没批准** → `denied` → `node-safe-delete-shim.cjs:598` 抛错。
4. broker 的批准粒度是**agent 工具调用**（`toolApprovals`）。vibe 的 MCP server 拉起的网关
   **不是** agent 工具调用，它永远拿不到批准 → 在会话里必然撞上 `denied`。
5. `create` 在写完 job 目录后要清理自己的 `.guard` 锁目录 → 被拒 → 整个 create 返回
   HTTP 500，**尽管 job 已经建成**（`state.json` 已是 `state:"working"`）。

#### 7.1.3 修复（已实施，四版；**只有第四版可用**）

**第一版（复审推翻，见 §7.1.6）**：只剥 5 个 broker 变量。它确实让 `create` 不再 500，
但把故障搬到了网关的 shell 里（rc=13），更隐蔽。**不要采用这一版。**

**第二版（复审推翻，见 §7.1.7）**：剥**整个注入面**。rc=13 消失了，但宿主的 safe-delete
护栏被一起关掉 —— 网关与它派发的每个作业都变成无监管的原生删除。**不要采用这一版。**

**第三版（复审推翻，见 §7.1.8）**：只剥 broker 绑定、保留护栏。rc=13 与「原生直删」都消失了，
但 `create` 变成**状态依赖** —— 网关每次派活都把删除记到**宿主当前回合**的账上，账一满就
500。**不要采用这一版。**

**第四版（当前）**：注入面里装的是**三件互相独立的东西**，坏在这里的是前两件。

| 装的是什么 | 含哪些 | 在这里能用吗 | 处置 |
|---|---|---|---|
| **broker 绑定** | `CODEBUDDY_SANDBOX_*`（IPC 地址 / session / tool-call / trace / program policy / file token / zsh bin）、`CODEBUDDY_BROKERED_*`、`CODEBUDDY_TOYBOX_*`、`SANDBOX_CENTER_*`、`TOYBOX_SANDBOX_SOCK`、`CODEBUDDY_BROKER_IPC_CLIENT`、`PATH` 里的 `brokered-bin` | **不能**：broker 只批准「agent 工具调用」的删除，网关不是 | 剥离 |
| **回合身份** | `CODEBUDDY_CONVERSATION_REQUEST_ID`、`CODEBUDDY_TOOL_CALL_ID` | **不能**：这是「一次删除算在哪个回合头上」的记账键，常驻守护进程没有回合 | 剥离 |
| **safe-delete 护栏** | `BASH_ENV` + `safe-bin` 包装（把 `rm`/`unlink`/`rmdir` 定义成函数）、`CODEBUDDY_SAFE_DELETE_*`、`NODE_OPTIONS`、`PYTHONPATH`、`CODEBUDDY_SESSION_ID` | **能**：broker 与回合都撤掉后它降级为「移进回收站」，正是应有的行为 | 保留 |

`_gateway_env()` 因此做三件事：按前缀删 broker 绑定（`CODEBUDDY_SANDBOX_` /
`CODEBUDDY_BROKERED_` / `CODEBUDDY_TOYBOX_` / `SANDBOX_CENTER_`）、按名单删
`TOYBOX_SANDBOX_SOCK` / `CODEBUDDY_BROKER_IPC_CLIENT` / 两个回合身份键，再从 `PATH`
摘掉 `brokered-bin` 一项。`safe-bin` 留在 `PATH` 上 —— 它只含 `rm`/`unlink`/`rmdir`，
留在裸系统工具之前是净收益。

**保留但不写进上表的还有**：`CODEBUDDY_SESSION_ID`（护栏自己的门控，见 §7.1.4）、
`CODEBUDDY_NODE_BIN`（守卫 helper 的解释器）、`CODEBUDDY_SAFE_DELETE_SANDBOX`（见 §7.1.4）、
`CODEBUDDY_SAFE_DELETE_REPORT_PATH`（删除去向的报告落点）。它们既不是 broker 绑定也不是
回合身份，保留理由是各自独立的，逐条写在 §7.1.4。

机制上为什么**必须**同时摘 `CODEBUDDY_BROKERED_BIN_DIR`：`BASH_ENV` →
`shell-runtime-bash-env.sh` 依次 source 两个脚本 —— `safe-bin/safe-delete-bash-env.sh`
（定义 `rm` 函数）与 `brokered-sandbox-bash-env.sh`（**整段被 `CODEBUDDY_TOYBOX_BIN` 与
`CODEBUDDY_BROKERED_BIN_DIR` 双非空门控**，门内 `unset -f rm` 并
`export PATH="${CODEBUDDY_BROKERED_BIN_DIR}:$PATH"`）。**这个顺序决定了一切**：只要那两个变量
还在，brokered 块就会把刚定义的护栏函数掀掉、并把 `brokered-bin` 塞回 `PATH` 首位。

**为什么必须连回合身份一起摘（第四版的关键，2026-10-05 深夜实测）**：网关每次 `create` 都要
清理 `~/.workbuddy-ai/jobs/.locks/*.candidate`，这些删除**不在 OS 临时目录下**，于是 bulk
守卫把它们记到 `CODEBUDDY_CONVERSATION_REQUEST_ID` 所指的那个回合头上 —— 也就是**宿主当前
回合**。实测每次 `create` 记**约 6 笔**（6327→6334→6340→6346），阈值 50，所以一个普通回合派
几次活就把账用穿；账一穿，守卫回 `confirmRequired`，网关自己的清理 fail-closed，`create`
返回 HTTP 500：

```text
[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]
{"count":6358,"threshold":50,"scope":"turn",
 "targets":["~/.workbuddy-ai/jobs/.locks/547a46cb.state.lock.4119.<uuid>.candidate"]}
```

更麻烦的是**它时好时坏**：能否过还取决于网关启动时继承的 `CODEBUDDY_TOOL_CALL_ID` 是否恰好
落在守卫的 `toolApprovals` 里。同一条探针的对照 —— 用未批准的 id 跑 6 次 `create`，
**6 次全 500**；用已批准的 id 跑 6 次，**6 次全成功**（同时继续给宿主回合加账）。第四版把两个
键都摘掉后，**同一条未批准 id 的探针 6 次全成功**，且宿主回合计数 **delta +0**。

摘掉是安全的，因为三个载体在缺 `CODEBUDDY_TOOL_CALL_ID` 时**一律放行**、不是 fail-closed：
`safe-bin/safe-delete-common.sh:48` 的 `|| return 0`、`node-safe-delete-shim.cjs:220` 的
`return`、`sitecustomize.py:853` 的 `return`。**回收站改道因此完全保留**；丢掉的只是「单回合
删满 50 个要弹窗确认」这道闸门，而它在后台作业里本来就弹不出来（没有人在那儿点），只会让作业
直接失败 —— 这一点由用户拍板确认（选项 1）。

**代价（显式登记）**：网关派发树内不再有 ≥50 的交互式批量确认，删除仍进回收站。已实测受影响
的是 `bash=true` 作业（它继承网关的环境，宿主不重新注入回合键）；agent 作业自身的工具调用由
宿主按次注入新键，推断不受影响 —— **这一条是推断，未实测**（作业探针跑满 3 分钟仍 `working`，
`get` 取不到输出，`~/.workbuddy-ai/logs/job-<id>.log` 为空）。

实测（2026-10-05，CLI 2.147.0）。判据必须落在**删除的去向**上：`safe_delete_rm` 对位于 OS
临时目录下的目标直接走 `$REAL_RM`（不进回收站），所以探针目标必须放在临时目录之外 ——
早先一次探针把目标放在 `/tmp`，于是每臂都得「无记录」，判据实际失效。

| 臂 | `rm` 解析 | `ls` rc | 删除去向 | 说明 |
|---|---|---|---|---|
| 完整宿主环境 | `…/shim/brokered-bin/rm` | 0 | broker 批准 | 护栏由 broker 兜住 |
| 第一版（只剥 5 个变量） | `…/shim/brokered-bin/rm` | **13** | — | §7.1.6 |
| 第二版（整类剥离） | `/bin/rm` | 0 | **原生直删** | §7.1.7 |
| 第三版（只剥 broker，已推翻） | `rm`（**函数** → `safe-bin`） | 0 | **回收站** | 删除去向正确，但 create 状态依赖，§7.1.8 |
| **第四版（当前）** | `rm`（**函数** → `safe-bin`） | 0 | **回收站** | 本版；去向与第三版同形，差别只在回合预算 |
| 负控：无 shim | `/bin/rm` | 0 | 原生直删 | 证明「进回收站」由 shim 造成 |
| 负控：第四版但去掉 `CODEBUDDY_SESSION_ID` | `rm`（函数） | 0 | 原生直删 | 确定性负控，不依赖时序 |

表里「完整宿主环境」一臂的 `ls` rc **取决于宿主此刻有没有把 broker 环境注入本进程**：作为
脱离活跃工具调用的常驻进程时，这一臂会得到 rc=13。它**不是**稳定可复现的臂，所以判据不建立在
它上面 —— 可复现的 rc=13 是「只剥 broker 变量、`brokered-bin` 仍在 `PATH` 上」那一行。

同一三分对照在 node（`fs.rmSync`）与 python（`os.remove`）上形状一致：第二版两侧护栏全丢，
第三版两侧都恢复、与宿主基线一致。读 / 写 / 删三类操作在三版下都实测过，**没有 fail-closed**：
`CODEBUDDY_SAFE_DELETE_SANDBOX=1` 保留下来会让 node shim 与 `sitecustomize.py` 仍去加载
brokered-fs 钩子，但该钩子拿不到 socket 时保持惰性（读 `readFileSync`/`readdirSync`、
写 `writeFileSync`/`open(w)`、删 `rmSync`/`os.remove` 全部 rc=0，与宿主基线同形）。

`create` 本身不 shell out，所以三臂的 `create` 都返回 200 —— **这正是第一、二版能蒙过
`create` 验收的原因**。真正的判据是 job 日志（`~/.workbuddy-ai/logs/exec-<job>.log`）与
删除去向，不是 `create` 的 HTTP 码。

验证：
- `tests/test_workbuddy_jobs_mcp.py::GatewayEnvironmentTests`（12 个用例）：
  - `test_the_fixture_shim_reproduces_the_real_wiring`：**不跳过**。用夹具复刻宿主的注入顺序，
    断言三种环境给出三种不同印记 —— 宿主 → `brokered`、第四版 → `safe-delete`、整类剥离 →
    空。夹具先自证能复现 broker 抢占，否则后面的断言没有意义；
  - `test_a_delete_still_routes_to_the_trash`：**不跳过，且夹具驱动**（2026-10-05 深夜改）。
    早先它跑宿主的真 `safe-delete-common.sh` 并读真实 `os.environ`，结果是**两个假绿叠在一起**：
    在宿主没注入的进程里 `_gateway_env()` 是恒等函数，没有 `BASH_ENV` 与 `safe-bin`，`rm` 就是
    `/bin/rm`，回收站断言悬空；而在宿主注入的进程里它又随回合内的 bulk 计数漂移，回合账一满就
    `SAFE_DELETE_BULK_CONFIRM_REQUIRED`、文件没删掉、用例失败。现在的夹具 `safe-bin/rm` 复刻
    真契约可观测的那一半（把目标交给回收站替身 + 写 `{"operation":"trash"}` 报告行），因此判据
    落在**删除走了哪条路**上，既不碰宿主也不碰回合账。**宿主环境与 `env -i` 干净环境实测同形**
    （宿主 50 run / 0 skipped 全绿；`env -i` 干净环境 50 run、2 条依赖宿主接线的按设计跳过，
    本条不在其中）；
  - `test_the_turn_identity_is_not_inherited`：钉住第四版新增的那一条 —— 两个回合键不得出现在
    网关环境里，且先断言夹具里确实带有它们（守门的门）。**期望集钉死在测试里，不从
    `TURN_IDENTITY_ENV_VARS` 取**：遍历生产常量自己的循环，在常量被置空时循环体一次都不跑，
    会空转变绿（实测：把常量置成空元组，50 个用例一个不红）。同端到端用例亦钉同一期望集；
  - `test_the_session_id_survives_so_the_guardrail_stays_armed`：`CODEBUDDY_SESSION_ID` 必须留下。
    它和回合键同前缀，极易被顺手摘掉，而 `safe-bin/rm` 在没有 session id 时直接透传真删；
  - `test_a_shell_under_the_gateway_environment_really_works`：真实宿主环境下跑
    `bash -c 'command -v ls; ls /tmp; echo rc=$?'` 与 `command -v rm; type -t rm`，
    断言 rc=0、`ls` 不经 `brokered-bin`、`rm` 仍落在护栏上（函数或 `safe-bin`）；
  - `test_the_probe_sees_the_half_fix`：用第一版的环境跑同一个探针，断言它**失败** ——
    没有这条，探针只是个永远绿的空壳。宿主沙箱未激活时跳过（拿不到 program policy 变量）；
  - `test_the_sandbox_flag_is_kept_and_is_not_the_guardrail`：`CODEBUDDY_SAFE_DELETE_SANDBOX`
    名字像护栏、实际只喂 brokered-fs 钩子（§7.1.4）。本条钉住「它保留」并写明它真正拨动的是
    哪个开关 —— 顺带把早先那条站不住的保留理由换成实测理由；
  - 其余为环境形状断言：broker 绑定全删、护栏载体全留、`PATH` 只摘 `brokered-bin`、
    `test_the_started_gateway_really_cannot_see_the_broker` 端到端复核同一套规则。
- **作业内删除去向（端到端，第四版实测）**：派一个 bash 作业，在它自己的 shell 里打印环境并
  删一个**临时目录之外**的文件：

  | 观测 | 值 |
  |---|---|
  | `CODEBUDDY_TOOL_CALL_ID` | `unset`（回合身份已摘） |
  | `CODEBUDDY_CONVERSATION_REQUEST_ID` | `unset` |
  | `CODEBUDDY_SESSION_ID` | `e3b2bc3d-…`（保留，护栏因此保持武装） |
  | `command -v rm` | `…/shim/safe-bin/rm` |
  | `rm` 返回码 / 目标文件 | `0` / 已删除 |
  | 报告文件里的 `trash` 记录 | **10 条** —— 删除确实进了回收站，不是原生直删 |
  | 宿主回合计数 delta | **+0**（第三版是每次 `create` +约 6） |
- 真实 CLI 端到端：`create` 0.24 秒返回、无 500、`.guard` 残留 47 → 47 不增加；
  job `b593da65` 的日志三行：`shell-ok` / `/bin/ls` / `inner-rc=0`。
- **匹配对端到端**（同一个作业、同一个网关二进制，只换网关启动时的环境）—— 最强的一条：

  | 臂 | job | `rm` 解析 | `inner-rc` |
  |---|---|---|---|
  | 第三版（收窄） | `137592ac` | `…/shim/safe-bin/rm` | 0 |
  | 第二版（整类剥离） | `ddac4103` | **`/bin/rm`** | 0 |

  两版都修好了 rc=13，但只有第三版把删除留在护栏上。反事实因此落在**原 bug 被测量的
  那个层级**，而不是助手函数的层级。

#### 7.1.4 仍未解释 / 未覆盖（诚实边界）

- **间歇性的来源未定**：19:27–19:49 共 7/7 失败，19:50 之后 23/23 成功。宿主的 broker 环境
  **不是每次都注入**（端到端复跑时父进程就没有这些变量；本仓库的 `Bash` 工具也会在「沙箱被
  绕过」时不注入）。为什么状态在 19:49→19:50 之间翻转，**未查明**。修复不依赖解释这一点 ——
  它让 create 在**两种**状态下都能成功。
- **§5 的额度撞线（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`）已由第四版处理**：它**不是**另一条
  路径，而是同一条 —— 网关继承的回合身份让它的清理记在**宿主回合**头上（见 §7.1.3）。「换回合
  绕过」只能绕过一次，因为下一个回合派几次活同样会把账用穿。第四版摘掉回合身份后：未批准 id
  的探针 6 次 `create` 全成功、宿主回合计数 delta +0。
- **brokered-fs 钩子不再生效，这是不可避免的降级**：`CODEBUDDY_BROKERED_FS_HOOK_ENABLED`
  被剥掉、broker socket 也没了，所以网关与它派发的作业**不再经 broker 审批文件写入**。
  这正是被修掉的那条通道，无法两全；但它不同于「关掉护栏」—— 删除仍走回收站。写路径实测
  在三版下都正常，没有 fail-closed。
- **`CODEBUDDY_SAFE_DELETE_SANDBOX=1` 保留（理由已按实测改写）**：全 shim 树里这个名字出现在
  三处 —— `node-brokered-fs-shim.cjs:22`、`node-language-shim.cjs:24` 与 `sitecustomize.py:34`
  —— 且三处都只喂
  `_BROKERED_FS_HOOK_ENABLED`，**不**参与 safe-delete（护栏由 `CODEBUDDY_SAFE_DELETE_ENABLED`
  门控）。socket 已随 broker 绑定一起撤掉，所以它在这里是惰性的，留与不留实测无差别。保留是
  「不按名字做推断」的默认选择；早先那条「剥掉它等于替宿主断言不在沙箱里」的理由实测站不住，
  已作废，不再作为依据。
- **测试负控 `_whole_class_strip` 只适用于 shell 臂**：它剥 `BASH_ENV` 与 shim 的 PATH 项，
  但**不动** `NODE_OPTIONS` / `PYTHONPATH`。拿它当 node / python 的负控会得到「仍进回收站」的
  误导结果 —— 语言 shim 还在，删除照样走护栏。node / python 臂若要负控，必须连这两个一起剥。
- **护栏本身由 `CODEBUDDY_SESSION_ID` 门控，本修复不改变这一点**：`safe-bin/rm` 的开头是
  `[ -z "$CODEBUDDY_SESSION_ID" ] && [ -z "$CLAUDE_SESSION_ID" ] && exec "$REAL_RM" "$@"`，
  即**没有 session id 就直接透传真删**。宿主注入它、本修复保留它，所以网关里护栏生效；
  但宿主不注入时（本仓库的 `Bash` 工具在「沙箱被绕过」时就不注入）网关也拿不到护栏。
  这是宿主既有语义，不是本修复引入的缺口 —— 负控臂「第三版去掉 `CODEBUDDY_SESSION_ID`」
  正是为了把这条边界钉成可复现的事实，而不是留成推断。
- **P3 残留边界（不阻塞合并，逐条登记）**：
  1. 护栏由 `CODEBUDDY_SESSION_ID` **单点门控**：宿主不注入它时（本仓库的 `Bash` 工具在
     「沙箱被绕过」时就不注入）网关也拿不到护栏。宿主既有语义，非本修复引入。
  2. 网关会继承 `CODEBUDDY_GATEWAY_PASSWORD` 与 `CODEBUDDY_MCP_CONFIG`（内含 Bearer 与 JWT）。
     本修复不处理凭据面，另立跟踪。
  3. **zsh 作业壳未覆盖**：`BASH_ENV` 只对 bash 生效，而宿主给的会话壳是
     `cli/vendor/zsh-macos/bin/zsh`，其删除走 `brokered-bin` 的 PATH 项而不是护栏函数。
     本表所有 shell 判据都是 `bash -c` 下测的。
  4. 「作业内删除是否进回收站」目前是**手工探针**证据（见 §7.1.3 末表），不是自动化测试 ——
     它需要真 CLI 与网络，做不成 hermetic 用例。
  5. **CLI bundle 里的引用未逐处核对**：本节的「三处」「一处消费者」等计数都是对
     `cli/vendor/shim` 树做的；`cli/dist/*.js` / `*.mjs` 打包产物里同名变量另有若干处引用，
     **未逐处核对**。bundle 是单行压缩代码，按行计数只报 1 行、无意义，所以 bundle 侧的
     出现次数**没有数字**（既不写「三处」也不写「不止三处」）。判据只覆盖 shim 树，
     bundle 里的读取者可能改变结论。
  6. **宿主自己有一套更大的「请求上下文」擦除集合，本修复只覆盖其中一部分**：CLI bundle 里
     有 `scrubRequestContextFromEnv`，它擦掉 `TRACEPARENT` / `TRACESTATE` / `BAGGAGE` /
     `CODEBUDDY_CONVERSATION_REQUEST_ID` / `CODEBUDDY_CONVERSATION_MESSAGE_ID` /
     `CODEBUDDY_TOOL_CALL_ID` / `CODEBUDDY_SANDBOX_BROKER_TOOL_CALL_ID` /
     `CODEBUDDY_SANDBOX_BROKER_TRACE_ID`。本修复与它的交集是我们要摘的两个回合键；另外三个
     `CODEBUDDY_SANDBOX_BROKER_*` 已被前缀规则顺带摘掉。**未摘的是**
     `CODEBUDDY_CONVERSATION_MESSAGE_ID` 与三个 trace 变量 —— 经查它们在 shim 树里**零消费者**
     （`CODEBUDDY_CONVERSATION_MESSAGE_ID`、`TRACEPARENT` 在 `cli/vendor/shim` 下 grep 无命中），
     不参与删除守卫，因此不影响本修复的结论；但「本修复的集合等于宿主的规范集合」**不成立**，
     这是登记在案的差异而非已验证的等价。
- **§7.1.3 补一条独立佐证**：上面第 6 条那个宿主自带的擦除函数，是**不依赖本仓库任何推理**的
  旁证 —— 宿主在派生长驻 / 嵌套进程时，自己也要擦掉 `CODEBUDDY_CONVERSATION_REQUEST_ID` 与
  `CODEBUDDY_TOOL_CALL_ID`。本修复摘的正是这两个，方向与宿主既有语义一致。
- **本次只修网关的环境继承**：宿主「必须能自动化操控可见任务窗口」的发布门禁口径不在本条范围，
  另见 `product-spec.json` 的 `in-session-sdd-topology` 节点。

#### 7.1.5 对上一版五条判据的处置

原判据 1 / 2 / 4 / 5 仍然成立（拒绝方确实是宿主 broker、不是沙箱、不是额度）。
**判据 3 被证伪**：它说「没有 vibeguide 侧回归可指认」——就**代码回归**而言是对的
（`workbuddy_jobs.py` 确实只有一个提交），但它被用来支撑「所以不修」，这一步不成立。
**没有回归 ≠ 没有可修点**：vibe 改不了宿主的 broker，但**可以不让自己的子进程继承那套接线**。

#### 7.1.6 复审推翻：第一版把 500 换成了 rc=13 的坏 shell（2026-10-05 晚，PR #158）

第一版修复在合并前被独立复审拦下，P0 成立：**它没有修好，只是换了个故障点，而且更隐蔽。**

第一版看起来对，是因为三件事同时成立：

1. 根因链条（§7.1.2）本身没错，剥掉 broker 变量确实让 `create` 不再被拒；
2. `create` 不 shell out，所以复跑验收时它照样返回 200 —— **验收指标选错了**；
3. 当时的 4 个测试只断言「名字被删掉」，而第一版恰好把名字删对了。

由此得到三条判据（已写进 skill）：

- **判据要落在被测对象真的会做的事上**：网关的核心用途是跑 shell 与派发子任务，
  所以判据必须是「网关里的一条 shell 命令能不能跑通」，不是「`create` 返回几」。
- **fail-closed 的组件被「删一半」比不删更糟**：shim 拿不到 broker 就拒绝一切，
  而被删掉的那一半正好是它赖以放行的部分。
- **载体要清点，不要列名字**：注入面有 20+ 个变量和 3 条 `PATH`/`NODE_OPTIONS`/`PYTHONPATH`
  通道，手写名单必然漏。

以及一条**当时推出、随后被 §7.1.7 证伪**的：

- ~~**这类修复必须整类剥离或完全不动**~~。它从上一轮的错误里推得太远：真正要剥的是
  **那一半依赖 broker 的接线**，不是整张注入面。把判据写成「整类剥离」恰好制造了
  下一个更严重的错误。

#### 7.1.6.1 由 §7.1.6 得到的判据，逐条复核

| 判据 | 现状 |
|---|---|
| 判据要落在被测对象真会做的事上 | **成立**。第三版的两条功能判据都落在「删除的去向」与「shell 能否跑通」上 |
| fail-closed 组件不能删一半 | **成立但表述不准**：应为「不能只删它赖以**放行**的那一半」。broker 变量正是放行通道，只删它才更糟 |
| 载体要清点，不要列名字 | **成立**。第三版的两条夹具判据都不依赖手写名单，夹具自证能复现宿主接线 |
| 必须整类剥离或完全不动 | **已证伪**，见 §7.1.7 |

#### 7.1.7 复审二次推翻：第二版把宿主的 safe-delete 护栏一起关掉了（2026-10-05 深夜，PR #158）

第二版（整类剥离）在合并前被第二轮独立复审拦下，P0 成立，但**性质与第一版相反**：
不是「没修好」，是**修过头了**。

`_gateway_env()` 不只摘掉 broker 变量，而是把**整套护栏**一起摘掉 —— 回收站路由、程序黑名单、
brokered-fs 钩子、批量删除守卫全没了。结果是：网关**与它派发出去的自主作业**都变成
「无监管的原生删除」。第一版至少还留着护栏，第二版连护栏都没了，而这个网关的用途正是
派发自主作业。

独立复现（同一探针，目标放在临时目录之外，三臂对照）：

| 臂 | `PATH` 首位 | `rm` 解析 | `rm` 类型 | `ls` rc |
|---|---|---|---|---|
| 完整宿主环境 | `…/shim/brokered-bin` | `…/shim/brokered-bin/rm` | file | 0 |
| 第二版（整类剥离） | node bin | **`/bin/rm`** | file | 0 |
| 第三版（收窄） | `…/shim/safe-bin` | `rm` | **function** | 0 |

关键：**第三版同样拿到 `ls` rc=0** —— 也就是说收窄版与整类剥离一样解决了原来的 rc=13，
但多了回收站路由。可见「必须整类剥离」这条判据是多余的代价。

由此修正出的判据（已写进 skill）：

- **先分清注入面里哪一半坏了**：一张注入面常常同时装着「坏掉的传输」与「独立的护栏」。
  修复的单位是**坏掉的那一半**，不是整张面。判据是「拿掉之后护栏还在不在」，不是「名字删干净没有」。
- **护栏的可观测性要单独设计**：删除的「去向」不会写在退出码里 —— 原生直删与移进回收站
  都返回 0。判据必须落到**去向**（回收站报告 / 替身二进制是否被调用），否则「rc=0 即通过」
  会把关掉护栏的版本判成绿的。
- **负控必须是确定性的**：靠「宿主此刻恰好注入/不注入」的对照会随环境漂移。可靠的负控是
  「同一环境只改一个开关」（如去掉 `CODEBUDDY_SESSION_ID`），它必须稳定地给出相反结果。

#### 7.1.8 复审三次推翻：第三版把「create 不再 500」建在了状态依赖上（2026-10-05 深夜，PR #158）

第三轮独立复审的总判是**不能 squash merge**。与前一版不同，**生产代码 `_gateway_env()` 本身
被认可**（剥离规则、保留规则、`PATH` 处理都没问题），两个 P0 分别落在**测试的 hermetic 性**与
**验收口径的完整性**上。开发者逐条独立复现，结论全部成立。

**P0-1 —— `test_a_delete_still_routes_to_the_trash` 是假绿。** 它读真实 `os.environ`：

- 在宿主**没有**注入的进程里，`_gateway_env()` 是恒等函数 —— 没有 `BASH_ENV`、没有 `safe-bin`，
  `rm` 就是 `/bin/rm`，于是「进回收站」的断言悬空，而在干净环境跑是 **FAIL 而不是 skip**；
  skip 守卫只检查 `_HOST_SHIM/safe-bin/rm` 这个文件在不在，本机装了 WorkBuddy 就在，
  **没有**检查宿主是否真的把接线注入了这个进程。
- 在宿主**有**注入的进程里，它又随回合内的 bulk 计数漂移：回合账一满，删除被
  `SAFE_DELETE_BULK_CONFIRM_REQUIRED` 拦下，文件没删掉，用例 **FAIL**。全量套件实测正是这一条。

两条加起来的意思是：**同一个用例，在两台机器上、或同一台机器的两个时刻，会因为与被测代码
无关的原因改变结论。**

**P0-2 —— `create` 的状态依赖。** 见 §7.1.3 的实测：网关每次 `create` 给宿主回合账加约 6 笔，
账一满就 500；能否过还取决于继承的 `CODEBUDDY_TOOL_CALL_ID` 是否落在 `toolApprovals` 里。
所以 PR 那句「create 不再 500」**不成立**。

**返工（第四版）**：摘掉回合身份（`CODEBUDDY_CONVERSATION_REQUEST_ID` /
`CODEBUDDY_TOOL_CALL_ID`）；测试改夹具驱动。两项都已验证，见 §7.1.3。

由此修正出的判据（已写进 skill）：

- **「成功」与「去向」必须分开观测，且两者都要有确定性负控**：`rm` 返回 0 只能证明删除发生了，
  证明不了它去了哪。第三版的判据只到「成功」这一层，于是把「关掉护栏」也判成绿。
- **依赖宿主环境事实的用例，要么夹具化，要么显式自证前置**：`skipTest` 检查「文件在不在」不是
  自证 —— 它检查的是**安装**，不是**注入**。判据是「宿主接线是否真的在这个进程的 `os.environ`
  里」。拿不到就做夹具，不要用 skip 掩盖。
- **验收口径要对齐最坏可复现状态，不要对齐幸运状态**：`create` 在「继承的 call id 恰好被批准」
  时全绿，这不是修好了，是躲过了。派复审时要把这类**环境事实**写进材料 ——
  review 门只校验形状与自报，查不出「这个结论依赖哪个未写明的环境前提」。

> 后两条与知识库里两条 Global 条目同型：
> `desensitization/degradation-path-false-green.md`（只断言产物形态分不清「主路径成功」与
> 「兜底顶上」）与 `vibe-guide/review-gates-structural-blindspot.md`（review 门不校验环境事实，
> review 上限由派发者的事实储备决定）。它们不是本次新发现的规律，但这次是第二个独立实例。



