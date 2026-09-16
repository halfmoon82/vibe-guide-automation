# V4.5 PRD Guide Spec/Issue

状态：修订草案，待用户确认。

## ISSUE-06：PRD Guide、代码现状规划与目标追踪

范围：自然语言入口、来源状态标记、只读代码分析、planning brief、PRD目标到运行时验收的完整追踪、结构校验与 `blocked_design` 语义。

文件白名单：`vibe_guide/planner.py`、`vibe_guide/prd_profiles.py`、`vibe_guide/session_entry.py`、`vibe_guide/dag.py`、`vibe_guide/models.py`、`tests/test_planner.py`、新增 ISSUE-06 定向测试；不得重做 Task 2 的 S0/S1 稳定入口。

验收：planning brief 至少含产品目标、用户场景、代码现状、方案、非目标、Spec/Issue 映射、运行时验收；证据引用相对路径和符号；普通缺失不阻断；重大歧义阻断；规划阶段无 worker/provider/Monitor/Git 副作用。

## 其他 Issue

ISSUE-01 保留入口 Skill 与初始化提案三态登记（原白名单）；ISSUE-02 保留 S0/S1 分流与稳定物化（Task 2 已完成，不重做）；ISSUE-03 保留候选归属、原任务恢复和递归保护；ISSUE-04 保留 V4.2/V4.3/V4.4 兼容与 native_control_plane；ISSUE-05 保留授权即开工、DAG 图和回归收口。各 Issue 原文件边界、Intent/Proof 和历史证据继续有效，新增目标追踪字段必须通过 ISSUE-06 合同。

## DAG

`ISSUE-02 → ISSUE-06 → ISSUE-05`；`ISSUE-01 → ISSUE-05`；`ISSUE-03 → ISSUE-05`；`ISSUE-04 → ISSUE-05`。初始 ready：ISSUE-01、ISSUE-02、ISSUE-03；ISSUE-06 等 ISSUE-02 accepted 后 ready；ISSUE-05 仅在 ISSUE-01/03/04/06 accepted 后 ready。integration-review 位于全部业务节点之后，`integration_after`，不进入初始 ready，且只追加一次。
