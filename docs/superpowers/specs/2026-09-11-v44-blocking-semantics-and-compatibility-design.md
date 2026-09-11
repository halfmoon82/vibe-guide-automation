# V4.4 阻塞语义、自愈与版本兼容设计

## 1. 目标

V4.4 收敛 V2→V4.3 累积的工程阻塞问题。系统只在产品、授权、不可逆安全和真实依赖不满足时阻塞；worktree、Git、provider 和宿主故障进入节点级恢复，不再把工程卫生转成用户决策。

`required-workflow.json` 不再是 complex 计划派发前置门禁。入口强制由入口层验证；运行阶段缺少该文件只记录审计事件，不追溯性阻塞。

## 2. 阻塞分类

节点故障统一映射为：

- `repairable`：worktree、branch、detached HEAD、dirty checkout 等可在不丢业务改动的前提下修复的状态。
- `retryable`：timeout、断线、空响应、短暂任务创建失败。
- `capacity_wait`：429、模型容量不足、活跃任务额度不足。
- `binding_unknown`：身份或合同事实不足以证明仍是原任务。
- `external_decision`：产品范围、外部权限、deploy、凭据、不可逆安全动作。

只有 `external_decision` 和未满足的真实硬依赖进入用户决策状态。`binding_unknown` 只隔离当前节点及其依赖闭包；无关 ready 节点继续运行。

## 3. 身份冲突预防

### 3.1 两阶段绑定：Intent 与 Proof

节点首次进入派发前只生成最小的不可变 `BindingIntent`。它只包含监工在 provider 调用前已经知道的事实：

```text
run_id, plan_id, plan_revision, node_id,
task_id, role, generation, writer,
worktree, branch, base_sha,
authorization_digest, node_contract_digest
```

`BindingIntent` 不要求 provider lease、cursor 或 live task 已存在。它由监工生成幂等键和 digest；provider 请求、task registry 和事件只引用该 intent digest，不允许各模块分别拼接身份字段。

provider 返回后再建立可变的 `BindingProof`，包含：

```text
provider_task_id, provider_host, lease_id, cursor,
observed_worktree, observed_branch, observed_writer,
observed_role, observed_generation, observed_contract_digest
```

Proof 可以被刷新、修复和替换，但必须始终匹配原 Intent。lease 和 cursor 是运行时证明，不是首次派发前置条件。

### 3.2 事务式派发顺序

派发必须按以下顺序完成，任何一步失败都不得留下可执行的半绑定状态：

1. 校验当前 plan、authorization 和 node contract digest；
2. 生成或重用同一 `BindingIntent`、`task_id` 和 `generation`；
3. 原子取得与 intent digest 绑定的 provisional writer lease；
4. 写入 `start_intent`，内容只包含 Intent；
5. 向 provider 发送带 Intent 的创建/续接请求；
6. provider 返回后生成 `BindingProof`，再验证 task、writer、worktree、branch、role、generation、issue 和合同摘要；
7. 证明通过后写入 `start_confirmed`，更新 snapshot 和 task registry；
8. 任何异常都写入 `repairable`、`retryable`、`capacity_wait` 或 `binding_unknown`，并保留原 Intent。

`start_intent` 未对应 `start_confirmed` 时，恢复只能重试同一 Intent，不能重新生成 task、generation 或 writer。缺少 lease/cursor 只表示 Proof 尚未完成，不构成身份冲突。

### 3.3 Lease 防冲突

- provisional lease key 为 `(run_id, node_id, role, generation, intent_digest)`，而不是 worktree 路径；
- 同一 key 只能有一个 active lease；
- lease 创建、续租和释放都校验 intent digest；
- worktree 或 branch 变化不能生成新 lease，只能刷新原 Intent 的 Proof 或触发 repair；
- 发现两个 active lease、两个不同 task 对应同一 writer，立即进入 `binding_unknown`，停止该节点所有写入。

### 3.4 Provider 返回验证

provider 的自然语言自报不构成身份事实。只有结构化、当前 host 上的 locate/list/wait 观察可以确认 task。返回值必须同时满足：

```text
provider、host、task_id、issue_id、role、generation、writer、
worktree、branch、cursor、authorization_digest、node_contract_digest
```

任一字段缺失或冲突都不能提升为正式 binding；保留原 envelope 并进入 `binding_unknown`。

## 4. 同任务 repair loop

`repairable`、`retryable` 和 `capacity_wait` 都保留原 task、generation、writer、lease、cursor 和合同摘要。

- repairable：读取 Git/provider 当前事实，执行最小可逆修复，再重新验证 envelope；
- retryable：按指数退避记录 attempt 和 next_retry_at；
- capacity_wait：记录容量观察，等待后继续同一 task；
- binding_unknown：不创建 successor，等待足够的结构化证据；
- 多次无法证明原身份时才升级为用户可见的 `binding_unknown`，但不得自行换 writer。

禁止自动删除或覆盖业务改动、切换到未列明分支、扩大文件范围、创建 successor、改变产品合同或执行外部 Git/deploy 动作。

## 5. DAG 调度

调度器按硬依赖闭包计算阻塞范围：

- 节点工程故障只阻塞其后继依赖；
- `integration_after` 不阻塞独立启动；
- 无依赖 ready 节点继续并行；
- repair/wait 节点保留容量占用和身份，但不改变其他节点 ready 判断；
- reviewer 是独立只读任务；rework 回到原 developer。

## 6. Workflow 证据门禁修订

`verify_workflow()` 保留为审计、诊断和收尾验证，不再阻断 complex monitor/provider 派发。

- 有 `required-workflow.json`：校验并登记；
- 缺失：写入 `workflow_evidence_missing`；
- 生成脚本失败或权限分类器拦截：写入 `workflow_evidence_generation_failed`；
- 入口已被绕过：写入 `entry_enforcement_bypassed_or_unobserved`；
- 以上三种情况都不阻断已通过当前 authorization、contract 和 binding 的执行；
- provider 内部续接不得再次调用用户入口门禁。

入口强制性由 CLI/桌面 App 入口探针和独立测试保证，不由运行时追溯文件保证。

## 7. V2→V4.3 状态兼容

旧运行、旧授权 epoch、旧 snapshot、旧 cursor 和旧事件只读保存为历史 namespace。新运行不得直接把旧状态恢复成可执行状态。

迁移只生成 `history_manifest.json` 和 `migration_evidence.json`，并保留原文件哈希。旧状态无法完整解释时标记 `historical_incomplete`，不转换为当前 `binding_unknown`。

事件验证分为两条路径：

1. 历史回放：校验 hash chain、schema 和 provenance，生成只读摘要；
2. 当前执行：重新校验当前 plan、authorization、contract、envelope、lease 和 live provider evidence。

历史回放通过不代表当前可执行；当前 binding 通过也不代表历史事件完整。

## 8. 安装与升级

安装器分别识别 package version、state schema、plan revision 和 provider contract version。发现多个版本混在同一目录时：

- 先生成只读迁移预览；
- 按版本 namespace 隔离旧 `.vibe` 运行；
- 不覆盖旧状态；
- 只有显式 `migrate-state` 才生成新当前 namespace；
- wheel、sdist、源码安装分别验收；
- 升级和回滚分别记录证据。

缺少旧版 workflow 或 capability 文件不应阻断新版本运行；仅当当前 provider 身份无法证明时才进入 `binding_unknown`。

## 9. 验收标准

- dirty checkout、detached HEAD、branch drift 可在同一 envelope 中修复；
- timeout、断线、任务创建失败可同任务退避重试；
- 429/容量不足进入 `capacity_wait`；
- 缺少 workflow 证据不阻断 complex 派发；
- 任何身份字段冲突都不会产生第二 writer 或新 successor；
- 节点故障不阻塞无关 ready 节点；
- 旧运行不会参与新运行恢复；
- 历史事件验证与当前执行验证互不替代；
- 混合版本安装被隔离并可生成迁移证据；
- 产品、权限、deploy、凭据和不可逆安全事项仍能可靠进入用户决策。

## V4.4 文档落地说明

实现文档必须把五类恢复（`repairable`、`retryable`、`capacity_wait`、`binding_unknown`、`external_decision`）与节点作用域写清楚。`repairable`、`retryable` 和 `capacity_wait` 均复用原 task/generation/writer/worktree/lease/cursor；`binding_unknown` 只阻塞当前节点及依赖闭包；`external_decision` 才暂停等待用户决定。

workflow 文件属于审计证据，不是复杂任务 dispatch 的硬门。迁移和回滚必须显式执行并分别留证；旧 V2–V4.3 运行只读隔离，生成 `history_manifest.json`、`migration_evidence.json` 时保留源哈希，历史解释不完整标记 `historical_incomplete`，不得恢复成当前执行状态。
