# VibeGuide 监工自治与恢复 PRD

状态：提案
日期：2026-10-13
适用范围：VibeGuide 已授权 DAG 的监工、心跳、worker 生命周期、provider 调用、换班与恢复
证据样本：`session-f9a554fff82f6766` / `run-ce79518f6b1e489db738ffef70437a20`

## 1. 问题与影响

VibeGuide 的目标是在一次授权后自动推进开发、测试、独立 Review、返工和验收。当前监工可以保存大量状态和证据，但在真实长运行中没有稳定完成“事件发生 → 唤醒 → 执行动作 → 绑定结果 → 更新 DAG → 派发下一节点”的闭环。结果是 worker 已交付或 provider 已返回，监工仍长期 `idle`、重复心跳或进入 `blocked_unknown`；换班会话增加，却没有唯一接管者完成后续动作；用户还需要反复点击 `Allow once`。

本次样本运行共有 14 个节点，其中 12 个已验收。`issue-156` 的实现与独立 Review 已结束，唯一 P1 属于监工生命周期：关闭并归档已完成的任务对，取得一次带相邻权威 usage/context 数据且比例 `< 0.5` 的真实 host idle beat，再执行正式验收。现有状态机却把该 P1 继续路由到 developer rework。`integration-review` 仍为 `planned`，从未启动。监工因此没有把已完成的工程结果转成 DAG 进度。

对用户的直接影响是：

- 已授权工作仍需持续人工盯屏、唤醒和批准；
- 心跳重复输出同一状态并消耗上下文，却不产生节点状态变化；
- 普通工程故障被放大成授权或产品阻塞；
- 多个换班会话争夺注意力，责任归属不清；
- `blocked_unknown` 掩盖了可自动恢复、可重试或已经有结果的具体状态；
- 证据门禁从保护边界变成例行推进的障碍。

## 2. 产品目标

1. 已授权 DAG 默认向前推进。普通工程故障由监工自行诊断、重试、恢复或正确改派。
2. worker 交付、Review 完成、provider 结果、重试到期、容量释放和依赖解锁都能可靠唤醒唯一监工。
3. 真正无事可做时保持静默 idle；出现可执行事件时自动恢复，无需用户盯屏。
4. 换班以原子方式转移唯一所有权、执行权限、cursor 和未完成动作，不产生第二 writer 或半接管会话。
5. 阻塞按责任层分类并路由到能解决它的执行者。只有产品或验收含义变化、新外部授权、deploy、凭据、系统安全权限和破坏性动作打断用户。
6. 保留必要的安全边界，同时把例行证据检查收敛到机器可判定、增量且与当前动作相关的最小集合。

## 3. 非目标

- 不降低唯一 writer、独立 reviewer、有效授权绑定、真实系统权限和 deploy 单独授权等边界。
- 不把自然语言自报、`ACTIVE`、收到消息、预检成功、测试通过或文件存在当作 DAG 已推进。
- 不根据本次样本宣称节省 token 或费用；该结论需要同类无换班对照、相同任务口径和价格数据。
- 不要求所有 provider 使用同一种原生工具，但要求它们满足同一动作、结果和恢复合同。

## 4. 产品原则

### 4.1 进度以状态转换为准

“监工恢复成功”必须至少产生一种持久变化：消费一个交付或 Review、绑定一个 provider 结果、解决一个可恢复故障、关闭或归档任务、改变节点状态、派发 ready 节点，或完成 run。会话变为 `active`、收到唤醒消息、执行预检和重复输出诊断都不算进度。

### 4.2 授权范围内自治

已验证的授权覆盖当前 DAG 中列明的开发、测试、Review、返工、验收和普通工程恢复。文件列表修正、同任务重试、工具重连、结果重绑、任务归档、ready 节点派发等不改变验收含义的动作，由监工执行并留痕。

### 4.3 事件驱动、静默等待

监工不靠固定频率复述状态维持“活着”。没有新事件时使用持久订阅或定时器静默等待；事件到达后按幂等方式处理。周期性心跳只负责租约、超时和丢失事件兜底。

### 4.4 具体故障优先于 UNKNOWN

系统能观察到 provider error、等待批准、未绑定结果、待续接任务或工具缺失时，必须保存具体类型和下一动作。只有完成能力刷新、结果查询和本地恢复仍无法判定时，才允许进入 `blocked_unknown`。

## 5. 运行时状态与事件模型

### 5.1 唯一运行所有者

每个 run 在任一时刻只有一个有效 supervisor lease：

```text
run_id + supervisor_id + lease_epoch + owner_thread_id + host_id
+ acquired_at + expires_at + last_event_cursor + authorization_digest
```

只有当前 lease 持有者可消费正式 mailbox、绑定 provider 结果、调用 `resume`、写 `state.json` / `tasks.json` / `events.jsonl` 和派发 worker。租约使用 epoch 防止旧监工在网络恢复后重新写入。

### 5.2 标准事件

至少支持以下持久、可去重事件：

- `worker.delivered`、`worker.failed`、`worker.waiting_approval`；
- `review.completed`、`review.findings_cleared`；
- `provider.action_required`、`provider.result_ready`、`provider.error`；
- `continuation.ready`、`retry.due`、`timeout.expired`；
- `capacity.released`、`dependency.satisfied`、`node.ready`；
- `authorization.changed`、`lease.expiring`、`handoff.requested`；
- `host.permission_required`、`host.toolset_changed`。

事件具有稳定 `event_id`、run/node/task 绑定、来源 cursor、发生时间和 payload digest。重复投递不得产生重复 worker、重复写入或重复用户通知。

### 5.3 运行状态与会话元状态分离

Codex 会话的 `active`、`idle`、`systemError` 和 host 的 `waitingOnApproval` 只是输入信号，不能直接映射为 run 或节点状态。运行健康由最近成功消费的事件、待处理动作、租约有效性、provider 调用结果和 DAG 状态共同判断。

## 6. 持久唤醒合同

### 6.1 worker 完成通知

worker 完成或失败时必须先持久写入带交付引用的事件，再尝试唤醒。唤醒失败不会丢失事件；当前监工、接班监工或兜底 heartbeat 都能从最后 cursor 重放。

### 6.2 idle 的定义与退出

只有同时满足以下条件才可进入 idle：

- 当前没有待消费事件或未绑定结果；
- 没有 ready 节点和到期重试；
- 没有需要关闭、归档或验收的任务；
- 所有活跃 worker 均有持久完成/失败订阅；
- 下一次租约与超时兜底已登记。

以下任一事件必须使 idle 监工自动恢复：交付、Review、provider 结果、超时、重试到期、容量释放、依赖解锁、授权变化、host 权限状态变化。用户发送“继续监工”仍可手动触发，但不是正常运行所必需。

### 6.3 防僵尸心跳

若连续 heartbeat 没有新 cursor、状态转换、native action 或交付消费，后续 heartbeat 不再向用户复述同一阻塞。它只更新租约并在达到停滞阈值时触发一次恢复动作。恢复动作仍无效时，记录结构化故障并按责任层处理。

## 7. 原生动作与结果绑定

### 7.1 外层执行闭环

`resume()` 负责计算下一步，但不能假设自己可以执行宿主原生工具。每个需要原生调用的步骤必须形成明确闭环：

```text
resume -> action_required(action_id, tool, args_digest)
宿主执行原生工具
-> provider result(action_id, terminal state, details)
-> bind_result(action_id)
-> resume -> DAG state transition
```

监工或 runtime bridge 必须负责外层循环，直到达到静默 idle、合法人类边界或 run 终态。单次 `resume()` 返回 `action_required` 不能被报告为完成。

### 7.2 结果收件箱

provider 结果先进入按 `action_id` 索引的持久收件箱。监工每次启动、恢复和 heartbeat 都先查询未绑定终态结果，再发起新动作。已存在结果的 action 不得重复执行。

### 7.3 竞态与终态优先级

晚到 timeout、旧 generation 结果和重复 callback 不能覆盖较新的有效终态。每个动作使用 generation、attempt 和单调状态版本；冲突结果进入审计记录，由确定性优先级处理。

### 7.4 错误保真

provider error 必须保留错误类型、原生错误码、是否可重试、需要的权限、建议动作和安全脱敏后的 stderr/结构化详情。不得把具体权限拒绝或工具缺失压缩为 `blocked_unknown`。

## 8. 原子换班与唯一接管

换班是一个事务，而不是“创建新会话并写摘要”：

1. 原监工冻结派发并写出 last cursor、未绑定 actions、活跃 tasks、重试计时器和 authorization/engine 摘要。
2. 候选监工校验同一 project/plan/run、授权摘要、工具能力和权限配置。
3. 候选监工完成预检，并证明可以执行当前所需的下一类原生动作。
4. registry 以 compare-and-swap 提升 `lease_epoch`，撤销旧 lease。
5. 新监工重放 last cursor 后的事件，绑定已有结果，执行下一动作并产生第一条真实进度。
6. 只有第 5 步成功后，换班标记 `complete`；否则回滚或进入一个明确的 failed handoff，不再创建第二候选。

同一 run 最多存在一个进行中的换班。新的恢复请求复用该事务，不创建平行 replacement chat。旧会话恢复后发现 lease epoch 过期，只能只读退出。

### 8.1 权限继承

替换会话应继承已验证的运行权限配置和当前 DAG 授权摘要。这里的继承是宿主创建参数与权限 profile 的显式复制，并在接管前验证，不是用自然语言声明“full access”。

若宿主无法继承用户已经授予的 full access，换班必须在转移 lease 前失败，并保留原监工所有权；不得创建一个持续弹出 `Allow once` 的半成品监工。

### 8.2 Approval 处理

- 已由宿主验证为本 run 持久允许的动作，不重复请求 `Allow once`。
- 真实系统安全权限、凭据或越出授权范围的动作仍请求用户。
- `waitingOnApproval` 的 worker/监工不能成为向自身投递 heartbeat 的唯一目标；approval 事件必须由独立控制面保存和通知。
- 未获批准时暂停具体动作，不冻结无依赖的其他 ready 节点。

## 9. 工具能力协商与降级

每次接班都从 session contract 读取当前暴露的原生工具及 evidence ref。所需工具缺失时按以下顺序处理：刷新能力 → 使用已验证的等价 adapter → 在同一任务上重连/续接 → 记录具体 capability failure。一次超时、工具未被提及或上一班拥有该工具，都不能证明当前可用或永久不可用。

路由校验以任务创建时持久保存并仍满足安全边界的 binding 为准。全局 worker profile 或 route projection 后续变化，不能否定一个可验证且未被撤销的有效 binding；确需迁移时走原子换班或 successor 合同。

engine attestation 的漂移只有在改变执行安全、代码来源或验收含义时才使授权失效。可从冻结 manifest、提交 SHA 和可信 runtime 路径自动重建的 attestation，由监工刷新后继续同一 run。

## 10. 阻塞所有权与路由

阻塞必须带 `owner_layer`、`next_action`、`retry_policy` 和 `user_interrupt`：

| 责任层 | 示例 | 默认处理 | 是否打断用户 |
| --- | --- | --- | --- |
| implementation | 测试失败、实现 P0–P2 | 回原 developer 同任务返工 | 否 |
| independent_review | Review 未完成、复审待执行 | 恢复原 reviewer 或创建合同允许的 reviewer | 否 |
| supervisor_lifecycle | 任务归档、结果消费、idle 测量、节点验收 | 监工执行生命周期 suffix | 否 |
| provider_host | 工具调用失败、结果未绑定、临时超时 | 查询结果、重试、重连或换班 | 通常否 |
| product_acceptance | PRD/验收存在多个实质答案 | 暂停受影响 DAG 后缀 | 是 |
| external_authorization | 新外部动作、凭据、系统安全权限、deploy | 请求明确授权 | 是 |

Review finding 必须声明 owning layer。`supervisor_lifecycle` finding 不得增加 developer generation。`provider_host` finding 不得伪装成代码缺陷。一个节点的局部阻塞不得阻断无依赖的其他 ready 节点。

## 11. 证据门禁精简

每个动作只检查会影响该动作安全或验收真实性的证据：

- 写代码：有效授权、唯一 writer、目标 worktree/branch、允许范围；
- Review：独立 reviewer、冻结 diff/提交、可复现验证；
- 结果绑定：action ID、run/node/generation、终态完整性；
- 接班：同 project/plan/run、authorization digest、lease epoch、工具与权限 profile；
- 验收：Issue 验收条件、P0–P2 清零和规定的真实运行证据；
- deploy：单独 deploy 授权和环境检查。

稳定不变的证据通过 digest 缓存；只在输入发生变化时重验。诊断报告、README、自报和重复全树哈希不能替代上述事实，也不能成为每次 heartbeat 的固定门槛。门禁失败必须指出缺失字段和机器可执行的补救动作。

## 12. 用户中断策略

监工只在以下情况请求用户：

1. 产品范围、行为或验收判定需要改变，且现有决策证据不能唯一回答；
2. 新的外部授权、deploy、release、凭据或系统安全权限；
3. 破坏性或不可逆动作；
4. 已穷尽已验证 adapter、重试和同 run 换班，仍无法取得完成任务所需的外部能力。

普通实现选择、已授权范围内的文件调整、worker 重试、结果重绑、任务归档、独立 Review 派发、临时 provider 故障和监工接班不打断用户。通知必须说明具体阻塞动作、已尝试恢复、唯一需要用户决定的事项；不单独报告 `blocked_unknown`。

## 13. 可观测性

每个 run 至少记录以下指标和事件：

- 从 worker 终态到监工消费的延迟；
- 从节点 ready 到派发、从 Review 通过到验收的延迟；
- heartbeat 总数、无状态变化 heartbeat 数和静默抑制数；
- native action 请求、执行、结果到达、绑定和重试的数量与延迟；
- 未绑定终态结果数量与最老年龄；
- handoff 尝试、成功、失败、重复候选和首次真实进度延迟；
- 每个 `Allow once` 的动作、权限 profile、是否属于已授权重复请求；
- `blocked_unknown` 次数、持续时间及转化后的真实原因；
- 每层 blocker 数量、停留时间和路由次数；
- supervisor 输入 token（cached/uncached）、输出 token、来源覆盖和接班重放量。

指标按 run、node、task、generation 和 lease epoch 关联。usage 只报告真实累计及口径；没有同类对照和价格时不输出“节省比例”或费用收益。

## 14. 验收场景

### A1：worker 完成自动推进

给定监工处于 idle 且 worker 写入 `worker.delivered`，在 30 秒内唯一监工被唤醒，消费交付、持久更新节点状态，并启动 Review 或记录一个具体合法边界。过程中无需用户消息或点击。重复投递相同事件不会重复启动 reviewer。

### A2：Review 完成后处理正确责任层

当 reviewer 返回 `P0=0, P1=1, P2=0` 且 P1 的 owner 为 `supervisor_lifecycle`，系统不增加 developer generation；监工执行任务对关闭、测量或验收 suffix。只有真实实现 finding 才回 developer。

### A3：provider 结果先于监工恢复

当 action 已有终态结果而监工重启，首次 `resume` 前先绑定该结果，不再次执行 action；节点随后产生确定的状态转换。

### A4：真正 idle 与事件唤醒

没有 pending 事件、ready 节点或到期重试时，连续三个 heartbeat 不产生用户可见复读，也不重复扫描全部历史。交付或 `retry.due` 到达后，监工在目标时限内恢复执行。

### A5：原子换班

模拟旧监工失联。系统只创建一个 replacement，复制并验证权限 profile，CAS 获得新 lease，重放 cursor，绑定已有结果并完成至少一个真实推进动作。旧监工恢复后的写入被 lease epoch 拒绝。任何失败阶段都不会留下两个 writer。

### A6：full access 不退化

给定宿主已验证当前 run 使用持久 full access，replacement 对同类已授权文件和测试动作不出现 `Allow once`。无法继承时接班在转移 lease 前明确失败，并保留可恢复原所有权。

### A7：approval 不自锁

监工会话处于 `waitingOnApproval` 时，approval 和 heartbeat 事件仍进入独立控制面；其他无依赖 ready 节点继续推进。批准后原 action 从同一 action ID 继续。

### A8：能力变化可恢复

换班后缺少上一班的原生工具时，系统先刷新 session contract 并选择已验证 adapter。失败结果保留具体能力证据；不得仅因超时进入永久不可用或创建多个 replacement。

### A9：竞态不覆盖成功

provider 成功结果已绑定后再收到旧 timeout 或旧 generation failure，节点保持成功状态，晚到事件仅进入审计。

### A10：样本 run 回归

用脱敏夹具重放 `run-ce79518f6b1e489db738ffef70437a20` 的末段：12 个已验收节点保持锁定；`issue-156` 不再派发 developer，而是执行 pair closure、真实 idle measurement 和验收；随后 `integration-review` 被派发并完成；run 到达合法终态或唯一真实人类边界。

### A11：阻塞不扩散

一个 provider action 等待外部安全权限时，所有不依赖它且在授权范围内的 ready 节点继续派发。用户只收到一次包含具体动作与权限的请求。

### A12：进度指标真实

测试必须证明 `ACTIVE`、消息送达和 preflight 不增加 progress counter；只有 §4.1 的持久变化增加。无对照数据时报告中不出现 token 或费用节省结论。

## 15. 发布与兼容策略

1. 先以只读 shadow 模式计算事件、blocker owner 和下一动作，与现有监工输出对比。
2. 再启用持久事件收件箱和结果绑定，保持现有 state/task 文件可读并提供一次性迁移。
3. 对新 run 启用 supervisor lease 和原子换班；旧 run 只有在 registry、授权摘要和 action generation 可验证时原地迁移。
4. 最后启用静默 idle、自动恢复和精简门禁。每阶段保留回滚开关，不回滚用户授权和已验收节点。
5. 发布前执行状态恢复、重复事件、竞态、权限继承、唯一 writer、独立 Review、真实 provider sandbox 和长时间 idle 唤醒测试。mock 通过不能替代至少一次受支持桌面 App 的真实验收。

## 16. 安全不变量

- 每个节点只有一个有效 writer；lease 和 generation 冲突时拒绝写入。
- developer 与独立 reviewer 的身份和上下文隔离可验证。
- 授权必须绑定 project、plan、run、DAG revision 和摘要；范围或验收含义变化时重新授权。
- full access profile 不能代替真实系统安全权限、凭据或外部平台授权。
- deploy、release、生产写入和破坏性动作保持独立授权。
- 日志不保存 token、密码、私有正文或不必要的个人数据。
- 任何恢复、换班和 fallback 都不能通过创建第二 writer、篡改旧证据或伪造 provider 结果实现。

## 17. 已观测问题覆盖表

| 已观测问题 | 本 PRD 的要求 |
| --- | --- |
| 心跳复读但不执行 native action | §4.1、§6.3、§7.1 |
| worker 完成不能可靠唤醒或消费 | §5.2、§6.1、A1 |
| idle 缺少持久唤醒源 | §6.2、A4 |
| `ACTIVE`、消息送达、preflight 被当作进展 | §4.1、A12 |
| `resume()` 无法独立执行宿主工具 | §7.1 |
| provider 结果存在但未绑定 | §7.2、A3 |
| 同任务 continuation 被判 `blocked_unknown` | §4.4、§5.2、§10 |
| 生命周期 P1 被循环派回 developer | §10、A2、A10 |
| 旧 worker profile/route projection 否定有效 binding | §9 |
| engine attestation 漂移导致重复授权 | §9 |
| 换班停在登记或摘要，没有完成执行闭环 | §8、A5 |
| 同一 run 创建多个换班会话 | §8 |
| replacement 未继承 full access | §8.1、A6 |
| 已授权会话反复弹 `Allow once` | §8.2、§13、A6 |
| `waitingOnApproval` 使 heartbeat 投递自锁 | §8.2、A7 |
| 不同班次原生工具暴露不一致 | §9、A8 |
| 会话 active/idle/systemError 与运行健康混淆 | §5.3 |
| 重复轮询消耗上下文但无状态变化 | §6.3、§13 |
| 换班降低局部上下文却增加重放成本 | §8、§13 |
| 过期 handoff 或源码路径造成加载不确定 | §8、§9 |
| 晚到 timeout/result 覆盖有效结果 | §7.3、A9 |
| native error 丢失权限和失败细节 | §7.4 |
| 宽泛证据门禁阻碍例行恢复 | §11 |
| 监工只报告 blocker，不 dispatch/recover/consume | §2、§7、§10 |
| 必须保留的安全与授权边界 | §16 |

## 18. 完成定义

本 PRD 的实现只有在以下条件同时满足时才算完成：所有 A1–A12 自动化回归通过；至少一个真实 Codex App 长运行完成 worker 交付到 DAG 下一节点的无人值守闭环；一次真实换班证明唯一 lease 和权限继承；样本 run 末段夹具不再进入 developer 循环；独立 Review P0–P2 清零；完整测试、CLI help 和 `git diff --check` 通过。正式发布、安装、迁移和 deploy 分别验收。
