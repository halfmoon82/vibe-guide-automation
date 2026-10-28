# Vibe Guide · PRD 引导协议（宿主 agent 用）

> 本文件随 vibe 包发布，由 `vibe init --confirm` 物化到 `.vibe/proposals/skills/prd-guide/SKILL.md`，也可用 `vibe plan --print-protocol` 直接打印。
> 面向 Claude Code / Codex 等宿主 agent。产品经理不需要读它。

## 阅读地图（按角色读，不必全量前置）

- **PRD 引导会话**（和产品经理谈需求、拆节点、写产品 spec）：读 §0–§5；§2.1 方法论按话题取用，§4 扩范围判据拆节点时再细读。
- **首班监工**：读 §0 + §6，每一拍的动作以随包 `supervisor-card.md`（监工卡）为准；§1–§5 只在要重新发布或改卡时回头读。
- **§6.4 平台差异表只读本宿主一行**；其他平台的行到需要对比时再读。
- **§6.2 整合审查节点的 `accepted` evidence 结构化格式**：到整合审查节点回写时再读。
- **§7 打断边界**：监工准备挂起或建决策时读；PRD 会话在要抛出 `needs_confirmation` 问题时读。

## 0. 分工

- **agent 出内容**：和产品经理对话、只读分析代码、写 PRD、拆节点、记录产品决策。
- **vibe 出协议 + 校验 + 脚手架**：路由分流、校验产品 spec、派生全部工程字段（含每个节点的能力档位）、发布计划、签授权卡、派发监工。
- agent **不得**在产品 spec 里写任何工程字段（见 §5.1 黑名单），vibe 会拒绝。
- agent **不得**替产品经理做产品决策：`decisions[].status` 只有产品经理明确选定后才能写 `approved`。
- **不向产品经理问工程问题**：心跳间隔、换班上限、模型、重试次数、拓扑、worktree 等一律由 vibe 按平台与证据推导，监工自定并留痕。

## 1. 入口判定

**跳过条件**：当前会话已通过 `vibe-entry` 协议做过 `--s1` 自评，跳过本节直接进入 §2。

收到新请求，先原样交给 vibe 分流，不要自己判断复杂度：

```bash
vibe plan --request "<产品经理的原话>" --json
```

- `route` 为 `simple` 或 `light_plan`：直接做，不进入本协议；涉及图形界面时，动手前仍要先过 vibe-entry §4 的界面设计稿门（出稿、等产品经理明确确认）。
- `route` 为 `complex`：vibe 生成 draft（`status: planned`，`execution: deferred_until_authorize`），记下 `plan_id`，进入 §2。
- 不要自己算 `--s1`：分数由 vibe 从文本推导并记进草案，发布时沿用；产品经理明确要求按复杂流程走，用 `--s1 5,5,5,5,5` 显式升级，绝不反向降级。

## 2. PRD 引导对话（只谈产品，不谈工程）

按五个话题逐一确认，每条结论带来源标记：

| 话题 | 要问清楚的 | 典型追问 |
|---|---|---|
| 问题 | 现在哪里不顺、谁遇到、多常见 | "没有这个功能时怎么绕过去？" |
| 目标 | 做完以后什么变了 | "上线一周后看什么判断成功？" |
| 成功标准 | 可观察、可验收 | "验收那天你会点哪几步？" |
| 非目标 | 明确不做的 | "有没有相关但这次不碰的？" |
| 用户场景 | 谁、什么情况、做什么、得到什么 | "举一个最常见的例子。" |

来源标记：

- `user_confirmed`：产品经理亲口说过或明确点头。
- `system_inferred`：agent 从上下文或代码合理推断、产品经理没有反对。**不打断**，直接记下继续。
- `needs_confirmation`：会改变产品方向、授权边界或验收标准的取舍。**必须停下来问**，得到答案前不能进入 §4。
- `unverified`：证据不足、暂时无法核实（多见于代码现状）。

只在 `needs_confirmation` 时提问，每次只问一个问题，说清楚不同答案会怎样改变结果。

**涉及图形界面时，PRD 必须带设计稿**：本次新增和改动的每个页面，在 `prd.design_mockups` 小节写一条，`value` 形如 `订单列表页（改动）：<稿件路径或链接>`。稿件必须打开就能看到页面（图片、HTML 原型、Figma 等链接），纯文字描述不算。产品经理确认前 `source` 写 `needs_confirmation`——vibe 因此拒绝发布，这是正确行为；确认后改 `user_confirmed`；稿件改过则改回重新确认。**设计稿没确认，不得进入 §4，更不得写代码。**

会改变产品方向、授权边界或验收标准的取舍，同时写成 `decisions[]`：`question` / `options`（至少两个、互不相同）/ `impact` / `recommendation`（必须是 options 之一）/ `field`（落在哪个契约字段，如 `export.watermark`）。选定后 `selected` 逐字等于所选 option，`status` 写 `approved`；**没选定的保持 `unresolved`，vibe 会拒绝发布。**

### 2.1 方法论引用（可参考，不强制）

对应话题可参考项目 `.vibe/proposals/skills/<名>/SKILL.md` 里的方法论 skill；未安装时按锁定版本装入（`<名>`、`<子目录>` 取自下表）：

```bash
vibe skill-install --source https://github.com/phuryn/pm-skills --sha 8607e3b077817f89bf4a9b623246219734ac3be0 --subdir <子目录> --name <名> --confirm
```

| 方法论 | 适用话题 | 仓库子目录（`--subdir`） |
|---|---|---|
| opportunity-solution-tree | 问题/场景拆分 | `pm-product-discovery/skills/opportunity-solution-tree` |
| north-star-metric | 成功标准 | `pm-marketing-growth/skills/north-star-metric` |
| metrics-dashboard | 成功标准度量 | `pm-product-discovery/skills/metrics-dashboard` |
| prioritize-features | 非目标与排序 | `pm-product-discovery/skills/prioritize-features` |
| prioritization-frameworks | 非目标与排序 | `pm-execution/skills/prioritization-frameworks` |
| job-stories | 用户场景 | `pm-execution/skills/job-stories` |
| customer-journey-map | 用户场景（可选） | `pm-market-research/skills/customer-journey-map` |
| strategy-red-team | 风险与反例 | `pm-execution/skills/strategy-red-team` |
| pre-mortem | 风险与反例 | `pm-execution/skills/pre-mortem` |

仅作参考、不强制，未安装时提示安装命令后继续，不得阻塞对话；输出仍为中文，不得把方法论产出另存为额外 markdown 文件；不得改变来源标记与停等门。

## 3. 只读代码分析

对话结束后、拆节点之前，只读地看代码，禁止改任何文件。找出入口点、涉及模块、现有行为（含已知问题）、现有测试。产出：

1. `prd.code_evidence[]`：每条一句话 + 项目相对路径（可带符号名），来源标 `unverified` 或 `system_inferred`。
2. 每个 `goals[]` 条目的 `code_evidence` 字段（§5.1）。

用产品经理能懂的话说结论，不念文件路径。

## 4. 拆节点

原则：默认并行，只把真正阻塞启动的关系写成硬依赖。

- `depends_on`：没有它完成就**无法开始**的节点。
- `integration_after`：可以先各自开发、最后联调的关系，不阻塞启动。
- `parallel_group`：可同时开工的一组，用同一个组名；不需要分组写 `null`。
- **写范围重叠的节点必须用 `depends_on` 串行。**两个节点的 `files` 有重叠时，`integration_after` 和 `parallel_group` 都不能解除冲突——它们不阻塞启动，两个 writer 会同时写同一批文件被审计拒绝。先把同文件工作归到同一节点；确要拆开，用 `depends_on` 串起来。同一 `parallel_group` 内的重叠连 `depends_on` 也救不了，只能拆开路径或把其中一个移出该组。
- 每个节点 `contract` 四个字段用验收语言写，不写实现细节：`input`（拿到什么）、`output`（交出什么）、`error_behavior`（出错时对用户的表现）、`acceptance_example`（验收者照着做就能判断通过与否的具体例子）。
- `files`：节点会动的文件，项目相对路径。这是**工程文件清单**，不是产品范围，扩展清单本身不等于产品设计变化；宁可列全。漏列的文件可由监工不问产品经理、自行补进该节点范围，须同时满足：① 在本仓库内；② 不涉及部署、凭据、外部系统配置；③ 没有被其他活跃节点的写范围覆盖；④ 留下 `scope_auto_expanded` 纠偏记录；⑤ 走通下面两类判据之一。

  - **判据一（机械规则，始终有效）**：文件在 `tests/` 下（含 `tests/` 下的依赖清单），或在 `.vibe/config.json` 的 `auto_scope_paths` 里登记过（逐条精确匹配，不支持通配符；登记本身即产品经理授权，所以不要登记依赖清单、部署脚本或凭据文件；配置缺失或非法按空清单处理）。
  - **判据二（验收判定不变，需项目显式批准）**：每个已批准 PRD 验收例子的判定结果都不变，且文件属于低风险源码或文档（白名单与排除项见 `planner._approved_scope_rule`）。只在项目 `.vibe/config.json` 写入 `acceptance_scope_rule_approved: true` 后生效，且该开关必须与项目 `AGENTS.md` §6 的同一规则成对出现；未开启一律按判据一处理。

  两类边界必须问产品经理：新增第三方依赖（发布/安全对外承诺）、执行模式从「可见可进入的任务」降级为「后台不可见」；验收判定改变同样单独授权。

  判据二用 `vibe scope-expand --run-id <run> --node <node> --payload '<json>'` 提出，只对**已验收、当前没有活跃任务**的节点生效（开发中途的扩围走事件自报，只按判据一）。payload 恰好五个键：`files`（至少一条、不重复、归一化仓库相对路径）、`acceptance_examples_unchanged`（必须 `true`）、`adds_third_party_dependency`（必须 `false`）、`visibility_downgrade`（必须 `false`）、`reason`（非空、≤200 字符、无控制字符）。缺键、多键、取值不符或路径不合法整条拒绝，不做部分补入。

  交付和验收汇报逐节点列出 `scope_expansions`（`vibe status --json` 同名字段，为空写明无）。不在两类判据内的文件按 §7 单独授权。
- 节点 `id` 用小写字母、数字、连字符；不要写 `integration-review`，vibe 会自动追加最终整合审查节点。
- **能力档位由 vibe 派生**：每个节点得到平台无关的 `light` / `standard` / `deep` 三档之一（纯文档→light；带 migration/security/cross_module 标签、改动文件 ≥8 或跨 3 个以上模块→deep；其余 standard；reviewer 至少 standard；整合审查 deep）。档位不写进产品 spec，写了会被拒。

拆完用产品语言复述一遍。

## 5. 产出与提交

### 5.1 产品 spec 的形状

写成一个 JSON 文件：用 §1 草案的同一个 `plan_id` 发布，放 `.vibe/plans/<plan_id>/product-spec.json`；用没有草案的新 `plan_id` 发布，放 `.vibe/plans` 之外（如项目根 `product-spec.json`）。不要事先建 `.vibe/plans/<新编号>/`，否则发布报 `plan already exists`。

结构以 `vibe_guide.node_spec.PRODUCT_SPEC_FIELDS` 为准，测试会断言本节与代码一致：

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
  "remote_git_actions?": "allow|deny",
  "heartbeat_interval_minutes?": "10|60",
  "rotate_hard_cap?": "150000|200000|300000|500000|800000",
  "dispatch_mode?": "background|visible"
}
```

`prd` 的推荐 section：`problem`、`user_scenarios`、`success_criteria`、`non_goals`、`code_evidence`；涉及图形界面时必须有 `design_mockups`（§2）。

`heartbeat_interval_minutes`、`rotate_hard_cap`、`dispatch_mode` 是授权卡上由 vibe 按平台推导的运行参数，缺省即可，**不向产品经理询问**（见 §5.3）。

**黑名单（写了就被拒）**：顶层 `complexity_band` `route` `route_result` `capabilities` `project_id` `integration_contract` `spec_path` `plan_id`；节点 `status`；`contract` 内 `adapter_id` `project_id` `worker` `reviewer_worker` `worker_profile` `worktree` `branch` `writer` `reviewer`。`worktree` 与 `branch` 由 vibe 按节点 id 派生成互不相同的一对（`.worktrees/<节点>` 与 `node/<节点>`），agent 写死会让并行节点撞进同一棵树。

### 5.2 登记本会话的能力（复杂计划发布前必做一次）

vibe 不猜宿主平台有什么能力，由 agent 盘点**这次会话里实际看到的**工具后登记：

```bash
vibe attest --adapter <claude-code|codex|...> --facts <facts.json> --provenance "<一句话说明依据>" [--project-id <id>]
```

`facts.json` 的键名以 `vibe_guide/adapters/manifests/<adapter>.yaml` 的 probes 为准，值只能是 `true` / `false`，看到对应工具才写 `true`。`project_id` 是宿主平台里这个项目的标识，可见任务派发需要它。

### 5.3 发布、授权、开工

发布前只问产品经理一件事：允许还是禁止远端 Git 动作（提交、推送、开 PR、合并），写进产品 spec 的 `remote_git_actions`（`allow` / `deny`，缺省 `deny`）。**不问心跳间隔与换班上限**：授权卡上的 `heartbeat_interval_minutes` 等运行参数由 vibe 推导（推送唤醒已验证的平台兜底检查为 60 分钟，未验证为 10 分钟），产品经理要改时才改。

```bash
vibe plan --request "<产品经理的原话>" --plan-id <plan_id> --from-prd <product-spec.json> --json
```

用 §1 草案的同一个 `plan_id` 发布，vibe 替换这份草案；已发布或已授权的计划不会被覆盖，仍报 `plan already exists`。发布不必传 `--s1`。替换中途被打断或 reason 含 `draft could not be restored` 时，按 reason 给出的路径（没给就找 `.vibe/plans/.<plan_id>.draft.*/draft`，多个时请产品经理选）确认草案：①没被挪动，原命令重发；②被挪开且 `.vibe/plans/<plan_id>` 不存在，挪回再重发；③被挪开且该目录已存在，不覆盖、不当成本次成功，取回产品 spec 放到 `.vibe/plans` 之外，换新 `plan_id` 重发（`--s1` 照草案 `plan.json` 里 `route_result.dimensions` 依次填），旧编号原样留着由产品经理决定。

- `status: ok`：vibe 已生成 PRD、Spec、Issue、DAG 审计、授权卡。把 `authorization_card` 用产品语言念给产品经理：做哪几件事、哪些同时开工、每件事一个开发任务加一个独立审查任务、每个节点的能力档位与当前平台换算出的模型、派发方式（Claude Code 默认后台、可改选可见卡片，见 §6.4）、远端 Git 开关、永远不含部署/发布/生产写入/凭据/对外通信。
- 分开说两件事，别让 `allow` 听起来像"允许了又排除了"：`excluded_actions` 里的推送/合并是开发任务自己永远不推送；`allowed_actions` 里的推送/合并是产品经理允许后进入授权范围的动作。**合并规则**：独立终审通过、P0–P2 清零后，vibe 在授权范围内标记「可合并」，监工执行合并命令，vibe 再核实合并提交可达目标分支后才记为已合并；监工自报不算证据。选 `deny` 时 `allowed_actions` 不含远端 Git 动作，`excluded_actions` 照样列着。
- 想改远端 Git 开关或运行参数：`vibe authorize` 改不了。复制产品 spec 到 `.vibe/plans` 之外改字段，用新 `plan_id` 重新发布（`--s1` 照抄首次结果里的 `s1`），再念新卡。
- 引擎证明有效期 72 小时：同一 plan revision 超期后，`vibe authorize` 重新观测引擎凭证并自动续签，不需要重新发布计划；PRD、Spec、节点文件真有改动时不借续签放行。
- `status: blocked`：reason 以 `product_spec_requires_complex_route` 开头→用草案 `plan_id` 发布或 `--s1` 显式升级；含 `product decisions remain unresolved`→回 §2；含 `engine_attestation_unavailable`→先做 §5.2。`blocked_design` 且带 `question`：未决产品问题，原样问产品经理。

产品经理说"授权"后：

```bash
vibe authorize --plan <plan_id> --authorize AUTHORIZE --json
vibe monitor --plan <plan_id> --authorize AUTHORIZE --json
vibe daemon start --run-id <run-id> --plan <plan_id>
```

之后 `vibe status --plan <plan_id>` 看进度，`vibe resume --plan <plan_id>` 从断点续接。

## 6. 监工：守护程序 + 短会话 + 三级自愈（复杂计划开工后）

完整每拍动作见随包 `vibe_guide/protocols/supervisor-card.md`（监工卡，≤3KB，`vibe supervisor-beat` 在 work 时按班次输出）；本节只写形态、边界和平台事实，不重复卡里的规则。

### 6.0 形态与开工

- **守护程序干机械活**：授权后 `vibe daemon start|status|stop --run-id <run-id>` 起一个分离的本地进程，持监工租约，每个 tick 确定性地 resume、收交付、派后台 worker、拉 reviewer、接受节点、一级自动重试；pid、心跳、最近错误写在运行目录，崩溃或租约丢失不产生第二个写入者。它不调用模型。
- **产品经理会话只保留 60 分钟兜底检查**：会话登记地址（`vibe supervisor-register --run-id <run-id> --provider <平台> --session-id <本会话 id> --host <本机标识> --session-record <本会话记录路径>`；`vibe daemon register-pm --run-id <run-id> --provider <平台> --session-name <会话名>` 登记推送目标），心跳指令逐字用 vibe 打印的 `vibe supervisor-beat --run-id <run-id> --accept-decision --session-record <本会话记录路径>`，每 60 分钟（推送唤醒未验证的平台为 10 分钟）一拍：先 `vibe daemon status`，失活则重启或上报，存活就按监工卡结束本轮。不要自写心跳，也不要把信箱服务写进心跳。
- **推送优先**：worker 交付后用 `vibe supervisor-address --run-id <run> --node <n> --role <r> --generation <g>` 回答里的 `wake`（`channel`、`target`、`signal`）原样推送唤醒登记的会话；`channel` 为 null 或地址 unknown 时不发，由兜底检查领取。监工不信任信号内容。
- 状态一律从磁盘读，不凭记忆；未知状态不当成功；宿主报 `No such tool available` 不是节点故障，重试一次后停下报告，不交班。

### 6.1 三级自愈与升级

1. **一级（守护程序自动）**：节点空转（同一节点连续数拍状态、代数、活跃任务、最新交付都不变）时先自动重试，不耗模型。
2. **二级（修复短会话）**：仍空转到「需要监工排查」档，守护程序按平台拉起**一次**一次性短会话（claude-code 用 `claude -p`，codex 用 `codex exec`，kimi-code 用 `kimi -p`；WorkBuddy 无命令行，写入信箱由其自动化每次新会话处理），模型按节点档位换算，输入排查包 + 监工卡 + 授权范围。会话在授权范围内修复（可改排查包列出的运行时文件，不得直接改 `state.json` 和 `events.jsonl`），**必须以 `vibe heal-verify --run-id <run-id> --node <节点>` 结束**，由守护程序复核；通过才推进并记 `self_heal_attempt`。每个运行同时最多一个短会话，次数与 token 有预算，用尽立即升级。
3. **三级（升给产品经理）**：同一空转指纹两次修复无进展，或修复需要越过产品范围、授权卡、deploy、凭据与系统权限边界（§7），守护程序建决策卡并推送到登记的会话，标签变为「需要人处理」；带排查包和已试过的尝试停下报告，不第三次重试，不挂成 `engineering` 类决策。

短会话命令不可用或超时记为 unknown 并升级，不当成功；返回用量里缓存读取连续为零记 `cache_health` 告警。

### 6.2 信箱回写

可见派发（卡片、窗口会话）的请求由监工服务；后台派发由守护程序直接执行，不经信箱。每份请求用 `vibe supervisor-mailbox --run-id <run-id> --json` 看摘要（正文换成 `{sha256, bytes, path}`），用 `vibe supervisor-mailbox --template <action_id>` 取回写示例，填好后 `vibe supervisor-mailbox --complete <action_id> --result <文件或 ->` 回写；形状不对在写入前被拒，错误列出缺少或多出的字段名，不产生 `blocked_unknown`。回写按操作给字段：

| 操作 | payload |
|---|---|
| `create` | `{"binding": {"task_id": "<真实会话 id>", "host": "<本机标识>"}}`（`threadId`/`hostId` 同样接受；没有真实会话 id 就不回写） |
| `locate` | `{"located": true}` |
| `visibility` | `{"visible": true, "direct_enter": true}` |
| `wait`（未完） | `{"status": "timeout", "cursor": "<游标>"}` |
| `wait`（developer 完） | `{"status": "completed", "cursor": "<游标>", "event": "complete", "delivery_evidence": {"completion_marker": "…", "delivery_path": "…", "thread_status": "complete"}}`（嵌套对象，三键齐全，摊平无效） |
| `wait`（reviewer 完） | `{"status": "completed", "cursor": "<游标>", "event": "accepted", "evidence": "<P0–P2 清零证据>"}` |
| `resume` | `{"resumed": true}`（沿用原会话 id，不回写绑定） |

`cursor` 给就必须是非空字符串（≤4096、无 NUL），不知道就整个不给。不确定就不回写，pending 比假成功便宜。回写被拒后不要重写同一 `action_id`；`vibe resume` 会发新的 `create`（`generation` 加一），认最大的那个。worker 自报（`vibe worker-deliver`）没有游标，交付门改认自报回执，不要再手工回写同代 `wait`。

**整合审查节点的 `accepted`**是整个 run 的验收，`evidence` 必须是对象，**恰好四个键**：

```json
{
  "findings": [{"severity": "p0|p1|p2|p3|p4", "status": "open|resolved|accepted|waived", "detail": "<一句话>"}],
  "iteration_compatibility": {"status": "verified|compatible|reviewed", "evidence": "<怎么核实的>"},
  "test_runtime_delivery": {"status": "verified|reviewed", "evidence": "<怎么核实的>"},
  "out_of_scope": ["<聚合范围之外被改动的东西>"]
}
```

- 全部清零就是 `findings: []`、`out_of_scope: []`。**只有 `resolved` 算清零**，`open`/`accepted`/`waived` 计入 `clearance` 落 `blocked_unknown`；`p3`/`p4` 是观察项不计入。没修完就报 `review_finding` 返工，别改字。
- 两个判断的 `evidence` 必须是非空字符串；`out_of_scope` 非空即拒；`status`、`severity` 是封闭取值，同义词判不完整。
- 只能给这四个键，其余由 vibe 派生。格式错误记 `acceptance_rejected`（可修正重报）；整合合同 digest 漂移和未清零 P0–P2 才 fail-closed 落 `blocked_unknown`。拒收原文落盘被打码，只能对照自己发出的 claim 查。

### 6.3 派发合同

`request.child_binding` 的 `worktree`、`branch`、`allowlist` 由 vibe 按节点 id 派生，每个节点互不相同。**创建会话必须把工作目录设成合同里的 `worktree`**，让它在合同的 `branch` 上开发，只碰 `allowlist` 里的文件；worktree 需先建好（`git worktree add <worktree> -b <branch>`），vibe 不替你建。

| 宿主 | 设定工作目录的方式 | 做不到时的后果 |
|---|---|---|
| Claude Code | 派发时传 `cwd` | 会话启动就落在 `worktree` 里 |
| WorkBuddy | Agent 工具**没有 `cwd` 参数** | 派发提示里逐条要求**每条命令先 `cd <worktree>`**、写文件用绝对路径；串行节点也一样 |

**唯一例外是交付自报**：`vibe worker-deliver` 必须在**主项目目录**执行（在 worktree 里会写进 worktree 自己的 `.vibe`，监工看不到）。

模型与监工独立：每个 developer/reviewer 的模型由节点档位按平台探针换算（探针失败回退平台默认并标注），派发和续接显式传模型与思考档位；同一节点审查打回两次自动升一档，监工或修复会话可 ±1 档但必须给理由。具体模型名不进授权指纹，换平台或换模型授权不失效。

**环境事实**：改动第三方 UI 组件用法的节点，IssueContract 必须带 `environment_facts`（`[{"fact": "str", "source": "str", "verified_at": "str"}]`），否则合同不闭环、不能开工。节点 goal、owned_paths 或 read_paths 命中 `vibe_guide.contracts.THIRD_PARTY_UI_LIBRARY_MARKERS`（大小写不敏感）而该字段缺失或为 `[]` 时拦停；`fact` 与 `source` 必须非空。`source` 含 `://` 视为外部引用，不含 `/` 且无常见文件扩展名视为描述性文字，其余按仓内相对路径、文件必须存在，绝对路径或含 `..` 越界。拆节点时就把实测事实核好。

### 6.4 平台差异：哪个平台能"授权一次、不用盯屏幕"

五个操作在三个可见 provider 上都已登记，Claude Code 另有降级后台 provider `claude-code-background`；创建会话时要不要人工点一下，各平台不一样：

| 操作 | `codex-app-visible` | `claude-code-visible` | `workbuddy-visible` | `claude-code-background` |
|---|---|---|---|---|
| `create` | `codex_app__create_thread` | `ccd_session__spawn_task` | `workbuddy_job__create` | `claude --bg`（详见下方条目） |
| `locate` | `codex_app__navigate_to_codex_page` | `ccd_window__open_session_in` | `workbuddy_job__get` | `claude attach <short-id>`（仅真实终端） |
| `visibility` | `codex_app__wait_threads` | `ccd_session_mgmt__get_session` | `workbuddy_job__list` | `claude agents --json --all` |
| `resume` | `codex_app__send_message_to_thread` | `ccd_session_mgmt__send_message` | `workbuddy_job__reply` | `claude stop` + `claude --bg --resume <完整会话 id>` |
| `wait` | `codex_app__wait_threads` | `ccd_session_mgmt__list_events` | `workbuddy_job__wait` | 轮询 `claude agents --json --all` 的 `state` + `vibe worker-deliver` 自报 |

- **Codex 本地桌面**：`codex_app__create_thread` 直接创建用户可见的 thread，无人值守即可全自动；Codex 的 `create` 不得用 `fork_thread` 替代（fork 出的会话被 `complete()` 拒收）。要在 Claude Code 上无人值守，改走 `claude-code-background`。
- **Claude Code 桌面**（未配置后台启动器时）：`ccd_session__spawn_task` 只是**提议**一个任务，界面上出一张卡片，**需要用户点一下**才创建会话，调用方拿不到 `sessionId`，要再 `ccd_session_mgmt__list_sessions` 取真实会话 id 回写 `create`。把它当"全自动"会一直卡在 `retry_pending`，授权卡念完就要告诉产品经理这一点。
- **Claude Code 后台（`claude-code-background`，降级后台）**：Claude Code 的默认派发方式，授权一次、不用盯屏幕，授权卡上可改选可见卡片。需要配置后台启动器（README「Claude Code 后台派发」节：在用户级 `$VIBE_HOME/config.json` 登记凭据设置文件与默认模型），且会话用 `vibe attest` 登记了 `claude-code.background_task.launch` 为真；未配置时仍走卡片路径。降级限制三条：① 桌面侧边栏看不到，`get_session` / `open_session_in` 也找不到；② 只能在真实终端 `claude attach <short-id>` 进入；③ 返工可续接同一会话——先 `claude stop`，再用**完整会话 id** 执行 `claude --bg --resume <完整会话 id> <返工消息>`，**除完整会话 id 和返工消息外不带任何其他参数**（如 `-n`、`--model`），否则会复制出新会话；vibe 发现复制会停掉副本并报未知。权限用 `bypassPermissions`（`acceptEdits` 第一条 Bash 就卡在 `blocked`，`dontAsk` 直接拒绝 Bash 却报 `done`）。每次派发显式带 `--model`（由节点档位换算）。`state=done` 只表示这一轮结束、不代表任务成功，完成依据是 worker 的 `vibe worker-deliver` 自报；`blocked` 需要处理，不能当进行中。vibe 不保存密钥，只记设置文件路径。未验证、不得当作可用：`--session-id` 预指定会话 id、`-w` 与 `--bg` 合用、长任务/休眠/daemon 退出后的恢复、桌面内置 CLI 下的 `--bg`。
- **WorkBuddy**：`workbuddy_job__*` 五个工具已登记，但 Jobs API 派生的 detached agent 拿不到模型凭据（凭证边界为宿主设计，补 flag 或环境变量修不好），可用的派发目标是用户已在 UI 打开的窗口会话（`workbuddy_job__create` 带 `session_id`，一个窗口同时只承载一个任务）。没有自动换班入口，也不得用 GUI 点击代开窗口。

**推送唤醒通道**（`vibe supervisor-address` 返回的 `wake.channel`）按平台登记：Codex 为 `send_message_to_thread`，Claude Code 为 `SendMessage`（在 `.vibe/push-wake-verification.json` 补齐成功、失败证据各一条之前视为未验证，兜底检查保持 10 分钟），WorkBuddy 没有登记通道（`channel` 为 null，由兜底检查领取）。

### 6.5 换班、预检分支与其他

- **不换班（Claude Code）**：Claude Code 没有换班原语，预检永不输出 `rotate`，到上限改为 `work` 并附 `context_pressure` 提示，同会话继续。
- **换班（其他平台）**：单班是默认（`supervisor_single_shift` 缺省 `true`），只在达到绝对上限（缺省 20 万，上限档位见授权卡）时输出 `rotate`。执行换班：新会话**必须建在本项目里**（Codex 用 `create_thread` 的 project 目标；projectless 的 cwd 落在宿主自动生成目录，每条读写 `.vibe/` 的命令逐条弹审批），必须是**全新会话**——禁止 `fork_thread`，`supervisor-register` 会拒绝 fork 出的会话、起步不低于 10 万的会话、以及不带 `--session-record`（绝对路径、文件存在）的接班登记。让它跑 `vibe supervisor-handoff --run-id <run-id>`，打印接班所需的全部内容，新会话照做：登记、建兜底检查、旧会话删除自己的检查并归档。接班会话写不了 `.vibe/`（`session_gate_blocked`，带 `host sandbox denied access`）时不要重试，停下把目录报给用户；WorkBuddy 的换班只能由用户在本项目新开会话接班。
- **预检**：`vibe supervisor-preflight --run-id <run-id> --accept-decision --session-record <本会话记录路径>`（每拍只运行一次），输出分支以监工卡为准：`contract_drift`（能力合同与运行快照 digest 不一致，停止推进，走同一运行的重新授权）、`decision`（渲染 `.vibe/decisions/` 未答卡片，降频到 60 分钟不暂停）、`idle`（回一字结束，不做任何写）、`work`、`unknown`（读不到会话记录，不当 idle，按 `work` 处理）。
- **等人拍板必须登记**：`vibe supervisor-hold --run-id <run-id> --node <节点> --category <product_acceptance|external_authorization> --decision <决策ID>|--authorization-gap <缺口> --reason <一句话>`；用户用 `decision-answer`（必须带决策卡 `--digest`）回答后由运行自动消费，不必手工释放；不经决策的授权缺口挂起用 `--release` 手工释放。挂起满 6 小时或任一节点状态变化，预检报 `work`（`hold needs recheck`），复核后仍在等人就刷新。`review`/`rework` 节点不能挂起。不登记，空闲心跳每拍被判 `work`，成本是设计值的数倍。

## 7. 什么时候才能打断产品经理

**已批准判据**：修改后，PRD 里每条验收例子的判定结果是否改变——不变由监工自决并留痕，变了才问；另外两类无论如何都问：新增第三方依赖、执行模式从「可见可进入的任务」降级为「后台不可见」。落地条件见 §4。

只有四类：产品范围或设计要变、部署或对外发布、涉及凭据或系统权限、授权卡外的不可撤销动作。`supervisor-hold` 与 `decision-raise` 只接受这四类（落盘仍是 `product_acceptance` / `external_authorization` 两个规范类别；`credentials`、`system_permission`、`irreversible_action` 是后者的别名），其他类别（如 `engineering`）直接报错并提示走自愈。其他工程问题（超时、任务创建失败、容量、分支漂移、能力未知、可按 §4 自动补进范围的文件）由 vibe 自行分类恢复。

**结构性待验项只认固定句式**：「[真机/生产环境/线上前缀]＋事项名词短语（中文 ≤8 字、英文 ≤5 词；不含否定、程度、谓语或“出现/发生/显示/返回/反馈/报错”类词，不含数字加单位，不以空白/模糊/低/慢/失灵/失效/延迟/late/delayed 等状态词结尾）＋[10/10]＋只能/仅能/需/须/待＋[在]验收期/上线后/真机上/生产环境＋验证/确认/完成」，英文同理（“<短名词> can only be verified in the acceptance period”），另认“验证脚本不入库/仅本地运行”；其余说法一律按未知处理、保持 `blocked_unknown`。符合句式的事项自动记为 `deferred_to_acceptance` 处置、节点照常进入验收，并单列在交付与验收汇报的“验收期待验项”（`vibe status` 的 `acceptance_pending_verification`，有则必列）；同一事项换措辞重报沿用原处置。

`blocked_unknown` 的恢复动作按原因执行，处置用 `vibe blocked-unknown --run-id <run> --node <节点> --disposition <处置> --reason <原因>` 记录，节点状态在 `vibe status` 可见，并保留结构化 `recovery_action`：

- `repair_node`：工作树、分支、合同测试或快照可由本地证据修复；保持原 task identity、generation、worktree 和 writer lease。
- `requeue_same_task`：超时、断开、容量或 provider pending；只续接同一任务，按退避等待，不创建 successor。
- `reconcile_binding`：绑定、身份、租约或 cursor 无法证明；先核对 task binding、worktree、branch、run 和 cursor，再走 `vibe repair --plan <plan> --run-id <run> --node <node>` 或同一 Monitor 的 repair/requeue 入口；未证明前不得盲派。
- `await_external_decision`：命中产品、外部授权或 deploy 边界；保持 blocked，等对应授权。

三轮返工仍不收敛时，监工记录换 reviewer 或拆节点并保留唯一 writer；只有会改变 PRD 验收判定的分歧才升级产品决策。reviewer 代改业务文件时本轮作废，退回 developer 并更换 reviewer。

一个例外要告诉产品经理：Claude Code 改选可见卡片派发时，每个节点都需要他点一下确认（§6.4）。授权卡念完就要说清楚，不要让他以为授权一次就不用管了。
