# 远端 open issue 分诊（2026-10-16）

> **历史文档（存档于 2026-10-16）**：下文「当前 open = 17」已过时（2026-10-21 为 11）；#128 已于 10-21 按「放弃本地预判」关闭；文件与行号坐标以 10-16 的 main（`0ec215e`）为准，**代码结论（如某函数是否仍零调用）同以当时为准**，不作为现行坐标。

扫描时间：2026-10-16 10:39–10:55（+08）
基线：`origin/main` = `0ec215e`（v5.0.14）
扫描范围：远端 open issue 22 个（#126–#220）、open PR 10 个
证据方法：`gh issue list` / `gh pr view` + 在 main 树上逐条 grep 代码与协议原文（下文给出 file:line）

---

## 一、有人管（活跃 run 内 / 在飞 PR 有明确 owner）

在跑的正式 run：`run-9edb07bb17df44f98e8ef6dbb341d076`，计划 `session-496b0af6bf727889`，PRD「监工整合 PRD 落地补齐」，7 节点，5 已验收。
第九班监工会话（`ab8da63a`）10:37 仍在写，monitor-routing worker 10:39 仍在写 → **run 正在推进中，不要插手**。

| Issue | 载体 | 状态 |
|---|---|---|
| #151 挂起须声明边界类别 | 节点 decision-boundary / PR #184 | 节点已验收，等整合审查后合并 |
| #152 乔哈里门在监工会话误判 | 节点 host-rules-exception / PR #206 | 节点已验收（g2） |
| #148 权限字眼误判 | 节点 monitor-routing / PR #188 | **返工中**；PR 与 main 冲突（CONFLICTING），合并前需解 |
| #150 严重度缺失默认要人决定 | 同上 PR #188 | 同上 |
| #154 心跳 10→30 分钟 | 节点 heartbeat-contract / PR #201 | 节点已验收；#201 已把间隔做成 10/30/60 单一来源 |
| #208 派发缺唤醒通道与信号格式 | PR #210 | MERGEABLE，未合并 |
| #209 授权卡加心跳/换班选项 | 等 #201、#207 合并 | PRD 补丁 PR #211 已合并；实现等前置 |
| #157 总表（追踪） | — | 子项多数已实现或在飞，建议收窄为剩余项 |

---

## 二、做到一半没人管了 — 2026-10-16 11:5x 分析后处置

| Issue | 做到哪 | 剩余量（实测核定） | 处置 |
|---|---|---|---|
| #145 产品范围 vs 文件清单 | 修复写完（PR #164，draft）；prd-guide §4/§6.2/§7 + digest + 4 个锚定测试 | **只差 rebase + 审查 + 合并**；PR 现与 main 冲突（3 个文件）。另 AGENTS.md §6 补丁待产品经理批准 | 保留 open，已登记**收尾批次**与合并顺序（见 issue 评论） |
| #146 自动扩范围入口不可达 | **核心已解决**：三条路径 + `vibe scope-expand` CLI + `scope_auto_expanded` 事件都在 main（`monitor.py:5373/5308/2557`、`cli.py:1911`） | 残余两项：① payload 形状未写进协议（`planner.py:295` 键集合 vs 协议零命中）；② `acceptance_scope_rule_approved` 未启用 | ✅ **已关闭**（core resolved，残余转 #145） |
| #153 协议措辞与恢复步骤 | 协议侧已落地（`prd-guide.md:475/477-485`） | **5 个验收用例失败**（main 实跑 12 failed/27 passed）；其中 1 个归 #188，本条名下 **4 个**：暂停不写 recovery_action、三轮写 `repair_node`、`_finding_changes_prd_acceptance`（`monitor.py:6233`）零调用、三轮后不派新 reviewer | 保留 open，已登记剩余范围 + 源码坐标 + 归入收尾批次 |
| #155 监工轻装加载 | 实测为负结果（PR #165） | — | ✅ 已关闭（not planned） |

**共同阻塞（关键结论）**：#145 / #146 / #153 的残余**全部落在同一批文件**——`prd-guide.md`、`protocols/__init__.py`、`monitor.py`——而这三处正是活跃 run `run-9edb07bb…` 正在改的（F1 协议摘要登记 + 节点 monitor-routing 的 PR #188）。硬做的代价是给 run 制造冲突、并让它的 F1 修复失效（最终 prd-guide 内容与已登记 digest 不符 → `is_shipped_protocol()` 失效 → 消费项目物化副本静默不再自动刷新）。

→ 因此合并成一个 **run 收口后的收尾批次**，一次改完一次合并。批次内容与执行步骤已登记在 [#145 评论](https://github.com/halfmoon82/vibe-guide-automation/issues/145#issuecomment-6207588795)。

**顺序结论**：run 的 5 个 PR（#184 / #188 / #201 / #206 / #207）先合 → PR #164 后合（由 #164 的 rebase 重算并登记新 digest，两侧都成立）。

另：PR #167（#154 的唤醒实测证据）也是 draft 停摆，但 #154 主体已被 #201 覆盖，不需要它单独推进。

---

## 三、完全没开始

### 3a 需要单独做（问题明确、无 owner、可独立开工）

| Issue | 类型 | 为什么值得单独做 |
|---|---|---|
| **#219** | bug | 派发未保证 worker 会话挂载 project，projectless 沙箱不覆盖合同 worktree → 单节点正常交付产生十几次人工授权打断。**直接影响可用性，建议最高优先**。10-16 新增 |
| **#220** | enhancement | 活跃 run 期间能力合同被外部改写无事前检测，digest 漂移攒到 resume 才 fail-closed。10-16 新增，有实测证据链 |
| **#216** | enhancement | 监工链路上下文成本：`plan` 未进输出瘦身白名单、派发协议全文三次计费、信箱无摘要视图。10-15 新增，源码坐标已核实。⚠️ **2026-10-16 12:1x 修正**：它要改 `protocols/__init__.py` + `prd-guide.md`（**协议摘要登记对象**），撞 run 的 F1 修复 → **改归收尾批次**（见 §5.3） |
| **#147** | 验证缺口 | 功能**已实现**（`monitor.py` 的 `invalidated_acceptances`，1721/1824/2728），AC-B5 用例 **3/3 通过**——但用例在未合并的 PR #212 里。要单独做的是「把验收用例合进来」，不是写功能 |
| **#126** | 前提待验证 | 项目规则只写 AGENTS.md、Claude Code 只读 CLAUDE.md。main 里 `vibe_guide/` 无任何 CLAUDE.md 规则写入（仅 `paths.py:54` 的 marker 列表）。⚠️ **但 PR #206 已在建「写 `~/.claude/CLAUDE.md` 标记块」的通道**，且 CC 是否已原生支持 AGENTS.md 未核实 → **先花 10 分钟验证前提，再决定做不做** |

### 3b 已关闭（已被实现）— 2026-10-16 11:11 执行完毕

| Issue | 关闭依据 | 结果 |
|---|---|---|
| **#149** 普通报错写永久授权失效 | `cli.py:1448 _is_contract_drift_error` 白名单（PR #182 / `645d46b`）；`state.py:1265` state.previous 自修；回归用例 `tests/test_reauthorization_gates.py:317`（读失败不写标记）+ `:134` / `:295` / `:404`（真实漂移仍失效） | ✅ closed as completed（[comment](https://github.com/halfmoon82/vibe-guide-automation/issues/149#issuecomment-6207199602)） |
| **#141** 决策信箱 + 通知桥 | `decisions.py` + `decision_boundary.py`（`notify_bridge`）+ CLI `decision-raise`/`decision-list`/`decision-answer`（`cli.py:126` / `:1741` / `:1769` / `:1776`）+ `tests/test_decision_mailbox.py` | ✅ closed as completed（[comment](https://github.com/halfmoon82/vibe-guide-automation/issues/141#issuecomment-6207200473)） |
| **#156** 心跳指令缩成一行 | `supervisor.py:723 heartbeat_command()` + `prd-guide.md:213-220` + `tests/test_heartbeat_canonical_prompt.py`。**未核实项如实记录**：验收里「空闲心跳增长降一半」的运行期测量仍在采集中，不达标需另开 | ✅ closed as completed（[comment](https://github.com/halfmoon82/vibe-guide-automation/issues/156#issuecomment-6207201456)） |
| **#155** 监工轻装加载 | 实测为负结果（39,031 token vs 目标 17k，PR #165），无已验证达成路径。**⚠️ 范围移交**：#216 明确把宿主侧固定成本排除在外 → 这块成本目前无承载 issue | ✅ closed as not planned（[comment](https://github.com/halfmoon82/vibe-guide-automation/issues/155#issuecomment-6207202412)） |
| **#154** 心跳间隔 | 其实现在 PR #201；实测证据在 PR #167，通道补齐在 #210。**并入 #201 合并时一并关闭，不单独做** | 仍 open |

机器核验（关单前实跑）：

```
env -u PYTHONPATH python3 -m pytest tests/test_reauthorization_gates.py tests/test_decision_mailbox.py -q
70 passed, 17 subtests passed in 5.34s
env -u PYTHONPATH python3 -m pytest tests/test_heartbeat_canonical_prompt.py tests/test_supervisor_context_budget.py -q
26 passed, 2 subtests passed in 2.08s
```

关单后远端 open issue：**22 → 17**（剩 #126 #128 #145 #147 #148 #150 #151 #152 #153 #154 #157 #166 #208 #209 #216 #219 #220）。其中 #146 已于 11:5x 追加关闭（core resolved，残余转 #145）。

### 3c 方向待定 → 2026-10-16 11:52 已核实并定处置（两条都不废弃）

| Issue | 核实结论 | 处置 |
|---|---|---|
| **#128** 接入决策网关（入口请求分诊） | ① 网关**活着**：`lsof -iTCP:8009 -sTCP:LISTEN` → PID 729 监听 `127.0.0.1:8009`，`GET /` 返回 FastAPI 的 `{"detail":"Not Found"}`；② 方向**已被已批准的整合 PRD 收进范围**（B5 条目 / G6 目标 / §5.10 / 映射表 `\| #128 \| B5、§5.10、AC-E1 \|`），non-goal 只排除「不实现真实网关服务与评测集标注」；③ 验收**现成**：AC-E1 三项在 main 实跑 **1 过 2 败**（`gateway_down_entry_still_triages_by_s1_rules` 过；`gateway_down_leaves_a_degradation_record`、`evaluation_scaffold_reports_accuracy_ece_p95` 败） | **不废弃**，已批准待排期，排在 #145 收尾批次**之后**（新模块 + CLI，独立一轮迭代）→ [comment](https://github.com/halfmoon82/vibe-guide-automation/issues/128#issuecomment-6207688416) |
| **#166** 每次心跳新会话 | 原方案三条前提：① 宿主已换（当前 provider `claude-code-visible`，不再是 Codex cron）→ 前提失效；② 路线已分叉（PR #178 单班 + #180 20 万上限是「少换班」，与「取消换班」不同）；③ 成本前提改善（#131/#156 已把心跳缩成一行）。**但目标（切断长会话二次增长）仍未解决**，且 #155 实测负结果说明宿主侧轻装路线走不通 | **不废弃但判据要换**：改用 A13 口径的空闲心跳实测数据判定 → [comment](https://github.com/halfmoon82/vibe-guide-automation/issues/166#issuecomment-6207688754) |

#### 附：#166 判据所需数据已到手（A13 口径实算，2026-10-16 11:55）

数据源 `a13-idle-measurements.jsonl`（run 目录内，最后一条 10-16 09:57 shift 9）。取最长连续空闲段（2026-10-15 08:59→12:09，10 分钟一拍）：

```
采样 20 点 / 19 拍（10 分钟一拍）   每拍增长 min=1310 max=3209 均值=1651.7
前10均=1721.7   后10均=1555.6   比值=0.9035   差=-166.1
```

对照 PRD §6.1 的 A13 口径（**20 次以上**空闲心跳；每次增长 ≤5,000 token；后 10 次均值 ≤ 前 10 次均值×1.2 或差值 ≤500 token）：

| 子判据 | 实测 | 判定 |
|---|---|---|
| 每次增长 ≤5,000 | max 3,209 | ✅ 满足 |
| 后 10 均 ≤ 前 10 均 ×1.2 或差 ≤500 | 比值 0.9035、差 −166（**不增反降**） | ✅ 满足 |
| 样本 ≥20 次空闲心跳 | 19 拍 | ❌ **差 1 拍** |

→ 结论：**除样本数外两项子判据全过，且后段无增长**。这与「长会话二次增长」的担忧相反——至少在这段数据上，空闲心跳的成本是**线性**而非二次的。补足 20 拍即可出具 A13 报告（PRD N3 要求「单班成为默认」正是以这份报告为前提）。**注意**：另一段（10-15 02:18→03:38，仅 8 拍）出现过一次 **+5,517** 的跳变，疑似会话切换后的再水化成本，需在报告里单列说明。

---

## 四、另需你决定的非 issue 项

### 🔴 4.0 最新发现（2026-10-16 11:55）：活跃 run 已经停摆，不会自己「收口」

收尾批次的触发条件是「run 收口」，但 11:55 复核发现 **run 从 10:58 起就没有再推进**：

| 证据 | 值 |
|---|---|
| `state.json` status | `blocked_unknown`（`event_sequence=408`） |
| 节点 | 5 accepted；**`integration-review`、`monitor-routing` 两个 `blocked_unknown`** |
| 最后写盘 | `events.jsonl` / `state.json` **10:57:45**；`next-supervisor-prompt.md` 10:58:19 |
| 第九班会话 `ab8da63a` | 最后一条消息 `2026-10-16T02:58:30Z`（= 10:58:30 +08），之后无输出 |
| 第十班 | 交接文档**已写好但没有任何会话接替**；run 目录 55 分钟内零变更 |

卡点内容（事件 406 `blocked_unknown_disposition_required`，节点 `monitor-routing`）——两条**待处置**的越界缺口：

1. **已回答的决策从不被消费**：preflight 回 `idle`，节点停在 `blocked_design`；PRD §5.5「回答后继续」需改 `protocols/` / `cli/` / preflight，**超出本节点允许路径**。
2. `recovery_action = raise_missing_decision`（原因不明的 `blocked_design`）**仍无读取方**；F3 主场景已改为监工当场登记决策卡。

→ 第九班已就**是否修 ①**请你决定（**登记缺口不修** / **扩授权修**），这个决定**没有传到这里**，所以 run 停在原地。**它不会自行收口。**

**要启动收尾批次，必须先做两件事：**

- **A. 给 run 的缺口 ① 一个处置**（决定后由第十班按 `blocked-unknown-disposition-no-cli.md` 处置：`Monitor.record_blocked_unknown_disposition` → 清 `consumed_by` → 重报交付）。
- **B. 起第十班**（`vibe supervisor-handoff --run-id run-9edb07bb…` + 注册 + 心跳）。

> ### ✅ 产品经理决定（2026-10-16 12:0x）
>
> 1. **run 走向：先不动 run，推独立项。** run 保持现状（`blocked_unknown`），不接第十班、不归档；先做不依赖它的独立项（#219 / #216 / #128 等）。
> 2. **缺口① 定性：登记缺口不修，放行节点。** 按 `confirmed_benign` 处置放行 `monitor-routing`，缺口写进整合审查报告正文，转一条新 issue 跟踪。**执行时机随 run 一起推迟**——等 run 被重新接起时，由接班会话按此执行。
>
> → 连带结论：**收尾批次（#145 / #153 / #146 残余）也继续挂起**，触发条件改为「run 被重新接起并收口」，而不是「等它自己收口」。

- **授权时效（已核实，原记载有误）**：第九班记「2026-10-16 21:17 到期」是**旧 24h 口径**。main 上 `vibe_guide/engine_attestation.py:108` 的 `max_age_seconds = 72 * 3600`（PR #213 / `d645484`，已在 main），而本 run 的 `engine-attestation.json` 是 `2026-10-15T21:17:47+08` 生成 → **实际到期 = 2026-10-18 21:17 (+08)，剩余约 57 小时**。**不构成紧急项**，但收尾批次 + run 收口 + 5 个 PR 合并仍应在窗口内做完。

### 4.1 其余待决项

- **PR #212**（验收用例，含 12 个真实失败用例，明写「勿合并」）：要么转成跟踪 issue，要么随缺口修复 PR 一起合。它同时也是 #147/#153 的验收证据载体。
- **合并清单**：#184 / #188 / #201 / #206 / #207 五个 PR，需你明确同意；#188 先解冲突。
- ~~授权时效~~ → 已在 4.0 核实：**2026-10-18 21:17 (+08) 到期，剩余约 57 小时**。

---

## 五、独立项开工可行性（冲突面实算，2026-10-16 12:1x）

产品经理决定「先不动 run，推独立项」。但 run 虽然停摆，它的 5 个 PR 分支仍在，**文件面很宽**——先算清楚哪些是真独立、哪些会重演 #145/#153 的静默降级。

### 5.1 run 占住的文件面（5 个 PR 的并集）

```
vibe_guide/  cli.py(#184/#201/#206)  monitor.py(#188/#207)  supervisor.py(#184/#207)
             state.py(#188)  decision_boundary.py  decisions.py
             protocols/__init__.py  protocols/prd-guide.md   ← 关键
             capability_wizard.py  doctor.py  host_rules.py  initializer.py
根目录        README.md  README.en.md
tests/       约 15 个测试文件
```

### 5.2 硬阻塞面 vs 合并摩擦面（决定性区分）

| 类别 | 判据 | 文件 |
|---|---|---|
| **硬阻塞**（改了会静默降级，必须等 run） | 该文件是 `SHIPPED_PROTOCOL_DIGESTS` 登记的对象 | **只有 `protocols/prd-guide.md` 和 `protocols/vibe-entry.md`**（`protocols/__init__.py:28-101`） |
| **合并摩擦**（只是 rebase 麻烦，可做） | 其它被 run 改的文件 | `cli.py` / `monitor.py` / `supervisor.py` / `state.py` / `README*` / `tests/*` |

> ⚠️ **`protocols/visible-sdd-worker.md` 不在登记范围**——这是 #219 能开工的关键前提。先前把「protocols/ 整个目录」当成禁区是**过宽的判断**，已更正。

### 5.3 逐项判定（2026-10-16 15:1x 用修正后判据重判，两行被改）

| 独立项 | 主要改动文件 | 硬阻塞？ | 摩擦 | 结论 |
|---|---|---|---|---|
| **#219** worker 未挂载 project | `protocols/visible-sdd-worker.md`（**未登记**）、`adapters/task_provider.py`、`runners/provider_action.py`、`cli.py` | ❌ | 有 | ✅ **已交付**（PR #221 squash 合并 `69348cd`） |
| **#220** 合同漂移无事前检测 | `supervisor.py`（beat/preflight）、**`protocols/prd-guide.md` §6.5**、**`protocols/__init__.py`**、`cli.py:1694` | ✅ **撞 prd-guide §6.5 + digest 登记** | 全部 | ❌ **必须进收尾批次**（原判「✅可开工」**错误**，见 §5.7） |
| **#128** 接入决策网关 | 拆两半：① `eval/decision/route.jsonl` + `eval/run_eval.py` + `calibrate/temperature.py`（新文件）；② **`protocols/vibe-entry.md` §2（S1 五维评分）** | ①❌ ②✅ | ①无 ②有 | ⚠️ **半个可开工**：先做 ①（评测集/校准/阈值报告），②「接入入口路径」进收尾批次 |
| **#147** 重新授权按 DAG 闭包失效 | `monitor.py`（重新授权失效判定）、`state.py`（失效闭包）、`tests/` | ❌ | `monitor.py` `state.py` | ✅ **已交付**（PR #222 squash 合并 `153041e`；**无需生产代码改动**，见 §5.8） |
| **#126** Claude Code 宿主规则不生效 | `scanner.py:37 RULES_FILE_CANDIDATES`、`initializer.py _rules_target()`、`paths.py:54`、`doctor` 检查、`tests/` | ❌ | 无（`monitor.py`/`state.py` 之外） | ✅ **可开工**（补列：覆盖表里有、判定表原先漏了） |
| **#216** 上下文成本 | `cli.py:3152`、`supervisor.py:979`、`protocols/__init__.py`、`prd-guide.md`、`runners/provider_action.py:600` | ✅ **撞 prd-guide + digest 登记** | 全部 | ❌ **必须进收尾批次** |

**判据复核的落地结果**：可开工面从「#219/#220/#128/#147」收窄为 **#147 + #126（整项）+ #128（半个）**；
收尾批次新增 **#220**（原列已有 #216 与 PR #164 的 digest 重算）。

### 5.4 开工前另需修正的两处坐标

- **#216 ② 坐标写法有误**：实际路径是 `vibe_guide/runners/provider_action.py`（不是 `vibe_guide/provider_action.py`），内联点在 **`:597-600` 附近**（注释原文「the protocol body is inlined into the worker prompt so the session does not need the repository to obey it」）。**该内联确实在 main 上**，issue 描述成立，只是路径少写了 `runners/`。
- **`#216 ①` 坐标近似成立**：`bounded_supervisor_output` 定义在 `supervisor.py:979`、调用点在 `cli.py:3152`（issue 写 `cli.py:3151`，差 1 行）。
- **附带确认**：`origin/node/inline-sdd-protocol-aa7da36c`（`11f7733`）**未并入 main**，只动了 `runners/provider_action.py` + README + 一个新测试——与 #216 ② 同题，收尾批次时需一并处理，避免两个改法打架。

### 5.5 已排除重复派发

全部 10 个 open PR 与 40+ 个远端分支已核对，**没有任何分支在做 #219 / #220 / #128**。两条真独立项无 owner 冲突。

### 5.6 #219 已交付（2026-10-16 14:0x）→ PR #221 squash 合并 `69348cd`

**交付内容**（4 件，全部在 main）：

1. `visible-sdd-worker.md` §1.5「派发前提：worker 会话必须挂载 project」（新建）+ §5 交付形状补 `session_cwd`。
2. 观测侧核对：worker 自报 `delivery_evidence.session_cwd`，`worker-deliver` 落点核对发现会话根不在项目根/worktree 内即报警（advisory，非门禁，缺省不报）；create 与 continuation 两条派发提示都要求填。
3. 请求侧登记：派发记录 `project_mount` 三态（`project:<id>` / `none` / 键缺失=legacy），只有显式 `none` 才报警；`resume`/返工继承创建代次的值。
4. 兜底守卫：`CodexAppBridge.create` 要求 `target.type=project` 且 `projectId` 非空，否则 fail-explicit（注释写明触达面：全仓无生产实例化，依赖宿主接线）。

**关键判据修正（推翻了 issue 自带的「改法建议 2」）**：查故障 run 的 mailbox，`run-17413883…` 的 **4 条 create 请求全部带 `target.type="project"` 与有效 `projectId=c0800618-…`**，而 `acceptance-pack` developer 仍落成 projectless。→ **「请求里写了挂载」与「宿主实际给了什么沙箱」是两件事**，纯请求侧检查原理上看不到本次故障。故本 PR 补了观测侧（会话根是链路里唯一能看到实际沙箱的事实）。

**残留（故 #219 保持 OPEN，已留评论）**：宿主侧事前拦截没有（要事前拦需宿主把 create 走 `CodexAppBridge`）；`session_cwd` 自愿上报、漏报即回到「不知道」；告警只到 worker，不进 supervisor 的 wait 结果（wait 结果由 payload 构造，塞进去会动 `idempotent_key` 与自报回执语义）。**待产品经理决定：就地关闭还是留作跟踪。**

**验证**：相关面 9 文件 `254 passed / 95 subtests`；全量 `9 failed / 2089 passed / 1 skipped / 642 subtests`，9 条失败在 `origin/main(0ec215e)` 对照检出上逐条相同（5 条沙箱禁 `ps`，4 条需网络/venv）。独立 reviewer 三轮，最终 P0=0/P1=0/P2=0。

**下一顺位**（2026-10-16 15:4x 更新：**#147 已交付**，见 §5.8）：
**#126（整项，可立即开工）** → **#128 的前半（评测集 + 校准 + 阈值报告）**；
**收尾批次**：#216、**#220**、PR #164 的 prd-guide digest 重算、#128 的后半（接入 `vibe-entry.md` §2）。

### 5.7 判据方法论修正（2026-10-16 15:1x，一次真实误判换来的）

**原判据（错）**：「这个 issue 想改的文件，是否在 `SHIPPED_PROTOCOL_DIGESTS` 登记表里？」
**修正后判据**：「**这个 issue 是否必须改一个已登记对象**（`prd-guide.md` / `vibe-entry.md` / `protocols/__init__.py`）？」

差别就是一次误判：#220 的 issue 正文只提 `supervisor-beat`，我据此判「❌ 不撞登记表 → ✅ 可开工」，
漏掉了**实现它必然要改 `prd-guide.md` §6.5**——因为 `prd-guide.md:455` 的标题就是
「监工心跳纪律（预检 → idle/work/rotate）」，`:464-469` 逐条枚举五个状态，新增 `contract-drift`
状态就是在改这一段。

**为什么这是硬阻塞而不是合并摩擦**（三条实证）：
1. `prd-guide.md` 是 `SHIPPED_PROTOCOL_DIGESTS` 的登记对象，改文本必须往 `protocols/__init__.py` **追加一条 digest**；
2. **PR #184 正在改同一段**（`prd-guide.md:452` 挂起条目 + 追加 digest `f8021c6c…`），**PR #201 也改 `protocols/__init__.py` + `prd-guide.md` + `vibe-entry.md`**；
3. 登记表是**共享的 append-only digest 列表**：追加位置必然文本冲突，且每条 digest 绑定特定文本版本，合并后必须全部重算——不是 rebase 能解的合并摩擦，是同一对象上的语义冲突。

**流程教训**：判「某独立项能不能开工」时，**不能只读 issue 正文里的文件清单**——issue 作者写的是他以为的改动点。
必须走一遍「实现它会碰到哪些协议文件」的推导，再拿登记表去比对。**issue 正文的文件清单是线索，不是判据。**

**连带改正的另两处**：
- **#147 的原判「AC-B5 用例合并 / 只把 PR #212 里的用例取出来」与本 issue 正文不符**。实际是「重新授权时按 DAG 闭包只让合同内容真正变化的节点失效」（`monitor.py`/`state.py`）。原判是错的，已改。
- **#126 漏列**：覆盖核对表（§附）把它算进「没开始-需单独做 5」，但判定表（§5.3）没这一行。已补，且复核为 ✅ 可开工（`scanner.py`/`initializer.py`/`paths.py`/`doctor`，不碰登记对象）。

**结论：一次误判 + 两处漏错，都是「只信 issue 正文、没推导实际改动面」造成的。判据必须落在代码上，不能落在 issue 文字上。**

---

### 5.8 #147 已交付（2026-10-16 15:4x）→ PR #222 squash 合并 `153041e`

**结论：不需要生产代码改动。** #147 要的行为早在 main 里——`executable_contract_digest`
自 `645d46b`（= **#182**「Runs authorized by 5.0.7 must stay readable after the #182 digest
change」）起用 `include_scope=False`，把 `files`/`allowlist` 排除出「验收合同摘要」，
改由 `AuthorizationRecord.file_scope` 单独授权。交付物 = 回归用例 + 真实数据根因证据。

**真实数据复现（run-182893a5，本地 `.vibe/runs/` 仍在）**：
- `authorization_reauthorized`：`changed_nodes=['contract-env-facts']`、`affected_nodes` 5、
  已验收 4 里保留 1 / 失效 3（1 改 + 3 下游 = 4，与 issue 描述吻合）。
- `previous_authorization` vs `new_authorization` **逐字段只差** `file_scope` 多了
  `vibe_guide/protocols/__init__.py`（协议摘要登记所需写范围）。
- 重建「扩展前」节点集后用 `legacy_executable_contract_digest` 复算：6/6 逐节点摘要 +
  整 DAG 旧摘要 `3d4365cd` + 新摘要 `dd6ea824` **全部精确命中记录值** → 重建忠实。
- **坑**：该文件同时在 `contract.files` **和** `contract.worker_profile.allowlist`（嵌套），
  只摘顶层 `files` 会对不上。

**两条可复用的方法教训**：
1. **「摘要相同」≠「行为已修」**。`reauthorize` 比的是「新活节点 vs **快照里存的旧摘要**」，
   不是整 DAG 摘要。run 140 快照存的是 5.0.7 含 scope 摘要 → **该场景仍会重派**。
   结论必须落在**实际被执行的比较**上。
2. **不要声称「不可修」**。快照节点确实只存 `contract_digest` 不存 `contract`，但
   `snapshot.authorization['file_scope']` 是持久化的 → 可做「按 scope 增量反推候选旧合同 +
   摘要校验」的验证式判定（fail-safe 但不完备：同一文件 A 增 B 减会抵消）。
   正确措辞是「本次不实现」。

**遗留**：#147 是否关闭、是否为「legacy 快照的验证式判定」另开一项 → **待产品经理定**
（已在 issue 留评论 `issuecomment-6210671233`）。

**独立 reviewer 三轮**：一轮 1×P2+2×P3 → 二轮 1×P2+1×P3 → 三轮 **P0–P2 清零，判可合并**。


---

## 附：issue 号覆盖核对（22/22）

有人管 8：148 150 151 152 154 157 208 209
做到一半 4：145 146 153 155
没开始-需单独做 5：126 147 216 219 220
没开始-已关闭 4：141 149 155 156（+146 转 #145 后关闭）
方向待定 2：128 166 → **11:52 核实完毕：两条都不废弃**（#128 已批准待排期；#166 判据换成 A13 实测）

**当前 open = 17**（22 − 5 关闭）。全部 17 条均有归属：
- 活跃 run 内 8：148 150 151 152 154 157 208 209
- 收尾批次 3：#145 #153（+ #146 残余）+ #154（随 PR #201 一并关）
- 单独做 5：126 147 216 219 220
- 已定处置待排期 2：#128（收尾批次后）、#166（等 A13 补足 20 拍）
