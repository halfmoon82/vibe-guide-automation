# VibeGuide PRD：入口收严与监工自愈

状态：提案（A、B 两份规则清单已由产品经理 2026-10-17 确认；节点拆分待产品经理过目）
日期：2026-10-17
适用范围：新会话入口路由（vibe-entry 协议与 planner 路由）、已授权 run 内的监工打断与恢复

## 来源与优先级

| 简称 | 来源 | 地位 |
| --- | --- | --- |
| A | 产品经理 2026-10-17 确认的「进入复杂模式的条件」清单 | 本 PRD 规范性需求 |
| B | 产品经理 2026-10-17 确认的「监工只在四种情况打断」清单 | 本 PRD 规范性需求 |
| R | run-ee4575fa（熟小麦护身符 Q2/Q3/Q4）实测账本与事件流 | 问题证据 |
| E | `eval/decision/route.jsonl` 入口路由评测集（118 条真实判定） | 问题证据 |
| P | [监工整合 PRD](2026-10-13-supervisor-integrated-prd.md) 的 B1、N4 | 已批准判据，本 PRD 沿用不改 |

与 P 冲突时：B 的四种打断情况是 P 中 N4 两个责任层（`product_acceptance`、`external_authorization`）的展开，不新增第三个可打断类别；P 未覆盖的自愈细节以本 PRD 为准。

## 1. 问题

### 1.1 入口太松：确认消息被当成新复杂任务

评测集 E 中被判为 `complex` 的 24 条请求，大部分是对已有工作的推进，不是新任务：「授权」「选A」「3」「允许，发布吧」「批准，发布 5.0.10 并升级本机」「同意，开始规划」。

原因有两个：

- 确认、选择、续接类消息被重新做 S1 评分，评分对象变成了"它背后的整件事"；
- vibe-entry §2 写着「拿不准一律按 >15 处理——误进复杂无害」。

R 的账本说明误进复杂是有代价的：7 个节点的 run 中，监工 6 班有效成本 8.3M，所有开发与审查会话合计 6.9M，**协调成本占总成本 55%**。

### 1.2 监工卡点几乎全是工程问题，却打断了产品经理

R 的事件流统计：

| 卡点 | 次数 | 实际性质 |
| --- | --- | --- |
| 授权作废、要求产品经理重新授权 | 2 | 两次前后授权逐字段比对，只有 `digest`、`engine_authorization_digest`、`engine_evidence_ref` 三项变化；PRD、Spec、节点合同、授权范围、允许动作全部不变。第二次导致 run 停摆约 6.5 小时（issue #232）；第一次与派发超容量判拓扑漂移相关（issue #225） |
| 节点进入 `blocked_unknown` | 10 | 全部是「只能在真机微信验证」「临时验证脚本不入库」一类，开发阶段结构上不可能闭合 |
| 同一待验事项被重复处置 | 4 组 | 监工处置后，开发子代理措辞微调重报，被当成新事项再处置一次 |
| 重试 | 17 | 卡在以上两类问题上反复撞门 |
| 夜间空转 | — | 第 5 班 22:48→次日 07:52 每小时约 18 次调用，消耗监工总输入的 28%，期间基本在等待 |

## 2. 目标

1. 只有真正需要多会话并行和跨中断续跑的请求才进入复杂模式；确认、选择、续接类消息永远不会把会话推进复杂模式。
2. 已授权 run 内，只有 B 列明的四种情况能打断产品经理；其余可唯一判定的情况由监工自愈并留痕。
3. 合同未变时，授权永不因内部簿记、凭证续期或容量计数被作废。

## 3. 产品决策（已确认）

### 3.1 A：进入复杂模式的条件

| # | 规则 |
| --- | --- |
| A1 | 进入复杂模式必须**同时**满足：① 能拆出 3 个或以上可同时开工的节点；② 预计跨会话、跨天，或需要中断后续跑。S1 >15 是必要条件，不再是充分条件 |
| A2 | 确认、选择、"继续"、"发布"、"授权"、单个数字或选项字母这类消息只延续当前档位，不重新评分，不调用 `vibe plan --request` |
| A3 | 拿不准时按**轻规划**处理，不再按复杂处理。删除「误进复杂无害」的表述 |
| A4 | 产品经理明确要求走复杂流程时仍可显式升级（`--s1 5,5,5,5,5` 或等价方式），只升不降，此路径不受 A1 约束 |
| A5 | S1 >15 但不满足 A1 时，路由为轻规划，并在评分行注明未满足的条件，例如 `S1：17→轻规划（可并行节点不足 3）` |

### 3.2 B：监工只在四种情况打断产品经理

| # | 可打断情况 | 对应 P·N4 责任层 |
| --- | --- | --- |
| B1 | 产品范围或设计要变（按 P·B1 判据：修改后验收例子的判定结果会变） | `product_acceptance` |
| B2 | 部署或对外发布 | `external_authorization` |
| B3 | 涉及凭据或系统权限 | `external_authorization` |
| B4 | 授权卡上没列、且不可撤销的动作 | `external_authorization` |

除此之外一律自愈并记录，至少包括：

| # | 自愈规则 |
| --- | --- |
| H1 | **凭证续期不作废授权**：engine attestation 到期或换新时，只要 PRD、Spec、节点合同、能力合同、授权范围与允许动作的指纹全部不变，自动续上并沿用原授权，写一条续期事件；任一指纹变化才按现有规则作废 |
| H2 | **簿记错位自愈**：监工入口证据（monitor entry evidence）不一致、快照与事件流投影错位时，以事件流为准重记投影并继续，不写作废标记（issue #232 方案 A/B） |
| H3 | **容量计数自愈**：派发侧必须遵守有效容量上限；容量计数与实际活跃会话不一致时，以任务登记为准纠正并暂停新派发，不判拓扑漂移、不作废授权（issue #225） |
| H4 | **结构性待验项自动归类**：只能在真机、生产环境或验收期验证的事项，自动记为「验收期待验项」，不让节点进入 `blocked_unknown`；交付和验收汇报单列这些事项 |
| H5 | **同一待验事项去重**：已处置的事项被换个说法重报时，识别为同一事项并沿用原处置，不再计为新的未处置项 |
| H6 | **无事可做时降频**：run 内没有可服务事项、只在等待 worker 时，心跳降频；有新交付或唤醒信号时恢复 |

## 4. 成功标准

### 4.1 入口

- AC-A1：用评测集 E 重放入口判定，原标签为 `complex` 的 24 条中，属于确认/选择/续接类的消息全部不再路由到 `complex`（以 A2 的消息类型清单逐条判定，清单与结果附在交付证据中）。
- AC-A2：一条 S1 = 17、但只能拆出 2 个节点的请求，路由为轻规划，评分行带未满足条件说明。
- AC-A3：产品经理显式要求复杂流程时，仍进入复杂模式。
- AC-A4：vibe-entry 协议源文件与 planner 路由对同一请求给出相同档位（契约测试覆盖"协议写的 == 代码判的"）。

### 4.2 监工

- AC-B1：重放 R 的两次授权作废场景（凭证换新、入口证据错位），授权不被作废，run 自动继续，事件流有续期或纠偏记录。
- AC-B2：任一合同指纹真实变化时，授权照常作废（防止自愈放过真实变更）。
- AC-B3：开发子代理报告「需真机微信验证」类事项，节点不进入 `blocked_unknown`，交付汇报中列出该待验项。
- AC-B4：同一待验事项措辞改动后重报，不产生第二次处置事件。
- AC-B5：在 B1–B4 以外的情况下，监工不创建决策、不挂起等人；`supervisor-hold` 与 `decision-raise` 对其他类别报错拒绝。
- AC-B6：用 R 的事件流做回放统计，需要产品经理介入的次数从 2 次重新授权 + 10 次 blocked_unknown 降为 0。

## 5. 非目标

- 不改 S1 五维评分表本身和 8/15 两条分数线。
- 不取消授权卡和一次授权；不放宽 deploy、凭据、系统权限的单独授权。
- 不改变"未知状态不能当成成功"的原则：H4 只针对结构上无法在开发阶段验证的事项，并且必须进入验收汇报，不是把未知当作通过。
- 不在本 PRD 内处理换班策略、心跳间隔数值（#154、#166、#209 另行推进）；H6 只要求等待时降频，不定具体数值。
- 不处理 Codex 换班新会话的沙箱弹窗问题（#231、#219）。

## 6. 代码现状（只读分析）

| 位置 | 现状 | 来源标记 |
| --- | --- | --- |
| `vibe_guide/protocols/vibe-entry.md` §2 | 「拿不准一律按 >15 处理——误进复杂无害」；继续/确认只在 S0 白名单里列了「继续」「好的」 | system_inferred |
| `vibe_guide/planner.py` `classify_s0` / `_S1_MARKERS` | 只要出现「设计」「实现」「开发」「测试」「部署」等词就进 S1；`_route_result_from_score` 只看分数和升级标记 | system_inferred |
| `vibe_guide/cli.py` `_CONTRACT_DRIFT_ERROR_MARKERS` / `_persist_invalidation` | `blocked_by_execution_topology_mismatch` 与 `execution_engine_unverified` 整类命中即作废授权，作废原因硬编码为 `executable_contract_changed` | system_inferred |
| `vibe_guide/monitor.py` `_validate_execution_topology` | 入口证据不一致直接抛 `monitor entry evidence drift` | system_inferred |
| `vibe_guide/monitor.py` `blocked_unknown_disposition_required` 路径 | 任何未处置事项都让节点停在 `blocked_unknown`，按文本逐条比对 | system_inferred |
| `vibe_guide/decisions.py` `BOUNDARY_CATEGORIES` | 已只有两个责任层，B 可直接复用 | system_inferred |

## 7. 节点拆分（草案，待产品经理过目）

一共四件事，最后由 vibe 自动追加的整合审查收口。已发布为计划 `session-a1b1bccb7c2995e0`，远端 Git 动作允许（产品经理 2026-10-17 确认）。

| 节点 | 内容 | 依赖 | 主要文件 |
| --- | --- | --- | --- |
| `entry-tighten` | A1–A5：协议文本、planner 路由加"可并行节点数 / 跨中断"两个条件、确认类消息不重新评分、评测集消息做测试数据 | 无 | `vibe_guide/protocols/vibe-entry.md`、`vibe_guide/planner.py`、`vibe_guide/session_entry.py`、`vibe_guide/scanner.py`、`README.md`、`tests/test_entry_tightening.py` |
| `reauth-self-heal` | H1、H2：凭证续期与簿记错位不作废授权，真实合同变化仍作废 | 无 | `vibe_guide/cli.py`、`vibe_guide/monitor.py`、`vibe_guide/engine_attestation.py`、`vibe_guide/orphan_recovery.py`、`vibe_guide/state.py`、`tests/test_reauth_self_heal.py` |
| `capacity-self-heal` | H3：派发守容量上限，计数错位自愈 | `reauth-self-heal` | `vibe_guide/monitor.py`、`vibe_guide/task_registry.py`、`tests/test_capacity_self_heal.py` |
| `pending-verification` | H4、H5、H6 与 B 的打断白名单：待验项自动归类、去重、等待降频、hold/decision 类别校验 | `capacity-self-heal` | `vibe_guide/monitor.py`、`vibe_guide/decision_boundary.py`、`vibe_guide/decisions.py`、`vibe_guide/supervisor_wakeup.py`、`vibe_guide/protocols/prd-guide.md`、`tests/test_pending_verification.py` |

`entry-tighten` 与监工链路同时开工。三个监工节点都要改 `vibe_guide/monitor.py`，vibe 的 DAG 审计不允许可同时派发的节点写同一文件，因此按 `reauth-self-heal → capacity-self-heal → pending-verification` 串行。评测集 `eval/decision/route.jsonl` 尚未合入 main，`entry-tighten` 把其中 24 条 complex 消息做成 `tests/fixtures/entry_route_complex_messages.json` 随节点提交。

项目 `AGENTS.md` 中「>15 或拿不准时才 vibe scan」的措辞按 §7 规则只生成补丁建议，不在本计划内直接改写。
