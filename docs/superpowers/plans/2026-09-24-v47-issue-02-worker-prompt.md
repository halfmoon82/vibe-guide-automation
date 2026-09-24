# V4.7 ISSUE-02 worker prompt v2（writer root 改为仓库内 `.worktrees/v47-issue-02`）

**操作**：在已开好的 ISSUE-02 会话（目录 `/Users/macmini/Desktop/vibeguide`）里，把下面横线以下全文作为新消息发送。不用新开会话。

---

监工更新（v2）：writer root 改为**仓库内** `.worktrees/v47-issue-02`（项目 `.gitignore` 第 10 行的既定约定），你的会话就开在主目录，不需要换目录。之前"cwd 必须是 issue-02 worktree"的第 0 步作废，按下面 v2 执行。

## 0. writer root（先做，任何编辑之前）
运行：`pwd && git -C .worktrees/v47-issue-02 rev-parse --short HEAD && git -C .worktrees/v47-issue-02 branch --show-current && git -C .worktrees/v47-issue-02 status --short`
- 期望：pwd == `/Users/macmini/Desktop/vibeguide`；worktree HEAD == `37fb64f`，detached（branch 为空），clean。
- 若 worktree 不存在或 HEAD≠37fb64f 或不 clean：**停止，不改任何文件**，报告输出。
- 通过后：`git -C .worktrees/v47-issue-02 switch -c fix/v47-issue-02`。

**铁律**：你的一切读写只发生在 `/Users/macmini/Desktop/vibeguide/.worktrees/v47-issue-02/` 之下。Edit/Write 一律用该前缀的绝对路径；所有 `python3 -m unittest`、`git add/commit/push`、`gh pr create` 一律以 `cd /Users/macmini/Desktop/vibeguide/.worktrees/v47-issue-02 && ...` 开头（Bash 每次调用都会重置到主目录）。**主目录的任何文件不得改动**；交付前必须跑 `git -C /Users/macmini/Desktop/vibeguide status --short`，结果只能是 3 个 `docs/superpowers/plans/` 下的未跟踪监工文档，多一行即视为越界。

## 1. 合同来源（只读，在 worktree 内读，都在 main 上）
- Spec：`docs/superpowers/specs/2026-09-23-v47-audit-integrity-hygiene-spec-issues.md` §ISSUE-02
- PRD：`docs/superpowers/specs/2026-09-23-v47-audit-integrity-hygiene-prd.md` AC-02
- worker 协议：`vibe_guide/protocols/visible-sdd-worker.md`（必读并遵守）
- `AGENTS.md` §6/§8/§10
- 参考已合并的 ISSUE-01（PR #69，monitor.py `_refuse_path_ownership_conflicts`）了解本迭代在 monitor.py 里的既有写法。

## 2. 文件白名单（唯一允许修改的文件，均在 worktree 内）
- `vibe_guide/binding_lifecycle.py`
- `vibe_guide/monitor.py`
- `tests/test_visible_sdd_contract.py`
白名单外不得改。若不扩白名单就无法完成（例如某夹具因新增必填字段而变红），**停下向用户报告**唯一解与理由，等监工裁定，不自行扩。

## 3. 合同要点（AC-02）
① `binding_lifecycle.py` 的 `VisibleSddAcceptance`（或等价的 accepted 事件数据结构——先读代码确认真名）新增 `contract_digest` 字段。**digest 定义已由授权卡钉死**：`hashlib.sha256(<节点 contract 内容 utf-8>).hexdigest()[:32]`（SHA-256 前 16 字节的 hex），不得另选长度或算法。写入 accepted 事件时必须携带，缺失 → 写入失败（fail-closed）。
② accepted 事件校验器（监工收口路径，`monitor.py` 中 `_replay_visible_sdd_acceptance`（约 line 2250）及其调用的校验）读取时，从 tasks.json / events.jsonl 里的 contract 内容重算 digest 比对；不一致 → 该 accepted 事件无效，节点保持待审，**不得**错误转 accepted。
③ `tests/test_visible_sdd_contract.py` 增加一条：accepted 事件里的 `protocol_ref` 与 `monitor.py` 的 `VISIBLE_SDD_PROTOCOL_REF` 常量一致性断言。
④ **监工移交项（来自 ISSUE-04 白名单裁定）**：`monitor.py:110` 附近 `_RULING_IN_SESSION_SDD = "in_session_sdd"` 的注释，以及约 line 714 docstring 里 `in_session_sdd` 的措辞——修正为"manifest 能力探针字段名 / DISPATCH_TOPOLOGY_MATRIX 裁定值（适配层）；由监工翻译为 topology 枚举值 visible-sdd（派发层）"。**只改注释，代码逻辑不动。**
- 错误行为：contract 内容不可读（空或 None）→ 拒绝生成 accepted 事件，节点进 `blocked_unknown`；不得产生空 digest 或占位符 digest。
- **字段名从读它的代码里取**：写入端与读取端字段名不一致会被静默拒收且看起来像成功。先找到 reader，再定 writer 的键名。
- **别把为别的文件写的判定借来用**：digest 的输入必须是"当前节点 contract 内容"，先确认 contract 在 tasks.json / events.jsonl 里的落盘形态，按落盘形态算。

## 4. 测试先行
先写失败测试再实现。必须覆盖：
1. digest 正确 → 校验通过、节点转 accepted
2. accepted 事件缺 contract_digest → 写入失败
3. digest 不匹配（contract 被篡改）→ 校验拒绝，节点保持待审
4. contract 为空/None → 拒绝生成 accepted 事件，节点 blocked_unknown
5. protocol_ref ≠ VISIBLE_SDD_PROTOCOL_REF → 测试红
6. 现有 `test_visible_sdd_contract.py` 全部保持绿
**变异验证**（提交后做）：a) 把校验器的 digest 比对改成恒 True → 用例 3 转红；b) 把写入端 digest 改成常量占位符 → 用例 1 或 3 转红；各自恢复、清 `__pycache__`、复跑。结果写进 PR body。

## 5. 会话内独立 review（visible-sdd 协议）——**预置降级裁定**
今日本仓库的 reviewer 子代理多次被 content policy 拦截（0 工具调用即结束）。规则：最多尝试 2 次；第 2 次仍被拦就停止，不做第 3 次，改走降级：会话内 review 标「不可用（content policy 拦截，trace ID 记入 PR body）」，你自己按 reviewer 检查项做自查写进 PR body，非作者复审由监工承担。若 reviewer 成功，它必须交出 P0–P3 意见 + 已核载体清单 + 未核载体清单；P0–P2 先复现再改，同一 reviewer 复审，上限 3 轮。

## 6. 验证与提交（全部 `cd /Users/macmini/Desktop/vibeguide/.worktrees/v47-issue-02 && ...`）
- 定向：`python3 -m unittest tests.test_visible_sdd_contract -v`
- 全量：`python3 -m unittest discover -s tests -v && python3 -m vibe_guide --help && git diff --check`（全绿，报告用例数；main 37fb64f 基线为 994）
- 只 `git add` 白名单三文件；禁止 `git add .`/`-A`。
- commit message 末尾：`Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- `git push -u origin fix/v47-issue-02`
- 开 PR：先把完整 PR body 写到 `/tmp/v47-issue-02-pr-body.md`（改动、用例对应、变异 a/b 结果、全量数字、review 轮次或降级披露、两份载体清单、注释移交项说明、未验证项，末尾 `🤖 Generated with [Claude Code](https://claude.com/claude-code)`），再 `gh pr create --base main --head fix/v47-issue-02 --title "fix(binding): accepted 事件 evidence 绑 contract digest 并校验 protocol_ref（V4.7 ISSUE-02）" --body-file /tmp/v47-issue-02-pr-body.md`。若被权限拦截，只重试 1 次，仍失败就停下，最后一条消息报告"分支已 push，body 在 /tmp/v47-issue-02-pr-body.md，请监工代开 PR"。
- **不要 merge**。

## 7. 禁止
不改主目录任何文件；不改 authorization.py schema；不改 DAG 依赖语义；不改 `in_session_sdd` manifest 字段名；不动 path_ownership.py；不 merge；不 push main；不写凭据。

## 8. 完成报告（会话最后一条消息）
PR URL 或"请监工代开"、分支、HEAD SHA、全量数字、变异 a/b 结论、review 轮次与结论或降级披露、注释移交项完成情况、主目录 `git status --short` 输出（应仅 3 个监工文档）、未验证项、白名单外改动（应为无）。
