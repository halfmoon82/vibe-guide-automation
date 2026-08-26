# Vibe Guide Capability Contract Design

**状态：** `approved_for_implementation`
**范围：** V2 capability-contract 增量
**目标：** 让监工会话和 worker 会话都不能仅凭 LLM 自报“没有工具/终端/接口”而自停；能力判断必须回到初始化生成的机器可读事实合同。

## 1. 设计边界

本增量只解决“能力误报导致错误中断”这一类协作故障，不承诺消除所有模型幻觉，也不改变真实权限、provider 故障、授权缺失或未知外部状态的安全阻断。

- `AGENTS.md`：保存人类可读的全局判断规则；已有文件只生成补丁建议，不直接覆盖或追加。
- `.vibe/session-contract.json`：保存初始化和会话入口可验证的 capability 状态、作用域、证据引用和合同摘要。
- `init`：创建缺失的 capability contract；重复初始化必须幂等。
- `monitor`、`resume`、provider action 和 worker dispatch：读取并校验同一合同，worker 继承父 run 合同并绑定 task scope。
- LLM 的自然语言自报只能作为待核验请求，不能直接改变 run/node 状态。

## 2. 能力状态合同

能力记录使用明确状态：

- `verified_available`：运行时探测或实际 canary 成功；
- `not_exposed`：当前 provider/host/task 明确没有暴露该 route；
- `permission_denied`：route 存在但当前授权拒绝；
- `probe_failed`：探测失败但原因不足以证明不存在；
- `unknown_timeout`：超时；
- `stale`：证据超过有效期。

没有字段、空响应、格式异常、模型记忆缺失和一次调用失败都不能生成 `not_exposed` 或 `verified_unavailable`。

合同至少包含 `schema_version`、`project_digest`、`provider`、`host_id`、`scope`、`capabilities`、`contract_digest` 和 `expires_at`。每条能力包含 `status`、`route`、`evidence_ref` 和探测时间。合同文件为普通 JSON，不保存 token、cookie、原始 provider 文本或不必要的绝对用户路径。

## 3. 数据流与状态门

```text
vibe init
  └─ 只读探测 → .vibe/session-contract.json

monitor/resume/provider action
  └─ 校验合同 → 注入 supervisor context → 绑定 run/task → 调度

LLM 声称能力缺失
  └─ 查合同
      ├─ verified_available → 使用 canonical route，继续
      ├─ not_exposed/permission_denied → 按合同错误路径处理
      ├─ probe_failed/stale → refresh 或有限重试
      └─ unknown_timeout → blocked_unknown，不改写为 unavailable
```

监工和 worker 使用同一份能力事实来源。监工拥有调度权，但不拥有绕过合同的能力裁决权；worker 不拥有状态终止权。

## 4. AGENTS 行为合同

初始化生成的补丁建议包含以下规则：

- 不得根据记忆、README、工具未被提及或一次失败判断能力不存在；
- “当前会话未暴露”不等于“平台不具备该能力”；
- 超时、空响应和格式异常统一保持 `UNKNOWN`；
- 监工和 worker 的自然语言自报不是能力证据；
- 能力判断必须引用 session contract 的 `evidence_ref`；
- 没有证据时请求 refresh 或报告 `UNKNOWN`，不得直接终止；
- 只有 runtime/provider 的结构化结果才能进入能力阻断状态。

## 5. 实现与兼容性

- 新增一个小型能力合同模块，负责 schema、摘要、原子读写、有效期和状态查询。
- 初始化只新增合同文件和 AGENTS 补丁建议；原有 `.vibe/state.json`、Skill proposal 和 session gate 行为保持不变。
- 公共入口缺少合同或合同无效时返回 `blocked_unknown`/`attention`，不得伪造能力缺失，也不得启动第二 writer。
- 合同仅作为事实与提示来源；commit、push、MR、merge、Deploy 和外部安装仍保持原授权边界。

## 6. 验收标准

1. 初始化首次运行创建合同，重复运行不改写有效合同。
2. 合同摘要不一致、过期、符号链接、非法状态或 malformed JSON 被拒绝并保留未知边界。
3. 监工/worker 的“没有 terminal/tool/API”文字不能直接产生阻断状态。
4. `verified_available`、`not_exposed`、`permission_denied`、`probe_failed` 和 `unknown_timeout` 的转换都有独立测试。
5. 当前会话入口与 child worker 使用相同合同摘要；task scope 不得冒充平台全局能力。
6. 所有新增写入原子、可恢复、无凭据；既有测试之外的基线失败单独记录，不被本功能掩盖。
