# Vibe Guide · PRD 引导协议（宿主 agent 用）

> 本文件随 vibe 包发布，由 `vibe init --confirm` 物化到 `.vibe/proposals/skills/prd-guide/SKILL.md`，也可用 `vibe plan --print-protocol` 直接打印。

## 阅读地图（按角色读）

- **PRD 引导会话**：读 §0–§5；§2.1 按话题取用，§4 扩范围判据拆节点时细读。
- **授权前打断边界**：要抛 `needs_confirmation` 时看 §5 收尾。
- **开工之后**：监工、整合审查回写与接班只读随包执行协议（`execution.md`，原监工与打断章节整体迁入、小节编号不变）、角色合同与 `supervisor-card.md`，不回读本协议。

## 0. 分工

- **agent 出内容**：对话、只读分析代码、写 PRD、拆节点、记录产品决策。
- **vibe 出协议 + 校验 + 脚手架**：路由分流、校验产品 spec、派生全部工程字段（含节点能力档位）、发布计划、签授权卡、派发监工。
- agent **不得**在产品 spec 里写工程字段（见 §5.1 黑名单），vibe 会拒绝。
- agent **不得**替产品经理做产品决策：`decisions[].status` 只有产品经理明确选定后才能写 `approved`。
- **不向产品经理问工程问题**：心跳间隔、换班上限、模型、重试次数、拓扑、worktree 等一律由 vibe 按平台与证据推导，监工自定留痕。

## 1. 入口判定

**跳过条件**：当前会话已通过 `vibe-entry` 协议做过 `--s1` 自评，跳过本节直接进入 §2。

新请求先原样交给 vibe 分流，不要自己判断复杂度：

```bash
vibe plan --request "<产品经理的原话>" --json
```

- `route` 为 `simple` 或 `light_plan`：直接做，不进入本协议；涉及图形界面时，动手前仍要先过 vibe-entry §4 的界面设计稿门。
- `route` 为 `complex`：vibe 生成 draft（`status: planned`），记下 `plan_id`，进入 §2。
- 不要自己算 `--s1`：分数由 vibe 推导并记进草案，发布时沿用草案评分；产品经理明确要求走复杂流程时用 `--s1 5,5,5,5,5` 显式升级，绝不反向降级。

## 2. PRD 引导对话（只谈产品，不谈工程）

五个话题逐一确认，结论带来源标记：

| 话题 | 要问清的 |
|---|---|
| 问题 | 现在哪里不顺、谁遇到、多常见 |
| 目标 | 做完以后什么变了 |
| 成功标准 | 可观察、可验收 |
| 非目标 | 明确不做的 |
| 用户场景 | 谁、什么情况、做什么、得到什么 |

来源标记：

- `user_confirmed`：产品经理亲口说过或明确点头。
- `system_inferred`：agent 从上下文或代码合理推断、产品经理没反对。**不打断**，记下继续。
- `needs_confirmation`：会改变产品方向、授权边界或验收标准的取舍。**必须停下来问**，得到答案前不进入 §4。
- `unverified`：证据不足、暂无法核实（多见于代码现状）。

只在 `needs_confirmation` 时提问，每次一个问题，说清不同答案的影响。

**涉及图形界面时，PRD 必须带设计稿**：本次新增和改动的每个页面，在 `prd.design_mockups` 小节写一条，`value` 形如 `订单列表页（改动）：<稿件路径或链接>`。稿件必须打开就能看到页面（图片、HTML 原型、Figma 等链接），纯文字描述不算。产品经理确认前 `source` 写 `needs_confirmation`（vibe 因此拒绝发布，这是正确行为）；确认后改 `user_confirmed`；稿件改过，把这条改回 `needs_confirmation`。**设计稿没确认，不得进入 §4，更不得动手写代码。**

会改变产品方向、授权边界或验收标准的取舍，同时写成 `decisions[]`：`question` / `options`（至少两个、互不相同）/ `impact` / `recommendation`（必须是 options 之一）/ `field`（契约字段，如 `export.watermark`）。选定后 `selected` 逐字等于所选 option，`status` 写 `approved`；**没选定的保持 `unresolved`，vibe 拒绝发布。**

### 2.1 方法论引用（可参考，不强制）

可参考项目 `.vibe/proposals/skills/<名>/SKILL.md` 里的方法论 skill；未安装时按锁定版本装入（`<名>`、`<子目录>` 取自下表）：

```bash
vibe skill-install --source https://github.com/phuryn/pm-skills --sha 8607e3b077817f89bf4a9b623246219734ac3be0 --subdir <子目录> --name <名> --confirm
```

| 方法论 | 适用话题 | 仓库包（`--subdir` = `<包>/skills/<名>`） |
|---|---|---|
| opportunity-solution-tree | 问题/场景拆分 | `pm-product-discovery` |
| north-star-metric | 成功标准 | `pm-marketing-growth` |
| metrics-dashboard | 成功标准度量 | `pm-product-discovery` |
| prioritize-features | 非目标与排序 | `pm-product-discovery` |
| prioritization-frameworks | 非目标与排序 | `pm-execution` |
| job-stories | 用户场景 | `pm-execution` |
| customer-journey-map | 用户场景（可选） | `pm-market-research` |
| strategy-red-team | 风险与反例 | `pm-execution` |
| pre-mortem | 风险与反例 | `pm-execution` |

未安装不得阻塞对话；输出仍为中文，不得把方法论产出另存为额外 markdown 文件；不得改变来源标记与停等门。

## 3. 只读代码分析

拆节点前只读看代码，禁止改文件：找出入口点、涉及模块、现有行为（含已知问题）、现有测试。产出 `prd.code_evidence[]`（每条一句话 + 项目相对路径，可带符号名，来源标 `unverified` 或 `system_inferred`）和各 `goals[]` 条目的 `code_evidence` 字段（§5.1）。

## 4. 拆节点

- `depends_on`：它没完成就**无法开始**的节点。
- `integration_after`：先各自开发、最后联调的关系，不阻塞启动。
- `parallel_group`：可同时开工的一组，用同一组名；不分组写 `null`。
- **写范围重叠的节点必须用 `depends_on` 串行。**两节点 `files` 重叠时，`integration_after` 和 `parallel_group` 都不能解除冲突（它们不阻塞启动）；先把同文件工作归到同一节点，确要拆开用 `depends_on` 串起来。同一 `parallel_group` 内的重叠连 `depends_on` 也救不了，只能拆开路径或移出该组。
- **每条 `depends_on` 都要有理由，否则发布被拒（决定 D6）。**vibe 为每条依赖边自动判定理由类型：两端 `files` 重叠记为“写范围重叠”；不重叠但下游节点在 `dependency_reasons` 里为这条边写了一句话，记为“数据依赖”；两者都没有记为“无理由”，`vibe plan` 拒绝发布并指明边。修改办法二选一：① 补理由——在下游节点写 `"dependency_reasons": {"<上游节点 id>": "依赖 <上游节点> 产出的 xxx 文件格式"}`，一句话说明依赖谁的什么产出；② 去掉这条 `depends_on`，改为并行。理由缺失、为空字符串、只有空格、不是字符串、含换行或超过 200 字，一律按“无理由”处理。不要为了省事把节点串成一条线：DAG 审计结果（`dag.yaml` 的 `audit.serial`）会写出最大并行宽度、关键路径长度和每条边的理由类型，供授权卡展示。5.2.6 及更早已发布的计划不重新审计。
- 每个节点 `contract` 四个字段用验收语言写，不写实现细节：`input`（拿到什么）、`output`（交出什么）、`error_behavior`（出错时对用户的表现）、`acceptance_example`（验收者照做就能判断通过与否的具体例子）。
- `files`：节点会动的文件，项目相对路径。这是**工程文件清单**，不是产品范围；宁可列全。漏列的文件监工可不问产品经理、自行补进该节点范围，须同时满足：① 在本仓库内；② 不涉及部署、凭据、外部系统配置；③ 没有被其他活跃节点的写范围覆盖；④ 留下 `scope_auto_expanded` 纠偏记录；⑤ 走通下面两类判据之一。

  - **判据一（机械规则，始终有效）**：文件在 `tests/` 下（含 `tests/` 下的依赖清单），或在 `.vibe/config.json` 的 `auto_scope_paths` 里登记过（逐条精确匹配，不支持通配符；登记本身即产品经理授权，这一支不再逐条检查依赖清单、敏感目录或外部配置根，所以不要登记依赖清单、部署脚本或凭据文件；配置缺失或非法按空清单处理）。
  - **判据二（验收判定不变，需项目显式批准）**：每个已批准 PRD 验收例子的判定结果都不变，且文件属于低风险源码或文档（白名单与排除项见 `planner._approved_scope_rule`）。只在项目于 `.vibe/config.json` 写入 `acceptance_scope_rule_approved: true` 后生效，且该开关必须与项目 `AGENTS.md` §6 的同一规则成对出现；未开启的项目一律按判据一处理。

  两类边界（新增第三方依赖、可见降级为后台不可见）和验收判定改变必须问产品经理（§7）。唯一例外：跨平台交接时新平台把可见改成后台，只披露（写进交接事件和新授权卡）、不确认。

  判据二用 `vibe scope-expand --run-id <run> --node <node> --payload '<json>'` 提出，只对**已验收、当前没有活跃任务**的节点生效（开发中途扩围走事件自报，只按判据一）。payload **恰好五个键**：`files`（至少一条、不重复、归一化仓库相对路径）、`acceptance_examples_unchanged`（必须 `true`）、`adds_third_party_dependency`（必须 `false`）、`visibility_downgrade`（必须 `false`）、`reason`（非空、≤200 字符、无控制字符）。缺键、多键、取值不符或路径不合法整条拒绝，不做部分补入。

  交付和验收汇报逐节点列出 `scope_expansions`（`vibe status --json` 同名字段，空写无）。不在两类判据内的文件请产品经理单独授权。
- 节点 `id` 用小写字母、数字、连字符；不要写 `integration-review`，vibe 自动追加整合审查节点。
- **能力档位由 vibe 派生**：每个节点得到平台无关的 `light` / `standard` / `deep` 三档之一（纯文档→light；带 migration/security/cross_module 标签、文件 ≥8 或跨 3 个以上模块→deep；其余 standard；reviewer 至少 standard；整合审查 deep）。档位不写进产品 spec，写了被拒。

## 5. 产出与提交

### 5.1 产品 spec 的形状

写成一个 JSON 文件：
- 用 §1 草案的同一个 `plan_id` 发布：建议放在 `.vibe/plans/<plan_id>/product-spec.json`，发布时随草案一起保留。
- 用没有草案的新 `plan_id` 发布（§5.3 改远端 Git 开关、恢复分支③）：放在 `.vibe/plans` 之外，建议项目根目录的 `product-spec.json`。
- 不要事先建 `.vibe/plans/<新编号>/`：vibe 只替换草案目录，新编号下已经存在的目录会让发布报 `plan already exists`。

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
     "files": ["project-relative path"],
     "dependency_reasons?": {"<depends_on node id>": "str"}}
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
  "daemon?": "true|false",
  "launchd_keepalive?": "true|false",
  "dispatch_mode?": "background|visible"
}
```

`prd` 的推荐 section：`problem`、`user_scenarios`、`success_criteria`、`non_goals`、`code_evidence`；涉及图形界面时必须有 `design_mockups`（§2）。

`heartbeat_interval_minutes`、`rotate_hard_cap`、`dispatch_mode` 由 vibe 按平台推导，缺省即可，**不向产品经理询问**（§5.3）。

授权卡两项开关：`daemon`（守护程序，默认开：授权后 `vibe monitor` 自动启动并登记守护程序）、`launchd_keepalive`（开机保活，默认关）。开机保活是守护程序被杀后自动重启的唯一途径；不勾选时守护程序被杀后不会自动恢复，监工心跳只报告、不重启。各平台均未经真机验证，授权卡上如实标注。守护程序关闭时保持旧形态，监工会话手工推进。

产品路径的 `risk_tags` 默认派生为 `write_scope`，表示节点写入合同文件范围的工程风险，不是业务风险评级或执行许可。旧 `--node-spec` 的显式标签保持原样；空值、错误类型和多处冲突仍由 DAG 审计拒绝。

**黑名单（写了就被拒）**：顶层 `complexity_band` `route` `route_result` `capabilities` `project_id` `integration_contract` `spec_path` `plan_id`；节点 `status` `risk_tags`；`contract` 内 `adapter_id` `project_id` `worker` `reviewer_worker` `worker_profile` `worktree` `branch` `writer` `reviewer` `risk_tags`。`worktree` 与 `branch` 由 vibe 按节点 id 派生（`.worktrees/<节点>` 与 `node/<节点>`），写死会让并行节点撞进同一棵树。

### 5.2 登记本会话的能力（复杂计划发布前必做一次）

vibe 不猜宿主能力，由 agent 盘点**这次会话里实际看到的**工具后登记：

```bash
vibe attest --adapter <claude-code|codex|...> --facts <facts.json> --provenance "<一句话说明依据>" [--project-id <id>]
```

`facts.json` 的键名以 `vibe_guide/adapters/manifests/<adapter>.yaml` 的 probes 为准，值只能是 `true` / `false`，看到对应工具才写 `true`。`project_id` 是宿主平台里这个项目的标识，可见派发需要。

### 5.3 发布、授权、开工

发布前只问一件事：允许还是禁止远端 Git 动作（提交、推送、开 PR、合并），写进产品 spec 的 `remote_git_actions`（`allow` / `deny`，缺省 `deny`）。**不问心跳间隔与换班上限**：授权卡上的 `heartbeat_interval_minutes` 等运行参数由 vibe 推导（推送唤醒已验证的平台兜底检查为 60 分钟，未验证为 10 分钟）。

```bash
vibe plan --request "<产品经理的原话>" --plan-id <plan_id> --from-prd <product-spec.json> --json
```

用 §1 草案的同一个 `plan_id` 发布，vibe 替换这份草案；已发布或已授权的计划不会被覆盖，仍报 `plan already exists`。

替换中途被打断（如 Ctrl-C）或 reason 含 `draft could not be restored` 时，先确认草案有没有被挪开：以 reason 里给出的路径为准，没给路径就找 `.vibe/plans/.<plan_id>.draft.*/draft`（命中多个时别猜：把每个目录的修改时间和其中 `plan.json` 的状态列给产品经理，请他选）。①找不到：草案没被挪动，直接按原命令重新发布；②找到了且 `.vibe/plans/<plan_id>` 不存在：把它整个挪回这个位置，再按原命令重新发布；③找到了且 `.vibe/plans/<plan_id>` 已存在就不要覆盖，也不要当成本次发布成功：从挪开的草案里取回产品 spec 放到 `.vibe/plans` 之外（见 §5.1），告诉产品经理这个编号已被占用，再换一个新的 `plan_id` 重新发布，`--s1` 照挪开草案 `plan.json` 里 `route_result.dimensions` 的 `steps,domains,uncertainty,failure_cost,toolchain` 依次填。旧编号下那份计划原样留着：不念它的卡、不对它 `vibe authorize` 或 `vibe monitor`，授权和开工只用新编号；把它的编号和 `plan.json` 的状态列给产品经理，说明它可能是本次被打断的发布，也可能是别的会话的，清不清理由产品经理决定。处理完后，挪开的草案移到废纸篓，剩下的空隐藏目录可以删掉。

- `status: ok`：vibe 已生成 PRD、Spec、Issue、DAG 审计、授权卡。先跑 `vibe authorize --plan <plan_id> --show-card --json`，它输出授权卡和依赖图（`card_text`：每个节点的前置节点、并行组、最大并行宽度、关键路径长度、无理由串行边），并记录展示回执；把 `card_text` 原样给产品经理看，不要自己另画依赖图。没有回执、或回执对应的计划版本、授权卡、依赖图已变化，`vibe authorize` 拒绝（reason 以 `dependency_graph_not_shown` 开头），重新 `--show-card` 再展示；依赖图渲染失败时展示和授权都被拒，把 reason 原样告诉产品经理。心跳间隔一行只有一个「已选中」，就是 `heartbeat_interval_minutes`；推送唤醒未验证时 60 分钟一项标「选它要付代价」，不是已选中。再用产品语言念给产品经理：做哪几件事、哪些同时开工、每件事一个开发任务加一个独立审查任务、节点档位与换算出的模型、派发方式（Claude Code 默认后台、可改选可见卡片，§6.4）、远端 Git 开关、永远不含部署/发布/生产写入/凭据/对外通信。
- 分开说两件事，别让 `allow` 像"允许了又排除了"：`excluded_actions` 里的推送/合并是说开发任务自己永远不推送；`allowed_actions` 里的是说产品经理允许后这些动作进入授权范围，vibe 只在独立终审通过后校验并记录，不代为执行。**合并规则**：独立终审通过、P0–P2 清零后，vibe 标记「可合并」，由监工执行合并命令，vibe 再核实合并提交可达目标分支才记为已合并；监工自报不算证据。选 `deny` 时 `allowed_actions` 不含任何远端 Git 动作，`excluded_actions` 照样列着推送/合并。
- 想改远端 Git 开关或运行参数：`vibe authorize` 改不了。把产品 spec 复制到 `.vibe/plans` 之外（见 §5.1），改字段，用新的 `plan_id` 重新发布（`--plan-id <新编号> --s1 <s1>`，`<s1>` 照抄首次发布结果里的 `s1`：草案已被替换，新编号没有评分可沿用），再念新卡。
- 引擎证明有效期 72 小时：同一 plan revision 超期后，`vibe authorize` 重新观测引擎凭证并自动续签，无需重新发布计划；PRD、Spec、节点文件真有改动时不借续签放行。
- `status: blocked`：reason 以 `product_spec_requires_complex_route` 开头→用草案 `plan_id` 发布或 `--s1` 显式升级；含 `product decisions remain unresolved`→回 §2；含 `engine_attestation_unavailable`→先做 §5.2。`blocked_design` 且带 `question`：未决产品问题，原样问产品经理。

产品经理说"授权"后：

```bash
vibe authorize --plan <plan_id> --show-card --json   # 授权前展示过就不用再跑；计划或卡变了要重跑
vibe authorize --plan <plan_id> --authorize AUTHORIZE --json
vibe monitor --plan <plan_id> --authorize AUTHORIZE --json
vibe daemon start --run-id <run-id> --plan <plan_id>
```

之后 `vibe status --plan <plan_id>` 看进度，`vibe resume --plan <plan_id>` 从断点续接。

## 6. 开工之后（只指路，不展开）

授权、开工后的所有执行、恢复、返工、接班、打断规则已迁入随包**执行协议**
`vibe_guide/protocols/execution.md`（`vibe init` 物化为
`.vibe/proposals/skills/execution/SKILL.md`），原 §6/§7 编号在那里保持不变。
规划会话到此为止；监工会话只读执行协议、角色合同与 supervisor-card.md，
不回读本协议。
