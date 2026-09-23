# V4.6 可见并行派发 run log（run-v46-20260923）

授权卡：v46-visible-parallel-dispatch-auth-1（2026-09-23 用户授权）
拓扑：visible-sdd 首次实战（每节点一个 create_thread 可见会话；监工=主会话，不写节点代码）

## 任务登记（tasks）

| Issue | provider | mode | threadId（hostId=local） | worktree | branch | 状态 |
|---|---|---|---|---|---|---|
| ISSUE-01 | codex-app | visible | 01a0cd0d-d5a0-79a1-9441-62790c66a580 | ~/.codex/worktrees/a8bd/vibeguide | codex/v46-issue-01 | accepted+archived |
| ISSUE-03 | codex-app | visible | 01a0cd0d-d7ac-77d2-b11c-a9ce23ed924c | ~/.codex/worktrees/f71c/vibeguide | codex/v46-issue-03 | accepted+archived |
| ISSUE-06 | codex-app | visible | 01a0cd0d-da50-79f3-83c9-1107524eafdf | ~/.codex/worktrees/8235/vibeguide | codex/v46-issue-06 | accepted+archived |
| ISSUE-07 | codex-app | visible | 01a0cd0d-ddcf-7a30-9d6a-54e111f6383a | ~/.codex/worktrees/1526/vibeguide | codex/v46-issue-07 | accepted+archived |

## 事件

- 2026-09-23 14:5x 授权卡落盘并置 authorized（authorized_at=2026-09-23）
- 2026-09-23 14:5x 4 × create_thread 同轮派发（时间窗重叠，满足 AC-01 同轮 ≥2 可见会话）
- 2026-09-23 14:57 worktree 实体确认：a8bd/f71c/1526/8235 全部从 main 6fdd1dd 切出；issue-01/03/07 分支已建并有未提交改动；8235（ISSUE-06）尚在启动

## 待收口证据

- [ ] 每节点 PR URL 与 review 子代理结论
- [ ] 监工侧独立 PR reviewer 复审（standing rule 2026-09-15）
- [ ] P0–P2 清零后 squash merge；merge_evidence 回填授权卡
- [ ] 名额释放后派发第二波（I02←I01；I05←I01；I04←I01+03+07）
- [x] threadId 已解出回填（2026-09-23，经 .codex-global-state.json clientId 映射）

- 2026-09-23 15:1x ISSUE-03 率先开 PR #58；监工派独立 PR reviewer 子代理 Herschel 审阅（standing rule 2026-09-15）
- 2026-09-23 15:18 ISSUE-03 PR #58 squash 合并 c97d75e；worker 会话归档，名额释放（活跃 3/5）
- 2026-09-23 15:2x ISSUE-01 PR #59 squash 合并 e1c799c；worker 会话归档，名额释放（活跃 2/5：I06、I07）→ 第二波放行 I02、I05（依赖 I01 已闭合）
- 2026-09-23 15:3x ISSUE-07 worker 遇越界阻塞（4 个白名单外测试夹具缺新探针陈述，全量 5F+10E）；监工按 §6 纠偏条款裁定：唯一解=夹具补陈述，扩白名单授权并记录 deviations；P2-1 设计说明闭合
- 2026-09-23 15:2x 第二波派发：I02（client 8db1a90f）、I05（client e69f61f2），活跃 4/5
- 2026-09-23 15:4x ISSUE-07 PR #60 squash 合并 9657cca；worker 会话归档。I04 依赖（I01+I03+I07）全部闭合 → 第三波派发 I04（client 待回填），活跃 4/5（I06、I02、I05、I04）

## 第二/三波任务登记

| Issue | threadId（hostId=local） | branch | 状态 |
|---|---|---|---|
| ISSUE-02 | 01a0cd2a-7a7c-75f1-abe8-0cd1d6ee79b1 | codex/v46-issue-02 | accepted+archived |
| ISSUE-05 | 01a0cd2a-7a7c-75f1-abe8-0cb16b91f6ac | codex/v46-issue-05 | accepted+archived |
| ISSUE-04 | 01a0cd3e-ec44-71e1-b720-a25affc027a1 | codex/v46-issue-04 | accepted+archived |
- 2026-09-23 15:5x ISSUE-06 PR #62 独立 reviewer Feynman 发现 P2-1：parallel_group 审计仅 advisory、运行期投影不消费 → 退回原 worker 返工（接入 dag.py 侧派发资格硬门禁）；P3 三条登记后续：①跨组/无组写范围冲突无审查（path_ownership.validate_path_ownership 存在但未接线）②引用匹配边缘漏判（连写前缀、目录引用）③空 allowlist 与缺失不可区分的 UX 后果
- 2026-09-23 15:5x ISSUE-07 交付时留下知识库沉淀候选：「新增 manifest 探针 = attest 写路径新增必填项，须同步全部 capabilities fixture」——待用户批准入库
- 2026-09-23 16:0x ISSUE-05 PR #61 独立 reviewer Beauvoir 发现 P2（契约测试未钉「返工回 dev 子代理」，变异体不红）→ 退回原 worker 返工；P3×3 随返工评估
- 2026-09-23 16:2x ISSUE-02 PR #63 squash 合并 83c191b；reviewer 子代理配额 403 → 监工亲自非作者复审（降级已披露）；worker 会话归档（活跃 3/5：I04、I05 返工、I06 返工）
- 2026-09-23 16:3x Kimi 配额 403 中断 I04/I05 两轮（I06 幸免）；配额恢复后监工发续跑指令，均从断点继续，工作树状态由 worker 自行核对
- 2026-09-23 19:2x ISSUE-05 PR #61 squash 合并 9ac7dfa（返工一轮闭环：P2 变异体转红经监工复核）；worker 会话归档（活跃 2/5：I04、I06 复审中）
- 2026-09-23 19:3x ISSUE-06 Erdos R4 发现 P1-1（真实派发点 _schedule_ready 绕过投影层门禁，动态探针实证两冲突节点仍被并行派发）→ 监工裁定方案①：monitor.py 接入点并入 I04 合同增补（白名单内），I06 保留 dag.py 侧门禁并 push 收口；此事件本身是 V4.6 唯一 writer 纪律的正面实证（I06 拒碰 I04 writer 范围）
- 2026-09-23 19:4x ISSUE-06 PR #62 squash 合并 cadd480（Russell 终审 APPROVE，窗口期披露实证一致）；worker 会话归档（活跃 1/5：I04）。P3 追加登记：dag.py 根`.`相交专属单测、CJK 粘连散文产物引用漏检（记 I04 或后续）
- 2026-09-23 20:2x ISSUE-04 交付 PR #64（draft）：核心链路改造完成（拓扑派发/上限=min(卡,配置)/监工writer硬拒/successor适配/组审计消费），会话内 review 2 轮无 P0–P2；两处越界阻塞经监工裁定扩白名单（closeout e2e 夹具适配 + provider_action.py 真实桥 topology 传播），worker 继续
- 2026-09-23 21:0x ISSUE-04 PR #64 squash 合并 f088206；worker 会话归档（活跃 0/5）→ 放行 I08 文档收口（含 PRD/Spec/授权卡/run-log 四文档随 PR 提交）
- 2026-09-23 21:0x ISSUE-08 派发：threadId 01a0ce5e-8785-7af1-beb8-0792a818d0e3（client c2e9ebca），活跃 1/5
- 2026-09-23 21:3x ISSUE-08 PR #65 squash 合并 7d845b7（Dirac 终审无 P0–P2）；worker 会话归档，活跃 0/5
- 2026-09-23 21:3x **run 完成**：8/8 Issue 全部 accepted（PR #58-#65 除编号顺序外全部 squash 进 main 6fdd1dd→7d845b7）；授权卡 status=complete

## 最终验收对照（PRD AC-01～08）

| AC | 结论 | 证据 |
|---|---|---|
| AC-01 同轮 ≥2 可见会话 | ✅ 机制交付+本次实战 | PR #64 fixture 测试；本 run 首波 4 会话同轮 create_thread（本文件登记） |
| AC-02 上限与名额释放 | ✅ | PR #58（配置）+ PR #64（min(卡,配置)、accepted+归档同 tick 释放、未知占名额） |
| AC-03 会话内 SDD 闭环 | ✅ | PR #61 协议 + 本 run 每 worker 实测 2-5 轮会话内 review |
| AC-04 review 只读/独立 | ✅ | PR #61 契约测试（含变异体）+ 各 worker 会话实证 |
| AC-05 监工不作 writer | ✅ | PR #63（卡片拒签）+ PR #64（结构性硬拒 choke point） |
| AC-06 parallel_group 依赖审查 | ✅ | PR #62（dag.py 门禁+投影）+ PR #64（_schedule_ready 消费，monitor 级回归） |
| AC-07 降级披露机器校验 | ✅ | PR #63（缺披露 ValueError） |
| AC-08 visible successor 适配 | ✅ | PR #64（_replay_visible_sdd_acceptance 全链校验、fail-closed） |

## 登记后续（不阻塞，供下一版规划）

1. 跨组/无组写范围冲突无审查；path_ownership.validate_path_ownership 存在但未接线（Feynman P3-1）
2. in_session_review evidence_ref 未绑 contract digest、accepted 事件 protocol 未比对常量（I04 R2/R4 P3）
3. dag.py 根 `.` 相交专属单测；CJK 粘连散文产物引用漏检（Russell P3）
4. 设计基线 §7.3 残留「任务对」旧口径；in_session_sdd 与 visible-sdd 命名双轨（Dirac P3）
5. lifecycle.py _CANONICAL_FIELDS 未含 topology（I01 交付时登记的证据噪声项）
6. AGENTS.md §6 补丁建议在 PR #65 body，等用户确认后落地
7. 知识库沉淀候选：「新增 manifest 探针 = attest 写路径新增必填项，须同步全部 capabilities fixture」（I07 worker 提出，待用户批准入库）
