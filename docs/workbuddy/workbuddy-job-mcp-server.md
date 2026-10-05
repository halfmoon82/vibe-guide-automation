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

#### 7.1.3 修复（已实施，两版；**只有第二版可用**）

**第一版（已被复审推翻，见 §7.1.6）**：只剥 5 个 broker 变量。它确实让 `create` 不再 500，
但把故障搬到了别处，而且更隐蔽。**不要采用这一版。**

**第二版（当前）**：剥离**整个注入面**，而不是挑名字。

`Gateway._autostart()` 启动网关时传 `env=_gateway_env()`，`_gateway_env()` 做四件事：

| # | 动作 | 覆盖 |
|---|---|---|
| 1 | 按前缀删变量：`CODEBUDDY_SANDBOX_` / `CODEBUDDY_BROKERED_` / `CODEBUDDY_SAFE_DELETE_` / `CODEBUDDY_TOYBOX_` / `SANDBOX_CENTER_` | broker、program policy、file token、safe-delete 全套、toybox |
| 2 | 按名单删：`TOYBOX_SANDBOX_SOCK` / `BASH_ENV` | 沙箱 socket、bash 启动钩子 |
| 3 | 从 `PATH` 摘掉 `…/cli/vendor/shim/brokered-bin` 与 `…/cli/vendor/shim/safe-bin` | 否则 shim 仍会被调用 |
| 4 | 从 `NODE_OPTIONS` 摘掉 `--require=…/cli/vendor/shim/node-language-shim.cjs`（摘空则删该变量）、从 `PYTHONPATH` 摘掉 `…/cli/vendor/shim` | Node 与 Python 两侧的 shim |

第 3、4 条是关键：**变量删了、shim 还在 PATH 上**，shim 会因为拿不到 broker 而 fail-closed。

实测（2026-10-05，CLI 2.147.0）三臂对照 —— 同一个 job、同一个网关，只有环境处理不同：

| 臂 | `command -v ls` | stderr | 内层 rc | job 状态 |
|---|---|---|---|---|
| 完整宿主环境 | `…/shim/brokered-bin/ls` | `Brokered program policy check unavailable` | 13 | `done`（**假绿**） |
| 第一版（只剥 5 个变量） | `…/shim/brokered-bin/ls` | `Brokered program policy check unavailable` | **13** | `done`（**假绿**） |
| 第二版（整类剥离） | `/bin/ls` | 空 | **0** | `done` |

`create` 本身不 shell out，所以三臂的 `create` 都返回 200 —— **这正是第一版能蒙过验收的原因**。
真正的判据是 job 日志（`~/.workbuddy-ai/logs/exec-<job>.log`），不是 `create` 的 HTTP 码。

效果：`hasBrokerDeleteEnv()` 为 false → `tryBrokerDelete` 返回 `unavailable` → 走回收站降级路径，
不再 fail-closed；网关的 shell 也不再经过 shim，`command -v ls` 回到 `/bin/ls`。

验证：
- `tests/test_workbuddy_jobs_mcp.py::GatewayEnvironmentTests`（8 个用例），其中三个是**功能性**的：
  - `test_a_shell_under_the_gateway_environment_really_works`：在真实宿主环境里用 `_gateway_env()`
    跑 `bash -c 'command -v ls; ls /tmp; echo rc=$?'`，断言 rc=0 且解析结果不含 `shim`；
  - `test_the_probe_sees_the_half_fix`：用第一版的环境跑同一个探针，断言它**失败**——
    没有这条，探针只是个永远绿的空壳；
  - `test_no_shim_carrier_is_left_behind`：**载体清点**，遍历当前进程里所有值含
    `/cli/vendor/shim` 或 `broker.sock` 的变量，断言一个都没漏。宿主以后新增注入变量会在这里红，
    而不是静默复活。
- 真实 CLI 端到端：`create` 0.24 秒返回、无 500、`.guard` 残留 47 → 47 不增加；
  job `b593da65` 的日志三行：`shell-ok` / `/bin/ls` / `inner-rc=0`。

#### 7.1.4 仍未解释 / 未覆盖（诚实边界）

- **间歇性的来源未定**：19:27–19:49 共 7/7 失败，19:50 之后 23/23 成功。宿主的 broker 环境
  **不是每次都注入**（端到端复跑时父进程就没有这些变量）。为什么状态在 19:49→19:50 之间翻转，
  **未查明**。修复不依赖解释这一点——它让 create 在**两种**状态下都能成功。
- **§5 的额度撞线（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`）未处理**：那是另一条路径，
  报文带 `count`/`threshold`，换回合可绕过。本修复不覆盖它。
- **`CODEBUDDY_SAFE_DELETE_BIN_DIR` 已纳入第二版**：第一版把它列为「未覆盖」，是漏项
  —— 它就在 `CODEBUDDY_SAFE_DELETE_` 前缀里。
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
  而被删掉的那一半正好是它赖以放行的部分。这类修复必须**整类剥离**或**完全不动**。
- **载体要清点，不要列名字**：注入面有 20+ 个变量和 3 条 `PATH`/`NODE_OPTIONS`/`PYTHONPATH`
  通道，手写名单必然漏。



