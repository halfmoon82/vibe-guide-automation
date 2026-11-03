# PRD：交付环免评审合并、空转信号、授权卡依赖图与低版本启动（5.2.7）

- 计划编号：`session-6cb7b10c98fb560b`（`vibe plan` 先判 simple，按实际失败代价显式升级为 complex，S1=17：steps 3、domains 3、uncertainty 3、failure_cost 5、toolchain 3）
- 基线：`v5.2.6`（`main` 的 `aeab928`）。本文行号指这个提交。
- 产品 spec：同目录 `2026-11-03-delivery-gate-and-stall-signals-product-spec.json`
- 状态：已定稿（产品经理 2026-11-03 定稿，含 D2 缩窄），六个产品决定都已闭合；远端 Git 动作选「允许」。
- 证据来源：issue [#385](https://github.com/halfmoon82/vibe-guide-automation/issues/385)、[#386](https://github.com/halfmoon82/vibe-guide-automation/issues/386)、[#387](https://github.com/halfmoon82/vibe-guide-automation/issues/387)、[#390](https://github.com/halfmoon82/vibe-guide-automation/issues/390)、[#391](https://github.com/halfmoon82/vibe-guide-automation/issues/391)；5.2.5 的真实运行 run-345fc559（Claude Code 后台）和 5.2.6 的另一个项目里的真实运行 run-9fef3109（WorkBuddy）。

## 1. 问题

1. **交付环在单账号仓库里过不去（#385）。** 整合审查第 3 代 P0–P2 清零后，交付环 prepare、push、create_pr 都成功，review 步骤两次失败（run-345fc559 事件 5573、5586），节点落 `blocked_unknown`。原因：交付环只认 PR 上 `state=APPROVED` 的 GitHub 评审，而仓库只有一个账号，GitHub 拒绝作者批准自己的 PR。PR #384 最后由 halfmoon82 账号手工合并。（`system_inferred`）
2. **整合审查等上游返工时被误判空转（#386）。** 整合审查第 1 代报 P2 并指向 card-and-handoff（事件 4966），上游正确进入第 4 代返工；整合审查节点指纹不变，被判空转：auto_retry 一次，连开 3 个修复短会话，都是 `nothing to re-check`，第 5097 条 `self_heal_no_progress`，节点和 run 都落 `blocked_unknown`，心跳报「需要人处理」。上游验收后整合审查自动开第 2 代，run 自行恢复，所以是误报。（`system_inferred`）
3. **心跳反复报「读不到会话记录」（#387）。** issue 认为会话记录在工作树对应的项目目录下、而代码只去主项目目录找。但读取代码按会话编号在所有项目目录里查找，v5.2.5 与 v5.2.6 代码相同；2026-11-03 在本机核对登记过的 85 个 worker 会话，每个都能找到记录。真实原因没有定位。（`unverified`）
4. **信箱请求没人服务被误判成节点卡住（#390）。** run-9fef3109（WorkBuddy in_session_sdd）里监工会话离线时，三个节点停在 `retry_pending`（attempt 141），心跳连续 13 拍报「需要人处理」，修复短会话白跑；事件日志里至少有 423 条 `retry_scheduled`（数字还在涨）、12 条 `self_heal_attempt`（原因都是 `nothing to re-check`）。信箱里待服务的请求在空转判定里完全没被考虑。（`unverified`：run 里的现象是读本机事件日志得出，跨平台范围是读代码的推断）
5. **授权卡心跳间隔自相矛盾（#391）。** 卡上 60 分钟那项标 `forced: true`，最终选中的却是 10 分钟，产品经理看不出到底是哪个。`forced` 在代码里的意思是「强制选择档、要带价格才能选」，不是「已选中」。（`system_inferred`）
6. **低版本 Python 上 vibe 一启动就崩（另一台机器升级时反馈）。** v5.2.4 起 `decision_boundary.py` 用了 `Dict` 但没导入。项目声明支持 Python 3.9 及以上；我在本机 3.12、3.13 上用 `main` 的副本实测，89 个模块（不含 `__main__`）里 13 个导入失败，同一个 `NameError`，`vibe_guide.cli` 也在其中；只补这一个导入后两个版本都是 0 个失败。3.14 因为延迟解析类型注解不报，所以日常开发看不出来。仓库的发布检查只在打 tag 时跑，3.9 只用在旧打包路径上，没有任何一步在最低支持版本上导入全部模块。（`system_inferred`，本机实测）
7. **“图形 DAG 与授权卡一起展示”和“能并行尽量并行”两条纪律没有被机械执行。** 产品经理观察到：展示图要靠追问，DAG 经常被偷懒串成一条线。读代码与本机数据的结果：授权卡数据里只有节点列表，没有依赖图字段；`vibe authorize` 的门禁只查十节点证据，没有“图已展示”回执；`audit_dag` 只检查环和写范围重叠，不检查“这条串行边有没有理由”，也不计算并行宽度。本机 32 个有 3 个以上节点的已发布计划里，5 个整条串行；排除整合审查和收口节点后，223 条串行边中 71 条（约 32%）两端写范围没有交集。71 条里可能有真实的数据依赖，现在 `depends_on` 没有理由字段，区分不了。（`user_confirmed` 观察，边数是本机统计）

**是单平台现象还是整体问题（本次查证）：**

| 问题 | 结论 | 依据 |
|---|---|---|
| #390 信箱被误判卡住 | 整体问题，WorkBuddy 暴露最明显 | 空转判定路径上没有任何按平台或拓扑分支的代码。凡是信箱请求靠监工会话服务的派发方式（Codex 可见、Claude Code 可见卡片、WorkBuddy）原则上都会这样。目前只在 WorkBuddy 上见到，其他平台是读代码的推断（`unverified`），所以节点验收要求用夹具在 Codex 可见派发上复现。Claude Code 后台由守护程序服务信箱，推断不受影响，没有实测。 |
| #391 心跳间隔矛盾 | 整体问题，不是 WorkBuddy 专属 | `heartbeat_interval_options` 只按 provider 取值：没有项目登记文件时，codex、claude-code、workbuddy、cursor、grok、kimi-code、deepseek-harness 都是 60 项 `forced`、选中 10；选中 60 取决于项目 `push-wake-verification.json` 的登记。WorkBuddy 的取值 10 与 prd-guide §6.0、§6.4 和 README 的现行口径一致，矛盾只在字段名和 reason 措辞。 |

#391 issue 建议的「WorkBuddy in_session_sdd 默认 60 分钟」与现行规则相反，不在本迭代范围；要改需另立产品决定。

## 2. 目标

- 整合审查通过后交付环不再被 GitHub 评审卡死，同时不放过「该等的评审」。
- 节点在等上游返工、或等监工服务信箱请求时，不再被当成卡住，不再白开修复短会话；真卡死时升级的是真正卡住的那个，无人服务的信箱也会在 60 分钟后上报。
- 心跳读不到会话记录时说清原因，并修掉真实原因；查不出就如实说。
- 授权卡上的心跳间隔只出现一个明确的选中值，与文档口径一致。
- vibe 在项目声明支持的最低 Python 版本上也能启动，并且有检查防止同类回归。
- 授权卡必须带依赖图，没展示就不能授权；每条串行依赖都要有理由，无理由的不能发布。

## 3. 用户场景

1. 产品经理在只有一个 GitHub 账号的仓库里授权一个 run；整合审查通过后，交付环直接合并并核实，不用他去 GitHub 上找人批准。（`user_confirmed`）
2. 同样的仓库里有人在 PR 上要求修改：交付环退回返工，不会因为整合审查通过而照合。（`user_confirmed`）
3. 有多个协作者、但 main 没有评审保护的仓库：整合审查通过后同样直接合并，不等同事批准（D3 选 A，产品经理接受这一行为变化）。（`user_confirmed`）
4. 仓库开了「需要评审」的分支保护，或 vibe 查不出有没有开：交付环等 GitHub 评审，交付报告和 `vibe status` 写明在等谁。（`user_confirmed`）
5. 整合审查发回上游节点返工、返工要跑很久：心跳不再对整合审查报假警；上游真卡死时被升级的是上游。（`system_inferred`）
6. 监工会话还没来得及服务信箱请求：心跳写「信箱待服务」，而不是报「需要人处理」；等满 60 分钟仍没人服务，心跳仍报「信箱待服务」并加上「需要监工服务信箱」的升级提示。（`system_inferred`）
7. 监工看心跳时，会话记录读不到的节点能直接看出是「没找到」还是「目录不可读」。（`system_inferred`）
8. 产品经理看授权卡时，心跳间隔只出现一个明确的选中值，与预检打印一致；卡上的说法与 prd-guide 一致。（`system_inferred`）
9. 在 Python 3.9 的机器上升级后运行 `vibe`：正常启动，不报 `NameError`。（`system_inferred`）
10. 产品经理授权前一定能看到依赖图，包括并行宽度、关键路径长度和没有理由的串行边；没展示过图，授权被拒并提示先展示。（`user_confirmed`，D5）
11. agent 拆节点时想把节点串成一条线：每条没有写范围重叠的依赖必须写明数据依赖理由，写不出就要改成可并行，否则计划发布不了。（`user_confirmed` 的观察，决定 D6 选 A：无理由的边拒绝发布）

## 4. 成功标准

1. 满足免评审条件（D3 选 A：凡 main 没有评审保护的仓库，包括有多个协作者的仓库）、整合审查已验收且绑定的授权与整合摘要没漂移：交付环不等 GitHub 评审，合并后核实合并提交可达目标分支。（`user_confirmed`，D1）
2. PR 上有 CHANGES_REQUESTED：照旧进入 rework，优先于免评审。（`user_confirmed`，D1）
3. 分支保护要求评审，或查不出来（接口返回 404 分不清「没开」和「没权限」、仓库用的是新式 ruleset 查不到、调用失败或无法解析）：等 GitHub 评审，不得默认当作没开；交付报告和 `vibe status` 写明在等谁。（`user_confirmed`，D1）
4. 整合审查绑定的授权摘要或整合摘要已漂移：不免评审，沿用既有绑定校验。（`system_inferred`）
5. 整合审查发回上游返工、上游仍有活动：整合审查节点不进入 `supervisor_investigate`，不开修复短会话，run 不变 `blocked_unknown`。（`system_inferred`）
6. 上游返工节点自己卡死：按原规则升级，升级对象是上游节点；非 Claude 平台没有会话记录信号时，豁免只看上游节点的状态，上游自己的空转评估照常，所以豁免不会永久有效。（`system_inferred`）
7. 节点有待服务的信箱请求：不进入空转评估，不开修复短会话，不落 `blocked_unknown`；心跳写「信箱待服务」并带请求数和最早一条已等时长。（`system_inferred`）
8. 信箱请求等满 60 分钟仍无人服务：心跳仍以「信箱待服务」为主语，并加上「已等满 60 分钟，需要监工服务信箱」的升级提示，只提示一次（按已等时长判断，不依赖修复短会话次数），仍不开修复短会话；60 分钟内只报信箱待服务；信箱读不到按未知处理，不当成空。#390 issue 的验收例子要求 60 分钟内报「信箱待服务」，这里一致；满 60 分钟加升级提示是有意增加的上限，issue 没有给无人服务的上限。（`system_inferred`）
9. 由守护程序服务信箱的后台派发节点，空转判定行为与 5.2.6 一致。（`system_inferred`）
10. WorkBuddy in_session_sdd 下，监工会话回写一次 `create` 后，信箱不再出现同一节点同一代的 `locate`、`visibility`，节点直接进入等待；自动补齐只在三个条件都满足时才做（in_session_sdd 且带能力证据引用、`create` 回执的任务编号等于本会话编号、不是收口审查节点），否则退回手工回写，不得把没有可见窗口的节点写成可见。（`system_inferred`，D4）
11. WorkBuddy 子代理通道的实测降级路径登记进 WorkBuddy 验证文档和 visible-sdd 协议：general-purpose 类型下各模型全被内容策略拦或不被允许，Explore 类型可用（能跑 Bash、没有写文件工具），dev/review 子代理改走 Explore 后正常交付；文档里标明这是单次运行的观察、宿主版本未知，来源写 run-9fef3109、2026-11-03、issue #390；不改平台账本（账本每格只有一个状态，`nested_dispatch` 现为已验证，往里加未验证备注会自相矛盾）。（`system_inferred`，D4）
12. 读不到会话记录时，心跳写明具体类别：未找到会话记录、会话记录目录不可读；记录存在于任一项目目录（含工作树对应目录）时不报。「会话编号未知」不进心跳（非 Claude 平台的节点本来就没有 Claude 会话编号，每拍都报会变成噪声），只在 `vibe status` 的节点详情里写明。（`system_inferred`，D2）
13. 授权卡上选中项单独标出；卡上的选中值与预检打印一致；prd-guide、README 对各平台心跳间隔的说法与授权卡一致；各平台默认选中取值不变；已授权在跑 run 的心跳间隔不变，5.2.6 签发的旧授权卡仍能通过核对。（`system_inferred`）
14. 真机心跳读取只能在验收期验证：在 Claude Code 监工会话里跑一个多节点后台 run，有会话记录的节点不再被报读不到；真实原因未能复现时，交付里写「未能复现」，不宣称修好。（`system_inferred`）
15. 全量测试通过，失败数不高于 `main` 上的既有基线。（`system_inferred`）
16. Python 3.9 和 3.12、3.13 上 `vibe --help` 正常；新增的全模块导入测试在 3.9 上被执行，故意删掉一个导入会让它失败并指出模块名。（`system_inferred`）
17. 审计为每条 `depends_on` 边标注理由类型：写范围重叠、数据依赖、无理由；无理由的边按 D6 处理；审计结果输出每个计划的最大并行宽度和关键路径长度。5.2.6 及更早已发布的计划照常读取，不重新审计。（`system_inferred`，D6 选 A）
18. 授权卡的输出内容里自带依赖图（每个节点的前置节点、并行组、最大并行宽度、关键路径长度、无理由串行边），不依赖 agent 另行粘贴；没有“依赖图已随授权卡展示”的回执，`vibe authorize` 拒绝并提示先展示；已授权在跑的 run 不追溯。（`user_confirmed`，D5）
19. 本 PRD 自己的节点依赖也满足第 17 条：每条串行边都有写范围重叠或声明的数据依赖理由。（`system_inferred`）
20. 展示回执对应当前计划版本和依赖图，计划或依赖图变化后回执失效；现有涉及授权的测试通过已有的授权测试辅助 tests/support_v45_authorize.py 自动补回执，不逐个改，也不再新建第二套辅助（严格匹配直接调用授权入口的约 20 个测试文件，宽松匹配涉及授权的有 158 个）。（`system_inferred`）

## 5. 非目标

- 不新增「产品经理批准合并」入口。（`user_confirmed`，D1 选 A）
- 开了「需要评审」分支保护的仓库，评审要求不变。（`user_confirmed`）
- 不改修复短会话 inconclusive 结果在 `self_heal_no_progress` 里的通用计数，只处理「整合审查在等上游返工」和「节点有待服务信箱请求」这两种合理等待。#386 issue 的第二条建议不采纳：否则真无法识别会话的卡死也不会升级。（`system_inferred`）
- 不按 #387 issue 原建议改项目目录推算方式。（`user_confirmed`，D2 选 B）
- #390 issue 的第 3 条建议（授权卡、接力口令明确写明 in_session_sdd 下监工会话必须常驻）不纳入本迭代；本迭代只让「信箱待服务」在心跳里被看见，并在等满 60 分钟时提示。（`system_inferred`）
- 不改任何平台默认选中的心跳间隔取值；#391 issue 建议的「WorkBuddy 默认 60 分钟」另立决定。（`system_inferred`）
- 不改由守护程序服务信箱的后台派发路径；不改信箱请求本身的回写格式（只在 D4 纳入的 host_session 自动补齐范围内，由 vibe 代为补写 `locate` 和 `visibility`）；不改 `retry_pending` 的重试节奏。（`system_inferred`）
- 不追溯审计 5.2.6 及更早已发布的计划，不让已授权、在跑的 run 失效。（`system_inferred`）
- 不要求把全模块导入测试扩展成全量多版本矩阵测试；只在最低支持版本上多跑这一个导入检查。（`system_inferred`）
- 不涉及部署、发版和凭据；不处理 #347。（`system_inferred`）

## 6. 产品决定

| # | 问题 | 选定 | 状态 |
|---|---|---|---|
| D1 | 无评审保护、整合审查通过时，交付环等不等 GitHub 评审 | A：不等，直接合并并核实；CHANGES_REQUESTED 仍返工；开了评审保护或无法确定时仍等并写明在等谁 | `approved`（产品经理 2026-11-03 选 A） |
| D2 | #387 的范围 | B：先分清原因类别并写进心跳，再按真实原因修；查不出就如实说未能复现。**实施中缩窄一处，产品经理 2026-11-03 已确认**：当时接受的原话是「未找到、目录不可读、会话编号未知」三类都写进心跳，现在把「会话编号未知」改为只写在 `vibe status` 的节点详情里（非 Claude 平台的节点本来就没有 Claude 会话编号，每拍都报会变成噪声） | `approved`（产品经理 2026-11-03 接受 B；同日确认缩窄，选「确认缩窄」而非恢复三类都进心跳） |
| D3 | D1 的免评审适用范围 | A：凡没开评审保护的仓库都免评审（含多人仓库）；产品经理没有采纳「只限单账号」的推荐 | `approved`（产品经理 2026-11-03 选 A） |
| D4 | #390 附带的两项（host_session 下 locate/visibility 自动补齐；WorkBuddy 子代理通道降级路径登记进文档）纳不纳入 5.2.7 | A：都纳入（产品经理没有采纳「只纳入文档登记」的推荐）；自动补齐只在三个条件都满足时做（in_session_sdd 且带能力证据引用、create 回执的任务编号等于本会话编号、不是收口审查节点），否则退回手工回写 | `approved`（产品经理 2026-11-03 选 A） |
| D5 | 授权前没有“依赖图已随授权卡展示”的回执时怎么处理 | A：拒绝授权，提示先展示依赖图（推荐被采纳） | `approved`（产品经理 2026-11-03 选 A） |
| D6 | 一条串行依赖既没有写范围重叠、也没有声明理由时，发布计划怎么处理 | A：拒绝发布，要求补理由或改成可并行 | `approved`（产品经理 2026-11-03 选 A） |

## 7. 代码现状（`v5.2.6`，只读核对）

| 关注点 | 位置 | 现状 |
|---|---|---|
| 交付环 review | `vibe_guide/remote_delivery.py:473` `_review`、`:820-823` | 只认 `state==APPROVED`；没有就抛 `integration PR has no approving review` |
| CHANGES_REQUESTED | `remote_delivery.py:803-818` | 置节点 rework 并发 `remote_delivery_rework` |
| 整合审查证据与绑定 | `remote_delivery.py:104`、`:119`、`:731` 附近 | `integration_evidence_digest`、`delivery_binding`；到 review 步骤时已验收且绑定已满足（`unverified`，未逐行核对 :731 附近） |
| 分支保护查询 | 无 | 没有任何 `gh api` 或 `reviewDecision` 调用 |
| 交付环状态文案 | `vibe_guide/cli.py:2475-2495` | 返工、受阻、进行中三种，没有「在等 GitHub 评审」 |
| 空转指纹 | `vibe_guide/stall.py:41` `node_fingerprint`、`STALL_BEATS=3`（:24） | 只含节点自身字段，没有「上游在为我返工」，也没有「信箱待服务」 |
| 空转升级档 | `stall.py:103-109` `_tier`、`ESCALATE_ATTEMPTS=2`（:25） | 只有修复短会话次数达到 2 才升级，不开修复短会话的节点永远升不到升级档 |
| 空转候选 | `vibe_guide/supervisor.py:2884` `_stall_candidates` | 取「自动修复中」的节点和会话静默的 running 节点；`state.py:191-192` 把 `retry_pending` 归入「自动修复中」，所以信箱待服务的节点会被评估 |
| 信箱在哪被看 | `supervisor.py:782` `_mailbox_backlog`、`daemon.py:509`、`:707` | 只用于推迟轮班和收尾判断，不用于空转判定 |
| 上游返工标记 | `vibe_guide/monitor.py:7925-7993` | 上游节点写 `integration_finding_ref={node_id, generation}`（`unverified`） |
| 修复短会话与升级 | `daemon.py:977`、`:1174`；`supervisor.py:3189`；`heal_pack.py:189`、`:196` | `nothing to re-check` 判为 inconclusive，累计到 `self_heal_no_progress`（`unverified`） |
| worker 活动信号 | `vibe_guide/worker_liveness.py` `liveness_signal` | 交付文件时间戳加 Claude 会话记录时间；非 Claude 平台只有交付文件，没有就返回 None |
| 读会话记录 | `worker_liveness.py:52` `transcript_path` | 在 `~/.claude/projects/*/<会话>.jsonl` 通配；根目录不是目录时返回 None，统一报 `transcript not found` |
| 心跳文案 | `supervisor.py:3151` `render_stall_beat_text` | 「读不到会话记录」，不分类别；只渲染 `unknown_sessions`，不渲染 unresolved |
| 心跳间隔选项 | `supervisor.py:1985` `heartbeat_interval_options`、`:1998-2004` | 60 项在推送唤醒未验证时带 `forced:true` 和代价；选中值由 `publish.py:58` 取「可用且非 forced」最大值，没有就回落 10 |
| 心跳间隔口径 | `prd-guide.md:167`、`:200`、`:311`、`README.md:108`、`:200` | 都写「推送唤醒未验证的平台 10 分钟」，与默认选中一致 |
| host_session 与信箱步骤 | `supervisor.py:2679-2731` `_host_session_service`、`runners/provider_action.py:847` `_require_result`（调用方在 `:1320-1346`） | `host_session` 只是信箱条目上的 `service.mode` 标记；`create→locate→visibility` 因每步没结果就抛 `ProviderPending` 而逐步出现，每步约 20–30 秒（`unverified`，行号来自子代理读取） |
| 信箱回写入口 | `supervisor.py:2820-2851` `mailbox_complete`、`provider_action.py:274-277`、`:542` | locate 回 `{"located":true}`，visibility 回 `{"visible":true,"direct_enter":true}`；目前没有自动补齐，也没有「create 回执的任务编号等于本会话编号」的校验（`unverified`） |
| WorkBuddy 子代理登记位置 | `docs/workbuddy/workbuddy-in-session-dispatch-verification.md`、`protocols/visible-sdd-worker.md` | 现有文档没有子代理模型矩阵；`tests/test_platform_ledger.py` 钉死 7 平台 9 格和「清单不带能力结论」，账本每格只有一个状态、`nested_dispatch` 现为已验证，所以不改账本、不放清单文件（`unverified`） |

## 8. 节点

| 节点 | 做什么 | 覆盖 | 依赖及理由 |
|---|---|---|---|
| python39-import-guard | 补缺失的导入，加全模块导入测试，并在 Python 3.9 上跑 | 问题 6，成功标准 16 | 无（`wave1`） |
| remote-review-gate | 满足免评审条件时直接合并并核实；否则保持等待，并在报告和 `vibe status` 写明在等谁 | #385，成功标准 1–4 | 无（`wave1`） |
| stall-visibility | 读不到会话记录分类别写进心跳；查出并修掉真实原因，或如实写未能复现 | #387，成功标准 12、14 | 无（`wave1`） |
| dag-serial-audit | 每条 `depends_on` 边标注理由类型，无理由的按 D6 处理，审计输出并行宽度和关键路径 | 问题 7，成功标准 17 | 无（`wave1`） |
| stall-mailbox-wait | 有待服务信箱请求的节点不判空转；等满 60 分钟心跳加升级提示 | #390，成功标准 7–9 | stall-visibility：同改 `supervisor.py` |
| host-session-autofill | WorkBuddy 会话回写 `create` 后，`locate`、`visibility` 由 vibe 补齐（三个前提条件） | #390 附带 ①，成功标准 10 | stall-mailbox-wait：同改 `supervisor.py` 和 `test_supervisor_mailbox_216.py` |
| stall-upstream-rework | 整合审查等上游返工时不被判空转；上游卡死时升级上游 | #386，成功标准 5、6 | host-session-autofill：同改 `supervisor.py` |
| workbuddy-subagent-docs | WorkBuddy 子代理通道降级路径登记进验证文档和 visible-sdd 协议（不改账本） | #390 附带 ②，成功标准 11 | dag-serial-audit：同改 `protocols/__init__.py` |
| authorization-card | 授权卡带依赖图，授权前要有展示回执；心跳间隔只出现一个选中值，卡面与文档口径一致 | #391，问题 7，成功标准 13、18、20 | stall-upstream-rework：同改 `supervisor.py`；remote-review-gate：同改 `cli.py`；workbuddy-subagent-docs：同改 `protocols/__init__.py`；与 dag-serial-audit 同改 `dag.py`、`prd-guide.md`，由 workbuddy-subagent-docs 传递串行，不再单列一条边 |

**并行结构（用本 PRD 自己要求的规则检查过）：** 9 个节点，关键路径 5 层，最大并行宽度 4。第一层 4 个节点同时开工；之后分成两条线：信箱线 stall-mailbox-wait → host-session-autofill → stall-upstream-rework（三者都改 `supervisor.py`），和文档线 dag-serial-audit → workbuddy-subagent-docs；authorization-card 最后收口。每条依赖边都对应表里写明的写范围重叠，没有无理由的边；把 `supervisor.py` 上的三个节点合成一个更大的节点也能把链变短，但那个节点会同时包含三个互不相干的验收例子，返工时互相拖累，所以保留拆分。

## 9. 风险

- remote-review-gate：免评审条件漏校验会让「该等的评审」没等就合并。D3 选 A 意味着多人仓库里没开评审保护的 PR 也不再等同事批准，这是产品经理明确接受的行为变化；多人仓库想保留评审，要在 GitHub 上开「需要评审」的分支保护，vibe 才会等。分支保护查询有四类漏洞要按「等评审」处理：接口返回 404 分不出「没开」和「没权限」、新式 ruleset 查不到、调用失败、结果无法解析；`vibe_guide` 里目前没有任何 `reviewDecision` 或分支保护调用，这套判断要从零写。CHANGES_REQUESTED 必须优先于免评审；绑定摘要漂移不能免评审。
- stall-mailbox-wait：60 分钟升级提示必须按已等时长算，不能依赖修复短会话次数（现有升级档只认修复次数，不开修复就永远升不上去）。信箱读不到按未知处理。心跳是 10 分钟一拍、`STALL_BEATS=3`，验收时要确认「60 分钟内只报待服务」和「满 60 分钟加升级提示」两条都成立。
- stall-upstream-rework：豁免必须有上限。「上游仍有活动」在非 Claude 平台没有会话记录信号，读不到按未知处理；豁免只在上游节点自己没有被升级时有效，否则真卡死的上游会被永久豁免。不得把 inconclusive 全局排除出 `self_heal_no_progress`。
- stall-visibility：真实原因未必能在本机复现，交付必须如实写「未能复现」，不能把分类改进当作修好。
- host-session-autofill：补齐必须同时满足三个条件：节点是 `in_session_sdd` 且带能力证据引用（与现有 `_host_session_service` 提供 `host_session` 的条件相同）、`create` 回执的任务编号等于本会话编号、不是收口审查节点。单靠「任务编号等于本会话编号」不够：对所有开发节点它天然成立，防不了伪装；代码里目前也没有这个校验，要新写。不满足就退回手工回写，否则会把没有可见窗口的节点写成可见，等于把降级路径伪装成完整可见自动化，违反 AGENTS.md §6 和「能力判断必须引用证据」的规则。补写要留事件日志并写明依据。现有测试 `test_session_self_answers_every_request_and_the_node_is_accepted` 断言四种请求都由 host_session 回写，补齐后要改。
- workbuddy-subagent-docs：这条降级路径来自单次运行的观察，宿主版本未知，必须标明来源和「单次运行、宿主版本未知」，不能写成已验证或平台的永久限制；不改平台账本（账本每格只有一个状态，往已验证的格里加未验证备注会自相矛盾）；改 `visible-sdd-worker.md` 要同步更新 `protocols/__init__.py` 的协议摘要，与 dag-serial-audit、authorization-card 同改该文件，所以排在 dag-serial-audit 之后、authorization-card 之前。
- authorization-card：`forced` 若改名会牵动授权卡的签名字段和运行时读取；5.2.6 签发的旧授权卡必须仍能核对，已授权在跑的 run 不能失效。改 `prd-guide.md` 需要在 `vibe_guide/protocols/__init__.py` 登记新的协议摘要，已列入节点文件清单。
- python39-import-guard：全模块导入测试遇到只在特定平台才能导入的可选模块，要显式列出豁免名单并写明原因，不能整体跳过；发布检查里最低版本那一步一旦被移除要有自检测试发现，否则同类回归还会漏过去；这个检查只在打 tag 时跑，发现得晚，节点验收里要求同时在默认测试里也跑一遍导入检查。
- dag-serial-audit：D6 选 A 会挡住“想不出理由的串行”，可能误伤真有顺序但写不出理由的计划，所以理由字段要允许写数据依赖；旧计划不重新审计，读取不能报错；新字段属于产品 spec 的形状，要和 `PRODUCT_SPEC_FIELDS` 与协议里的黑名单同步，否则 spec 校验测试失败。
- authorization-card 的展示回执：授权入口实际在 `authorize_entry.py`（`materialize_workflow_evidence`，:413）和 `cli.py`，已列入节点文件清单；直接调用授权入口的约 20 个测试文件（宽松匹配涉及授权的有 158 个）必须靠已有的授权测试辅助 `tests/support_v45_authorize.py` 自动补回执，不能逐个改，否则改动面失控；回执必须绑定计划版本和依赖图摘要，否则计划改了、图没重展示也能授权；只约束新授权，不能让已授权在跑的 run 失效。
- 整体：#390 的跨平台范围有一部分是读代码推断，没有在 Codex 或 Claude Code 可见路径上实测；stall-mailbox-wait 的验收例子 2 要求用夹具覆盖。#390 本身的重试循环（`retry_pending` 的重试节奏）不在本迭代范围，豁免空转评估不会停止它。

## 10. 验收期待验

- 真机心跳读取只能在验收期验证（成功标准 14）。
