# Vibe Coding 辅助开发向导 V3.8 PRD

**状态：** `approved`
**Revision：** `2`
**PRD ID：** `vibe-guide-v3.8-prd`
**日期：** 2026-08-30
**基线：** V3 已批准的阶段治理、可见任务、授权、Review、恢复和 Guidance Contract 约束
**定位：** 执行治理优化版，不改变产品目标，不引入新的独立版本流水线

> 本 PRD 将此前拟议的“授权前预检与绑定收敛”吸收为 V3.8 的一部分。项目不定义名为 `rev7` 的独立计划、授权卡、运行目录、节点或状态；相关能力只以本 PRD 的功能需求和数据契约存在。

## 1. 背景与问题

V3 rev3–rev6 已证明现有安全边界是必要的，但执行成本过高，主要表现为：

1. 同一事实同时存在于 plan、authorization card、state、tasks、events、handoff、review 和 delivery 多套文件中，容易发生版本、基线和状态漂移。
2. worker 在开始写代码前没有一份稳定、可审查的实现简报。当前交付通常能说明“改了什么”和“测试通过”，却不能在开发前证明每条验收条件对应哪个真实入口、正例、负例和调用链。
3. allowlist、Issue 合同和真实调用链可能不闭合。缺口直到独立 Review 或返工时才暴露，形成“修复一个边界、再发现一个边界”的串行返工。
4. 并行节点的共享文件、集成分支和 provider 路由没有在授权前统一检查。rev4 曾出现 Change Request 初始目标为 `main` 以及跨节点冲突；rev5 又在授权后才发现旧任务和 worktree 占用原绑定。
5. 基线本身的导入错误、缺失模块和不可用入口由多个 worker 重复发现，混淆了“本节点回归失败”和“冻结基线已知缺口”。
6. Review 证据过度依赖不断追加的长篇 delivery 文档。Red→Green 的原始时序、当前结论和历史证据混在一起，增加上下文读取和复审成本。

### 1.1 复盘判断

返工次数多与 developer 前置计划不足有关，但不是唯一或首要原因。更完整的根因是：

- 计划没有成为首次写入前的可验证门；
- 合同没有覆盖完整生产调用链；
- 授权前缺少原子预检；
- Review 以增量发现为主，缺少一次性验收矩阵；
- 多种运行状态缺少单一事实源。

因此 V3.8 的目标不是继续增加治理层，而是减少重复控制面，并把关键检查前移到 worker 创建和首次写入之前。

## 2. 目标用户与角色

### 2.1 目标用户

- **产品/技术负责人：** 希望一次看懂当前执行范围、风险、成本和是否可以授权。
- **监工：** 希望在派发前得到可信的基线、资源、路由和并行性结论，减少中途纠偏。
- **developer worker：** 希望得到边界清晰、可直接执行且不要求猜测调用链的 Issue 合同。
- **independent reviewer：** 希望按固定矩阵一次性核对不变量，而不是从长日志中逐轮寻找遗漏。
- **交付/收口角色：** 希望明确区分技术接受、commit、push、Change Request、merge、Deploy 和未知状态。

### 2.2 角色边界

- 监工负责预检、合同分发、独立 Review、Git 收口和状态推进，不代写业务代码。
- developer 是 provider 创建并完成运行时绑定的实际 worktree 内唯一代码 writer，负责实现简报、TDD、实现和自测。
- reviewer 与 developer 是两个不同的可见任务，只读审查，不修改业务代码。
- 产品取舍、范围扩大、外部权限、Deploy 和未列明动作仍须由用户单独决定或授权。

## 3. 产品原则

1. **先验证再授权：** 授权卡只能引用已完成的预检报告；预检为 `mismatch` 或 `unknown` 时不可执行。
2. **一次表达、按需引用：** 当前运行事实只保留在一个 Run Manifest，其他文件只保存引用和不可变历史证据。
3. **计划足够小但必须可审查：** developer 只需提交一份短 Implementation Brief，不新增用户确认轮次。
4. **契约覆盖真实入口：** acceptance 依赖的生产调用链、文件所有权和错误边界必须在创建 worker 前闭合。
5. **并行以文件所有权为准：** 共享文件冲突不是默认可接受的“联调关系”；未声明的写入重叠必须阻止并行。
6. **未知保持未知：** timeout、空响应、provider 自报和本地 fixture 不能转化为成功或不可用。
7. **历史保留、当前收敛：** 旧 revision、task、worktree、review 和 rework 证据不可覆盖；当前结论只从当前 Run Manifest 和被引用的证据读取。
8. **动作目标结构化：** `merge` 必须带显式目标分支；`main` 不因通用 merge 开关而隐式获得授权。

## 4. 范围

### 4.1 本次包含

V3.8 只优化执行治理和交付效率，包含：

- 授权前基线、资源、路由、并行性和调用链预检；
- Run Manifest 作为当前运行事实源，以及 execution epoch 绑定；
- developer 首次写入前的 Implementation Brief；
- allowlist 与真实调用链的 Contract Closure 检查；
- 并行节点的路径所有权与冲突检查；
- 一次性的 Baseline Health Manifest；
- Review Matrix、finding bundle 和重复返工升级规则；
- 分层、可回放的状态和证据目录；
- 与现有 V3 visible developer/reviewer、fail-closed、授权和 Git 边界兼容。

### 4.2 本次不包含

- 不改变 S0/S1 阈值、simple/light_plan/complex 产品路由；
- 不改变 visible developer/reviewer、唯一 writer、独立 Review 和 fail-closed 原则；
- 不实现新的 provider 生命周期，不把 fixture 或 conformance 当作真实 provider 证据；
- 不自动升级 light_plan，不为缺失计划生成 `plan.json`；
- 不自动获得 Deploy、系统权限、凭据、外部仓库权限或未列明动作；
- 不重做 V3-4/V3-6/V3-8 已通过技术 Review 的业务实现；
- 不删除、覆盖或强制回收旧 task、worktree、revision 或证据；
- 不引入新的 reviewer 层、周期 automation 或“总监工”层；
- 不把 rev7 作为独立产品版本或执行拓扑；
- 不在本 PRD 阶段执行 commit、push、Change Request、merge 或 Deploy。

## 5. 术语与状态边界

### 5.1 版本与执行绑定

- **Product plan revision：** 产品合同、Issue、依赖和验收变化时递增。
- **Execution epoch：** 同一产品计划因旧资源占用、工具恢复或 provider 重绑定而重新建立执行绑定时递增；不改变产品范围。
- **Run Manifest：** 当前运行唯一事实源，绑定 plan revision、execution epoch、基线、目标分支、节点和任务身份。
- **历史证据：** 旧 manifest、authorization、task、review、rework 和事件，只读保留，不参与当前授权判断，除非被当前 manifest 明确引用。

### 5.2 当前状态

V3.8 只新增下列治理状态，不扩展为更多相似状态：

| 状态 | 含义 | 是否允许代码写入 |
|---|---|---:|
| `preflight_pending` | 尚未完成执行前预检 | 否 |
| `preflight_blocked` | 预检存在 mismatch/unknown | 否 |
| `ready_to_authorize` | 预检通过，可展示授权卡 | 否 |
| `binding_pending` | provider task 已创建，实际 worktree/branch binding 尚未完成校验 | 否 |
| `brief_pending` | worker 已创建，尚未提交合格简报 | 否 |
| `implementing` | 简报已通过，允许在绑定现场写入 | 是 |
| `reviewing` | 等待独立 Review | 否（reviewer 只读） |
| `rework_required` | finding bundle 未清零，返回原 developer | 否，直到 brief/返工门通过 |

现有 `accepted`、`git_delivery_pending`、`blocked_unknown` 等状态继续使用，但其含义不被 V3.8 改写。

## 6. 用户与系统流程

```text
需求/PRD
  → Spec/Issue/DAG
  → V3.8 preflight
      ├─ mismatch/unknown → preflight_blocked（不生成可执行授权）
      └─ passed
          → 展示一次授权卡
          → 创建 visible developer/reviewer
          → developer 提交 Implementation Brief
              ├─ 不合格 → brief_pending，补齐后再写入
              └─ 合格 → TDD Red → Green → Refactor
          → reviewer 按 Review Matrix 一次性审查
              ├─ finding bundle → 原 developer 批量返工
              └─ PASS → 技术接受 → 独立 Git 收口
```

V3.8 不增加“计划确认 → 再确认简报 → 再授权”的用户步骤。Implementation Brief 是 worker 与监工之间的技术完整性门，不是新的产品决策门。

## 7. 功能需求

### FR-801：Run Manifest 单一事实源

系统必须为每个执行 run 维护一份原子可恢复的 `run-manifest.json`，至少包含：

```json
{
  "run_id": "run-v3-...",
  "plan_id": "vibe-guide-v3-spec-issue-dag",
  "plan_revision": 3,
  "execution_epoch": 1,
  "base": {
    "branch": "codex/v38-integration",
    "local_sha": "...",
    "remote_sha": "...",
    "verified_at": "..."
  },
  "merge": {
    "allowed": true,
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
  "preflight": {},
  "status": "preflight_pending"
}
```

要求：

- 当前状态、当前基线、目标分支和当前 binding 只能从 manifest 读取；
- `state.json`、`tasks.json` 和授权卡只保存 manifest 引用、历史索引或角色专属证据，不得产生互相矛盾的当前事实；
- manifest 原子替换，保留前一版本 hash 和事件引用；
- plan revision 或执行 epoch 变化必须使旧授权摘要失效；
- 不删除历史 generation、cursor、host、worktree、branch 或 review 证据。

### FR-802：授权前 Preflight

在展示或接受可执行授权卡前，监工必须生成 `preflight-report.json`，逐项返回 `passed`、`mismatch` 或 `unknown`：

1. 本地基线 commit 存在且可读取；
2. 本地集成分支与远端目标分支 SHA 一致；
3. 每个节点的 branch policy、provider、adapter、worktree strategy 和 allowlist 可解析；
4. 新 binding 名称未被旧 generation 或 active task 占用；
5. 旧 task/worktree 已登记为历史且无 active writer；
6. 节点之间没有未声明的 allowlist 写入重叠；
7. 每条 acceptance 的真实生产入口都能映射到 allowlist；
8. provider 能力、可见任务能力和归档/恢复能力按结构化证据标注 freshness；
9. 基线测试 collection/import 状态已生成 Baseline Health Manifest；
10. merge 动作具有显式 `target_branch`，且 `merge_to_main=false`；
11. 当前 run、plan revision、authorization digest、execution epoch 相互一致。

规则：

- 任一关键项为 `mismatch` 或 `unknown`，报告 `preflight_blocked`，不得创建 developer/reviewer、worktree 或 provider action；
- 预检必须是只读的，不创建 plan、task、worktree、commit、push、MR、merge 或 deploy；
- 预检通过后生成授权卡；授权卡只引用报告，不复制一套可漂移的事实；
- 授权后若基线、目标分支、binding 或合同发生变化，授权立即失效并回到预检；provider 创建任务后的实际 binding 不得以合同中的占位字段替代。

### FR-803：Execution epoch 与旧资源隔离

同一产品 plan 因旧资源占用或工具恢复需要重新绑定时：

- 递增 `execution_epoch`，保留旧 epoch 的 task/worktree/证据；
- 节点先使用 `worktree_strategy=provider_managed_runtime`，不在创建前承诺固定绝对 worktree 路径；
- provider 创建任务后，首次业务写入前必须登记并校验 `task_id`、`host_id`、实际绝对 `worktree/cwd`、其所属 managed root、节点唯一 branch、HEAD、clean、base SHA、唯一 writer lease 和 cursor；
- provider 初始返回 detached HEAD 只能作为尚未写入的候选现场；首次业务写入前必须解析或创建节点唯一 branch，并再次校验 HEAD、clean 和 base SHA；
- 新 branch 名称必须可证明不与旧 generation 冲突，推荐使用节点名加 epoch 后缀；
- 旧 task 不得被继续写入，不得通过删除登记制造空闲容量；
- 旧资源状态不确定时保持 `unknown`，不创建 successor 或第二 writer。

### FR-804：Implementation Brief

每个 developer 在首次修改业务代码前必须写入当前 run 的 `implementation-brief.json`。简报必须短、结构化且可审查，至少包括：

```json
{
  "issue_id": "V3-8",
  "goal": "...",
  "non_goals": ["..."],
  "owned_paths": ["..."],
  "call_chain": ["registry", "provider_action", "monitor", "cli"],
  "invariants": [
    {
      "id": "G-1",
      "entrypoint": "VisibleTaskProvider.create",
      "positive_case": "...",
      "negative_case": "missing or drifted contract",
      "test_command": "python3 -m unittest ..."
    }
  ],
  "expected_red": ["..."],
  "risk_notes": ["..."],
  "base_sha": "..."
}
```

监工只检查：

- 每条 acceptance criterion 是否有对应 invariant；
- 每个 invariant 是否有真实入口、正例、负例和测试命令；
- call chain 是否越过 allowlist；
- 简报基线、Issue、epoch 和 worktree 是否与 manifest 一致。

简报不合格时只允许补齐简报，不允许写业务代码；不新增用户确认步骤。

### FR-805：Contract Closure

预检必须静态检查“合同所需调用链”与“实际可修改文件”是否闭合：

- acceptance 引用的入口、依赖模块、provider bridge 和状态转换必须存在于当前基线或显式列入 allowlist；
- 若必须修改的文件不在 allowlist，返回 `contract_not_closed`，并指出缺失文件、所属 Issue 和建议的串行/拆分关系；
- 不能用 shim、monkey-patch、测试私有 helper 或本地 fallback 掩盖未闭合调用链；
- contract closure 通过后，才允许创建 developer task。

### FR-806：并行 Path Ownership

系统必须为每个节点计算 `owned_paths` 和 `read_paths`：

- 两个并行节点的 `owned_paths` 有交集时，预检失败；
- 只读共享依赖可以保留 `read_paths` 交集；
- 如确需共享写入，必须将文件所有权归属一个节点，并把另一个节点改为硬依赖；
- `integration_after` 只表达联调顺序，不得掩盖未声明的写入冲突；
- 预检输出冲突路径、节点、分支和建议串行化方案。

### FR-807：Baseline Health Manifest

每个 run 只生成一次 `baseline-health.json`，至少记录：

- 测试 collection 数量、import errors、失败命令和 exit code；
- 缺失模块、缺失入口和已知冻结基线缺陷；
- 与当前 Issue allowlist 的关系：`in_scope`、`out_of_scope` 或 `must_restore`；
- Python/runtime、依赖和工作区 root；
- 生成时间、基线 SHA 和 hash。

后续 worker 和 reviewer 必须引用该文件，不重复把同一基线错误当作本节点新缺陷。若一个基线缺陷阻断多个节点，监工必须在 DAG 中创建一个共享前置修复节点，不能让每个节点各自绕过。

### FR-808：Review Matrix 与 Finding Bundle

Reviewer 必须按 manifest、implementation brief 和 contract closure 生成固定矩阵：

| invariant | 真实入口 | 正例 | 负例 | 证据命令 | 结果 |
|---|---|---|---|---|---|

Review 必须覆盖至少：

- missing/wrong/conflicting provider route；
- foreign run、schema、generation 和 provenance；
- ancestor/root/lock/run symlink；
- missing/damaged/drifted Guidance；
- create、existing binding、start、resume、status 各路径；
- allowlist、调用链和实际副作用；
- Red→Green 时序和非恒真断言。

同一根因的多个表现必须合并为一个 finding bundle，包含影响的 invariants、最小修复边界和一次性复验命令。P0–P2 仍必须回到同一 developer/reviewer 任务，不创建 successor。

### FR-809：重复返工升级

- 首次 Review 可以发现多个独立 finding，但必须在同一 bundle 中一次返回；
- 同一 Issue 第二次返工后若仍出现同类不变量缺口，系统必须暂停继续返工，生成 `contract_or_call_chain_review_required`；
- 该升级只要求监工复核合同、brief、allowlist 和调用链，不自动扩大范围；
- 若发现合同确实不完整，必须先更新受影响合同和 DAG 后缀，再重新预检；
- 若只是实现缺陷且合同闭合，不改变 plan revision，继续原 developer/reviewer 身份。

### FR-810：证据分层与回放

每个节点的证据目录改为：

```text
runs/<run-id>/<issue-id>/
  contract.json
  implementation-brief.json
  baseline-ref.json
  review/
    generation-0.json
    generation-1.json
  rework/
    generation-1/
      red.json
      handoff.json
      green.json
  summary.json
```

要求：

- 历史证据不可变；当前 `summary.json` 只保存当前结论、generation、finding 计数和证据引用；
- Red/Green 证据包含命令、exit code、精确断言、base SHA、时间和文件 hash；
- `delivery.md` 可以作为人类可读摘要，但不再是唯一事实源；
- 监工正常等待只读取 manifest、状态和 summary；只有证据矛盾或正式 Review 才下钻到完整 diff。

### FR-811：结构化 Git 动作目标

授权和 Change Request 合同必须使用：

```json
{
  "commit": true,
  "push": true,
  "create_change_request": true,
  "merge_to_target_branch": true,
  "merge_target_branch": "codex/v3-9",
  "merge_to_main": false,
  "deploy": false
}
```

系统拒绝只有 `merge=true` 而没有目标分支的当前授权。`merge_to_main=true` 必须是单独、明确、重新授权的动作；V3.8 默认始终为 false。

## 8. 失败与恢复行为

| 场景 | 结果 | 最小恢复动作 |
|---|---|---|
| 本地/远端 SHA 不一致 | `preflight_blocked` | refresh 远端并重建 manifest；不创建 worker |
| 旧 task/worktree 占用新绑定 | `preflight_blocked` | 递增 execution epoch，生成新 binding；保留旧现场 |
| provider 路由字段缺失或冲突 | `preflight_blocked` 或 `blocked_unknown` | 重新探测真实 provider；不回填本地合同值 |
| allowlist 与调用链不闭合 | `contract_not_closed` | 拆分 Issue、调整 owner 或扩大合同后重新审计 |
| 基线 import/collection 错误 | 记录到 `baseline-health` | 引用已知缺陷；只有共享前置修复才改变它 |
| Implementation Brief 不合格 | `brief_pending` | worker 补齐 brief，不写业务代码 |
| Review 发现 P0–P2 | `rework_required` | 原 developer 批量修复，原 reviewer 复审 |
| 同类问题第二次返工仍出现 | `contract_or_call_chain_review_required` | 监工先审合同和调用链，不自动追加返工 |
| timeout、空响应、状态查询失败 | `unknown`/`blocked_unknown` | 保留 retry/remediation；不声称成功或不可用 |
| worker 工具丢失 | `successor_blocked_unknown` | 先证明原 task 已终止、唯一 writer、HEAD/clean 匹配，再考虑 successor |

任何失败分支都不得执行未列明的 commit、push、Change Request、merge、Deploy、删除或强制回收。

## 9. 验收标准

### AC-801：预检先于授权和派发

给定一个存在旧 task/worktree、错误基线或目标分支缺失的运行：

- 预检报告准确列出冲突；
- 状态为 `preflight_blocked`；
- 不创建 developer/reviewer、worktree、provider request 或 Git 外部动作；
- 修复资源并递增 execution epoch 后，新的 manifest 可回放且旧证据仍存在。

### AC-802：预检通过才能授权

给定本地/远端 SHA、路径所有权、调用链、provider 能力证据和目标分支全部一致：

- 生成 `ready_to_authorize`；
- 授权卡只引用预检报告；
- `merge_target_branch` 为显式 `codex/v3-9`，`merge_to_main=false`；
- 创建任务后，实际 `task_id/host_id/worktree/managed_root/branch/HEAD/clean/base_sha/lease/cursor` 登记完成并与 manifest 和节点合同一致，才允许首次业务写入；创建前不要求固定绝对 worktree 路径。

### AC-803：Implementation Brief 阻止盲写

给定 worker 没有 brief、brief 缺少 invariant 或调用链超出 allowlist：

- 状态保持 `brief_pending`；
- 业务文件没有写入；
- 系统返回具体缺口；
- 补齐 brief 后才能进入 `implementing`，且不新增用户授权轮次。

### AC-804：并行冲突提前发现

给定两个 ready 节点共享一个未声明的写入文件：

- 预检阻止并行并指出冲突节点和路径；
- 选择 owner/硬依赖后可重新计算 ready 集合；
- 不创建两个 writer，也不等待到 MR/merge 阶段才发现冲突。

### AC-805：Review 一次性闭包

给定一组跨 registry、provider、Monitor、CLI 的验收条件：

- brief 和 Review Matrix 覆盖真实入口、正例、负例和调用链；
- reviewer 输出一个可执行 finding bundle；
- 同一根因不通过连续多个独立返工隐式追加；
- 第二次同类返工自动升级合同/调用链复核。

### AC-806：基线问题不重复消耗

给定冻结基线缺少 `paths.py`、`diagnostics.py` 或 `__main__.py` 等模块：

- run 只生成一份 Baseline Health Manifest；
- 每个节点引用同一证据，不重复创建同类阻塞；
- 节点报告区分 `out_of_scope baseline defect` 与当前 Issue 缺陷。

### AC-807：证据可回放、状态不漂移

- 任何当前状态都可由 Run Manifest、summary 和不可变证据重建；
- 历史 generation、cursor、host、worktree、branch 和 review 记录不被删除或覆盖；
- Red/Green 证据包含可校验 hash 和退出码；
- manifest、授权卡、任务登记和事件之间的 plan revision、epoch、digest 不一致时 fail closed。

### AC-808：Git 目标不含糊

- 系统拒绝无 `merge_target_branch` 的通用 merge 授权；
- `codex/v3-9` 目标可被独立验证；
- `main` 没有隐式授权；
- Deploy 始终独立且为 false，除非另有明确授权。

## 10. 成功指标

V3.8 上线后的首个完整 run 以以下指标评估，不以新增文件数量或规则数量评估：

- 100% worker 在首次业务写入前拥有合格 Implementation Brief；
- 100% 可执行授权卡关联 `preflight-report.json`，且预检无 `mismatch/unknown`；
- 0 次未声明的并行写入冲突进入 developer 或 MR 阶段；
- 0 次无目标分支的 merge 动作进入 Change Request；
- 同一基线缺陷只在一个 Baseline Health Manifest 中记录；
- 普通 Issue 首轮 Review 通过，或最多一次批量返工；
- 第二次同类返工必触发合同/调用链复核；
- 所有 unknown、governance_pending、blocked_unknown 均保留结构化原因和最小恢复动作；
- 不新增任何 deploy 或 merge-to-main 权限。

这些是运行质量指标，不是对单个 provider 能力或真实外部生命周期的替代证明。

## 11. 迁移与兼容策略

1. **rev3–rev6 历史：** 原目录、授权卡、task、worktree、review、rework 和事件只读保留，不改写为当前 V3.8 事实。
2. **当前 rev6：** 已通过技术 Review 的 V3-4、V3-6、V3-8 不重做；V3.8 只在其 Git delivery 或后续节点创建前提供预检和目标分支硬约束。
3. **旧状态文件：** 读取时通过适配层映射到 Run Manifest；无法确定 plan revision、epoch、run provenance 或 writer 唯一性时返回 `blocked_unknown`，不猜测补全。
4. **旧 delivery 文档：** 保留为人类可读历史；后续新 run 使用分层证据目录，逐步减少对长文档的依赖，不强制一次性迁移所有历史文件。
5. **旧 provider API：** 保留现有 `vibe` CLI/API 和三参数兼容桥；新增字段只能向后兼容，不能通过全局 monkey-patch 破坏 custom-path 或 import-order 行为。
6. **授权：** V3.8 是产品和执行合同变化，必须生成新的 V3.8 plan/authorization 摘要；旧授权不自动继承。预检通过后仍需用户明确授权当前列明动作。

## 12. 实施分期

### Phase A：预检与 Manifest

实现 FR-801–803、FR-811 的最小闭环。先验证基线同步、资源占用、epoch、唯一 writer 和 merge 目标。

### Phase B：Brief 与 Contract Closure

实现 FR-804–805。先阻止盲写和调用链未闭合，再开放常规 developer。

### Phase C：并行与基线治理

实现 FR-806–807。将路径所有权和基线缺陷从 Review 阶段前移到授权前。

### Phase D：Review 与证据收敛

实现 FR-808–810。保留旧证据，新增分层 summary、finding bundle 和重复返工升级。

每个 Phase 都必须有独立测试和可回滚边界；任何 Phase 不得自动包含 Deploy、merge-to-main 或外部 provider 权限。

## 13. 风险与取舍

| 风险 | 取舍 |
|---|---|
| 预检过严导致可并行任务被串行化 | 只阻止真实 `owned_paths` 冲突，允许纯读取共享 |
| Brief 变成新的文档负担 | 限定为结构化短表，不增加用户确认和独立 reviewer |
| Manifest 与旧 state 双写再次漂移 | manifest 做当前事实源，旧文件只保存引用/历史，不允许互相覆盖 |
| Contract Closure 误报 | 报告具体调用链和缺失文件，允许监工基于已批准合同修正，不自动扩大范围 |
| Review 矩阵过度全面 | 只覆盖会改变验收结论的入口和负例，稳定公共知识不重复列出 |
| 迁移历史成本过高 | 只读适配层渐进迁移，不重写 rev3–rev6 历史 |
| provider 真实能力仍不可用 | 保留结构化 unknown/retry_pending；本地 fixture 只证明代码边界 |

## 14. 交付与完成定义

V3.8 PRD 本身完成的条件：

- 目标、非目标、角色、状态、输入/输出/失败行为和验收示例完整；
- 不再把此前拟议的独立版本作为产品对象；
- 每个功能需求可映射到实现阶段和测试；
- 明确与现有 V3-8 Issue 的命名区别；
- 明确 rev3–rev6 历史保留和 rev6 不重做边界；
- 文档自审通过后已由用户审阅并批准，可进入 Spec/Issue/DAG 和实施计划；后续阶段仍须遵守各自的审计与授权门。

本 PRD 获批不等于授权创建 worker、修改代码、commit、push、Change Request、merge 或 Deploy。
