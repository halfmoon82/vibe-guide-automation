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
