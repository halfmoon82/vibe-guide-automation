# Vibe Coding 辅助开发向导设计

## 1. 设计目标

构建一个通用于多种 Agent 的辅助开发向导。它以统一 CLI 为执行后端，以各家桌面 App 的会话为主要使用入口，帮助用户：

- 初始化或适配项目规则；
- 复用架构规范、Akasha 和乔哈里等外部 Skill；
- 根据任务复杂度选择轻流程或复杂流程；
- 在复杂任务中先完成需求讨论、PRD、Spec/Issue 和 DAG；
- 通过一次明确授权，自动并行开发、测试、Review、返工和验收；
- 将每个开发 Issue 和每次独立 Review 映射为对应桌面 App 中用户可见、可进入、可继续的独立任务；
- 只在产品设计变化、需要明确外部授权或 deploy 时请求人类介入；未知关键事实先自动做最小验证，无法验证则记录阻塞，不伪造完成。

设计原则是“能简单就简单、默认优先并行、状态可恢复、证据可复核、技术完成不等于业务批准”。

## 2. 已确认的范围与边界

### 2.1 Agent 支持

首版支持：

- Codex；
- Claude Code；
- Cursor；
- Grok；
- WorkBuddy；
- Kimi Code；
- DeepSeek Harness。

统一 CLI 是核心；每个 Agent 通过轻量适配器在桌面 App 会话中调用 CLI。适配器不复制核心流程，只声明调用方式和能力差异。

显式独立任务是七个平台共同的首选产品合同。每个 Agent 适配器必须实现或声明一个 `VisibleTaskProvider`：创建 developer/reviewer、返回平台任务 ID 和 host、让用户在桌面 App 中进入任务、按原任务续接返工/复审，并提供精确等待 cursor/token。Codex App 的 provider 使用 `create_thread`；Claude Code、Cursor、Grok、WorkBuddy、Kimi Code 和 DeepSeek Harness 使用各自可验证的等价能力。平台确认没有等价桥接时，可以降级为 `background` 模式使用 subagent；授权卡和能力报告必须披露任务不可见、不可直接进入及返工续接限制，不能把它描述为完整可见自动化。

在各家桌面 App 的最高可授予权限下，验收目标是：用户在会话中输入触发词，Agent 能调用 CLI、展示授权卡、自动运行已授权 DAG，并在会话中回传进度和结果。权限不足时必须检测并如实降级，不绕过沙箱。

### 2.2 外部 Skill

Skill 不嵌入本项目代码，通过 GitHub 拉取并安装到用户级共享缓存。

- `architecture-skill-pack`：必装，来源 `https://github.com/lov-team/architecture-skill-pack`；
- `akasha-grimoire`：选装，来源 `https://github.com/lov-team/akasha-grimoire`；
- `aligning-with-johari-windows`：选装，来源 `https://github.com/halfmoon82/aligning-with-johari-windows`。

项目只记录来源、提交 SHA 和安装配置，不保存凭据或用户绝对路径。已存在的安装目标不得强制覆盖。

### 2.3 项目规则与知识库

- 已有 `AGENTS.md` 时，只读扫描并生成补丁建议，不自动修改原文件；
- 没有项目运行知识时，用户确认后在项目内初始化最小 `.vibe/knowledge/`；
- 项目内知识用于本项目运行上下文，不替代统一持久开放区；跨项目长期知识仍遵循统一知识库的查询、范围和入库审批规则；
- 扫描先读后写，`scan` 不产生写入，`init` 及安装动作必须经用户确认。

## 3. 复杂度分流

每条请求先经过零成本 S0 规则预筛：明显简单的任务直接执行，疑似复杂的任务进入 S1。

S1 使用五个维度评分：

1. 步骤数量；
2. 涉及知识域；
3. 不确定性；
4. 失败代价；
5. 工具链复杂度。

默认阈值：

- `<= 8`：直接执行；
- `9-15`：轻规划；
- `> 15`：复杂流程。

阈值在项目配置中可调整，但调整必须记录来源和原因。

复杂流程固定为：

```text
需求讨论 → 产品决策卡 → PRD → Spec/Issue → DAG 审计
→ 开发计划确认 → 一次授权 → 监工执行 → 独立验收
```

未解决的产品取舍不能进入 `authorized`。监工阶段只处理实现缺陷，不重新猜测已经应在 PRD 阶段解决的产品决策。

## 4. DAG 与并行设计

默认按“是否阻塞节点启动”判断依赖，而不是按“最终是否需要联调”判断依赖。

### 4.1 依赖类型

- **硬依赖**：没有前置成果无法安全启动，例如核心数据模型、授权边界或不可逆架构决策未确定；阻塞启动。
- **契约依赖**：接口、输入输出、错误行为和测试夹具可以先约定；允许使用 stub、fixture、fake service 或最小占位实现并行开发。
- **集成依赖**：只影响联调、合并或最终验收，不阻塞独立开发。

节点至少记录：

- `depends_on`：真正阻塞启动的硬依赖；
- `integration_after`：允许先开发、后联调的节点；
- `contract`：占位实现必须遵守的输入、输出和验收示例；
- `parallel_group`：可以同时启动的节点组。

共享文件或潜在冲突不是天然的串行理由，优先使用隔离 worktree。只有契约变化或占位无法满足真实约束，才需要回到设计变更确认。

## 5. 项目产物

建议的项目运行目录：

```text
.vibe/
├── config.yaml
├── knowledge/
├── proposals/
│   └── agentsmd/
├── plans/
│   └── <plan-id>/
│       ├── prd.md
│       ├── specs/
│       ├── issues/
│       ├── dag.yaml
│       └── authorization.md
└── runs/
    └── <run-id>/
        ├── state.json
        ├── tasks.json
        └── events.jsonl
```

`state.json` 是当前运行快照，`tasks.json` 登记每个开发/Review 任务的 provider、平台任务 ID、host、worktree、branch、状态/交付路径和 cursor/token；Codex 额外使用 `threadId`、`hostId`。`events.jsonl` 是追加式轮转证据。恢复时以项目快照、任务登记和事件为准；App 任务索引用于可见性核验，不作为唯一事实源。

## 6. CLI 与桌面会话

首版 CLI 命令：

```text
vibe scan       # 只读扫描
vibe init       # 确认后初始化
vibe doctor     # 检查依赖、适配和权限
vibe plan       # 需求讨论和规划
vibe monitor    # 启动或继续监工
vibe status     # 查看 DAG 和节点状态
vibe resume     # 从快照恢复
```

桌面 App 会话负责需求讨论、展示决策卡和授权卡、接收短触发词、展示进度；CLI 负责真实状态、调度和证据写入。

桌面 App 适配器优先通过 `VisibleTaskProvider` 为每个 developer 和 reviewer 创建独立任务。创建成功后任务必须出现在该 App 的任务/会话列表，用户可以进入查看过程。监工通过精确平台任务 ID 和 host 下发后续输入并用逐任务 cursor/token 等待；不得用全局任务列表轮询代替精确登记。Codex App 的具体映射为 `create_thread`、`threadId`、`hostId` 和 cursor。若 provider 明确返回“不支持”，才可在授权卡中声明 `background` 降级并使用 subagent。

配置中的任务对上限表示同时活跃并发量，不表示整个 DAG 期间累计只能创建这么多任务。一个 Issue 的 developer/reviewer 已完成、独立 Review 的 P0–P2 清零且证据已登记后，监工关闭或归档对应会话，释放并发名额；任务 ID、host、worktree、branch、状态/交付路径和最终 cursor 继续作为历史证据保留。仍可能返工或复审的原任务不得提前归档，也不得通过删除登记绕过唯一 writer 或续接要求。新解锁的 Issue 使用释放后的名额创建新的独立 developer/reviewer。

三组同义触发词，均不超过 10 个字：

- `启动监工`；
- `推进开发`；
- `执行DAG`。

## 7. 监工执行模型

### 7.1 授权卡

触发词只启动预检，不立即执行。授权卡必须列出：

- DAG 范围和当前版本；
- ready 节点、并行组和依赖；
- worker、worktree 和文件范围；
- 测试、Review、返工、验收权限；
- commit、push、创建 MR、merge 是否包含；
- 明确 deploy 不在默认授权内；
- 设计变化、未知状态和越界动作的暂停条件。

一次授权覆盖当前 DAG 中已列明的全部非 deploy 动作。授权与当前 DAG 版本绑定，计划或范围变化会使旧授权失效。

### 7.2 节点状态

```text
planned → ready → running → delivered → review → accepted
                              ↑             ↓
                              └── rework ───┘
```

`planned` 是正常待执行状态，`ready` 可以显式保存，也可以由契约和依赖条件推导。`delivered` 只表示 developer 已技术交付、独立 Review 尚未接受；只有 `accepted` 才是节点成功终态并能解锁硬依赖。`complete` 只用于全部节点均为 `accepted` 后的 run-level 状态；provider 名为 `complete` 的事件只能映射为 developer delivery。`start_pending` 仅是私有启动意图状态，不属于规划生命周期。

暂停状态：

- `blocked_design`：需要修改 PRD、Spec、Issue、验收标准或 DAG；
- `blocked_deploy`：需要单独的 deploy 授权；
- `blocked_unknown`：证据不足或外部状态未知。

### 7.3 轮转规则

1. 找出所有硬依赖已完成且契约满足的 `ready` 节点；
2. 按并行组为没有 writer 冲突的 Issue 创建用户可见 developer 任务，同时登记 `threadId`、`hostId`、worktree、branch、`status_file`、`handoff_file` 和 cursor；
3. worker 自主计划、写 Red 测试、实现、测试并交付；
4. developer 交付后创建另一个用户可见 reviewer 任务，独立检查累计 diff、测试和交付证据；
5. 实现缺陷退回原 developer 任务，修复后复审回到原 reviewer 任务；
6. 节点通过 Review 后锁定成果，解锁后续节点；
7. 全部节点完成后执行最终 DAG 验收。

调度容量按活跃任务对计算：已完成且归档的任务不阻塞后续 ready 节点；同一时刻不得超过授权卡列明的活跃 developer/reviewer 对数。

一个节点只允许一个有效 writer。reviewer 只读审查，不能代改业务代码。developer 与 reviewer 必须是两个不同的显式独立任务；不得因不确定的线程索引、短暂超时或状态延迟创建第二 writer，也不得用内部 subagent 替代已登记的可见任务。

### 7.4 异常处理

- 网络或线程超时：标记未知并进行有限恢复，不直接变成 no-op；
- worker 启动失败：记录具体失败原因，不留下假 `running`；
- 独立任务创建后未能取得平台任务 ID/host（Codex 为 `threadId`/`hostId`）或未能核验可见性：保持 `blocked_unknown`，不得降格为后台 worker 继续；
- 状态查询失败：不能解释成“没有任务”；
- 监工中断：保存快照，`resume` 从上次状态继续；
- 可见任务丢失终端/文件能力：先将原任务 aborted/archived 并保留 thread/cursor、冻结 HEAD 与零写入证据；只有在原任务已终止、writer worktree/branch/HEAD/clean state 精确匹配且无第二 writer 时，才允许一个 visible successor 复用原 writer root。App host worktree 仅提供工具，不是新的 writer root；不满足时保持阻塞；
- 重复失败且没有新证据：进入 `blocked_unknown`，不伪造完成；
- 技术完成、Review 通过、交付授权和最终发布分开表达。

## 8. 人类介入边界

监工处理规则或合同冲突时，按“用户当前明确决定 → 已批准 PRD/Design Spec → 授权卡 → Issue 合同 → 下层实现”确定优先级。若证据只产生一个答案，且纠偏仍在已授权项目、DAG 和非 deploy 边界内，监工记录纠偏、更新受影响的 DAG 后缀或合同并自动继续；可唯一解决的实现不一致、命名不一致或过期下层合同不单独请求用户。

**已确认规则能够唯一判断的事项由监工自动执行并记录，不得作为盲区或产品取舍再次询问；只有无法唯一判断且会改变产品方向、授权边界或外部承诺的事项才中断。** 可自动执行的纠偏必须使用受信来源，并同时绑定当前项目摘要、plan ID/revision、已批准 PRD 决策摘要、授权摘要和 Issue 合同摘要；候选结果唯一、文件和 action 仍在授权范围且不含 deploy。`current_user` 或 `approved_prd` 候选值还必须存在于持久化的已批准决定，未绑定的 reviewer/provider 文本不能覆盖产品决定。

同一 plan 的执行合同变化使旧授权失效时，重新授权是原 run 的可审计 continuation：先证明旧活动任务已停止，再保留旧授权、规范化变更原因、原任务身份与 cursor，登记新授权和新合同摘要，随后在修正后的 DAG 上续接原 developer/reviewer。重新授权不把已经由上述规则唯一确定的实现纠偏再次上抛为产品问题；无法证明旧任务终止或 continuation 身份时保持未知阻塞。

只有以下情况请求人类：

1. 用户目标或产品方案变化；
2. 修改 PRD、Spec、Issue 或验收标准；
3. 新增、删除或重排 DAG 节点；
4. 扩大文件、数据、权限或外部系统范围；
5. deploy；
6. 需要产品取舍或明确外部授权。

上述暂停边界只在仍有多个会实质改变产品结果的答案、产品范围或方向变化、需要外部/deploy/系统权限授权，或证据无法区分安全结果时触发。

developer/reviewer 的任务可见性、独立性或控制面拓扑变化属于产品设计变化，会使既有授权失效。

设计变更卡必须说明原因、证据、受影响节点、已完成且保留的成果、建议变更和将重新执行的 DAG 后缀。用户确认后只重建受影响后缀，已锁定成果不重做。

## 9. 验收示例

### 简单任务

错别字修正应直接执行，不产生 PRD、DAG 或监工。

### 并行任务

后端接口、前端页面和测试夹具在共同契约下并行开发，最后增加联调节点。

### 产品取舍

实时数据与脱敏样例二选一，必须在 PRD 阶段展示方案卡并锁定选择，监工阶段不重复询问。

### 实现缺陷

测试失败但 PRD、接口和验收标准未变时，返工回到原 developer 任务，复审回到原 reviewer 任务；不创建替代任务丢失上下文。

### 任务可见性

任一受支持桌面 App 的完整可见自动化路径中，开发和 Review 均应在其任务/会话列表显示为独立任务。用户进入任务后可看到过程并继续返工。Codex App 以左侧任务列表作为具体验收入口。background subagent 只满足明确标识的降级模式验收，不能满足“用户可见、可进入”的验收项。

### 设计变化

真实业务规则与 Spec 冲突时进入 `blocked_design`，用户确认后只重建受影响 DAG 后缀。

### 权限不足

桌面 App 无法启动子进程时，`doctor` 报告缺失能力并降级，不宣称完整无人值守监工。

## 10. 明确不做事项

- 不把任一 Agent 私有 API 作为核心依赖；
- 不把外部 Skill 源码复制进项目；
- 不自动覆盖已有 `AGENTS.md`；
- 不自动 deploy 或修改未授权系统权限；
- 不自动替用户做未确认的产品取舍；
- 不把未知状态转为无事项；
- 不把 CODING、MR、定时任务或 ActionLoop ledger 固定写入核心；
- 不建设完整云端任务平台；
- 不追求七个平台完全同构的原生 UI；
- 不静默使用 background subagent 冒充可见独立开发或 Review 任务；
- 不为未来场景预先添加大量插件和门禁。

## 11. 完成定义

设计阶段完成的判定：

- 需求、边界、用户、输入、输出、成功标准和失败行为明确；
- S0/S1 分流、DAG 并行规则、授权边界和人类介入边界明确；
- 桌面 App 兼容和权限降级有可验收定义；
- 七个平台 adapter 都能报告显式独立任务能力或明确的 background 降级；声明完整可见自动化的平台，其 developer/reviewer 可见性、任务身份登记、返工与复审续接必须可验证；
- 项目产物、状态和证据位置明确；
- 不做事项已列出；
- 没有未决的产品取舍被带入监工阶段。

本设计不包含实现代码、平台私有 API 逆向或部署方案。
