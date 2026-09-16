# V4.5 PRD Guide DAG 审计（revision 5）

旧 revision 保留为历史并被本 revision 替代；旧授权不得继承。关系：`ISSUE-02 → ISSUE-06 → ISSUE-05`，`ISSUE-01 ─↗ ISSUE-05`，`ISSUE-03 ─↗ ISSUE-05`，`ISSUE-04 ─↗ ISSUE-05`。`depends_on` 为硬依赖，`integration_after` 仅收口，`parallel_group` 仅展示并行。

初始 ready：ISSUE-01、ISSUE-02、ISSUE-03、ISSUE-04；ISSUE-06 等 ISSUE-02；ISSUE-05 等 ISSUE-01/03/04/06。integration-review 位于所有业务节点之后、不在 ready 集合、不可重复追加，合同摘要必须等于 IntegrationAcceptanceContract。

每个 complex plan 必须包含 iteration_context、compatibility_scope、agentsmd_acceptance_refs、integration_acceptance_contract、unverified_or_excluded；BindingIntent 固化范围/节点/文件，BindingProof 刷新 writer/worktree/lease/cursor。规划阶段不等待运行时能力证据。
