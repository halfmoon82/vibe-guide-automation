# V4.5 PRD Guide、代码现状规划与目标追踪 PRD

状态：修订草案，待用户确认。旧版 `2026-09-14-v45-session-s0-s1-entry-*` 保留为历史，不自动继承授权。

## 产品链路

自然语言目标 → PRD Guide → PRD 草案与关键产品决策确认 → Agent 只读代码分析 → `.vibe/plans/<plan-id>/planning-brief.md` → Spec/Issue → DAG → plan → authorization。

用户只描述问题、目标或期望结果。系统结合当前代码生成 PRD 初稿；自动内容标记 `system_inferred`，用户明确或确认内容标记 `user_confirmed`，重大但未决取舍标记 `needs_confirmation`，证据不足标记 `unverified`。只有会改变产品方向、授权边界或验收语义的问题要求确认；普通缺失继续规划。重大歧义才为 `blocked_design`。

Agent 在计划生成前只读检查代码入口、模块、调用链、现有行为和测试。规划阶段只验证结构、合同、拓扑、代码证据引用和范围，不等待 engine/provider/Monitor/lifecycle 证据；执行阶段再验证 execution engine、provider binding、runtime plan 和 lifecycle evidence。

## 成功标准

每个 complex 计划都有产品经理可读 planning brief；每个 PRD 目标均追踪到用户场景、当前代码证据（项目相对路径和符号）、Spec、Issue、DAG 节点和运行时验收。复杂计划统一包含 `iteration_context`、`compatibility_scope`、`agentsmd_acceptance_refs`、`integration_acceptance_contract`、`unverified_or_excluded`。旧格式兼容读取，但不能绕过能力、授权或拓扑校验。

## V4.5 修订增量：执行闭环缺陷

新增范围：修复 4.4 授权卡路径无法直接物化计划的入口断层，并修复授权卡选择项与权限字段矛盾的签发缺陷。旧 revision 与旧授权卡保留为历史，新 revision 不自动继承旧授权。
