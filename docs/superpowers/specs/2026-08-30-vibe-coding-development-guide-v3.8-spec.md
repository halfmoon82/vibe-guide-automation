# Vibe Coding 辅助开发向导 V3.8 Spec

**状态：** `reviewed`
**Spec ID：** `vibe-guide-v3.8-spec`
**Revision：** `2`
**PRD：** `docs/superpowers/specs/2026-08-30-vibe-coding-development-guide-v3.8-prd.md@2`
**基线 Spec：** `docs/superpowers/specs/2026-08-29-vibe-coding-development-guide-v3-spec-rev3.md@3`
**Issue/DAG：** `docs/superpowers/specs/2026-08-30-vibe-coding-development-guide-v3.8-issues.yaml@2`；`docs/superpowers/plans/2026-08-30-vibe-coding-guide-v3.8-spec-issue-dag.yaml@3`

本 Spec 将已批准的 V3.8 PRD 转换为可实现、可测试的接口与 Issue 合同。它不创建 Worker、run、worktree、授权卡或 provider action，也不授权代码变更、commit、push、Change Request、merge 或 Deploy。V3 历史任务、旧授权和旧运行证据只作为只读基线，不能被本 Spec 重新绑定。

## 1. 共同约束

1. 所有新 JSON 产物必须可序列化、原子写入、带 `plan_id`、`plan_revision`、`run_id`（适用时）、`execution_epoch` 和 `evidence_ref`。
2. 当前运行事实只从 `run-manifest.json` 读取；`state.json`、`tasks.json`、授权卡和事件只能保存引用、历史索引或角色专属证据。
3. `mismatch`、`unknown`、timeout、空响应、格式异常和 provider 自报均不能转化为通过、不可用或成功。
4. 一个节点只有一个有效 writer；developer 与 reviewer 必须是不同的独立任务，reviewer 只读。
5. V3.8 不改变 S0/S1 路由、visible worker/reviewer、fail-closed、Deploy 排除或现有状态含义。
6. 未经新的用户授权，本文档集不得创建 Worker、run、授权卡或执行任何外部动作。

## 2. 数据契约

### 2.1 Run Manifest

路径：`.vibe/runs/<run-id>/run-manifest.json`

```json
{
  "schema_version": 1,
  "run_id": "run-v38-001",
  "plan_id": "vibe-guide-v3.8-spec-issue-dag",
  "plan_revision": 3,
  "execution_epoch": 1,
  "base": {
    "branch": "codex/v38-integration",
    "local_sha": "<40-hex-sha>",
    "remote_sha": "<40-hex-sha>",
    "verified_at": "<timestamp>"
  },
  "merge": {
    "allowed": false,
    "target_branch": "codex/v38-integration",
    "merge_to_main": false
  },
  "nodes": {
    "V38-1": {
      "worktree_strategy": "provider_managed_runtime",
      "worktree": null,
      "branch_policy": "node_unique_branch_before_first_write",
      "binding_status": "pending_until_provider_create"
    }
  },
  "preflight_ref": "runs/run-v38-001/preflight-report.json",
  "status": "preflight_pending",
  "previous_manifest_sha256": "<sha256>",
  "evidence_ref": "run-manifest:run-v38-001:1:1"
}
```
`plan_revision` 或 `execution_epoch` 变化时，旧授权摘要必须失效；历史 manifest、generation、cursor、host、worktree、branch 和 review 证据必须保留。

### 2.1.1 Provider-managed Worktree Binding

节点合同在 provider 创建任务前只声明运行时策略，不声明固定绝对 worktree：

```json
{
  "worktree_strategy": "provider_managed_runtime",
  "worktree": null,
  "branch_policy": "node_unique_branch_before_first_write",
  "binding_status": "pending_until_provider_create"
}
```

provider 创建 visible task 后，首次业务写入前必须追加并校验以下运行时绑定：

```json
{
  "task_id": "<provider task id>",
  "host_id": "<host id>",
  "worktree": "<absolute actual worktree>",
  "managed_root": "<provider managed root>",
  "branch": "codex/v38-1-e0",
  "head": "<40-hex-sha>",
  "clean": true,
  "base_sha": "<40-hex-sha>",
  "writer_lease": "<unique lease>",
  "cursor": "<provider cursor>"
}
```

初始 detached HEAD 只能表示“尚未写入”的候选现场；首次业务写入前必须解析或创建节点唯一 branch，并重新校验 `HEAD`、`clean` 和 `base_sha`。路径无法证明属于 provider managed root、branch/HEAD/clean/base SHA 无法确认、task/host/cursor 无法续接，均保持 `blocked_unknown`，不得写入、替换 writer 或创建 successor。

### 2.2 Preflight Report

路径：`.vibe/runs/<run-id>/preflight-report.json`

每个检查项使用 `passed`、`mismatch` 或 `unknown`，并带 `check_id`、`observed`、`expected`、`evidence_ref` 和 `recovery_action`。至少包含：基线同步、节点绑定策略、旧资源占用、路径写入重叠、真实入口到 allowlist、provider/可见任务能力、Baseline Health Manifest、Git 目标和 manifest/授权 digest/epoch 一致性。实际 provider task binding 在任务创建后由运行时 binding 门单独校验，不要求预检提前知道 provider 分配的绝对路径。

关键规则：

- 任一关键检查为 `mismatch` 或 `unknown`，整体状态为 `preflight_blocked`；
- 预检为只读，不创建 plan、task、worktree、provider action、commit、push、MR、merge 或 deploy；
- 只有全为 `passed` 才能产生 `ready_to_authorize`，授权卡只引用报告。

### 2.3 Implementation Brief

路径：`.vibe/runs/<run-id>/<issue-id>/implementation-brief.json`

必填字段为 `issue_id`、`goal`、`non_goals`、`owned_paths`、`read_paths`、`call_chain`、`invariants`、`expected_red`、`risk_notes`、`base_sha`、`plan_revision`、`execution_epoch` 和 `evidence_ref`。每个 invariant 必须有唯一 `id`、真实 `entrypoint`、`positive_case`、`negative_case` 和 `test_command`。

简报检查只验证结构和引用闭合：每条 acceptance 都有 invariant，每个 invariant 都有入口/正例/负例/测试，call chain 不越过 allowlist，绑定字段与 manifest 一致。未通过时为 `brief_pending`，不得写业务代码，也不增加用户确认轮次。

### 2.4 Contract Closure 与路径所有权

Contract Closure 输出 `contract_closure.json`，明确每条 acceptance 需要的真实入口、依赖模块、provider bridge、状态转换和 allowlist 文件。缺失文件或未声明入口返回 `contract_not_closed`，并指出 Issue、owner 和串行化建议。

每个节点输出 `owned_paths` 与 `read_paths`：

- `owned_paths` 有交集时预检失败；
- `read_paths` 交集允许并行；
- 共享写入只能归属一个 owner，其他节点改为硬依赖。

### 2.5 Baseline Health Manifest

路径：`.vibe/runs/<run-id>/baseline-health.json`

每个 run 只生成一次，记录测试 collection 数量、import errors、失败命令及 exit code、缺失模块/入口、Python/runtime、依赖、workspace root、基线 SHA、生成时间和 hash，并逐项标记 `in_scope`、`out_of_scope` 或 `must_restore`。

### 2.6 Review Matrix、Finding Bundle 与证据目录

Review Matrix 的每行必须包含 `invariant`、`real_entrypoint`、`positive_case`、`negative_case`、`evidence_command`、`result` 和 `evidence_ref`。Review 至少覆盖 provider route、foreign run/schema/generation/provenance、ancestor/root/lock/run symlink、Guidance 缺失或漂移、create/existing binding/start/resume/status、allowlist/副作用和 Red→Green 时序。

同一根因的多个表现合并为一个 finding bundle，字段为 `bundle_id`、`root_cause`、`severity`、`affected_invariants`、`minimal_fix_boundary`、`verification_command` 和 `generation`。P0–P2 必须退回原 developer，由原 reviewer 复审；第二次同类返工仍出现时转为 `contract_or_call_chain_review_required`。

证据目录为：

```text
.vibe/runs/<run-id>/<issue-id>/
  contract.json
  implementation-brief.json
  baseline-ref.json
  review/generation-0.json
  rework/generation-1/red.json
  rework/generation-1/handoff.json
  rework/generation-1/green.json
  summary.json
```

历史证据不可变；`summary.json` 只保存当前结论、generation、finding 计数和证据引用。

### 2.7 Git 动作目标

授权和 Change Request 合同必须使用 `merge_to_target_branch` 与 `merge_target_branch`；禁止只有 `merge=true` 的模糊授权。`merge_to_main` 默认且本轮固定为 `false`，Deploy 固定为 `false`，缺失目标分支时拒绝动作。

## 3. Issue 接口合同

### V38-1 Manifest 与 execution epoch

**Input：** 当前 plan、基线、节点绑定策略、历史 task/worktree/generation。

**Output：** 原子 `run-manifest.json`、epoch 变更事件、历史引用和 provider-managed binding 记录。

**Error：** plan revision、epoch、writer 或 provenance 无法确认时 `blocked_unknown`；不删除旧资源，不创建 successor。

**Acceptance：** 同一产品 plan 重新绑定时 epoch 递增；旧授权失效；provider task 创建后实际 binding 在首次业务写入前完成校验；重复恢复不产生第二 writer。

### V38-2 Preflight 与 Contract Closure

**Input：** manifest、节点合同、allowlist、路径所有权、provider 结构化能力证据和 baseline health。

**Output：** `preflight-report.json`、`contract_closure.json`、`ready_to_authorize` 或 `preflight_blocked`。

**Error：** 任一关键 mismatch/unknown 阻断授权前派发；缺失入口返回 `contract_not_closed`。

**Acceptance：** 旧占用、SHA 不一致、未声明写入重叠和真实入口缺失均在创建 worker 前被列出且零副作用；预检只校验 provider-managed 策略，实际 task/worktree binding 由首次写入前的运行时门校验。

### V38-3 Implementation Brief Gate

**Input：** Issue acceptance、manifest、allowlist、worktree 和 brief。

**Output：** `brief_pending` 或 `implementing`，并保留 brief 证据。

**Error：** 缺字段、调用链越界或绑定不一致时不得写业务代码。

**Acceptance：** 每条 acceptance 映射到完整 invariant；不合格 brief 只允许补齐，不增加用户授权轮次。

### V38-4 Path Ownership 与 Baseline Health

**Input：** 节点 owned/read paths、冻结基线、测试 collection/import 结果。

**Output：** 路径冲突报告、单份 `baseline-health.json` 和节点范围分类。

**Error：** 未声明写入重叠阻断并行；基线缺陷不被伪装成本节点缺陷。

**Acceptance：** 两个节点共享写入文件时只有 owner 可写，另一个节点被硬依赖；同一基线缺陷只登记一次。

### V38-5 Review Matrix 与 Finding Bundle

**Input：** manifest、brief、contract closure、不可变证据和当前代码。

**Output：** 一次性 Review Matrix、合并后的 finding bundles 或技术接受。

**Error：** 证据矛盾、路径越界或 provider 结果未知时保持 `blocked_unknown`，不以 PASS 替代缺口。

**Acceptance：** Review 覆盖所有 load-bearing invariants；同一根因只返回一个 bundle。

### V38-6 返工升级与证据回放

**Input：** Review bundle、历史 generation、原 developer/reviewer identity、summary 和回放证据。

**Output：** 原任务返工/复审、generation evidence、`contract_or_call_chain_review_required` 或当前 summary。

**Error：** 第二次同类缺口暂停继续返工；合同变化先使授权失效并重建受影响 DAG 后缀。

**Acceptance：** 原身份、cursor、worktree、branch 和证据链保留；重复 resume 不产生 successor。

### V38-7 结构化 Git 目标

**Input：** 当前授权动作、target branch、Change Request 合同。

**Output：** 显式目标分支的动作合同。

**Error：** 缺目标分支、`merge=true` 模糊动作或隐式 main 授权一律拒绝。

**Acceptance：** `codex/v38-7` 可独立校验；`merge_to_main=false`、`deploy=false` 不被覆盖。

### V38-8 V3.8 集成与文档验收

**Input：** V38-1 至 V38-7 的合同、测试和证据。

**Output：** V3.8 端到端验收报告、PRD 覆盖矩阵和可进入实施的计划输入。

**Error：** 任一 FR/AC 无映射、DAG 有环、授权边界漂移或未验证 provider 被冒充为可用时阻断。

**Acceptance：** FR-801..FR-811 与 AC-801..AC-808 全部映射；无外部动作副作用；所有 provider-managed task 在首次业务写入前都有可回放 binding 证据。

## 4. PRD 覆盖与验收映射

| PRD | 实现 Issue | 验收 |
|---|---|---|
| FR-801 | V38-1 | AC-807 |
| FR-802 | V38-2 | AC-801、AC-802 |
| FR-803 | V38-1 | AC-801、AC-807 |
| FR-804 | V38-3 | AC-803 |
| FR-805 | V38-2 | AC-802、AC-803 |
| FR-806 | V38-4 | AC-804 |
| FR-807 | V38-4 | AC-806 |
| FR-808 | V38-5 | AC-805 |
| FR-809 | V38-6 | AC-805 |
| FR-810 | V38-6 | AC-807 |
| FR-811 | V38-7 | AC-808 |

## 5. 阶段边界

```text
当前阶段：Spec/Issue/DAG
状态：reviewed
下一阶段：授权卡准备与执行前预检
用户动作：审阅预检结果和非执行授权卡；如需执行，再明确授权当前列明动作
禁止动作：创建 Worker、run、worktree、授权卡、provider action、commit、push、MR、merge、deploy
```
