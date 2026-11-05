# r4 先导 P1/P2 开发交付

- writer：`.worktrees/v528-r4-preconditions`，分支 `fix/v528-r4-preconditions`
- 冻结基线：`885236783e1a680d7165ee2ee2c4e0002430a30a`
- 合同：`docs/superpowers/specs/2026-11-04-v528-r4-preconditions-contract.md`（SHA `31114516…6e34`）
- 批准卡：`docs/superpowers/specs/2026-11-04-v528-r4-preconditions-authorization.json`（SHA `24e60564…8166`）

## 改动范围

### P1 — 同 plan/run 受保护合同修订（`vibe plan-amend`）

- 新增 `vibe_guide/plan_amendment.py`：`propose` 生成不可变候选（`plans/<plan>/amendments/<digest>/`，含 candidate、spec、候选卡、节点、DAG 审计与 diff）；`show` 出具绑定候选摘要的展示回执；`confirm` 校验 `AMEND <digest>` 令牌 + 回执 + 加锁 + 对 live record/card/run 指针做 CAS + 实时执行体核查，先写 transfer marker 再装新工件，crash 前后分别有回滚与幂等完成（`recover`）。传输未决期间所有执行入口经 `require_no_pending_transfer` fail-closed。
- `vibe_guide/monitor.py`：`reauthorize` 新增 `change_reason="plan_amendment"` 与 `amendment_digest`；`start/resume/tick/reauthorize` 挂修订屏障预检。
- `vibe_guide/state.py`：事件数据键登记 `amendment_digest`。
- `vibe_guide/cli.py`：新增 `plan-amend` 命令与 `--amendment/--amend-spec/--amend-reason/--confirm-token`；authorize 两路径加传输屏障。

### P2 — 显式签名的本地 commit 窄许可（`local_commit_allowed`）

- `vibe_guide/authorization.py`：`local_commit_allowed` 进入签名负载（仅 true 参与摘要）、`AuthorizationCard`（拒绝 allow+true 组合与错类型）、`AuthorizationRecord`、`from_dict` 白名单归一化、`build/refresh/authorize` 与一致性校验全链路；新增 `commit_landing_gate` 纯函数与 worker 提示文案三态。
- `vibe_guide/node_spec.py`：产品 spec 声明 `local_commit_allowed?`（`true|false`）。
- `vibe_guide/publish.py`：从 spec 传入该字段。
- `vibe_guide/monitor.py`：`_check_developer_commit_landing` 在交付落账前核对提交落在合同节点分支（file_scope 归既有 landing report 裁定，此处不重复）；deny 存量语义不变。无法读取的 Git 状态走 #336 landing-unreadable 结构化 blocked_unknown 停车（`rejection_code=commit_landing:unreadable`），分支不符/越界走既有交付拒收（`commit_policy:wrong_branch|outside_scope|commits_forbidden`）。
- `vibe_guide/adapters/claude_bg.py`、`vibe_guide/cli.py`：worker 启动/续接提示按记录值三态生成；launcher 工厂读取 `local_commit_permitted`。

### 文档

- `README.md`：deny 语义重写 + 「同 plan/run 合同修订」小节。
- `vibe_guide/protocols/prd-guide.md`：§5.1 字段 + §5.3 修订双问。
- `vibe_guide/protocols/visible-sdd-worker.md`：收口规则。
- `vibe_guide/protocols/__init__.py`：追加两条新协议 digest（用户已批准补充）。

## 验证命令与结果

writer root 内执行（全部通过）：

```bash
python3 -m unittest tests.test_plan_amendment tests.test_plan_amendment_recovery tests.test_local_commit_authorization -v   # 31 例 OK
python3 -m unittest tests.test_helper_ratchet tests.test_heartbeat_native_actions tests.test_recovery_fidelity_256 -v      # OK
python3 -m vibe_guide --help                                                                                             # OK
git diff --check                                                                                                          # OK
```

全量分片回归（254 文件，8 分片）：与冻结基线同法对照。工作树失败集合 = 基线失败集合（环境噪声：真实 `claude`/workbuddy 探测不可用、并行负载下平台 handoff 计时、orphan 派发夹具等）；差异文件 `test_integration_review_closeout_entry`/`test_platform_handoff_hides_stale_creates`/`test_platform_handoff_rebind`/`test_scope_reconcile_entry` 单独串行重跑均 OK，判为负载敏感 flake，非回归。

## 未验证项

- `test_sensitive_identity_durability` 计时 flake：基线 10 次 4 败、工作树 10 次 7 败，均为既有 `contract_error` 重试计时竞态，与本次改动无关（该路径未触及派发重试逻辑）。
- `test_adopt_orphan_dispatch`、`test_claude_bg_*`、`test_platform_handoff_*`、`test_workbuddy_*` 等 20 个文件在基线树同法同败，属环境性遗留。
- 真实桌面 App 原生任务能力、真实 worker 会话端到端未验证（本地夹具 + 临时 Git 仓库覆盖）。

## 未纳入路径

真实 `.vibe` 项目状态、主 plan/run、holds、AGENTS.md 与上层仓库、任务登记（coordinator 持有）。

## Git 状态

全部改动限白名单文件，一次性白名单本地 commit 落在 `fix/v528-r4-preconditions`；未执行 push/PR/MR/merge/deploy。

## 第 2 次交付（复审返工，2026-11-05）

复审第 2 轮 P1×3 / P2×2 全部修复，另采纳 P2-3 建议 4 项。

### P1-1（B08/B10）：`local_commit_allowed` 接入运行时门禁

- `monitor._check_developer_commit_landing` 从 `_snapshot_record` 读 `remote_git_actions` 与 `local_commit_allowed` 传入 `commit_landing_gate`，硬编码 `True` 移除。
- **裁定（None 映射）**：缺失/不显式 false → 本地 commit 允许（旧含义）。依据：① 合同「缺失/false 保持旧含义与旧摘要，不反向重解释历史」；② 旧提示原文 deny 下「只在本地分支提交」；③ `scope_landing` 的 uncommitted 拒收使 deny 节点必须提交，「deny 禁提交」会让全部默认卡交付死锁（已由 `test_landing_required_before_review` 回归证实）；④ 例外仍有意义：签名授予 + 提示词收窄为显式白名单。裁定已写进 monitor 注释。
- 文案修正：`REMOTE_GIT_BAN_TEXT` 不再把无例外 deny 描述成「严禁提交代码」（与落账矛盾），改为只列远端动词禁令——既不承诺也不禁止本地提交，满足 B10；新增 `LOCAL_COMMIT_STRICT_TEXT` 供伪造/存量显式 `false` 的 fail-closed 提示。
- 附带修复（同缺陷链）：`_landing_project_head` 在非 git 项目提前求值导致 base=None 静默放行 → 懒求值；`commit_landing_gate` 在 `base is None` 时禁止 commit 或有 scope 时返回 `unknown`（不可证明零提交≠零提交）。
- 新用例：deny/allow/例外三态运行时门禁、错分支拒收、伪造 false 提示、错类型拒绝。

### P1-2（B06/P1.4）：修订切换窗口内不派发

- `Monitor.reauthorize` 在 `change_reason == "plan_amendment"` 时跳过 `_schedule_ready`；窗口内出现的活跃 executor 不再被 runner 停止/消费，直接 `PermissionError` 中止转移。
- `confirm` 在 marker 写入后、事件提交前二次重验 `_live_executors`，把竞态窗口压进 `.amendment.lock`。
- 新用例：ready 节点 confirm 不产生派发/状态污染；窗口内活跃任务中止转移且不消费其状态。

### P1-3（B03）：候选工件摘要绑定

- `propose` 先在临时目录写齐 5 个工件（spec/card/nodes/dag-audit/diff），逐字节 sha256 记入 `artifact_digests` 参与候选摘要，再原子改名 `amendments/<digest>/`。
- `_load_candidate` 装载前逐一重算并比对 artifact_digests，篡改任一工件（schema 合法篡改）→ `PermissionError`；show/confirm/_write_staging/_commit_transfer_event/recover 全部经过此入口。
- 新用例：篡改 card/nodes/spec 三个工件 confirm/show 必拒。

### P2-1：修订证据如实记录令牌

- `authorize_entry.build_workflow_evidence` 接受 `AMEND <digest>` 令牌形态并如实记录 `token_required`/`verified_fact`；`plan_amendment._finish_projection` 改为传 `confirmation_token(digest)`，不再伪造 AUTHORIZE。

### P2-2：quiescence 纳入 writer 租约

- `_live_executors` 枚举 `.vibe/leases/*.json`：本 run 的 active 租约与不可读/畸形租约均判问题；`_start_run` 测试夹具的 quiesce 同步释放租约以模拟真实交付态。

### P2-3 建议采纳

- `_write_staging` 游离字符串移到 docstring 位；`commit_landing_gate` dirty 判定括号化；`AuthorizationRecord.from_dict` 对非布尔 `local_commit_allowed` 拒绝（原一律归一化 None）；CLI plan-amend 把 `OSError`（含 fsio JsonMissing/Corrupt）映为 `blocked_unknown`，验证类 `ValueError/KeyError/TypeError` 保持 `blocked_design`。

### 附带修复

- `protocols/__init__.py` 追加 `visible-sdd-worker.md`/`prd-guide.md` 的新已发布 digest（编辑后未登记导致 dispatch `contract_error`，`test_prd_guide_publish_path` 全红）。

### 验证（writer root）

```bash
python3 -m unittest tests.test_plan_amendment tests.test_plan_amendment_recovery tests.test_local_commit_authorization -v   # 43 例 OK（新增 9 例）
python3 -m unittest <15 个受影响文件> -v                                                                              # 250 例 OK
python3 -m vibe_guide --help; git diff --check                                                                          # OK
```

`test_prd_guide_publish_path`（digest 未登记回归）修复后全绿；此前披露的基线环境性失败与计时 flake 判断不变。

## 第 3 次交付（第 3 轮复审返工，2026-11-05）

### P1-4（B05/P1.4）：quarantined writer 租约纳入静止证明

- `_live_executors` 对本 run（或无法归属）的租约按状态分类：`active`/`quarantined` 一律判问题（quarantined 在 lease 生命周期里是「终止无法证明、仍需接管」，不是安全静止）；空状态与未知状态也判问题，不可读/畸形保持拒绝。
- 新用例：`test_quarantined_writer_lease_blocks_confirm`；`test_active_writer_lease_blocks_confirm` 与正常 quiesce 夹具（显式 release）保持绿。

### P1-5（B06/P1.4）：transfer marker 只有持锁者能判定

- `recover` 拆为公共入口（先获取 `.amendment.lock`，10s 拿不到 → `PermissionError`，不回滚任何东西）与 `_recover_locked`（锁已持有或无竞争时执行原逻辑）。锁内可见的 marker 只能是崩溃残留——持锁 `confirm` 期间外部入口只会等待或拒绝，不可能删 marker/staging。
- `confirm` 自恢复的已提交转移改走 `_recover_locked`，避免同进程自锁。
- monitor/CLI 入口经 `require_no_pending_transfer` 同构受保护；`_require_amendment_barrier(owner_digest)` 保证 confirm 自身在 reauthorize 内不被误拦。
- 新用例：`test_contended_lock_refuses_recovery_and_keeps_marker`（持锁窗口内 marker/staging 原样保留）、`test_free_lock_recovers_crash_residue_marker`（崩溃残留正常回滚）。

### 验证（writer root）

```bash
python3 -m unittest tests.test_plan_amendment tests.test_plan_amendment_recovery -v   # 25 例 OK（新增 3 例）
python3 -m unittest <11 个受影响文件> -v                                            # 284 例 OK
```

## 第 4 次交付（真实 plan/run propose 阻断修复，2026-11-05）

### B01 阻断：files 修订刷新 worker_profile.allowlist 派生投影

- 根因：`worker_profile.allowlist` 是发布期 `contract.files or ["."]` 的派生投影（`node_spec.complete_node_contracts`）。修订 spec 改 `contract.files` 时旧投影不刷新，`_conflict_write_scope` 把陈旧 allowlist 与新 files 取并集，缩窄/迁移文件范围的修订必撞写范围冲突；且工程字段回验又禁止改 profile，构成死锁。派发路径（`Monitor._prepare_worker_profile` → `WorkerProfile.from_dict`）读的也是这份存储投影，不刷新会按旧范围绑定。
- `_merge_nodes`：files 变化且节点持有 dict 形态 `worker_profile` 时，按同一派生规则（`_derived_allowlist`，镜像 `files or ["."]`）原地刷新 `allowlist`；其余 profile 字段保持位级不变。files 为 truthy 非 list（无合法投影）时不动投影，交给回验 fail closed。
- `_profile_projection_diff`：工程字段回验对 `worker_profile` 的唯一放行差集——`allowlist` 恰等于新 files 的派生投影、其余字段位级不变；投影缺失、不等于投影、其他字段漂移或类型异常分别报 `worker_profile.allowlist`/`worker_profile`/`worker_profile.<key>`。
- 新用例 `FilesScopeProjectionTests`：files 变更刷新投影且 propose 通过并绑定候选卡 file_scope；节点间文件迁移（夹具单业务节点 skip）；篡改非 allowlist 字段端到端拒绝；谓词级单测覆盖合法投影/伪造 allowlist/其他字段漂移/缺键。

### 验证（writer root）

```bash
python3 -m unittest tests.test_plan_amendment tests.test_plan_amendment_recovery tests.test_local_commit_authorization   # 52 例 OK（skip 1：单业务节点夹具）
python3 -m unittest tests.test_dag tests.test_planner tests.test_prd_guide_publish_path tests.test_authorization_card_options tests.test_empty_decisions_authorize tests.test_auto_scope_files tests.test_scope_landing tests.test_node_scoped_blocking   # 235 例 OK
python3 -m unittest tests.test_monitor tests.test_monitor_binding_transaction tests.test_cli_supervisor_commands tests.test_rework_prompt_auto_scope   # 180 例 OK
python3 -m vibe_guide --help; git diff --check   # OK
```

未验证项不变：真实桌面 App、真实目标 plan/run 切换、远端 Git、发布/deploy 均不在先导范围；此前披露的基线环境性失败与计时 flake 判断不变。

## 第 5 次交付（第 5 轮复审返工，2026-11-05）

### P1-6（B04/B12）：IR 节点派生 contract.files 在 files 修订后重派生

- 裁定：采纳 reviewer 建议 (a)——按发布规则对 `integration-review` 节点自动重派生 `contract.files = integration_review_scope(merged_business)`，并把 spec 中对 IR.files 的显式声明判为工程字段拒绝（与发布期行为一致，IR 只聚合不持有范围）。未选 (b)：让产品 spec 抄写派生值只会制造第二处可漂移的口径。
- `_rederive_integration_review` 在业务字段合并后运行：`files` 重写为合并后并集；若 IR 携带陈旧 dict 形态 `worker_profile`（旧派生期之前的 plan），`allowlist` 收窄到并集保持单投影；并集为空时按发布回退补 `files=[]` + 全项目 fallback profile（否则 dispatch 永远无法给 IR 绑定会话）。
- `_validate_amendment_spec`：IR 节点的 `contract.files` 移出可写业务键，显式声明按「由系统自动生成」拒绝，错误文本复用既有工程字段口径。
- `_business_diff` 如实把 IR.contract.files 的变化记入 `contract.files` 项（业务键口径），候选卡 `file_scope` 与节点存储投影自洽。

### P2-4：双业务节点夹具覆盖迁移与 IR 派生

- 新增 `_multi_authorized_probe`：走真实 `vibe plan` 发布双业务节点（`alpha/one.py`、`beta/two.py`）+ 授权 + run 静止，替代会 skip 的单节点夹具。
- `IntegrationReviewScopeTests` 6 例：IR.files 起始并集断言；业务 files 修订后 IR.files=新并集且候选卡 file_scope 含新路径；节点间文件迁移同时刷新两端 allowlist 与 IR 并集；spec 显式 IR.files 拒绝；业务 files 清空收窄 IR 并集（重叠 ["."] 场景先串行化，验证 `dependency_reasons`/depends_on 修订路径）；全空并集时 IR 获发布回退 profile。
- 顺带删除在单节点夹具下恒 skip 的旧迁移用例。

### 验证（writer root）

```bash
python3 -m unittest tests.test_plan_amendment -v                                                                                                   # 29 例 OK（新增 6 例）
python3 -m unittest tests.test_plan_amendment_recovery tests.test_local_commit_authorization tests.test_dag tests.test_planner tests.test_prd_guide_publish_path tests.test_authorization_card_options tests.test_empty_decisions_authorize tests.test_auto_scope_files tests.test_scope_landing tests.test_node_scoped_blocking tests.test_v310_plan_repair   # 265 例 OK
python3 -m unittest tests.test_monitor tests.test_monitor_binding_transaction tests.test_cli_supervisor_commands tests.test_rework_prompt_auto_scope   # 180 例 OK
python3 -m vibe_guide --help; git diff --check                                                                                                       # OK
```

未验证项不变：真实桌面 App、真实目标 plan/run 切换、远端 Git、发布/deploy 不在先导范围；scope_expansions 快照层语义为既有边界（reviewer 已记录非缺陷）。

## 第 6 次交付（第 6 轮复审返工，2026-11-05）

### P1-7：空并集 + dict profile 时 allowlist 归位 ["."]

- 根因：`_rederive_integration_review` 空并集回退只覆盖「IR 无 profile / profile 非 dict」形态；IR 已携收窄后 dict `worker_profile`（上一轮收窄修订留下，或旧 plan 手写形态）而本次业务 files 全空时，`contract.files` 已改写为 `[]` 但旧 `allowlist` 原样保留——该合法修订被拒/或陈旧路径继续绑给 reviewer，与发布期空并集语义（`files=[]` + `allowlist ["."]`）不一致。
- 修复：空并集且 profile 为 dict 时 `dict(profile, allowlist=["."])`，保留 worker/writer/worktree/branch 等身份字段，与发布 fallback 等价（单行语义改动）。
- 采纳 reviewer 建议：IR `worker_profile` 非 dict 形态在 `_rederive_integration_review` 显式 `ValueError` fail-closed——此前该损坏值会原样通过 re-verify（身份不变跳过差集校验）进入候选卡与派发，属可证伪的放行缺口；拒绝点与「工程字段不可改」共用 ValueError 语义但措辞区分 malformed。
- 新用例 2 例：`test_ir_narrowed_dict_profile_returns_to_project_wide_on_empty_union`（收窄 profile + 全空并集 → allowlist 归位、其余字段位级不变、propose 通过）；`test_ir_malformed_profile_rejected`（非 dict profile 拒绝）。

### 验证（writer root）

```bash
python3 -m unittest tests.test_plan_amendment tests.test_plan_amendment_recovery tests.test_local_commit_authorization tests.test_dag tests.test_prd_guide_publish_path tests.test_authorization_card_options tests.test_node_spec_contract tests.test_empty_decisions_authorize   # 211 例 OK
python3 -m unittest tests.test_monitor tests.test_monitor_binding_transaction tests.test_planner tests.test_v310_plan_repair tests.test_scope_landing tests.test_auto_scope_files tests.test_node_scoped_blocking   # 265 例 OK
python3 -m vibe_guide --help; git diff --check   # OK
```

未验证项不变：真实桌面 App、真实目标 plan/run 切换、远端 Git、发布/deploy 不在先导范围。

## 第 7 次交付（retire_uninvoked_legacy 工程恢复路径，2026-11-05）

新增公开恢复路径：证明从未原生调用的遗留 pending create 可被受保护退役，使同 run 的已批准修订 confirm 不再被 `_live_executors` 静止检查挡住。

### 实现（白名单内）

- `vibe_guide/plan_amendment.py`
  - `_rollout_native_calls`：解析 rollout JSONL 的 function_call/custom_tool_call/local_shell_call 及其 *_output；不可归类 *call* 类型、无 id 调用、重复 call_id 一律 fail closed。无 call_id 的 host 注入记录（delegation/fco_ 形态）计 skipped 不作返回证据；返回 payload 保留供纠偏与配对。
  - `_embedded_create_invocations`：对 exec 内嵌 JS 做字符串/注释感知扫描，区分 tools.mcp__codex_app__create_thread( 真实调用与 cmd 字符串/提示词/审计脚本中的文本引用；模板字符串内容按字符串处理（保守记录）。
  - `_retire_consumer_proof`：终端回执 + 日志 SHA + 全量 native call 声明核对；未返回调用必须 `inherited_unreturned` 声明，并在其他已退役消费者日志中以同 call_id 配对（fork 继承形状）。
  - `_verify_declared_creates`：不要求消费会话零 create；每个 flagged create（原生或内嵌）必须 declared_creates 声明 purpose + returned_thread_ids，且 audited return 实际含该 threadId；payload 含退役 action_id/request_digest/worker prompt 逐字时拒绝（绑定退役请求）；幽灵声明拒绝。
  - `_verify_base_correction`/`_verify_correction_chain`：分支 reflog 必须为唯一 `branch: Created from <full sha>` 条目且 digest 绑定；creation call 必须在已审计 rollout 内，payload digest 绑定且含精确 `git worktree add <wt> -b <branch> <sha>`；`actual_output` 与 audited call return 的实际文本比对（不再信自报），须含 `Preparing worktree (new branch '<branch>')` 且 `HEAD is now at <短SHA>` 短 SHA 为 corrected_base 前缀。
  - `retire_uninvoked_legacy`：.amendment.lock → 全证据锁内重验 → 两阶段 intent 记录（intent→locked，含每个 action 锁文件 SHA）→ 持有全部 action .lock 跨 stale marker、事件、快照投影、租约释放完整边界 → 事件落账后删除 intent 与自建锁残留。complete() 同锁互斥；锁内二次 result 检查；幂等重跑收敛无重复事件（state.py 已登记 coalesced latest）。
  - `_retire_intent/_retire_own_locks/_retire_claim_evidence`：区分本路径崩溃残留的自有锁（按 intent+SHA）与外来/历史锁（fail closed）。
- `vibe_guide/state.py`：`retire_uninvoked_legacy` 入 _COALESCED_LATEST_EVENTS；`base_corrections` 入 _EVENT_DATA_KEYS（位于 evidence 之外，避开 provider-text 脱敏对 SHA 的遮罩）。
- `vibe_guide/cli.py`：`plan-amend retire-uninvoked --evidence <file>` 阶段接线。

### 测试（tests/test_plan_amendment.py，+18 例，合计 80 例）

- 崩溃收敛：before_event 留 intent+locks+stale 无事件→重跑收敛单事件+清理残留+confirm 通过；after_event 事件已写→重跑收敛不双写。
- 外源证据：无 intent 的锁文件、他 amendment 的 intent 记录一律拒绝。
- declared-create：已声明 bootstrap create 通过；未声明、绑定 action_id/request_digest、错 threadId、幽灵声明拒绝；无 call_id 的 host 注入输出仅计 skipped。
- base_correction：真实 git worktree（Created from <sha> 单条目 reflog）正向纠偏并更新 worktree_base + 顶层 base_corrections 事件字段；伪造 actual_output、digest 篡改、未审计 call、reflog 漂移、分支已移动、stored_base 前提不符拒绝。

### 验证

- `python3 -m unittest discover -s tests -p 'test_plan_amendment*.py' -v`：80 例 OK
- `python3 -m unittest discover -s tests`：Ran 4164 tests，FAILED (failures=51, errors=20, skipped=3, expected failures=4)（并行 discover 全量，日志 /tmp/r4_full_v2.log）。
  - 与冻结基线 `c745254` 同法串行对照：本树失败集 = 基线失败集（self_report_recovery/hybrid_state_recovery/legacy_run_guard/worker_questions/stale_session_276/release_5_2_4_review_fixes/sensitive_identity_durability/worktree_preflight_235/supervisor_* 及 adopt_orphan_dispatch/claude_bg_*/workbuddy/handoff_* 等环境性失败，基线 8852367/c745254 两树逐字一致）。
  - 全量并行中额外出现的差异项（platform_handoff_contract_refresh、scope_reconcile_entry、supervisor_rotation_cost 1 例）经单文件串行重跑均 OK，判为负载敏感 flake，非回归；本树失败数不多于基线。
- `python3 -m vibe_guide --help`：exit 0；`git diff --check`：干净。
- 真实只读 proof（不写 run）：动态取 supervisor-registry/lease（current epoch3=01a1aa87，history epoch1 01a1a449+epoch2 01a1aa72），6 份归档 rollout 解析 550 calls 全配对（fork1/fork2 各 1 条继承 start_helper 经 owner 同 call_id 配对；epoch2 12 条无 call_id host 注入计 skipped；epoch2 唯一内嵌 create 声明绑定返回 threadId=01a1aa87），3 pending requests 摘要绑定，3 worktree 精确 add/HEAD/reflog Created/干净 + snapshot 误投影纠偏，_verify_retire_evidence 全链 PASS。脚本 /tmp/r4_proof.py，证据 JSON /tmp/r4_evidence_dryrun.json（sha 37f4c812…b6bd28，不含真实 run 写入）。

### 未验证/缺口

- 真实 retire/confirm 未执行（须当前 owner 在冻结交付+独立复审后按公开路径执行）。
- epoch2 原生终止回执为合成 shape（archive_status/read_thread_status 已由 owner 原生核验，但未有独立 receipt 文件落盘）；其余 5 消费者可用 native-consumer-termination.json 作为 receipt 源。
- 协调者活动日志仅绑定到 prefix cut-off（audit JSON cc477f7b…f8f60b5eda96c25f55dfc2），其后的 create 仍按 unknown 处理。
- 模板字符串内嵌入调用的 lexing 为保守边界；真实日志未出现该形态。

## 第 8 次交付（R8-1～6 返工，待独立复审）

本节取代第 7 次交付中的锁文件删除、宽松 create 判定和 proof PASS 叙述；历史记录仅作当时自报，不作为当前验证结论。第 7 次声称全量基线等价和 flake 的归因本轮未独立证实，保持 **UNKNOWN**，不据此宣称无回归。临时脚本合成回执不构成原生终止证据。

接班基线：branch fix/v528-r4-preconditions、HEAD 7f0376fb9bc7ca3876e259692714e972731127a4；本次 successor 接手前以 /tmp/r4-epoch4-terra2-freeze.json 校验四个未提交文件 SHA、HEAD、branch、status 均一致。本节是开发验证，不代表独立 reviewer 清零或真实 run 可执行。

### 六项实现及验证

1. **R8-1 回执**：匹配 plan/run、consumer/session、host、状态和终止 turn；调用引用须匹配源记录中的原生参数及结构化返回，rollout session_meta 也须对应。任意文件 SHA、篡改状态/来源及其他 session 拒绝。fixture：R8ReceiptTests。
2. **R8-2 create**：静态解析参数并拒绝未知间接/动态形式；允许排除的 create 须绑定 target、结构化返回、已登记 supervisor successor 的 session record 及该会话首条 prompt；调用及返回摘要入事件。R8CreateTests 检查改写 worker prompt、混合返回、未知 JS；RetireDeclaredCreatesTests 检查合法继任及假声明。多次 create 同一调用返回暂按未知拒绝，不广泛放行 bootstrap。
3. **R8-3 锁**：保留共享 action lock inode 和 complete intent，不在持锁时 unlink；test_queued_complete_and_late_complete_share_stable_inode 用独立子进程测试排队 complete、后来调用和迟到结果阻断。
4. **R8-4 崩溃恢复**：同证据摘要和 action 集合绑定唯一已提交事件，纠偏投影事件后可恢复；单 action 的 stale/event/snapshot/lease/cleanup 各中断点重复执行只产生一个事件。新增双节点、双 action 夹具，逐个 stale、event 前后、snapshot、逐 lease、cleanup 共八个中断点重放两次，核对唯一事件、两个状态、纠偏、租约与 hold 原字节；禁用事件恢复的 mutation 使测试失败。该测试仍须独立审查。
5. **R8-5 租约**：同一 lease 锁内按 run/node/root/status/role/generation/intent 验证，完整记录 CAS 释放，失败抛错；跨 run 同 root 或节点冲突阻断；未绑定 action 的旧租约拒绝。见 R8LockLeaseTests。
6. **R8-6 事件**：真实 append/readback 保留结构化 plan/run/授权/候选、request/consumer/lease/create 摘要、epoch 与纠偏摘要；提供敏感文本负例和同一证据崩溃重放。

### RED → GREEN 与验证

- 前任 RED：/tmp/r4-r8-red.log，11 个新测试产生 16 failures + 1 error（含子例）。接班后起点 25 项中 2 失败；额外发现未绑定旧 lease、错误 session rollout、动态属性绕过及事件缺 create 摘要，均先复现失败后修复。
- PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_plan_amendment*.py'：111 项 OK；test_local_commit_authorization.py：23 项 OK。来源为指定 writer root，版本 5.2.7，加载时 HEAD 如上。
- 冻结源码全量：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s tests -v`，4195 项、50 failures、21 errors、3 skipped、4 expected failures，exit 1；日志 `/tmp/r4-r8-full-epoch4.log`。**全量失败，非 PASS。**
- 全量发现 helper ratchet 本次回归，原因是新增私有摘要 helper 与共享摘要约束冲突；修复复用 `sha256_bytes` 并保留原排序 JSON 的字节编码。修复后 `tests.test_helper_ratchet`、双 action 恢复、真实事件读回共 17 项 OK，111 项定向重新通过。全量是在此最小修复前的冻结源码执行；修复后未重复整套，不能声称最终源码全量通过。
- 失败归因：与前一完整失败日志 `/tmp/r4_full_v2.log` 按测试名比较，71 条中 63 条相同；这不等于基线证明。29 条接管失败中 29 条与既有 8852367 基线日志 `/tmp/r4-t1-base.log` 同名；敏感身份一项亦有基线失败。差异中的两个安装测试在已有干净基线 8852367 与当前树复现相同缺包/缺入口；另外四个差异项单独重跑当前树通过，其中整合审查一项在组合重跑仍失败、单独通过。其余同名与差异项的根因/回归关系保持 UNKNOWN；测试报告 `/tmp/r4-new-fail-current.log`、`/tmp/r4-new-fail-baseline.log`、`/tmp/r4-closeout-isolated.log`。未新建基线 worktree、未修改基线源码。
- python3 -m vibe_guide --help 与 git diff --check 通过。首次本轮运行因源码变化中断（退出 130），不计为完整结果。

### 真实回执及未验证边界

对 epoch2 01a1aa72-4b33-70c1-9138-a6776b3fc293，以 epoch2-native-terminal-receipt.json 与真实 observer session 源的 read/archive 调用和返回只读核对通过。owner 随后提供 epoch4-native-terminal-receipt.json（SHA-256 163620b58ede1c6cd3ca7fecf99e39cd3df3bc84557d7e0b441b250386e26415），包含以下五个 consumer 的调用引用；只读验证 observer 快照长度、SHA 与原日志前缀逐字节一致，并用 `_verify_terminal_receipt` 对五行逐项通过。此前 native-consumer-termination.json 本身仍缺调用引用。这只证明上述终止调用及绑定，**完整消费日志、请求排除、唯一 writer 与真实 retire 全链仍 UNKNOWN**：

- 01a1a449-fdcf-76f2-86ec-cf3140bdd562
- 01a1a596-77a2-7a23-8db7-70b4d0c5a2db
- 01a1a5d5-8ef4-7701-b0f5-9dbb5c1969a2
- 01a1a593-4497-7e82-afc7-52ac64e3128e
- 01a1a593-6bc2-7232-b9b4-ea1c6e60a568

完整证据仍须绑定 consumer 的 rollout SHA、session_meta、全部调用与请求、lease，并由独立 reviewer 核验；活动源日志持续追加时全文件 SHA 会变化，不能视为稳定。禁止合成补证。

真实目标 run 的 retire、confirm、hold release、resume、daemon、mailbox 均未执行；真实跨 action 全链证据、桌面能力、远端 Git、发布与 deploy 均未验证或未授权。未创建新 worktree、任务或依赖；未 push、PR、merge。

## 第 9 次交付（review9 五项返工，待独立复审）

- R9-1：consumer 文件必须与当前宿主唯一原生归档逐字节一致，记录序号连续且尾部终态事件与原生回执 turn 绑定；截断、删中间记录、终态不符和重复归档均拒绝。此检查依赖本机 Codex 原生归档及其目录完整可信，来源缺失或不一致为 UNKNOWN。
- R9-2：observer 的 session/epoch/host 与 registry 绑定，路径摘要仅用于身份关联；快照须与本机原生记录完整字节前缀一致、序号连续，来源/回执的调用与返回逐一核对。源会话被归档后支持以同名原生归档核验，缺失或漂移拒绝；伪造来源和前缀漂移回归已覆盖。
- R9-3：经用户明确批准 `vibe_guide/adapters/task_provider.py` 单文件最小扩围，unresolved/fork 观测与 result 共用稳定 action 锁；retire 持全部 action 锁后在写 stale/event 前重查 claim。独立进程覆盖两类回调先到时阻断并保留租约、退役先持锁时回调等待、迟到观测保留并阻断 confirm。此项不覆盖未经登记的其他外部写入。
- R9-4：CLI 只对 `amendment_revision` 验证正整数并从 Plan 加载副本移除，磁盘版本不变，未知字段仍拒绝；隔离真实 CLI confirm 后 status 同 run 可读，resume 经同一授权进入调度后因 FakeRunner 派发门返回 `blocked_unknown`，没有把它计为业务成功。旧 schema 保持原路径。
- R9-5：支持原生日志中静态 JavaScript 合法转义与正则，动态/无法证明的调用仍拒绝；对真实 `exec` 返回的精确编译 SyntaxError，还要求调用本机现有 Node `--check` 不执行代码且错误内容匹配，无法检查即拒绝。未安装 Node 或新增依赖；此路径对 Node 可用性有运行时依赖，需独立审查。

验证：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest tests.test_plan_amendment tests.test_plan_amendment_recovery tests.test_local_commit_authorization tests.test_forked_worker_rejected tests.test_helper_ratchet`：166 项 OK；原始 RED `/tmp/r4-review9-red.log`；`python3 -m vibe_guide --help`、`git diff --check` 通过。固定源码完整 `python3 -m unittest discover -s tests -v`：4206 项、51 failures、20 errors、3 skipped、4 expected failures，exit 1，日志 `/tmp/r4-r9-full-final.log`，**完整测试失败**。源码四项 SHA 在全量前后不变：test `e473a8c480fb1382934d4b6639c9aa4e020296f15bfc2f568c9b595dc680b50b`、adapter `24e0ac3576ed3ac2e70bbb4c04f09b615f34cac8549cd57c947e6aeb0de1f6a7`、CLI `4a90f8a8f4fc41e2b382c49dcb3c96199cb0472b7cbaefdbd2ee57c139bc9c56`、amendment `8b5542baf55a3c458e41bc79fd74c66918e7a8e580b38ab6e0ca4df02bbad78c`。

全量失败身份与 review9 `/tmp/r4-review9-full.log` 比较：71 个不同身份中 64 个相同、7 个新增、5 个不再出现。7 个新增项复跑 `/tmp/r4-r9-new7-recheck.log` 4 通过、3 失败；3 项再跑 `/tmp/r4-r9-new3-isolated.log` 2 通过、1 仍失败，该 3 项在已有干净 8852367 基线 `/tmp/r4-r9-new3-baseline.log` 通过。该差异尚未确认根因或是否回归，**UNKNOWN，须 reviewer 独立判断**；不能以间歇通过认定无问题。

真实证据：七份完整原生归档逐一解析，calls 分别 352/38/90/35/35/102/302；两条继承未配对调用均在其他原生归档找到对应返回。epoch4 五份回执、epoch4 历史回执及 epoch2 回执的 observer 来源与 native read/archive 调用只读核对通过；这不代表全部 create 已排除。尤其 epoch3 `call_rUHb9rlGBpe5Yo8E6e1ybx1b` 真实创建先导 developer `01a1aae8-2756-72e0-983c-abd8847e3245`，不是登记的 supervisor successor；当前排除规则只接受登记监工继任，故真实完整 retire proof **UNKNOWN/尚不成立**。下一轮须由 owner 以先导合同、原生归档和逐 action 不重叠证据判断该调用，不得将其伪称监工继任。

未执行真实 retire、confirm、hold release、resume、daemon、mailbox；未 push/PR/merge、发布或 deploy。以上为开发证据，review9 P1 是否关闭由原 reviewer 独立复审决定，工程恢复不等业务验收。
