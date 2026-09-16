# V4.5 PRD Guide 实施计划（revision 6）

先完成 ISSUE-06，再并行实现 ISSUE-07 授权卡路径物化入口与 ISSUE-08 授权卡权限一致性校验，最后由 ISSUE-05 收口整合 Review。Task 2 已完成成果保留，不重做。

复杂计划统一包含 `iteration_context`、`compatibility_scope`、`agentsmd_acceptance_refs`、`integration_acceptance_contract`、`unverified_or_excluded`。旧格式兼容读取但不得绕过能力、授权或拓扑校验。规划阶段不等待 engine/provider/Monitor/lifecycle 证据；执行阶段校验这些运行时证据。

验证：`python3 -m unittest discover -s tests -v`、定向 ISSUE-06/07/08 测试、`python3 -m vibe_guide --help`、`git diff --check`。

## 工程缺陷修复验收

ISSUE-07 必须覆盖：初始化 `.vibe/state.json` 后可恢复；`authorize <card-path>` 与 `monitor --plan <plan-id>` 解析语义不冲突；缺少物化计划时给出可恢复诊断；授权卡绑定源物化成功后才允许 Monitor；不得用临时 session 计划替代 rev6 计划。
