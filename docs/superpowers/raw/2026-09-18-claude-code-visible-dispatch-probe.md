# Claude Code 可见任务派发：真实实测记录

日期：2026-09-18　　执行者：Claude Code 桌面会话（本机）

实测时基底是 main `5a7ccbe`；本文提交在 `3ea0f0e`（#42）之上。两者之间本文引用的
五个文件——`provider_action.py`、`models.py`、`node_spec.py`、`cli.py`、
`adapters/task_provider.py`——逐字节相同，所以下面的证据不受影响。

这份记录写的是**实际发生过的一次派发**，不是设计意图。fake 信箱服务的端到端测试
（`tests/test_pm_path_end_to_end.py`）已经绿了很久，但它证明不了桌面工具的真实行为，
所以有了这一次。

> `docs/superpowers/raw/` 放的是实测原始记录：只写观察到的事实与当场的判断，
> 后续会话可以引用。判断事后被推翻时，**在原处标明并保留原判断**（见下面的
> "信箱回写"一节），不要改写成好像当时就对了——错判本身是有价值的证据。

## 走通的链路

探针项目（临时目录，`vibe init --confirm` 起）：

```
attest --adapter claude-code  →  plan --from-prd（复杂路由）
  →  authorize --authorize AUTHORIZE  →  monitor --authorize AUTHORIZE
  →  信箱出现两个 pending create 请求（两个并行节点各一个）
  →  按合同建 worktree  →  调 ccd_session__spawn_task 创建可见会话
  →  会话在自己的分支上提交  →  complete() 回写绑定（键名用错，被拒，见下）
```

三个操作用真实桌面工具跑过，另两个未单独验证。工具名与 `NATIVE_TOOL_MAP` 登记的一致：

| 操作 | 工具 | 实测结果 |
|---|---|---|
| `create` | `ccd_session__spawn_task` | 会话创建成功，但**行为与契约设想不同**，见下 |
| `locate` | `ccd_window__open_session_in` | 未单独验证（会话已在前台） |
| `visibility` | `ccd_session_mgmt__get_session` | 返回 `cwd`/`isRunning`/`model`，可用于可见性判定 |
| `resume` | `ccd_session_mgmt__send_message` | 未单独验证（本次任务一轮完成） |
| `wait` | `ccd_session_mgmt__list_events` | 返回事件流，cursor 是 `before_uuid`（形如 `c_a076d1ef…`） |

## 与契约设想不符的一处：`create` 是建议式的

阶段 5.5 的计划把 Claude Code 的 create 绑定字段写成 `sessionId` + `hostId`，
并假设 `create` 调用后直接返回它。两处都不成立：

- **代码从来不接受 `sessionId`**。`runners/provider_action.py:509-510` 读的是
  `threadId` 或 `task_id`、`hostId` 或 `host`；全包搜不到 `sessionId`。
- **`create` 也不同步返回绑定**：`ccd_session__spawn_task` **只提议一个任务**，在用户界面显示一张卡片，
由用户点击才真正创建会话。调用方当场拿不到会话身份，返回的是一个 `task_id`
（形如 `task_1b72b080`）加一句"chip is showing for the user"。

本次是用户点了卡片之后，再用 `ccd_session_mgmt__list_sessions` 查到真实会话 id 的。
也就是说，`create` 在真实环境里是**两步、且中间需要人**：

1. 调 `spawn_task` → 得到 `task_id`，用户看到卡片
2. 用户点击 → 会话创建 → 需要另一次查询才能拿到会话身份

（桌面工具自己把这个字段叫 `sessionId`——那是 ccd 侧的名字。回写给 vibe 时
必须换成 `task_id` 或 `threadId`，见下面"信箱回写"一节。混用这两套名字正是
本次实测判错的原因。）

**影响**：任何假设 `create` 同步返回绑定的实现都会在这里断。监工要么等用户点，
要么把 `task_id → 真实会话 id` 的解析做成一次独立的查询步骤。这也意味着
"全自动派发"在当前 ccd 工具下做不到——`create` 天然带一个人工确认点。

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
| `visibility` | `claude agents --json` | 返回 `sessionId` / `state` / `cwd`，见下 |
| `resume` | `claude --bg --resume <id>` | 同 id 后台续接；会话已在运行时起副本 |
| `wait` | `claude logs <id>` | 读输出 |

两处按实测订正（`claude agents --json --all`，本机只有一个**已停止**的后台会话，
running 的后台会话是否改报别的字段没验）：

- 后台条目的字段是 `state`（这次值为 `stopped`），**不是** `status`；只有
  `kind: "interactive"` 的条目才有 `status`。监工按 `status` 判可见性会读到
  `undefined`。
- 后台条目同时带一个短 `id`（`4f31def4`）和完整的 `sessionId`，两者不同。
  `attach` / `logs` / `stop` / `rm` 收的是**短 id**。
- `--bg` 条目（`claude --help:32-39`，不是 `-r, --resume` 自己那条）写明：配
  `--resume <id>` 时，会话**已在运行**就「starts a copy and says so」。监工靠
  这条续接、重复调用时会拿到一个副本，不是同一个会话。

额外好处：`--allowedTools` 与 `--permission-mode` 可以把授权卡的 allowlist
**翻译成进程级权限约束**，而不是靠被派发会话自觉遵守合同。这比 `spawn_task`
的信任模型强。

**一个真实障碍**：实测 `claude --bg` 起的会话报 `Not logged in · Please run /login`，
没干活——它没有继承桌面应用的 OAuth。

CLI 里确实存在"只认 API key"的路径：`claude --help` 的 **`--bare`** 一条写明
「Anthropic auth is strictly ANTHROPIC_API_KEY or apiKeyHelper via --settings
(OAuth and keychain are never read)」。但那句属于 `--bare`，不是 `--bg`——
`--bg` 的条目通篇没提认证。

同机再查了一层：`claude auth status` 回 `{"loggedIn": false, "authMethod": "none"}`，
`~/.claude/.credentials.json` 不存在，钥匙串里也没有对应条目——**CLI 侧从未单独
登录过**。所以 `Not logged in` 更可能是这个，而不是"`--bg` 拒绝继承桌面认证"。
另有两条路：`claude --help:293-294` 的 `setup-token`（条目自注 requires Claude
subscription），以及 `claude auth --help` 里的 `login`（主 `--help` 只写到
`auth  Manage authentication` 这一层）。**两条都没实测**（登录会改动本机认证
状态，超出这次只读探针的范围）。因此这里不下"必须另配 API key、要另计费"的结论——
`setup-token` 那句话本身就说明存在走订阅的路径。

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

各自一棵树、一个分支，无一落在项目根或 `main`。#40 之前两者都是 `.` 和 `main`，
来源是 `node_spec.complete_node_contracts` 里的
`contract.get("worktree", ".")` / `contract.get("branch", "main")`
（见 `git show 675697b^:vibe_guide/node_spec.py`）——产品 spec 按设计不含工程字段，
所以每个节点都吃这两个默认值。

被派发的会话实际落点，与合同逐字一致：

```
会话 id:   local_c3f86730-431e-4745-993c-e6ff65a97cdb
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

## 信箱回写：这一步当时判错了

本次回写用的是计划里写的键名：

```python
store.complete(action_id, {"binding": {"sessionId": "local_c3f86730-…",
                                       "hostId": "<主机名>"}})
```

当时看到 pending 从 2 降到 1、`resume` 返回 `retry_pending`，就判成"正确地在等
第二个节点"。**这个判断是错的**：`retry_pending` 同样是 create 被拒时的状态，
而 `pending()` 只数没有结果文件的请求（`task_provider.py:394-401`），
降到 1 只说明有一个请求拿到了结果，不说明结果被接受。

事后用同一条链路只换回写键名跑了三次，差别很清楚：

```
sessionId + hostId   resume=retry_pending   已发出的操作=['create']
task_id   + host     resume=retry_pending   已发出的操作=['create', 'locate']
threadId  + hostId   resume=retry_pending   已发出的操作=['create', 'locate']
```

（上表是"两个请求只服务其中一个"这个配置下的结果，按表复现时要保持这个前提。）

`sessionId` 那次**从未发出 `locate`**——绑定被丢掉了，链路停在 create 没有前进。
另两种键名下 create 被接受、`locate` 随即发出。

**顶层 `status` 三种情况下完全相同，但节点状态分得出来**：

```
sessionId, 服务 1/2   顶层=retry_pending    节点={date-range-filter: running, export-button: blocked_unknown}
task_id,   服务 1/2   顶层=retry_pending    节点={date-range-filter: running, export-button: running}
sessionId, 服务 2/2   顶层=blocked_unknown  节点={两个都 blocked_unknown}
```

顶层之所以掩盖它，是 `cli.py:620-627`：只要存在任一"有 `retryable_action`、
状态 `running`、且没有 `active_task`"的节点，顶层就被改写成 `retry_pending`，
run 级真实状态被盖掉。所以判断回写有没有被接受，**看 `payload["nodes"]` 里
你刚服务的那个节点**，比数已发出的操作直接：被拒是 `blocked_unknown`、被接受
是 `running`。注意**没被服务的节点同样显示 `running`**，与"被接受"同形，不能
拿它判定——上表里 `export-button` 不是天生被阻的那个，它只是被服务的那个
（反过来服务 `date-range-filter` 就镜像成它 `blocked_unknown`、另一个
`running`）。两个请求都用错键名时顶层也会变 `blocked_unknown`，不再掩盖。

**结论**：本次实测走通的是 create 请求的派发与会话真实落点（上面"节点隔离在真实派发里的验证"一节的证据仍然成立），
**没有**走通 create 的绑定回写。回写时任务身份用 `threadId` 或 `task_id`、
主机用 `hostId` 或 `host`——`509-510` 两行各是一个 `or`，所以四种组合都通
（实测了其中三种）。

用正确键名重跑，逐轮服务信箱（两个节点各一份请求），链路会一路推进：

```
round 0: 服务 create×2      → resume=retry_pending  已发出=[create, locate]
round 1: 服务 locate×2      → resume=retry_pending  已发出=[create, locate, visibility]
round 2: 服务 visibility×2  → resume=running        信箱清空
```

`retry_pending` 在前两轮出现是对的——它表示还有没服务完的请求；`visibility` 服务完
（`{"visible": true, "direct_enter": true}`）之后 run 进入 `running`。
这条序列是用 fake 回写值跑的，证明的是**回写契约与推进逻辑**；真实会话落点由上面"节点隔离在真实派发里的验证"一节证明。

## 被派发会话报告的两个真实问题

这两条不是链路问题，但会影响真实项目，记在这里备查：

1. **git 身份**：这台机器没配全局 `user.name`/`user.email`，提交作者被 git 从
   本机主机名推导成 `<用户名> <用户名@主机名.local>`。探针项目无所谓，真实项目里
   作者信息会不对。
2. **commit message 的附加行**：被派发会话按其会话规范给提交附了
   `Co-Authored-By` 行。如果监工要逐字比对 commit message，这一行会算作差异。

## 尚未验证

- `locate`（`open_session_in`）与 `resume`（`send_message`）没有单独跑过
- create 的绑定回写已用正确键名与 **fake 身份**走通（见"信箱回写"一节）；
  用**真实会话身份**回写仍待实测
- `claude --bg` 在 CLI 单独登录（`auth login` 或 `setup-token`）之后能否用桌面
  订阅的额度跑起来。本次只确认了 CLI 侧当前无任何自有凭据、`--bg` 那次报
  `Not logged in`；两条登录路径都没走（会改动本机认证状态）
- `claude agents --json` 对**正在运行**的后台会话报什么字段（本机只观察到一个
  已停止的，它报 `state: "stopped"`）
- 多节点并发派发只在 fake 回写值下走通（两个节点各一份请求全程服务完）；
  两个节点同时接**真实会话**仍待实测——真实那次只派了一个节点，第二个留在 pending
- reviewer 角色的派发（本次只有 developer）
- 一个 run 走到 `complete`（本次停在两节点其一）

## 2026-09-21 补测：一个 run 走到 `complete`（全 fake 回写，无真实会话）

基底 main `cc8f8d4`（#49）。探针项目是独立 git 仓库 `/tmp/vg-journey-20260921`
（main `f5a061b`，两个节点 worktree 已按 `child_binding` 建好），run
`run-8efee9bdae9e4201bc60e1f39bcee64a`，计划 `journey`（complex，两个并行节点
`export-button`、`date-range-filter` + `integration-review`）。

**先说没做到的**：原定混合方案（前 3 个会话真实、后 3 个 fake）没有执行。桌面端
`fn__ccd_*` 工具在连续三个会话里都不可用——ToolSearch 能列出名字并显示 "Tool loaded"，
实际调用一律返回 `No such tool available: <去掉前缀的短名>`（会话 1 六次、会话 2 三次、
会话 3 一次，涉及 `session_mgmt` 与 `session` 两组）。这是桌面 app 的工具桥没接进会话，
与 vibe-guide 无关，也与 Codex 适配器无关（ccd 只被 `claude-code` 适配器使用）。
用户决定改走全 fake。**所以本节证明的只是回写契约与推进逻辑，六个会话全部没有真实落点。**

### 实际发生的序列

30 次信箱回写、27 次 `resume`，每个（节点 × 角色）恰好 5 个操作：
`create → locate → visibility → wait(timeout) → wait(completed)`。

| 轮 | 服务的请求 | resume 顶层 | 节点状态（被服务的节点） |
|---|---|---|---|
| create×2（developer） | 回写 `{"binding": {"task_id","host"}}` | `retry_pending` | `running` |
| locate×2 | `{"located": true}` | `retry_pending` | `running` |
| visibility×2 | `{"visible": true, "direct_enter": true}` | `running` | `running`，**信箱清空** |
| （不服务，再 resume 一次） | — | `blocked_unknown` | `blocked_unknown`，此时才派出 `wait` |
| wait×2 第一次 | `{"status": "timeout", "cursor"}` | `running` | `retry_pending`，信箱清空 |
| （再 resume 一次） | — | `blocked_unknown` | `blocked_unknown`，重新派出 `wait` |
| wait×2 第二次 | `completed` + `event: complete` + `delivery_evidence` | `retry_pending` | `running`，随即派出 reviewer 的 `create` |
| reviewer 五步同上（visibility 服务完节点是 `review` 而不是 `running`） | wait 第二次回 `event: accepted` + `evidence: "<一句话>"` | `retry_pending` | 两节点 `accepted`，`integration-review` 变 `running` 并派出 create |
| integration-review developer 五步 | 同 developer | `retry_pending` | `running` |
| integration-review reviewer 五步 | wait 第二次回 `accepted` + 四键 evidence | **`complete`** | 三个节点全 `accepted` |

落盘核实：`load_snapshot` 的 `status == "complete"`；`integration_review_evidence` 含
`clearance {"p0":0,"p1":0,"p2":0}`、`aggregated_scope.nodes == ["date-range-filter","export-button"]`、
`prd_digest/spec_digest/node_contract_digest/authorization_digest` 等 15 个键；
`evaluate_v41_closeout(snapshot).allowed == True`。

### 观察到的事实（对照 09-18 的判断）

1. **`blocked_unknown` 不只是"被拒"**。09-18 那节写"被拒是 `blocked_unknown`、被接受是 `running`"。
   本次看到：`visibility` 或 `wait(timeout)` 服务完后信箱为空、顶层 `running`；**再 `resume` 一次**
   监工才派出 `wait` 请求，并在 `wait` 未被回复期间把节点标成 `blocked_unknown`（fail-closed 等待态）。
   所以看到节点 `blocked_unknown` 时要再看一眼 `pending()`：该节点有一条 `wait` 在等 → 是等待态；
   没有任何请求在等 → 才是回写被拒。09-18 的判断在"刚回写完 create"这个时点仍然成立，
   但不能推广到整条链路。
2. **`wait` 是被 `resume` 触发才派出的，不是回写完上一步就自动进信箱**。服务信箱的一方要
   在"信箱空且顶层 `running`"时主动再 `resume`，否则链路停在那里，看起来像"正常运行中"。
3. **create 请求的 `prompt` 里没有节点标题、合同、文件、分支**。原文只有两句：
   `请执行 developer 任务，Issue export-button。Capability contract: {...}` 和
   `一致性纠偏证据必须原样绑定：{...digest×5...}`。节点的 allowlist / branch / worktree
   在 `request.child_binding` 和 `request.worker_profile` 里，`request.target` 只有
   `{"type":"project","projectId":...,"environment":{"type":"local"}}`，**没有 cwd**。
   真实派发时，服务信箱的一方要自己从 `child_binding.worktree` 推出 `spawn_task` 的 cwd，
   并自己把合同拼进 prompt——协议不替它做。这是一条契约事实，不是 bug 判定。
4. **回写与回读的字段名不对称**：回写 create 用 `task_id`/`host`（`provider_action.py:509-510`），
   之后的 `wait`/`locate` 请求里 `targets` 回显为 `[{"threadId": ..., "hostId": ...}]`。
   两边都是对的，但抄错一侧就会静默被拒（09-18 已踩过）。
5. **reviewer 的 evidence 两种形态都被接受**：普通节点一句话字符串；`integration-review`
   必须恰好四键 `findings / iteration_compatibility / test_runtime_delivery / out_of_scope`
   （协议原文 `prd-guide.md`「整合审查节点的 accepted」）。本次两种都用 fake 值，字符串内容
   明写"fake：由脚本回写，未经真实会话"，落盘的 evidence 里也是这句。
6. 本 run 全程**没有派出过 `resume` 操作**（信箱只出现 create/locate/visibility/wait 四种）。

### 尚未验证（逐条对照 09-18 的清单）

- `locate`（`open_session_in`）：本次只回了 fake `{"located": true}`，桌面工具没调。**仍未验证**。
- `resume`（`send_message`）：本 run 没有派出该操作。**仍未验证**。
- create 用**真实会话身份**回写：ccd 不可用。**仍未验证**。
- `claude --bg` 登录路径、`claude agents --json` 运行中字段：本次没碰。**仍未验证**。
- 两个节点同时接**真实会话**：**仍未验证**；两节点并行在 fake 下全程走完（本次是第二次）。
- reviewer 角色的派发：**fake 下已走通**（四个 reviewer 会话），真实会话未验。
- 一个 run 走到 `complete`：**fake 下已走通**，真实会话未验。

fake 值约定：`task_id = "sess_" + action_id[:8]`、`host = "probe"`、
`cursor = "c_" + action_id[:6] + "_" + n`，与
`tests/test_integration_review_closeout_entry.py::MailboxClosesTheRunTests.reply` 一致。
逐轮日志在探针目录 `journal.jsonl`（`/tmp`，不入库）。
