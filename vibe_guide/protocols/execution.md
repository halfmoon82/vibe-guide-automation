# Vibe Guide · 执行协议（监工 / worker 会话用）

> 本文件随 vibe 包发布，由 `vibe init --confirm` 物化到 `.vibe/proposals/skills/execution/SKILL.md`。授权后执行与接班只读本协议、角色合同与已批准业务验收切片，不再回读规划协议。

## 阅读地图（按角色读）

- **首班监工**：读 §6 全文，每拍以随包 `supervisor-card.md` 为准。
- **§6.4 只读本宿主一行**；**§6.2 整合审查节点的 `accepted` evidence**：回写整合审查时读。
- **§7 打断边界**：监工要挂起或建决策时读。

## 6. 监工：守护程序 + 短会话 + 三级自愈（复杂计划开工后）

完整每拍动作见随包 `vibe_guide/protocols/supervisor-card.md`（监工卡，≤3KB，`vibe supervisor-beat` 在 work 时输出）；

### 6.0 形态与开工

- **守护程序干机械活**：授权后 `vibe daemon start|status|stop --run-id <run-id>` 起分离的本地进程并持监工租约，每个 tick 确定性地 resume、收交付、派后台 worker、拉 reviewer、接受节点、一级重试。
- **产品经理会话只保留 60 分钟兜底检查**：登记地址（`vibe supervisor-register --run-id <run-id> --provider <平台> --session-id <本会话 id> --wake-target <会话名> --host <本机标识> --session-record <本会话记录路径>`（Claude Code 必带 `--wake-target`，值是 ListAgents 显示的会话名；只给 UUID，worker 的 SendMessage 全部发不到，vibe 会警告并把心跳保持短间隔）；`vibe daemon register-pm --run-id <run-id> --provider <平台> --session-name <会话名>` 登记推送目标），心跳指令逐字用 vibe 打印的 `vibe supervisor-beat --run-id <run-id> --accept-decision --session-record <本会话记录路径>`，每 60 分钟（推送唤醒未验证的平台 10 分钟，记录在 `.vibe/push-wake-verification.json`）一拍：先 `vibe daemon status`，失活则上报（只上报，不重启：重启归 launchd 或产品经理手工），存活就结束本轮。不要自写心跳或把信箱服务写进心跳；不登记则唤醒无处可发，`rotate` 也无接班地址。
- **推送优先**：worker 交付后用 `vibe supervisor-address --project-root <主项目绝对路径> --run-id <run> --node <n> --role <r> --generation <g>` 回答里的 `wake`（`channel`、`target`、`signal`）原样推送；`channel` 为 null 或地址 unknown 时不发，由兜底检查领取。
- 状态从磁盘读；未知不当成功；宿主报 `No such tool available` 不是节点故障，重试一次后停下报告，不交班。

### 6.1 三级自愈与升级

1. **一级（守护程序自动）**：节点空转（同一节点连续数拍状态、代数、活跃任务、最新交付都不变）时先自动重试，不耗模型。
2. **二级（修复短会话）**：仍空转到「需要监工排查」档，守护程序按平台拉起**一次**短会话（claude-code 用 `claude -p`，codex 用 `codex exec`，kimi-code 用 `kimi -p`；WorkBuddy 写入信箱），模型按节点档位换算，输入排查包、监工卡和授权范围。会话在授权范围内修复（可改排查包列出的运行时文件，不得直接改 `state.json` 和 `events.jsonl`），**必须以 `vibe heal-verify --run-id <run-id> --node <节点>` 结束**，复核通过才推进并记 `self_heal_attempt`。同时最多一个短会话，次数与 token 有预算，用尽升级。
3. **三级（工程故障留在系统内）**：同一空转指纹两次修复无进展、预算用尽、修复结果未知、守护程序连续出错，是工程问题，守护程序写 `engineering_unresolved` 事件，带排查包和已试次数停下报告，不建决策卡、不推送，不第三次重试，该节点不再拉修复会话。运行结束时，以及无进展超过 6 小时（可配置得更短，不可更长，同一组项目只报一次），用产品语言汇报「未能自动解决的项」（受影响的验收例子、现在的情况、三个选项）；`vibe status` 展示同一份。只有修复要越过产品范围、授权卡、deploy、凭据与系统权限边界（§7）时，才建决策卡并推送，标签变「需要人处理」。

### 6.2 信箱回写

可见派发（卡片、窗口会话）的请求由监工服务；后台派发由守护程序执行。每份请求用 `vibe supervisor-mailbox --run-id <run-id> --json` 看摘要（正文换成 `{sha256, bytes, path}`），用 `--template <action_id>` 取示例，填好后 `vibe supervisor-mailbox --complete <action_id> --result <文件或 ->` 回写；形状不对在写入前被拒（报错写明缺什么、期望形状和可复制示例，结果文件不落盘），不产生 `blocked_unknown`；占位符 `<填写>`（含全角、前后空白、只填一半）一律拒收。`create`、`locate`、`visibility` 做不成时回写 `{"failed": true, "reason": "…"}`；`locate` 的宿主原始输出 `{"navigated": true}` 不是结果，要写 `{"located": true}`。下表由代码里的字段表生成，`--template` 取到的示例与它一致：

<!-- mailbox-table:begin -->
| 操作 | 角色 | payload | 说明 |
|---|---|---|---|
| `create` | 任意 | `{"binding": {"hostId": "<填写>", "threadId": "<填写>"}}` | 没有真实会话 id 就不回写；`task_id`/`host` 同样接受 |
| `create`（失败） | 任意 | `{"failed": true, "reason": "<填写>"}` | 没能创建任务时如实说明原因 |
| `locate` | 任意 | `{"located": true}` | 宿主原始输出 `{"navigated": true}` 不被接受 |
| `locate`（失败） | 任意 | `{"failed": true, "reason": "<填写>"}` | 没能定位到任务时如实说明原因 |
| `visibility` | 任意 | `{"direct_enter": true, "visible": true}` | 两个标志都必须为 true |
| `visibility`（失败） | 任意 | `{"failed": true, "reason": "<填写>"}` | 没能观察到任务时如实说明原因 |
| `wait`（未完） | 任意 | `{"status": "timeout"}` | 未完；`cursor` 给就必须是非空字符串（≤4096、无 NUL），不知道就不给 |
| `wait`（完） | developer | `{"cursor": "<填写>", "delivery_evidence": {"completion_marker": "<填写>", "delivery_path": "<填写>", "thread_status": "complete"}, "event": "complete", "status": "completed"}` | （嵌套对象，三键齐全，摊平无效）；`cursor` 填真实值（servicer 路径的交付门读它），不知道就整个删掉，别留占位符 |
| `wait`（完） | reviewer | `{"cursor": "<填写>", "event": "accepted", "evidence": {"summary": "<填写>"}, "status": "completed"}` | P0–P2 清零证据；`cursor` 填真实值，不知道就整个删掉，别留占位符 |
| `resume` | 任意 | `{"resumed": true}` | 沿用原会话 id，不回写绑定 |
<!-- mailbox-table:end -->

`cursor` 给就必须是非空字符串（≤4096、无 NUL），不知道就整个不给；不确定就不回写。worker 自报（`vibe worker-deliver`）没有游标，交付门改认自报回执，不要再手工回写同代 `wait`。回写被拒后不要重写同一 `action_id`；`vibe resume` 会发新的 `create`（`generation` 加一），认最大的那个。

#### 整合审查节点的 accepted

它是整个 run 的验收，`evidence` 必须是对象，**恰好四个键**。evidence 本身就是这四个键：findings、iteration_compatibility、test_runtime_delivery、out_of_scope，不再套一层 claim。

```json
{
  "findings": [{"severity": "p0|p1|p2|p3|p4", "status": "open|resolved|accepted|waived", "detail": "<一句话>"}],
  "iteration_compatibility": {"status": "verified|compatible|reviewed", "evidence": "<怎么核实的>"},
  "test_runtime_delivery": {"status": "verified|reviewed", "evidence": "<怎么核实的>"},
  "out_of_scope": ["<聚合范围之外被改动的东西>"]
}
```

- 全部清零就是 `findings: []`、`out_of_scope: []`。**只有 `resolved` 算清零**，`open`/`accepted`/`waived` 计入 `clearance`，判 `integration review acceptance still reports open P0-P2 findings` 落 `blocked_unknown`；`p3`/`p4` 是观察项不计入。这是格式约定，不是权限边界：同一条 P0 改写成 `resolved` 照样清零。这里没有第二道人工审批门，这道门只记账、不替人把关；没修完就报 `review_finding` 返工，别改字。
- 两个判断里的 `evidence` **必须是非空字符串**。给嵌套对象会被拒（`... needs a status and a non-empty evidence string`）。
- 只能给这四个键，`run_id`、`clearance` 等由 vibe 派生，多给是 schema 错误。格式错误记 `acceptance_rejected`（可修正重报）；整合合同 digest 漂移和未清零 P0–P2 才 fail-closed 落 `blocked_unknown`。

拒收原因在盘上读不到：节点 `reason` 落盘被打码成 `[REDACTED_PROVIDER_TEXT]`，顶层 `status` 会被其他重试中的节点改写成 `retry_pending`，它不告诉你是哪个节点，所以按节点看。能读到两样：①事件日志有该节点的 `acceptance_rejected`（`recoverable: true`）且 reviewer 会话 handle 还在，说明是格式拒绝可重报；②节点 `evidence` 列表里你回写的 claim，键名保留、值打码，**形状读得出来**：整条是字符串说明只给了一句话，少 `out_of_scope` 就是少键，`out_of_scope` 非空说明范围外有改动，嵌套对象里像敏感信息的键被逐个丢掉（只有每个键都像时才会只剩 `{}`，混着写还剩几个键）。被拒的时候它是最后一条；清零那次最后一条是派生出来的证据包，不是 claim。值读不出来：`resolved` 和 `waived` 长得一样，所以 `findings` 非空**不代表**有没清零的项。

形状合法却被拒就是值的问题，对着自己的 claim 查这五条：
1. `status` 写 `accepted` 或 `waived` 一样不算清零。
2. `out_of_scope` 非空判 `integration aggregated scope contains out-of-scope changes`，先收回范围外改动。
3. `evidence` 给空字符串、只有空格或换行、或者干脆不给这个键，和嵌套对象一样被拒（同一句 non-empty evidence string）。
4. `severity` 只收 `p0`–`p4`，`status` 只收 `open`/`resolved`/`accepted`/`waived`：写 `critical`、`p5` 判 `integration finding schema is invalid`，写 `pending` 判 `integration finding status is invalid`。
5. 两个判断的 `status` 同样封闭：写 `ok`、`passed` 判 `... evidence is incomplete`，写 `unknown`/`expired`/`stale` 判 `... evidence is unknown or expired`。

#### 整合审查发现缺陷：指给上游节点返工

整合审查报缺陷别只写 `review_finding` 一句话：不带 `target_node_id` 且缺陷文件不在任何已验收节点范围内时，只会让整合审查自己返工重审，三轮后停放。要让缺陷回到出问题的上游节点，在 `worker-deliver` 的 payload 顶层带 `target_node_id`（已验收节点的 id），需要新增文件再带 `scope_expansion`：

```json
{"event": "review_finding", "finding": {"severity": "p1", "detail": "<一句话>"},
 "target_node_id": "mailbox-shape",
 "scope_expansion": {"files": ["tests/test_mailbox_shape.py"], "reason": "<为什么只是补测试>",
   "acceptance_examples_unchanged": true, "adds_third_party_dependency": false,
   "visibility_downgrade": false}}
```

- 缺陷文件全在该节点原范围内：不用 `scope_expansion`（`scope-expand` 也回「无新增文件」），节点直接进返工，返工提示带上整合审查的发现。只给 `files`（缺陷文件列表）且都落在同一个已验收节点的范围内，监控自动指给它。
- 只有改验收判定、新增第三方依赖、或文件被别的活跃节点占用才报产品经理复核，文案与上一条不同。
- 上游节点的 HEAD 都没变时不会再开新的整合审查，节点显示「等待返工」。

### 6.3 派发合同

`request.child_binding` 的 `worktree`、`branch`、`allowlist` 由 vibe 按节点 id 派生，每个节点互不相同。**创建会话必须把工作目录设成合同里的 `worktree`**，在合同的 `branch` 上开发，只碰 `allowlist` 里的文件。

**worktree 由谁建，按派发方式分。** 后台派发（`claude-code-background`）由 vibe 在派发前自建缺失的节点 worktree：目录不存在时按运行基线执行 `git worktree add <worktree> -b <branch> <base>`，并把该节点已验收的上游分支合入基线；分支已存在、目录不在任何 git 仓库内或基线读不到时拒绝创建，不复用、不覆盖已有分支。目录已存在则核对它在合同的 `branch` 上；既有目录不是 git worktree 时同样 fail-closed，不直接派发。随后把它当前的 HEAD 作为本次派发的基线（vibe 自建时写进节点 `worktree_base`），不切换、不重置、不覆盖；分支不符报 `worktree_mismatch` 并 fail-closed。

在 `vibe monitor` / `vibe resume` 的一次性推进里，这类失败会把节点标成「需要监工建工作树」，`worktree_preflight` 里带机读码和一条可直接执行的处置命令（建树命令，或该码对应的核对/处置说明）。`vibe monitor --watch` 进入 watch 后的每一拍以及守护程序续跑也会记录同一预检标记，按节点事件流或 `vibe status` 复核。

**可见派发同样先经 vibe 预检**（Codex/Claude 的可见会话按合同目录挂载）：缺失且能按合同安全创建时由 launcher 预检代建；绑定时要求它是干净、在合同 `branch` 与 `base_sha` 上的 git worktree，并持有 writer lease。目录不能安全创建、目录里没有 `.git` 或状态无法确认时派发被拒：机读码 `worktree_missing`（固定枚举，与后台派发同一词汇表），节点状态「需要监工建工作树」、`worktree_preflight` 里带可直接执行的建树命令；由监工按合同 `base_sha` 处理后重新派发。

| 宿主 | 设定工作目录的方式 | 做不到时的后果 |
|---|---|---|
| Claude Code | 派发时传 `cwd` | 会话启动就落在 `worktree` 里 |
| WorkBuddy | Agent 工具**没有 `cwd` 参数** | 派发提示里逐条要求**每条命令先 `cd <worktree>`**、写文件用绝对路径；串行节点也一样 |

**唯一例外是交付自报**：`vibe worker-deliver` 必须指向**主项目目录**：写成 `vibe worker-deliver --project-root <主项目绝对路径> …`（也可设环境变量 `VIBE_PROJECT_ROOT`，参数优先），不要依赖当前目录；在 worktree 里无参数执行会写进 worktree 自己的 `.vibe`，监工看不到。

每个 developer/reviewer 的模型由节点档位按平台探针换算（失败回退平台默认），派发和续接显式传模型与思考档位；审查打回两次自动升一档，监工或修复会话可 ±1 档须给理由；模型名不进授权指纹。

**环境事实**：改动第三方 UI 组件用法的节点，IssueContract 必须带 `environment_facts`（`[{"fact": "str", "source": "str", "verified_at": "str"}]`），否则不能开工。节点 goal、owned_paths 或 read_paths 命中 `vibe_guide.contracts.THIRD_PARTY_UI_LIBRARY_MARKERS`（大小写不敏感）而该字段缺失或为 `[]` 时拦停；清单含 `element-plus` `element-ui` `antd` `ant-design` `vuetify` `quasar` `naive-ui` `arco` `material-ui` `mui` `primereact` `chakra`。`source` 含 `://` 为外部引用；不含 `/` 且不以 `.json` `.md` `.py` `.js` `.jsx` `.ts` `.tsx` `.vue` `.yaml` `.yml` `.toml` `.lock` `.txt` `.html` `.css` 结尾为描述性文字；其余按仓内相对路径、文件必须存在，绝对路径或含 `..` 越界。

### 6.4 平台差异：哪个平台能"授权一次、不用盯屏幕"

五个操作在三个可见 provider 上都已登记，Claude Code 另有降级后台 provider `claude-code-background`；创建会话要不要人工点一下因平台而异：

| 操作 | `codex-app-visible` | `claude-code-visible` | `workbuddy-visible` | `claude-code-background` |
|---|---|---|---|---|
| `create` | `codex_app__create_thread` | `ccd_session__spawn_task` | `workbuddy_job__create` | `claude --bg` |
| `locate` | `codex_app__navigate_to_codex_page`：参数形状 `{"threadId": <task_id>}`，只传 threadId——不要多传 hostId/page，多传即参数错误（#316 实录） | `ccd_window__open_session_in`：在窗口中打开会话（session + 窗口）；参数形状未验证（2026-09-18 探测未单独跑过），照抄登记参数、不要多传 | `workbuddy_job__get`：传 job id（仓内 server 实证 `GET /api/v1/jobs/<id>`） | `claude attach <short-id>`（终端命令） |
| `visibility` | `codex_app__wait_threads` | `ccd_session_mgmt__get_session` | `workbuddy_job__list` | `claude agents --json --all` |
| `resume` | `codex_app__send_message_to_thread` | `ccd_session_mgmt__send_message` | `workbuddy_job__reply` | `claude stop` + `claude --bg --resume <完整会话 id>` |
| `wait` | `codex_app__wait_threads` | `ccd_session_mgmt__list_events` | `workbuddy_job__wait` | 轮询 `claude agents --json --all` 的 `state` + `vibe worker-deliver` 自报 |

- **Codex 本地桌面**：`codex_app__create_thread` 直接创建用户可见的 thread（`create_thread` 没有审批门），无人值守即可全自动；Codex 的 `create` 不得用 `fork_thread` 替代。Claude Code 要无人值守走 `claude-code-background`。
- **Claude Code 桌面**（未配置后台启动器时）：`ccd_session__spawn_task` 只是**提议**一个任务，界面上出一张卡片，**需要用户点一下**才创建会话，调用方拿不到 `sessionId`，须 `ccd_session_mgmt__list_sessions` 取真实会话 id 回写 `create`。把它当"全自动"会一直卡在 `retry_pending`，授权卡念完要告诉产品经理。
- **Claude Code 后台（`claude-code-background`，降级后台）**：Claude Code 默认派发方式，授权一次、不用盯屏幕，授权卡上可改选可见卡片。需配置后台启动器，且会话用 `vibe attest` 登记了 `claude-code.background_task.launch` 为真；未配置仍走卡片路径。降级限制三条：① 桌面侧边栏看不到，`get_session` / `open_session_in` 也找不到；② 只能在真实终端 `claude attach <short-id>` 进入；③ 返工可续接同一会话——先 `claude stop`，再用**完整会话 id** 执行 `claude --bg --resume <完整会话 id> <返工消息>`，**除完整会话 id 和返工消息外不带任何其他参数**（如 `-n`、`--model`），否则会复制出新会话。权限用 `bypassPermissions`（`acceptEdits` 第一条 Bash 就卡在 `blocked`，`dontAsk` 直接拒绝 Bash 却报 `done`）。`state=done` 只表示这一轮结束、不代表任务成功，完成依据是 worker 的 `vibe worker-deliver` 自报；`blocked` 需要处理，不能当进行中。未验证、不得当作可用：`--session-id` 预指定会话 id、`-w` 与 `--bg` 合用、长任务/休眠/daemon 退出后的恢复、桌面内置 CLI 下的 `--bg`。
- **WorkBuddy**：`workbuddy_job__*` 五个工具已登记，但 Jobs API 派生的 detached agent 拿不到模型凭据，可用的派发目标是用户已在 UI 打开的窗口会话（`workbuddy_job__create` 带 `session_id`，一窗口一个任务）。不得用 GUI 点击代开窗口。

**推送唤醒通道**（`wake.channel`）：Codex 为 `send_message_to_thread`，Claude Code 为 `SendMessage`（`.vibe/push-wake-verification.json` 补齐成功、失败证据各一条前视为未验证，兜底检查保持 10 分钟），WorkBuddy 无通道（`channel` 为 null，由兜底检查领取）。

### 6.5 换班、预检分支与其他

- **不换班（Claude Code）**：Claude Code 没有换班原语，预检永不输出 `rotate`，到上限改为 `work` 并附 `context_pressure` 提示，同会话继续。
- **换班（其他平台）**：单班是默认（`supervisor_single_shift` 缺省 `true`），项目 `.vibe/config.json` 写 `"supervisor_single_shift": false` 可关闭，恢复按增长阈值（10 万/8 万）换班；只在达到绝对上限（缺省 20 万，上限档位见授权卡）时输出 `rotate`。执行换班：新会话**必须建在本项目里**（Codex 用 `create_thread` 的 project 目标；projectless 的 cwd 落在宿主自动生成目录，读写 `.vibe/` 逐条弹审批），必须是**全新会话**——禁止 `fork_thread`，`supervisor-register` 拒绝 fork 出的会话、起步不低于 10 万的会话、不带 `--session-record`（绝对路径、文件存在）的接班登记。让它跑 `vibe supervisor-handoff --run-id <run-id>` 并照打印内容做：登记、建兜底检查、旧会话删除自己的检查并归档。接班会话写不了 `.vibe/`（`session_gate_blocked`，带 `host sandbox denied access`）时不重试，停下报告；WorkBuddy 换班只能由用户在本项目新开会话。
- **预检**：`vibe supervisor-preflight --run-id <run-id> --accept-decision --session-record <本会话记录路径>`（每拍一次），分支以监工卡为准；`contract_drift`（能力合同与运行快照 digest 不一致）停止推进、走同一运行的重新授权；`decision` 降频到 60 分钟不暂停；`unknown` 按 `work`。
- **等人拍板必须登记**：`vibe supervisor-hold --run-id <run-id> --node <节点> --category <product_acceptance|external_authorization> --decision <决策ID>|--authorization-gap <缺口> --reason <一句话>`；用户用 `decision-answer`（必须带决策卡 `--digest`）回答后由运行自动消费；不经决策的授权缺口挂起用 `--release` 手工释放。挂起满 6 小时或任一节点状态变化，预检报 `work`（`hold needs recheck`），复核后仍在等人就刷新。`review`/`rework` 节点不能挂起；不登记，空闲心跳每拍判 `work`。

## 7. 什么时候才能打断产品经理

**已批准判据**：修改后，PRD 里每条验收例子的判定结果是否改变——不变就由监工自决并留痕，变了才问产品经理；另外两类无论如何都问：新增第三方依赖（发布/安全对外承诺）、执行模式从「可见可进入的任务」降级为「后台不可见」。例外：`vibe monitor --platform-handoff` 交接到新平台后变成后台，只披露不问（产品经理 2026-11-02 裁定）；交接前旧平台的 worker 必须排空：等它交付，或在旧平台停掉后用 `vibe reconcile --plan <plan> --run-id <run> --evidence <文件>` 登记终止证据（`kind: handoff_worker_stopped`，字段见交接拒绝输出），再重跑交接。判据二只在项目开启 `acceptance_scope_rule_approved` 后可用（§4），未开启的项目按判据一处理；落在判据一之外的文件仍按 §4 单独授权，会问产品经理，理由是文件本身越界。

**总原则**：开发过程中的工程问题，只要不改变 PRD 决策，监工和 worker 自行判断、自行决定并留痕，不问产品经理；非必要不改变 PRD 决策。下面的清单只列「必问」的情形，不是工程问题的白名单，清单外的一律按此原则处理。

只有四类：产品范围或设计要变、部署或对外发布、涉及凭据或系统权限、授权卡外的不可撤销动作。`supervisor-hold` 与 `decision-raise` 只接受这四类（落盘仍是 `product_acceptance` / `external_authorization` 两个规范类别；`credentials`、`system_permission`、`irreversible_action` 是后者的别名），其他类别（如 `engineering`）直接报错并提示走自愈。其他工程问题（超时、任务创建失败、容量、分支漂移、能力未知、可按 §4 补进范围的文件）由 vibe 自行分类恢复。

**结构性待验项只认固定句式**：「[真机/生产环境/线上]＋≤8 字的事项名词短语＋只能/仅能/需/须/待＋在验收期/上线后/真机上/生产环境＋验证/确认/完成」（英文同理），另认“验证脚本不入库/仅本地运行”；名词短语里不含否定、程度、谓语或状态词，其余按未知处理、保持 `blocked_unknown`。符合句式的事项自动记为 `deferred_to_acceptance`、节点照常验收，并列入交付与验收汇报的“验收期待验项”（`vibe status` 的 `acceptance_pending_verification`，有则必列）。

`blocked_unknown` 按原因恢复，处置用 `vibe blocked-unknown --run-id <run> --node <节点> --disposition <处置> --reason <原因>` 记录，节点状态在 `vibe status` 可见，并保留结构化 `recovery_action`：

- `repair_node`：工作树、分支、合同测试或快照可由本地证据修复；保持原 task identity、generation、worktree 和 writer lease。
- `requeue_same_task`：超时、断开、容量或 provider pending；只续接同一任务、退避等待，不建 successor。
- `reconcile_binding`：绑定、身份、租约或 cursor 无法证明；先核对 task binding、worktree、branch、run、cursor，再走 `vibe repair --plan <plan> --run-id <run> --node <node>` 或同一 Monitor 的 repair/requeue 入口；未证明前不得盲派。
- `await_external_decision`：命中产品、外部授权或 deploy 边界；保持 blocked 等授权。

三轮返工仍不收敛时，监工记录换 reviewer 或拆节点并保留唯一 writer；只有会改变 PRD 验收判定的分歧才升级产品决策。

一个例外要告诉产品经理：Claude Code 改选可见卡片派发时，每个节点都需要他点一下确认（§6.4）。
