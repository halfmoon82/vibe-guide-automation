# 监工自治与恢复 PRD：脱敏证据摘要

状态：PR #177 附件，2026-10-13 只读取证快照。对应同目录 PRD 第 1 节、第 17 节。本文不是 A10 可执行夹具，也不表示原 DAG 已验收。

## 口径与脱敏

- 样本是同一已授权 plan/run。本文仅保留 Issue 编号、正式事件 sequence、UTC 时间、非秘密状态；不公开用户目录、线程 UUID、授权摘要、原生请求及结果正文、完整宿主日志或 usage 明细。
- 正式事件引用 E:序号；诊断日志引用 W:行号（追加顺序）；R:history/current 指监工 registry。原始文件只在本机保留，不随 PR 上传。编号供有权读取原始记录者复核。
- 正式事件流没有逐条可信墙上时钟，先后顺序以单调 sequence 为准；诊断时间是观察时间。登记新会话、成功转移所有权、实际推进是不同事实。

## 可复核计数

| 指标 | 结果 | 边界 |
| --- | ---: | --- |
| 去重登记监工身份 | 18 | registry history 加 current；首班之外 17 个身份。历史 19 项加当前 1 项含重复。这不是 17 次成功换班。 |
| 密集相邻登记 | 2 对 | 2026-10-10 10:13/10:32 UTC，以及 2026-10-11 11:56/12:04 UTC；只能证明密集登记，不能据此证明双 writer。 |
| 诊断唤醒日志 | 22 条 | 混合观察、投递和待验证，不能说成 22 次有效恢复。 |
| Allow once 弹窗 | 精确次数未核实 | 用户多次报告；正式 run 与 registry 不含逐次宿主审批账本。不能填 0，也不能拿 15 条重授权事件代替弹窗次数。 |
| issue-156 直接错误路由 | 至少 5 对 | reviewer g13–g17 的生命周期 P1 分别紧接 developer g13、g14、g15、g18、g19 返工；另外 g16/g17 由授权恢复触发，不叠加为新 Review 结论。 |
| 最终正式 DAG 快照 | 12/14 accepted | issue-156 为 blocked_unknown，integration-review 为 planned。reviewer g18 报告在正式事件序列 2641 后形成，尚未由正式 run 消费。 |

## 关键时间线

| 编号 | 时间或顺序 | 脱敏事实及证据指针 |
| --- | --- | --- |
| T01 | 2026-10-10 10:13–10:32 UTC | 两个监工身份相继登记；前者后续停于 waitingOnApproval，后者登记后也未立即完成原生结果闭环。R:history、W:4–8。 |
| T02 | 2026-10-11 06:00–09:43 UTC，观察时间 | preflight/换班与 blocked_unknown 反复出现；同任务 developer g5 的 resume action 无结果，待审批卡住。W:6–9、诊断报告“13:24 UTC 补记”。消息和 ACTIVE 不等于 DAG 进展。 |
| T03 | 2026-10-11 11:56–12:04 UTC | 又两个相邻身份登记；新会话先遇 execution_engine_unverified，接班仍需授权及运行时证据。R:history/current、W:10–12。 |
| T04 | 2026-10-12 02:51–02:53 UTC，观察时间 | sequence 2344 的 reviewer P1 和 idle pair 未闭合；恢复回合遇宿主模型通道容量失败，改模型投递仍待原生结果。W:13–14。 |
| T05 | E:2496→2497 | reviewer g13 指出真实 idle 是下游验收而非代码缺陷；run 紧接着启动 developer g13 rework。原 reviewer g13 报告、正式事件。 |
| T06 | E:2516→2517 | reviewer g14 明言 P1 不应再路由实现缺陷；仍启动 developer g14 rework。原 reviewer g14 报告、正式事件。 |
| T07 | E:2536→2537；E:2541/2547 | reviewer g15 指出 pair closure/archive 和真实 idle 属监工；仍启动 developer g15。之后两次重授权使 g16/g17 进入 rework，但不能算新 reviewer→developer 对。原 reviewer g15 报告、正式事件。 |
| T08 | E:2599→2600 | reviewer g16 再次指出生命周期未执行；run 启动 developer g18 rework。原 reviewer g16 报告、正式事件。 |
| T09 | E:2619→2620；E:2625–2627 | reviewer g17 指出非实现缺陷；run 启动 developer g19。g19 完成后启动 reviewer g18。原 reviewer g17 报告、正式事件。 |
| T10 | 正式快照 E:2641 后 | reviewer g18 独立报告 P0=0/P1=1/P2=0，建议 blocked_design，要求停止 developer 循环、由监工关闭归档任务对并采集真实 host idle pair。报告尚未进入正式 run 的已消费事件。原 reviewer g18 报告和状态 JSON、state.json。 |
| T11 | 2026-10-12 12:54–16:39 UTC，观察时间 | 诊断记录空转、engine gate、start 已确认但无 native continuation、provider result 存在却未绑定，以及停止派发 developer g20 的指令。W:15–22。 |

## PRD 第 17 节逐项证据索引

| 原表问题 | 支撑 | 证据边界 |
| --- | --- | --- |
| 心跳复读无动作；worker 完成不可靠唤醒/消费；idle 缺持久唤醒；ACTIVE、消息和 preflight 被当作进展 | T02、T04、T11；诊断报告“当前结论”“追加：终态工程失败丢失恢复” | 运行及诊断交叉支持；worker 完成至无人值守唤醒的精确时延待 A1 实测。 |
| resume 不能独立执行宿主工具；provider 结果未绑定；同任务 continuation 被判 blocked_unknown | T02、T11、W:20–21；诊断报告“追加：同一宿主回合的预检断点与公开恢复入口” | 现场及结果记录；不公开原生正文。 |
| 生命周期 P1 循环派回 developer | T05–T10 | 五对正式 sequence 加独立 Review；g18 尚未被 run 消费。 |
| 旧 route projection 否定 binding；engine attestation 漂移导致重授权 | T02–T03、T07；诊断报告“13:24 UTC 补记”“追加：continuation 绑定 profile 漂移 incident” | 重授权事件不是 Allow once 次数。 |
| 换班登记不闭环；多会话；full access 未继承；Allow once 反复出现；approval 自锁；不同班工具不一致 | T01–T03、18 个登记身份、W:4–12、用户现场报告 | 登记/自锁有记录；权限 profile 和弹窗缺逐次宿主账本，不宣称精确次数或每次同一授权动作。 |
| 会话状态与 run 健康混淆；重复轮询；换班重放成本；过期 handoff 或源码定位 | T02–T04、T11；诊断报告“13:24 UTC 补记”“追加：历史交付投影丢失”及 usage audit | 只有累计 cached/uncached/output 和来源覆盖；无等价不换班对照与价格，不推断净 token/费用节省。 |
| 晚到 timeout 覆盖结果；native error 丢细节；宽泛证据门；只报 blocker 不恢复 | 诊断报告“已复现并修复”1–4、“13:24 UTC 补记”、T02/T11 | 复现/修复测试与现场观察，不冒充正式 DAG 验收。 |
| 必须保留的安全与授权边界 | PRD 第 16 节、T10 | 不以 idle、UNKNOWN 或 Review 文本代替正式验收；不主张自动批准系统权限。 |

## A10 脱敏夹具：后续交付，不阻塞本次合并

本附件仅规定夹具合同，尚无可执行夹具，A10 未通过。后续开发从有权访问的原始 run 只读抽取：14 节点拓扑（锁定 12 个 accepted）、issue-156 任务对生命周期与 g18 Review 结论、缺失的 qualifying idle pair、下一动作及 integration-review 的 planned 依赖。用稳定假 ID 代替真实线程、handle、路径、授权摘要；不得包含 provider 原文、凭据或原始 usage。夹具需有输入摘要、脱敏规则、golden event sequence 和断言：不得再派 developer；先 pair closure/archive，再采真实 idle 证据与比例判定，随后 issue 验收、integration-review。

验收需先复现当前错误路由，修复后重放至合法终态或唯一真实人类边界；原 12 个 accepted 不回退，唯一 writer、独立 Review、授权和安全门不削弱。合成/脱敏 idle 输入只能测路由逻辑，不能代替真实 host idle beat 与相邻权威 usage/context 测量。A10 可随产品实现提交和审查，不作为本次仅交付 PRD/证据附件的合并门槛。
