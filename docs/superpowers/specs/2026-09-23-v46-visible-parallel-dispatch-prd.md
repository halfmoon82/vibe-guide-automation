# PRD：可见并行派发（visible parallel dispatch）——DAG 真并行的两级拓扑

状态：设计已定稿（2026-09-23 用户三项决策全部确认；平台拓扑依据当日产品文档核查），并入 V4.6，主线程暂缓发布；下一步进入 Spec/Issues
上游证据：vibe-entry 执行差距复盘（[授权卡](../plans/2026-09-23-vibe-entry-skill-authorization-card.json) `workers` 字段；合并顺序 #53→#54→#56→#57 串行；reviewer 为 background subagent 且未披露降级）
修订对象：[2026-08-24 设计基线](2026-08-24-vibe-coding-development-guide-design.md) §任务拓扑 与 AGENTS.md §6 的「developer 与 reviewer 必须是两个不同的可见独立任务」条款

## 背景与问题（执行差距实证）

V4.6 vibe-entry 是 V4.5 合同生效后的第一个真实 DAG 执行，暴露出三条系统性差距：

1. **可见任务派发没有发生。** 授权卡自报 topology 为「developer = 主会话单写 + 每节点一个 worktree；reviewer = 每 PR 一个 background 子代理」。全程未调用 `create_thread`，用户在 App 任务列表看不到任何独立会话。
2. **DAG 并行组名义化。** Spec 声明 ISSUE-01/02 可并行，实际串行合并；单 writer（主会话）拓扑下真并行在结构上不可能，且两节点存在软依赖（02 的 AGENTS.md 块指向 01 物化的协议文件），parallel_group 划分未经依赖审查。
3. **降级未披露。** §6 要求降级为 background 时"能力报告、授权卡和交付必须明确标识降级及限制"；实际授权卡只写了 workers 构成，交付报告完全未提可见性降级。

根因：**可见派发目前只靠监工自律，没有机制强制**；且现行「dev 一个可见 thread + review 再一个可见 thread」的双 thread 拓扑重、活跃名额消耗快，监工在中小 DAG 上有强烈动机绕开。

## 设计意图（用户钉死，不得回退）

两级并行拓扑：

```text
监工（主会话）
  └─ 按 DAG ready 集并行派发 ≤5 个可见独立会话（Codex: create_thread，user-owned）
       └─ 每个会话内部走 SDD：1 个 dev 子代理（实现）+ 1 个 review 子代理（独立上下文、只读）
            └─ 返工/复审循环在该会话内闭环
```

- **真并行的载体是可见会话**，不是监工手里的 worktree；监工只派发、等待、收口，不亲自当 writer。
- **并发上限默认 5**（同时活跃的可见 worker 会话数，可配置），按 DAG ready 集尽量打满。
- **reviewer 独立性改由"同会话内独立子代理上下文 + 只读约束"保证**，不再要求 reviewer 是另一个可见 thread。这是对现行 §6 的合同修订点，本 PRD 生效后基线设计与 AGENTS.md §6 相应条款须同步改写。

## 目标

1. DAG 中无硬依赖冲突的 ready 节点，真实同时推进（任一时刻活跃 worker 会话数 >1 成为常态，而非例外）。
2. 每个开发节点对应一个用户可见、可进入、可继续的独立会话；会话内 dev/review 子代理过程对用户透明可查。
3. 降级（无可见桥接的平台用 background subagent）必须在能力报告、授权卡、交付三处显式标识，否则视为违规而非风格差异。

## 用户故事与验收

| AC | 内容 | 验证 |
|---|---|---|
| AC-01 | 监工对 ≥2 个无硬依赖 ready 节点的 DAG，在同一轮派发中创建 ≥2 个可见 worker 会话（`create_thread` 或等价桥），登记 threadId/hostId/worktree/branch/cursor | 端到端：2 节点 fixture DAG，断言 tasks.json 两条 `mode=visible` 记录且创建时间窗重叠 |
| AC-02 | 同时活跃 worker 会话数不超过配置上限（默认 5）；有节点完成并归档后，名额释放给后续 ready 节点 | 状态机单测 + fixture |
| AC-03 | 每个 worker 会话内部按 SDD 运行：dev 子代理实现 → review 子代理（独立上下文、只读）审 → P0–P2 返工回 dev → 复审，全部在同一会话身份内闭环，证据入 events.jsonl | fixture 会话证据链检查 |
| AC-04 | review 子代理与 dev 子代理上下文隔离、只读（不得代改业务代码）；违规即 blocked 并记录 | 契约测试 |
| AC-05 | 监工自身不作为任何节点的 writer；授权卡 `workers` 字段禁止再出现 "main session" 作为 developer | 授权卡 schema 校验测试 |
| AC-06 | parallel_group 划分前强制依赖审查：组内节点间存在文件级/产物级软依赖时必须改标 `integration_after` 或降级出组 | 审计器单测（用 vibe-entry 的 01/02 案例做回归夹具） |
| AC-07 | 降级路径：平台无可见桥时授权卡 `mode=background` + 限制披露缺一则授权卡校验失败 | schema 校验测试 |
| AC-08 | 可见会话工具丢失时走 visible successor 恢复（既有规则沿用，适配单 writer=worker 会话语义） | 既有恢复测试适配 |

## 非目标

- 不改变 DAG 语义（depends_on / integration_after / contract / parallel_group 定义不变，只加派发前的依赖审查）。
- 不改变 S0/S1 入口路由与授权卡整体结构。
- 不做跨平台新适配器；Codex 先行，其余平台按既有 VisibleTaskProvider 合同跟进或显式降级。
- 不追求"无限并行"：上限 5 是纪律不是瓶颈，写冲突与依赖正确性优先于吞吐。

## 关键产品语义（钉死）

- **可见性验收粒度变更**：从「dev thread + review thread 双可见」改为「每 Issue 一个可见 worker 会话，会话内 SDD 双角色可审计」。任务登记 `tasks.json` 增加 `topology=visible-sdd`，降级为 `background`。
- **reviewer 独立性红线不松动**：独立子代理上下文、只读、非作者视角；只是不再独占一个可见 thread。
- **披露义务升级为机器校验**：降级未披露 = 授权卡不合法，而非事后口头补记。
- **监工职责收窄**：派发、等待、收口、纠偏；亲自写代码即违规（本次 vibe-entry 的复盘结论固化为规则）。
- 现有「一次授权覆盖 DAG 非 deploy 动作」「计划变化使授权失效」「blocked_unknown 不得伪造」等纪律全部沿用。

## 平台拓扑决策矩阵（2026-09-23 产品文档核查）

总规则（用户定稿）：**支持会话内 SDD 的平台走「单可见会话 + dev/review 双子代理」；不支持或能力未证实的平台，dev 与 reviewer 仍各自独立派发为两个可见任务。** 能力判断只认产品文档或探针证据，UNKNOWN 一律按"不支持"处理（能力合同 §Capability and Tool Truth）。

| 平台 | 拓扑 | 证据（2026-09-23 核查） |
|---|---|---|
| Codex | 会话内 SDD（定稿） | 用户决策；App 内 subagent 能力已在实际会话使用 |
| Claude Code | 会话内 SDD | 官方文档 Create custom subagents：Task 委托、独立上下文、`.claude/agents/` 自定义、并行/后台、resume |
| Cursor | 会话内 SDD | cursor.com/docs/subagents：editor/CLI/Cloud Agents 均支持，官方用例含 "independent verification of work" |
| Kimi Code | 会话内 SDD | moonshotai.github.io/kimi-code「Agents and Sub-Agents」：主 Agent 派发子代理、隔离上下文、`.kimi-code/agents/` 自定义 agent、内置 coder/explore/plan（注：旧 kimi-cli 已归档，以 kimi-code 为准） |
| DeepSeek Harness | 会话内 SDD（条件） | 仓库 AGENTS.md 含 `subagent/`（delegated agents）结构证据；派发语义未经产品文档核实，**探针验证通过前按独立派发处理** |
| WorkBuddy (CodeBuddy) | dev/reviewer 独立派发 | CLI 参考文档无 subagent 能力记录，agents 文档页为占位 stub；UNKNOWN→不假定支持 |
| Grok CLI | dev/reviewer 独立派发 | 无 subagent 产品证据；本机既有用法即为"单一 TUI worker"（grok-cli-development skill），与该拓扑天然一致 |

探针要求：各平台 manifest 增加 `in_session_sdd` 能力探针（create/enter/resume/wait 之外），证据入能力报告；DeepSeek Harness 探针通过后可升级到会话内 SDD，无需改 PRD。

## 用户决策记录（2026-09-23，全部定稿）

1. **§6 修订定稿**：Codex 照本 PRD 执行；其余 harness 按上方矩阵，不支持会话内 SDD 的仍 dev/reviewer 各自独立派发（依据产品文档逐个裁定，已完成）。
2. **并发上限配置位置：`.vibe` 项目配置**（用户建议，监工同意）。理由：并行胃口是项目级属性（仓库规模、worktree 磁盘、CI 容量因项目而异），不适合每次授权卡重复决策。语义：项目配置为上限默认值 5；授权卡只展示本次生效值快照，可为单次 run 调低、不得调高；配置变更使在途授权失效（与既有纪律一致）。
3. **生效版本：并入 V4.6**；4.6.0 发布暂缓，等本 PRD 实现验收后一并发布。
