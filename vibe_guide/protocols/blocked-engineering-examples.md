# 工程阻塞上报实例（blocked_engineering）

本页给 developer/reviewer 在**初次、续接、返工**三个阶段的最小可执行例子。它是入口指引与事实形状示例，不是新增的全局必填表单；监工以公共 `worker-deliver` 入口和当前任务合同校验身份、代次、writer 与授权。

## 上报规则

- 失败步骤写可定位动作，已试动作写结果；缺口与证据引用要能让监工继续查证。
- 命令行 `run-id/node/role/generation` 必须照抄当前派发合同；正文身份是诊断事实，不能代替入口绑定校验。续接/返工沿用原任务身份，以当前派发代次为准，不自行加一。下例 1/2/3 仅作示意，不由阶段推导代次。
- “错角色拒收”只指未派发的目标角色；当两个角色均已派发时，正文 `role/task_id` 自报不等于身份认证，不能据示例推导正文冲突必被拒绝。可信 caller、绑定和回执血缘由核心链校验；本页不授予代另一角色上报的权限。
- `event=blocked_engineering`、`status=blocked`、`implementation_complete=false` 表示工程阻塞，不得改写为 `delivered`、`complete` 或 `accepted`，也不需要伪造 `delivery_evidence`。
- 唤醒地址须实时查询，不从示例或历史聊天猜测。通知成功、已读、新 turn 都不等于原阻塞已解除。
- scope/合同许可必须由受保护入口实际落地；聊天中的“同意”或本上报中的文字不能替代授权、占用和唯一 writer 校验。

## 公共入口

在主项目目录执行（参数中的绝对路径按当前合同填写）：

```bash
vibe worker-deliver --project-root <主项目绝对路径> \
  --run-id <run-id> --node <node-id> --role <developer|reviewer> \
  --generation <generation> --payload @blocked-engineering.json --json
```

`--payload` 使用下表中选定的一项 JSON 对象（不是整个数组）；替换全部示例身份、时间和引用。证据只保留必要安全摘要及可定位的真实引用，不默认附整份会话，不写凭据、敏感原文或无关个人信息。阶段仅用于说明场景，不是新增调度字段。

上报成功只表示已归档/运输，返回的 `consumed` 不表示问题解决；传输回执 `status=completed` 也不是业务完成。监工负责沿原运行消费；worker 不调用 core resume，不另开任务。错角色、旧代次或未派发的未来代次被拒后先核对合同，不改数字碰运气。

上报后（包括拒收时）查询实际登记监工：

```bash
vibe supervisor-address --project-root <主项目绝对路径> \
  --run-id <run-id> --node <node-id> --role <developer|reviewer> \
  --generation <generation> --json
```

仅当响应为 `ok` 且有 `wake.channel/target/signal` 时，用本平台已授权原生工具按返回值唤醒；不根据本文臆造工具或目标。地址 unknown、无通道、发送超时/失败时保留具体缺口，交现有运行的兜底检查；不得换平台、另开会话、重发 create 或把通知当验收。已归档上报不丢，后续同代次 wait 可领取，相同内容重报幂等。

## 六个实例

以下 JSON 数组可以直接作为 `--payload` 的单项内容参考；示例中的事实字段均需替换为本次会话的真实观察。

```json
[
  {
    "role": "developer",
    "stage": "initial",
    "task_id": "original-developer",
    "generation": 1,
    "event": "blocked_engineering",
    "status": "blocked",
    "implementation_complete": false,
    "failed_step": "读取合同指定的入口模块后无法定位当前 writer worktree",
    "attempted_actions": [{"action": "读取任务绑定与 worktree", "result": "绑定文件缺失，未写入源码"}],
    "missing_conditions": ["需要监工核对原始任务绑定和 writer root"],
    "evidence_refs": ["session://current#turn-1", "contract://node#writer"],
    "observed_at": "2026-11-09T00:00:00Z",
    "next_action": "监工核对当前任务绑定，原 developer 重新验证 writer root 后继续；未验证前不写源码"
  },
  {
    "role": "developer",
    "stage": "continuation",
    "task_id": "original-developer",
    "generation": 2,
    "event": "blocked_engineering",
    "status": "blocked",
    "implementation_complete": false,
    "failed_step": "续接后执行定向测试时，合同依赖的输入证据无法读取",
    "attempted_actions": [{"action": "按原 cursor 重读输入", "result": "文件不可读，保留原代次上下文"}],
    "missing_conditions": ["需要补齐输入证据或确认其为未知"],
    "evidence_refs": ["session://current#turn-2", "artifact://input#unreadable"],
    "observed_at": "2026-11-09T00:01:00Z",
    "next_action": "监工协调上游补证，原 developer 在输入可读后复跑原失败测试；不创建 successor"
  },
  {
    "role": "developer",
    "stage": "rework",
    "task_id": "original-developer",
    "generation": 3,
    "event": "blocked_engineering",
    "status": "blocked",
    "implementation_complete": false,
    "failed_step": "返工修复后复跑回归，发现当前 writer 分支与冻结基线不一致",
    "attempted_actions": [{"action": "只读比较冻结 HEAD 与当前分支", "result": "发现差异，停止写入并保留返工证据"}],
    "missing_conditions": ["需要监工核对基线和唯一 writer 后再返工"],
    "evidence_refs": ["session://current#turn-3", "git://diff#frozen-head"],
    "observed_at": "2026-11-09T00:02:00Z",
    "next_action": "监工核对基线，原 developer 按有效合同纠偏并复跑原回归；保留旧成果"
  },
  {
    "role": "reviewer",
    "stage": "initial",
    "task_id": "original-reviewer",
    "generation": 1,
    "event": "blocked_engineering",
    "status": "blocked",
    "implementation_complete": false,
    "failed_step": "独立审查开始前无法读取 developer 交付证据",
    "attempted_actions": [{"action": "按合同路径读取交付", "result": "路径不存在，未代改业务代码"}],
    "missing_conditions": ["需要 developer 任务补齐交付路径或由监工标记未知"],
    "evidence_refs": ["session://current#review-1", "delivery://missing"],
    "observed_at": "2026-11-09T00:03:00Z",
    "next_action": "监工协调原 developer 补齐交付，原 reviewer 在交付可读后审查；不代改业务代码"
  },
  {
    "role": "reviewer",
    "stage": "continuation",
    "task_id": "original-reviewer",
    "generation": 2,
    "event": "blocked_engineering",
    "status": "blocked",
    "implementation_complete": false,
    "failed_step": "续接审查时，关键验收证据与当前任务代次不匹配",
    "attempted_actions": [{"action": "校验 evidence_ref 与 generation", "result": "发现旧代次证据，未消费为当前审查结论"}],
    "missing_conditions": ["需要绑定当前 reviewer 代次的证据"],
    "evidence_refs": ["session://current#review-2", "evidence://generation#stale"],
    "observed_at": "2026-11-09T00:04:00Z",
    "next_action": "监工定位当前有效输入，原 reviewer 核对证据绑定后复审；旧证据只留痕"
  },
  {
    "role": "reviewer",
    "stage": "rework",
    "task_id": "original-reviewer",
    "generation": 3,
    "event": "blocked_engineering",
    "status": "blocked",
    "implementation_complete": false,
    "failed_step": "返工后复审发现文件范围占用状态无法验证",
    "attempted_actions": [{"action": "只读检查节点 allowlist 与活跃 writer", "result": "占用证据缺失，停止审查"}],
    "missing_conditions": ["需要监工确认唯一 writer 和当前 allowlist"],
    "evidence_refs": ["session://current#review-3", "scope://writer#unknown"],
    "observed_at": "2026-11-09T00:05:00Z",
    "next_action": "监工查证占用和许可事务，原 reviewer 在唯一 writer 与 allowlist 一致后复审"
  }
]
```

监工收到后记录事实、判断、具体行动、原 worker 结果和复核条件；阻塞本身不产生 `complete`/`accepted`，也不自动取得 scope 或权限。

入口契约同时覆盖 request 内真实 `child_binding` 的正常 writer 与缺失 writer：后者应仍可归档工程阻塞，不创建目录、不获得写权；正常绑定不代表原生 App 续接已验。阻塞事实即使附上形状合法的完成证据，也必须拒绝改标 `accepted`，且不得产生接受回执或推进验收。当前 R2 对缺失 writer/`accepted` 的反例先冻结为 RED 待核心修复；不得把待集成预期写成已通过，原生帮助/验收闭环另验。

## scope 不是文字批准

报告“监工已口头同意新增文件”仍是阻塞事实，不会修改 allowlist。监工须通过现有受保护的 scope/reconcile 入口校验当前授权、项目规则、文件占用、唯一 writer 并留事件；worker 核对合同、绑定和实际范围一致后才继续写。无权、占用或事务未落地时保持原范围，不扩 `auto_scope_paths`、不加全局忽略、不借其他项目开关。有效许可的后续代次继承与提示模板接入由核心合同验证，本页不授予这些权限。
