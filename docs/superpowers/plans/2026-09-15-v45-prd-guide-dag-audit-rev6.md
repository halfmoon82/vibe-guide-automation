# V4.5 PRD Guide DAG 审计（revision 6）

旧 revision 及旧授权卡保留为历史，被 revision 6 替代且不得自动继承授权。

```text
ISSUE-02 → ISSUE-06 → ISSUE-07 ─┐
                         ISSUE-08 ─┼→ ISSUE-05
ISSUE-01 ──────────────────────────┤
ISSUE-03 ──────────────────────────┤
ISSUE-04 ──────────────────────────┘
```

`depends_on`：ISSUE-06 依赖 ISSUE-02；ISSUE-07/08 依赖 ISSUE-06；ISSUE-05 依赖 ISSUE-01/03/04/07/08。`integration_after`：integration-review 位于全部业务节点之后，仅追加一次。`parallel_group`：初始 ISSUE-01、02、03、04；ISSUE-07 与 ISSUE-08 可并行。初始 ready 集合：ISSUE-01、ISSUE-02、ISSUE-03、ISSUE-04。规划阶段只验证结构、合同、拓扑、证据引用和范围。
