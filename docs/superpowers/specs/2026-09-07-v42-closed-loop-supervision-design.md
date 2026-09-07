# V4.2 闭环监工与 Provider 自愈设计

## 目标

让 V4.2 complex DAG 具备一个不可旁路、可持续恢复的运行闭环：用户一次授权后，监工负责推进 DAG、处理工程故障、维护原任务身份和证据链；只有产品决策、外部权威或新增范围变化才能让运行暂停。

## 非目标

- 不伪造 Provider 能力、身份、凭据或平台授权。
- 不把远端 Git、deploy、系统权限或凭据获取纳入普通 DAG 授权。
- 不创建第二 writer、隐式 successor 或未登记的后台路径。
- 不把历史 V2/V3 运行态冒充为当前 V4.2 运行态。

## 核心不变量

1. **单一运行入口**：complex DAG 的启动、恢复、重试、重绑定、返工、复审和验收只能经 `Monitor` 统一入口；CLI、adapter 和 runner 不得直接推进节点。
2. **一次授权持续有效**：授权摘要绑定 plan revision、节点合同、能力合同和 Provider 调用范围；在这些内容未变化时，工程重试不再次询问用户。
3. **同一身份恢复**：重试和恢复保留原 node、task、generation、worktree、branch、lease 和 cursor；身份不明时保持隔离，不创建替代 writer。
4. **下游只认结构化证据**：节点只有在当前任务、当前授权 epoch、当前合同摘要、独立 reviewer、P0–P2 清零和交付证据全部匹配时才能解锁下游。
5. **禁止隐式旁路**：SDD-only、直接 runner、旧状态、worker 自报、历史 marker、临时 successor 和“默认继续”都不能绕过 Monitor 硬门。
6. **工程故障不终止 DAG**：可恢复工程故障进入 `retry_pending` 或 `repairing`，由 supervisor heartbeat 持续处理；父会话退出不改变运行进度。
7. **外部权威独立阻断**：登录、凭据、系统权限、远端批准、deploy 或超出授权范围的动作不能由自愈伪造；这些状态结构化记录为 `blocked_unknown` 或等待新增授权。

## 状态机

```text
ready
  -> dispatch_pending
  -> running
  -> retry_pending <-> repairing
  -> review
  -> accepted
  -> archived
```

异常映射：

- timeout、空响应、429、连接断开、pending clientThreadId、worker 退出、快照写入中断、临时绑定漂移：`retry_pending`，保留同一身份并由 heartbeat 重试。
- 可修复的 worktree/branch/lease/cursor 漂移：`repairing`，先验证原身份和冻结 HEAD，再原地修复；验证失败则 `blocked_unknown`，不得创建 successor。
- Provider 明确需要登录/凭据/系统权限或远端批准：`blocked_unknown`，保留任务和证据，等待外部条件变化。
- 产品决策、Spec、DAG、授权范围或执行拓扑变化：`blocked_design`，使受影响授权失效并等待重新确认。
- reviewer P0–P2 未清零、交付 marker 缺失或证据不匹配：保持 `review`/`retry_pending`，不得解锁下游。

## 数据流与组件边界

### Monitor

负责唯一的生命周期转移、授权校验、节点调度、恢复、重试、容量、writer lease 和下游解锁。所有转移追加到 `events.jsonl`，并原子更新 `state.json`。

### Supervisor

持有 supervisor lease 和 heartbeat。父会话结束后继续调用 Monitor 的 `resume/tick`；对 `retry_pending` 和 `repairing` 不做终止判断。重复 supervisor 只能保留一个有效 lease。

### Provider adapter / action store

只负责发起已授权、已绑定的 Provider 操作和读取结构化结果。不得自行改变节点状态、生成 task identity 或将 `clientThreadId` 提升为正式 thread。

### CLI

只负责参数解析、只读 status、授权卡展示和 Monitor 调用。不得复制调度逻辑或提供绕过 Monitor 的 complex 执行命令。

### V4.2 project state

初始化和升级必须物化 `workflow_version=4`、`execution_mode=sdd_first`、`session_gate=s0_required`、`capability_contract_required=true`。旧状态只读迁移；任何缺失、冲突或降级状态都不能作为当前 V4.2 运行态。

## Provider 自愈策略

每个节点维护一个持久化 retry record：`attempt`、`reason_class`、`next_retry_at`、`same_task_required`、`binding_digest`、`last_observation_ref`。重试使用有界退避并在每次 heartbeat 重新读取当前快照和授权摘要。

自愈顺序固定为：

1. 重放未应用事件和未完成快照写入；
2. 读取原 task/handle/cursor 的 Provider 结果；
3. 验证 plan、node、task、generation、worktree、branch、lease 和合同摘要；
4. 仅在验证通过后重新 poll 或续接原任务；
5. 绑定漂移时执行原身份的受限 repair；
6. 结果达到交付或 review 条件后继续原 DAG；
7. 仍无法区分时保留 `blocked_unknown`，等待下一个 heartbeat，不创建旁路。

没有固定的失败次数后自动放弃；停止条件由外部权威、授权范围、设计变化或用户明确停止决定。

## 授权边界

一次用户授权覆盖当前 plan revision 和授权卡中已列明的：Provider create/poll/wait/resume、工程重试、原身份重绑定、快照修复、review/rework 和同一 DAG 内的后继节点。授权不覆盖：新增节点或范围、产品方向变化、凭据/登录/系统权限、远端 Git、deploy 或其他外部承诺。

任何合同或范围变化都生成新的授权 epoch，保留旧授权、原因、原任务身份、cursor 和证据；不能通过刷新旧摘要继续执行。

## 验收矩阵

- 直接调用旧 CLI/runner/adapter 无法启动 complex 节点。
- 初始化新项目得到完整 V4.2 状态；V2 状态不能被当作 V4.2。
- Provider timeout、429、空响应、pending clientThreadId、worker 退出和快照中断均保持同一 task/writer 并在 heartbeat 后继续。
- worktree/branch/lease/cursor 漂移只允许原身份 repair；证据不足时不创建 successor。
- reviewer 缺失、P0–P2 非零、marker 缺失或历史证据均不能解锁下游。
- 父会话退出后 supervisor 继续推进；重复 supervisor 无法取得第二 lease。
- 需要外部授权的 Provider 结果不会被伪造为 capability 或完成，但也不会要求用户为每次工程重试重新授权。
- `status`、事件、快照和任务登记能完整重放当前 DAG，且无凭据、token、原始 Provider 文本或私密业务数据。

## 实施顺序

1. 先补 Red 测试：唯一入口、V4.2 状态、Provider 自愈、外部权威分类、不可旁路。
2. 收敛 Monitor/CLI/adapter 的 complex 调用路径。
3. 实现持久 retry record、heartbeat 恢复和原身份 repair。
4. 修正 initializer/upgrade/version metadata 的 V4.2 一致性。
5. 运行定向测试、完整测试、CLI help、干净安装和 `git diff --check`。
6. 只在所有证据一致后生成独立验收报告；不自动 commit、push、merge 或 deploy。
