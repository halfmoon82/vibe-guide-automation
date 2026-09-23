# 产品决策卡：新会话 S0/S1 入口接线（形态 C）

状态：用户已确认（2026-09-23，含外部用户无 complex-task-methodology 的自包含要求）
背景：V4.5 已交付工程入口（session_entry.py，由 `vibe plan --request` 调用），但 agent 侧接线从未物化——新会话看不到 S0/S1。知识库定论见 wiki/vibe-guide/s0-s1-entry-trigger-chain.md。本卡锁定形态 C（入口 SKILL.md + AGENTS.md 指针块）的剩余产品决策，确认后进入 PRD/Spec/Issue/DAG。

## D1 形态（已确认）

C = A+B：随包发布入口 SKILL.md（轻量自评协议，见修订后 D3），AGENTS.md 提案新增一行强制指针块（保证自动可见）。

## D2 载体与命名（建议默认，可改）

- 协议源文件随包：`vibe_guide/protocols/vibe-entry.md`（与 prd-guide 同机制、同目录）。
- init 物化到：`.vibe/proposals/skills/vibe-entry/SKILL.md`；**物化后永不被 init 改写**（用户改动在 re-init 后存活，与 prd-guide 行为一致）。
- AGENTS.md 块标题：决策卡草案为 `## 新会话入口（Vibe Guide）`；**落地实现为英文标题 `## New Session Entry`**（marker 同名，与既有两块 `Capability and Tool Truth`/`Complex Request Entry` 的英文标题风格一致，2026-09-23 实现期决定、此处补录留痕），走现有三态提案机制（proposal / pending-update / offered-sections），由 `vibe apply-agentsmd --confirm` 合入；reviewer 在提案里删掉该块 = 拒绝，与现有语义一致。

## D3 入口协议内容范围（SKILL.md 写什么）——轻量版（2026-09-23 按用户决定修订）

- **自评前置，不逢任务必过 vibe**：agent 在会话内做 S0/S1 自评，零 CLI 成本。**SKILL.md 完全自包含**：内嵌 S0 白名单要旨 + vibe planner 五维速查表 + 阈值 + 动态升级规则，fresh 机器只装 vibe 即可完整执行入口协议；`complex-task-methodology` 仅是可选增强指针（宿主已装时可参阅其完整评分表），绝不构成依赖——协议任何一步不得要求该技能存在。
- **只有自评确认为复杂（>15）才进入 vibe**：`vibe scan`（只读，确认项目态）→ `vibe plan --request <请求> --s1 a,b,c,d,e`（显式传入五维分数，planner 校验并正式路由）。<=8 直接执行、9-15 轻规划，均不触碰 vibe。
- **五维以 vibe planner 为准**（steps/domains/uncertainty/failure_cost/toolchain，喂给 `--s1`）；本机技能第五维「读码深度」语义不同，自评时近似对应即可，正式路由以 planner 输出为准。
- **防呆方向**：拿不准一律进 `vibe plan`（误进复杂无害，误放简单危险）；连续失败/实际步骤远超预估时动态升级重评。
- 阈值与维度不重定义，引用 planner 现有契约为唯一真相源；短提示协议与「会话门阻塞必须停下报告、不得伪造或跳过」纳入协议。
- 明确不教：不教绕过门禁、不教手写 plan 字段、不教任何写操作快捷方式；Git 远端动作/发布/生产变更的授权纪律与任务复杂度无关，始终生效。

## D4 覆盖项目与生效路径

- 随下一版本（建议 4.6.0）发布后，4 个已初始化项目各自执行：`vibe init --confirm`（幂等物化 SKILL.md + 生成 AGENTS.md 提案增量）→ 人工审提案 → `vibe apply-agentsmd --confirm`。
- 每个项目的人工确认不省略、不批量代确认；vibeguide 本仓库是否初始化自身另行决定（当前无 .vibe）。

## D5 验收标准（先写测试）

- 单测/契约测试：init 物化幂等（不改用户已改的 SKILL.md）；AGENTS.md 块检测/合入/拒绝三态；prd-guide 既有行为零回归；全量测试绿。
- **自包含契约测试**：fixture 级断言 SKILL.md 内含完整五维速查表与阈值、全文不强制引用任何外部技能（`complex-task-methodology` 仅以「可选」措辞出现）。
- **真实验证（不可 mock 替代）**：发布后至少在 1 个真实项目用真实 agent 开新会话，验证两条路径：① 简单请求 → agent 自评后不触碰 vibe；② 复杂请求 → agent 自评 >15 后执行 `vibe scan` + `vibe plan --request --s1`。未验证则交付报告如实标注「真实会话未验」。

## D6 明确不做

- 不改 S0/S1 阈值、路由逻辑或 plan CLI 行为；不做编辑器/平台 hook；不自动合入 AGENTS.md（proposal-only 原则不动）；deploy/发布仍是独立授权。

## D7 版本边界

- 本改造进 main 后随 4.6.0 发布；发布动作（tag/Release/资产）需届时单独授权，不在本卡范围。
