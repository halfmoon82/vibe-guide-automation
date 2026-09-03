# V3.10 Rev2 DAG 审计

**Plan:** `vibe-guide-v3.10-rev2`

**Revision:** `2`

**Status:** `reviewed`

**Inputs:**

- PRD: `docs/superpowers/specs/2026-09-02-vibe-guide-v3.10-prd-design-rev2.md`
- Spec/Issue: `docs/superpowers/specs/2026-09-02-vibe-guide-v3.10-spec-issues-rev2.yaml`
- Implementation plan: `docs/superpowers/plans/2026-09-02-vibe-guide-v3.10-implementation-plan-rev2.md`
- DAG: `docs/superpowers/plans/2026-09-02-vibe-guide-v3.10-spec-issue-dag-rev2.yaml`

## 1. 结构结果

- 节点数：9；ID 唯一。
- 拓扑序：`V310-INSTALL-CONTRACT → V310-MIGRATION / V310-CAPABILITY / V310-ROUTING / V310-SELF-HEAL → V310-CLI / V310-ADAPTERS → V310-MERGE → V310-PACKAGE`。
- 初始 ready：`V310-INSTALL-CONTRACT`。
- 硬依赖无环；不存在同一 `parallel_group` 内的硬依赖。
- `integration_after` 只用于联调与收口，不改变独立节点的启动资格。

## 2. 并行审计

| 并行组 | 节点 | 审计结论 |
|---|---|---|
| foundation | INSTALL-CONTRACT | 单一基础合同，合法 |
| core | MIGRATION、CAPABILITY、ROUTING、SELF-HEAL | 共享安装合同后可并行；无相互硬依赖 |
| integration | CLI、ADAPTERS | 分别依赖各自输入；联调关系非阻塞 |
| delivery | MERGE | 等待目标合同和自愈合同，独立于 package |
| release | PACKAGE | 只在本地实现、测试和迁移证据齐全后收口 |

## 3. 合同与安全审计

- 每个 PRD 主题均映射到至少一个 Issue、节点、验收示例和测试文件。
- 每个 Issue 保留一个 writer 与一个独立只读 reviewer；工程绑定问题由 `V310-SELF-HEAL` 统一治理。
- 授权前冻结 provider、仓库/项目、目标分支、Issue/PR/MR 类型、源分支、文件范围和合并方法。
- 授权动作包含 `create_pr`、`create_mr`、`merge_local`、`merge_remote`，但这些动作必须逐项出现在用户确认的授权卡中。
- `deploy`、`credentials`、`system_permissions`、`destructive_write` 明确排除；push 也不由普通 V3.10 卡隐式获得。
- 已终审通过、P0–P2 清零和目标合同匹配是自动创建/合并的运行条件；漂移先由监工自愈，不能自愈时只隔离外部动作。
- provider unknown、visible locate 缺证、worktree/branch/cursor 漂移不构成全局 DAG 阻塞。

## 4. 作用域审计

- 不修改 V3.9 Rev4、V2 历史交付证据或 `E2E_MAILBOX`。
- 不从本审计启动 worker、Monitor、PR/MR、merge、push 或 Deploy。
- 旧 `vibe-guide-v3.10-rev4` run 与授权已失效并保留原始证据；Rev2 是新 plan/revision，不复用旧授权摘要。

## 5. 结论

`REVIEWED_READY_FOR_AUTHORIZATION`。Rev2 DAG 结构、并行关系、门禁作用域和自动化 PR/MR/merge 边界满足已确认 PRD。下一步是展示 Rev2 授权卡；在用户明确授权前不得启动监工或创建 worker。
