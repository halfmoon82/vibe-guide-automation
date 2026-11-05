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
