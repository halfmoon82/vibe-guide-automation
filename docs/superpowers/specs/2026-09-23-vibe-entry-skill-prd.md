# PRD：新会话 S0/S1 入口接线（vibe-entry）

状态：已验收完成（2026-09-23；PR #53/#54/#56 已合并，授权卡 vibe-entry-skill-auth-1 status=complete）
上游合同：[2026-09-23-vibe-entry-skill-decision-card.md](2026-09-23-vibe-entry-skill-decision-card.md)（用户已确认，形态 C + 自包含要求）

## 背景与问题

V4.5 已交付 S0/S1 工程入口（session_entry.py，由 `vibe plan --request` 调用），但 agent 侧接线从未物化：包里唯一的 agent-facing 产物是 prd-guide.md（管 PRD 业务字段），AGENTS.md 提案块只有 Capability/Tool Truth 与 Complex Request Entry（后者在 4 个项目里均躺在 pending-update 未合入）。结果：新会话里的 agent 不知道要调 vibe，S0/S1 对用户不可见。

## 目标

新会话中的 agent 在**不逢任务必过 vibe** 的前提下，能自动发现并执行轻量入口：会话内自评 S0/S1（零 CLI 成本），仅自评确认复杂（>15）时进入 vibe scan + vibe plan 正式路由。

## 用户故事与验收

| AC | 内容 | 验证 |
|---|---|---|
| AC-01 | fresh 机器只装 vibe 的 agent 能仅凭物化的 SKILL.md 完整执行入口自评（五维速查表、阈值、动态升级规则齐备，不依赖任何外部技能） | 契约测试断言自包含 + 真实会话验证① |
| AC-02 | 简单/轻量请求（≤15）：agent 自评后直接执行或轻规划，不触碰 vibe | 真实会话验证① |
| AC-03 | 复杂请求（>15 或拿不准）：agent 执行 `vibe scan` 后 `vibe plan --request --s1` 进入正式路由 | 真实会话验证② |
| AC-04 | AGENTS.md 提案出现「新会话入口」指针块；reviewer 删除该块 = 拒绝且重 init 不复活（三态语义与现有块一致） | 单测 |
| AC-05 | init 物化 `.vibe/proposals/skills/vibe-entry/SKILL.md`，幂等且永不覆盖用户改动；prd-guide 行为零回归 | 单测 |
| AC-06 | 全量测试绿；发布仍走独立授权（4.6.0） | CI/本地 |

## 非目标

不改 S0/S1 阈值/维度/路由逻辑；不改 plan CLI 行为语义；不做平台 hook；不自动合入 AGENTS.md；不代替用户确认任何项目的提案合入。

## 关键产品语义（钉死）

- 五维以 vibe planner 为准（steps/domains/uncertainty/failure_cost/toolchain）；`complex-task-methodology` 仅为可选参考指针，协议任何一步不得要求其存在。
- 防呆方向：拿不准一律进 `vibe plan`；连续失败或步骤远超预估时动态升级重评。
- Git 远端动作/发布/生产的授权纪律与任务路由无关，始终生效。
