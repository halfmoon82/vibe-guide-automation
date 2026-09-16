# V4.5 PRD Guide 实施计划（revision 5）

阶段一（规划）：新增 ISSUE-06 的自然语言 PRD Guide、只读代码现状分析、planning brief 和目标追踪校验；复用并保护 Task 2 的 S0/S1 入口。阶段二（执行，须另行授权）：按 DAG 实施、测试、Review、返工与验收。

复杂计划统一字段：`iteration_context`、`compatibility_scope`、`agentsmd_acceptance_refs`、`integration_acceptance_contract`、`unverified_or_excluded`。旧格式仅兼容读取，仍须通过能力、授权和拓扑校验。规划阶段验证结构/合同/拓扑/证据引用/范围；执行阶段验证 engine/provider/runtime/lifecycle。

文件白名单：ISSUE-06 仅限 planner、prd_profiles、session_entry、models、dag 及其定向测试；Task 2 文件边界保持不变。验证：`python3 -m unittest discover -s tests -v`、`python3 -m vibe_guide --help`、`git diff --check`。
