# Vibe Guide · PRD 引导协议（宿主 agent 用）

> 本文件随 vibe 包发布，由 `vibe init --confirm` 物化到 `.vibe/proposals/skills/prd-guide/SKILL.md`，也可用 `vibe plan --print-protocol` 直接打印。
> 面向 Claude Code / Codex 等宿主 agent。产品经理不需要读它。

## 0. 分工

- **agent 出内容**：和产品经理对话、只读分析代码、写 PRD、拆节点、记录产品决策。
- **vibe 出协议 + 校验 + 脚手架**：路由分流、校验产品 spec、派生全部工程字段、发布计划、签授权卡、派发监工。
- agent **不得**在产品 spec 里写任何工程字段（见 §5 的黑名单），vibe 会拒绝。
- agent **不得**替产品经理做产品决策：`decisions[].status` 只有产品经理明确选定后才能写 `approved`。

## 1. 入口判定

收到一条新请求，先原样交给 vibe 分流，不要自己判断复杂度：

```bash
vibe plan --request "<产品经理的原话>" --json
```

- `route` 为 `simple` 或 `light_plan`：直接开始做，不进入本协议。
- `route` 为 `complex`：vibe 会生成一个 draft（`status: planned`，`execution: deferred_until_authorize`），记下 `plan_id`，进入 §2。
- 不要传 `--s1`。分数由 vibe 从文本推导；如果产品经理明确要求按复杂流程走，用 `--s1 5,5,5,5,5` 显式升级，绝不反向降级。

## 2. PRD 引导对话（只谈产品，不谈工程）

按下面五个话题逐一确认，每条结论都带来源标记：

| 话题 | 要问清楚的 | 典型追问 |
|---|---|---|
| 问题 | 现在哪里不顺、谁遇到、多常见 | "现在没有这个功能时，你们是怎么绕过去的？" |
| 目标 | 做完以后什么变了 | "上线一周后，你看哪个数字或反馈来判断成功？" |
| 成功标准 | 可观察、可验收 | "验收那天，你会亲手点哪几步？期待看到什么？" |
| 非目标 | 明确不做的 | "有没有相关但这次不碰的？" |
| 用户场景 | 谁、在什么情况下、做什么、得到什么 | "举一个最常见的例子。" |

来源标记规则：

- `user_confirmed`：产品经理亲口说过或明确点头的。
- `system_inferred`：agent 从上下文或代码合理推断、产品经理没有反对的。**不打断产品经理**，直接记下继续。
- `needs_confirmation`：会改变产品方向、授权边界或验收标准的取舍。**必须停下来问**，得到答案前不能进入 §4。
- `unverified`：证据不足、暂时无法核实的（多见于代码现状）。

只在 `needs_confirmation` 时提问。每次只问一个问题，说清楚不同答案会怎样改变结果。普通的信息缺口记为 `system_inferred` 继续。

会改变产品方向、授权边界或验收标准的取舍，同时要写成 `decisions[]` 条目：`question` / `options`（至少两个、互不相同）/ `impact` / `recommendation`（必须是 options 之一）/ `field`（这个决定落在哪个契约字段上，如 `export.watermark`）。产品经理选定后，`selected` 逐字等于所选 option，`status` 写 `approved`。**没选定的决策保持 `unresolved`，vibe 会拒绝发布，这是正确行为。**

## 3. 只读代码分析

对话结束后、拆节点之前，只读地看代码。禁止改任何文件。找出：

- 入口点：用户操作从哪个文件/函数进来。
- 涉及模块：会碰到哪些模块、它们之间怎么调用。
- 现有行为：今天这条路径实际做什么，有没有已知问题。
- 现有测试：哪些测试覆盖了这条路径。

产出两种东西：

1. `prd.code_evidence[]`：每条一句话 + 项目相对路径（可带符号名），来源标 `unverified` 或 `system_inferred`。
2. 每个 `goals[]` 条目的 `code_evidence` 字段（§5）。

用产品经理能懂的话把结论说一遍："这个需求会碰到保单查看页和 PDF 工具两处，现有测试没有覆盖导出。"不要念文件路径。

## 4. 拆节点

原则：默认并行，只把真正阻塞启动的关系写成硬依赖。

- `depends_on`：没有它完成就**无法开始**的节点。
- `integration_after`：可以先各自开发、最后要联调的关系。不阻塞启动。
- `parallel_group`：可以同时开工的一组，用同一个组名；不需要分组就写 `null`。
- 每个节点 `contract` 四个字段用验收语言写，不写实现细节：
  - `input`：拿到什么。
  - `output`：交出什么。
  - `error_behavior`：出错时对用户的表现。
  - `acceptance_example`：一个具体的例子，验收者照着做就能判断通过与否。
- `files`：这个节点会动的文件，项目相对路径。这是授权卡上的文件范围，宁可列全，不要漏。
- 节点 `id` 用小写字母、数字、连字符。不要写 `integration-review`，vibe 会自动追加最终整合审查节点。

拆完用产品语言复述一遍给产品经理："一共三件事，前两件可以同时做，第三件要等第一件做完。"

## 5. 产出与提交

### 5.1 产品 spec 的形状

写成一个 JSON 文件（建议 `.vibe/plans/<plan_id>/product-spec.json`，也可放项目内任意位置）。结构以 `vibe_guide.node_spec.PRODUCT_SPEC_FIELDS` 为准，测试会断言本节与代码一致：

```json
{
  "title": "str",
  "objective": "str",
  "decisions": [
    {"question": "str", "options": ["str"], "impact": "str", "recommendation": "str",
     "status": "approved|unresolved", "selected": "str|null", "field": "str"}
  ],
  "nodes": [
    {"id": "str", "title": "str", "depends_on": ["node id"], "integration_after": ["node id"],
     "parallel_group": "str|null",
     "contract": {"input": "str", "output": "str", "error_behavior": "str", "acceptance_example": "str"},
     "files": ["project-relative path"]}
  ],
  "prd?": {"<section>": [{"value": "str", "source": "user_confirmed|system_inferred|needs_confirmation|unverified"}]},
  "goals?": [
    {"id": "str", "user_scenario": "str", "code_evidence": "str", "spec_ref": "str",
     "issue_ref": "str", "dag_nodes": ["node id"], "runtime_acceptance": "str"}
  ],
  "rationale?": {"framing|tradeoffs|flow|acceptance": "verified_fact: ..."},
  "remote_git_actions?": "allow|deny"
}
```

`prd` 的推荐 section：`problem`、`user_scenarios`、`success_criteria`、`non_goals`、`code_evidence`。

**黑名单（写了就被拒）**：顶层 `complexity_band` `route` `route_result` `capabilities` `project_id` `integration_contract` `spec_path` `plan_id`；节点 `status`；`contract` 内 `adapter_id` `project_id` `worker` `reviewer_worker` `worker_profile` `worktree` `branch` `writer` `reviewer`。这些全部由 vibe 派生。

其中 `worktree` 与 `branch` 由 vibe 按节点 id 派生成互不相同的一对（`.worktrees/<节点>` 与 `node/<节点>`），保证每个节点在自己的目录和分支上开发；agent 写死它们会让两个并行节点撞进同一棵树。

### 5.2 登记本会话的能力（复杂计划发布前必做一次）

vibe 不猜宿主平台有什么能力，由 agent 盘点自己**这次会话里实际看到的**工具后登记：

```bash
vibe attest --adapter <claude-code|codex|...> --facts <facts.json> --provenance "<一句话说明依据>" [--project-id <id>]
```

`facts.json` 的键名以 `vibe_guide/adapters/manifests/<adapter>.yaml` 的 probes 为准，值只能是 `true` / `false`，只有确实看到对应工具才写 `true`。`project_id` 是宿主平台里这个项目的标识（Codex 的 project id、Claude Code 的会话 cwd 标识），可见任务派发需要它。

### 5.3 发布、授权、开工

```bash
vibe plan --request "<产品经理的原话>" --plan-id <plan_id> --from-prd <product-spec.json> --json
```

- `status: ok`：vibe 已生成 PRD、Spec、Issue、DAG 审计、授权卡、planning-brief。把 `authorization_card` 用产品语言念给产品经理：要做哪几件事、哪些同时开工、每件事一个开发任务加一个独立审查任务、**唯一要选的是允许还是禁止远端 Git 动作**（提交、推送、开 PR、合并）、永远不含部署/发布/生产写入/凭据/对外通信。
- `status: blocked` 且 reason 含 `product decisions remain unresolved`：回到 §2 把决策问清楚。
- `status: blocked` 且 reason 含 `engine_attestation_unavailable`：先做 §5.2。
- `status: blocked_design` 且带 `question`：这是 PRD 检查点发现的未决产品问题，原样问产品经理。

产品经理说"授权"并选定远端 Git 动作后：

```bash
vibe authorize --plan <plan_id> --authorize AUTHORIZE --json
vibe monitor --plan <plan_id> --authorize AUTHORIZE --json
```

之后用 `vibe status --plan <plan_id>` 看进度，`vibe resume --plan <plan_id>` 从断点续接。

## 6. 服务监工信箱（复杂计划开工后，每轮都要做）

`vibe monitor` 自己不会创建开发会话。它把每个要派发的动作写成一份请求放进信箱，然后停下来等。**请求没人服务，run 就一直停在那里**——`status` 会显示 `retry_pending`，这不是失败，是在等你。

包里没有任何代码能替你做这一步：创建会话需要宿主平台的桌面工具，只有当前这个会话持有它们。所以这是 agent 的职责。

### 6.1 一轮的动作

```python
from vibe_guide.paths import ProjectPaths
from vibe_guide.adapters.task_provider import ProviderActionStore

store = ProviderActionStore(ProjectPaths(<项目根>))
for action in store.pending():          # .vibe/provider-actions/requests/ 里还没有结果的
    ...                                 # 按 action["native_tool"] 调对应桌面工具
    store.complete(action["action_id"], <结果 payload>)
```

也可以直接读 `.vibe/provider-actions/requests/*.json`。回写建议走 `complete()`：结果文件必须恰好含 `schema_version` / `action_id` / `request_digest` / `payload` 四个键，且前三个与请求逐字对应，错一个就会被判"未绑定到请求"而拒收——`complete()` 替你填对。手写也能被接受，但没有理由自己去对 digest。

每份请求里你要看的字段：

| 字段 | 用途 |
|---|---|
| `action_id` | 回写时的键，原样传给 `complete()` |
| `operation` | 五个之一：`create` `locate` `visibility` `resume` `wait` |
| `native_tool` | 这个平台上该调哪个工具，vibe 已经替你查好 |
| `issue_id` / `role` | 哪个节点的哪个角色（`developer` / `reviewer`） |
| `request.child_binding` | **合同**：`worktree` `branch` `allowlist`，见 §6.3 |
| `request.prompt` | 原样交给被派发的会话，里面含它必须回绑的证据 |

### 6.2 回写的形状

`payload` 必须是一个对象，按操作给出 vibe 会读的字段：

| 操作 | payload |
|---|---|
| `create` | `{"binding": {"task_id": "<真实会话 id>", "host": "<本机标识>"}}`（`threadId` / `hostId` 同样接受） |
| `locate` | `{"located": true}` |
| `visibility` | `{"visible": true, "direct_enter": true}` |
| `wait`（还没干完） | `{"status": "timeout", "cursor": "<最后一条事件的游标>"}` |
| `wait`（干完了，developer） | `{"status": "completed", "cursor": "<游标>", "event": "complete", "delivery_evidence": {"completion_marker": "<完成标记>", "delivery_path": "<交付物路径>", "thread_status": "complete"}}` |
| `wait`（干完了，reviewer） | `{"status": "completed", "cursor": "<游标>", "event": "accepted", "evidence": "<P0–P2 清零证据>"}`；**整合审查节点例外**，`evidence` 必须是结构化判断，见下 |
| `resume` | `{"resumed": true}`（会话 id 沿用原来的，**不要**回写绑定） |

`wait` 的终态字段各有各的判定，缺一个就整轮作废：`status` 只认
`complete` / `completed` / `failed` / `stopped`，`event` 只认
`complete` / `delivered` / `accepted` / `review_finding` / `failed` / `stopped`，
且**角色不同事件名不同**——developer 报 `complete`、reviewer 报 `accepted`，
报错会被判 `provider event is unsupported`。

**复杂计划的终态还要过一道交付证据门，两个角色各要一样东西**（这道门只在
复杂计划上生效：授权时 `complexity_band == "complex"` 会把引擎设成
`vibeguide_monitor`，非复杂计划不走这里）：

- **developer** 要 `delivery_evidence`，**必须是嵌套对象**，三个键齐全：
  `completion_marker`、`delivery_path`、`thread_status`（只认 `complete` /
  `completed` / `DELIVERED`）。摊平成顶层三个字段**不算**，门读不到。缺任何
  一个，节点直接 `blocked_unknown`（理由如 `completion marker is missing`），
  而顶层看起来只是还在等。
- **reviewer** 要 `evidence`：`accepted` 之后没有它，节点被判
  `review acceptance has no registered P0-P2 clearance evidence`。

#### 整合审查节点的 accepted：`evidence` 必须是结构化判断

最后那个 `integration-review` 节点的 `accepted` 不只是收下一个节点，它是**整个 run 的验收**：vibe 收到它才写 run 级的整合审查证据包，顶层才从 `running` 走到 `complete`。所以这一个节点的 `evidence` 不能是一句话，必须是一个对象，**恰好四个键**：

```json
{
  "findings": [{"severity": "p0|p1|p2", "status": "open|resolved|accepted|waived", "detail": "<一句话>"}],
  "iteration_compatibility": {"status": "verified|compatible|reviewed", "evidence": "<怎么核实的>"},
  "test_runtime_delivery": {"status": "verified|reviewed", "evidence": "<怎么核实的>"},
  "out_of_scope": ["<聚合范围之外被改动的东西>"]
}
```

- 全部清零就是 `findings: []`、`out_of_scope: []`。**只有 `resolved` 算清零**：`open`、`accepted`、`waived` 一律计入 `clearance`，报了会被判
  `integration review acceptance still reports open P0-P2 findings`，节点落到 `blocked_unknown`。审查者不能给自己签豁免——P0–P2 没修完就报 `review_finding` 事件让整合审查返工，要不要放行是人的决定，不是审查者的。
- 两个判断里的 `evidence` **必须是非空字符串**。给嵌套对象会被拒（`... needs a status and a non-empty evidence string`）：落盘时 `evidence` 整个字段会被打码，对象里"看起来像敏感信息"的键会被丢掉，于是写进去的包回读时不再合法——顶层会先报一次 `complete`，下一次读又退回去。所以这里只收一句话。

- **只能给这四个键，多一个就是 schema 错误**。`run_id`、`plan_id`、`plan_revision`、四个 digest、`aggregated_scope`、`clearance`、`agentsmd_acceptance_refs`、`unverified_or_excluded` 全部由 vibe 从 run 自己和计划的整合合同派生。这不是省事：审查者不能改写它被追责的血缘，也不能缩小它被要求覆盖的范围。
- 键名错、少键、或者给一个字符串，节点会落到 `blocked_unknown`，代码里的判定是
  `integration review evidence cannot be derived (...)`。

**但这三条的报错原文在盘上读不到。** 节点的 `reason` 落盘时会被打码成
`[REDACTED_PROVIDER_TEXT]`——`vibe status --json`、事件日志、隔离记录里都一样，四种
完全不同的拒收原因长得一模一样。所以别指望"看理由"定位，能读到的是两样东西：

1. 这个节点的 `status` 是 `blocked_unknown`，顶层只是没到 `complete`（看到 run 长时间
   停在 `running`，先查这个节点）。
2. 你回写的那份 claim 被记在这个节点的 `evidence` 里，**键名保留、值打码**。照上面三条
   规则对着它的形状看就能分辨：整条是一个 `[REDACTED_PROVIDER_TEXT]` 说明你给的是字符串；
   少 `out_of_scope` 就是少一个键；`iteration_compatibility.evidence` 显示成 `{}` 说明你给了
   对象；`findings` 非空说明有没清零的项。

**`cursor` 在复杂计划的 developer 终态里也是必需的**：绑定上的游标只有你回写时
才会被写进去（`provider_action.py:1153-1160`），不给就等于绑定没有游标，交付证据门
报 `current cursor is missing`。这道门只挂在 `delivered` / `complete` 上，所以
reviewer 的 `accepted` 不受它约束——但每轮都回写游标本来就是对的（`wait` 靠它
接着上一次的位置读），所以上表两行都给了。

`resume` 只看 `resumed`，**完全不读 `binding`**。按 `create` 的形状回写它，
`resumed` 就是缺的，这一轮会报 `visibility_unknown` 事件，节点落到
`blocked_unknown` 或重试态，续接推不下去。

三条硬规则：

1. **`create` 的 `binding` 必须用上面那几个键名，并且含真实的会话 id**。vibe 只认 `task_id`/`threadId` 与 `host`/`hostId`；用别的名字（比如桌面工具自己叫的 `sessionId`）会被判"没有任务身份"而丢掉绑定。只有设置句柄、没有真实会话 id 时，不要回写——留着 pending，下一轮再来。

   **顶层状态看不出被拒**：绑定被丢掉和"正在等下一个节点"，顶层 `status` 都是 `retry_pending`，`pending()` 的计数也都会少一个（它只数没有结果文件的请求，不管结果有没有被接受）。要分辨就看 `payload["nodes"]` 里那个节点的状态——被拒是 `blocked_unknown`，被接受是 `running`。顶层之所以掩盖它，是只要还有一个节点在重试，顶层就被改写成 `retry_pending`。

   **发现回写被拒之后**：不要重写同一个 `action_id`，已经有结果文件的请求不会被重读，重写没有任何效果。`vibe resume` 会为同一个节点发出一个**新的 `create` 请求**（`generation` 加一），服务那个新请求才能恢复。所以同一个节点在信箱里可能先后有多份 `create`，认 `generation` 最大的那个。
2. **给 `cursor` 就必须是非空字符串**（长度 ≤ 4096、不含 NUL）。空串会被判
   `provider cursor is invalid`，**整个结果被丢掉**，节点停在 `blocked_unknown`——
   不是"可能重复消费"这种可以容忍的风险。不知道游标就整个字段不给（终态允许省略）。
3. **不确定就不回写**。pending 比假成功便宜得多；vibe 会一直等，不会把未知当成功。

回写一个之后 `vibe resume --plan <plan_id>`，剩下的请求下一轮继续。`status` 变回 `retry_pending` 只是说还有没服务完的请求。

### 6.3 派发时必须遵守合同

`request.child_binding` 里的 `worktree` 和 `branch` 是 vibe 按节点 id 派生的、每个节点互不相同的一对。**创建会话时必须把它的工作目录设成合同里的 `worktree`**（Claude Code 传 `cwd`），并让它在合同的 `branch` 上开发。

这不是形式要求：两个并行节点落进同一个目录同一个分支，就是两个 writer 改同一棵树，改动互相覆盖。`allowlist` 同理——被派发的会话只能碰这些文件。

worktree 需要你先建出来（`git worktree add <worktree> -b <branch>`），vibe 不会替你建。

### 6.4 平台差异：哪个平台能"授权一次、不用盯屏幕"

五个操作在两个平台都已登记，但**创建会话时要不要人工点一下，两边不一样**。这是平台事实，不是 vibe 的功能差异：

| 操作 | `codex-app-visible` | `claude-code-visible` |
|---|---|---|
| `create` | `codex_app__create_thread` | `ccd_session__spawn_task` |
| `locate` | `codex_app__navigate_to_codex_page` | `ccd_window__open_session_in` |
| `visibility` | `codex_app__wait_threads` | `ccd_session_mgmt__get_session` |
| `resume` | `codex_app__send_message_to_thread` | `ccd_session_mgmt__send_message` |
| `wait` | `codex_app__wait_threads` | `ccd_session_mgmt__list_events` |

- **Codex 本地桌面**：`create_thread` 没有审批门，`create` 直接返回会话身份。这是目前唯一能做到**无人值守**派发的路径——产品经理授权一次之后，agent 可以把整批节点派完，不需要人再看屏幕。
- **Claude Code 桌面**：`ccd_session__spawn_task` 只是**提议**一个任务。它返回一个 `task_id` 并在界面上显示一张卡片，**需要用户点一下**才真正创建会话；调用方拿不到 `sessionId`。所以每个节点都有一个人工确认点，拿到 `task_id` 之后还要再查一次（`ccd_session_mgmt__list_sessions`）才能取到真实会话 id 去回写 `create`。

在 Claude Code 上把这条流程当成"全自动"会一直卡住：卡片没人点，`create` 永远拿不到身份，run 停在 `retry_pending`。要么守着确认每个节点，要么用 Codex 本地桌面跑派发。

（以上两条平台行为为 2026-09-18 本机实测所得，非推断。仓库里另有一份完整实测记录，
但它不随包发布，所以这里不给路径。）

## 7. 什么时候才能打断产品经理

只有三类：产品设计要变、需要新的外部授权、要部署。其他工程问题（超时、任务创建失败、容量、分支漂移、能力未知）由 vibe 的 Monitor 自行分类恢复；恢复不了的会标 `blocked_unknown` 等待，不会伪装成成功。agent 看到 `blocked_unknown` 或 `retry_pending` 时先按 §6 查信箱里有没有没服务完的请求，不要立刻报告失败。

一个例外要告诉产品经理：在 Claude Code 桌面上派发，每个节点都需要他点一下确认卡片（§6.4）。这属于"需要新的外部授权"那一类，授权卡念完之后就要说清楚，不要让他以为授权一次就不用管了。
