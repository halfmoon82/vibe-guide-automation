# pm-skills 审阅层（pm-ai-shipping）与 vibeguide 的结合方案

> 状态：**待用户确认的方案稿**，尚未实施、未开分支、未改任何代码。
> 日期：2026-09-28｜vibeguide 版本：v4.9.1（main `9478145`）
> 上游：[phuryn/pm-skills](https://github.com/phuryn/pm-skills) v2.1.0，MIT，commit `8607e3b077817f89bf4a9b623246219734ac3be0`（2026-09-14）

---

## 0. 一句话结论

pm-skills 里真正该进 vibeguide 的只有 **3 个 skill**（`pm-ai-shipping` 插件），它们补的是
vibeguide **唯一一块结构性空白：reviewer 知道按什么分级，但不知道怎么审**。接进去需要先给
`vibe install` 加一个子路径能力（已实测证明现在装不进来），然后用两处协议文本 + 一个 AGENTS.md
分发块把方法论绑到现有的 P0–P3 判定上。**零业务逻辑改动，不 fork、不 vendor 上游源码。**

---

## 1. 证据基线（本方案的全部结论都出自这里）

本节所有条目为 2026-09-28 本机实测，非记忆、非上游 README 自述。

### 1.1 vibeguide 当前实现程度

| 事实 | 证据 |
|---|---|
| 代码规模 v4.9.1：`vibe_guide/` 22,508 行，`monitor.py` 5,145 行 | `wc -l vibe_guide/*.py` |
| **reviewer 的"判定与证据链"完备**：交付门校验 `in_session_review.protocol` 逐字匹配、`clearance{p0,p1,p2}` 必须是整数 0、`evidence_ref` 非空 | `vibe_guide/protocols/visible-sdd-worker.md:61-74`、`monitor.py:4080` |
| **reviewer 的"方法论"是空的**：`review.py` 全文 50 行，只做「按 root_cause 分桶 + 取最高严重度」，没有任何"如何发现缺陷"的内容 | `vibe_guide/review.py` 全文 |
| 全项目关于"怎么审"的文字**只有一句**：「按 P0（资金/数据正确性）、P1（逻辑缺陷/事务边界）、P2（边界条件/行为回归）、P3（建议）分级产出意见」 | `visible-sdd-worker.md:15`，AGENTS.md 同句 |
| 已有分发通道 1：`AGENTSMD_BLOCKS` 四个规则块 → `vibe apply-agentsmd --confirm` 合入项目 AGENTS.md，每块自带 MARKER + 版本化 SENTINEL | `scanner.py:196-262` |
| 已有分发通道 2：协议物化到 `.vibe/proposals/skills/<name>/SKILL.md`，**写一次永不改写**（现有 prd-guide、vibe-entry） | `initializer.py:334-360` |
| 已有分发通道 3：`vibe install` → `$VIBE_HOME/skills/<name>` + `.vibe/config.json` 记 `{name, source, commit}` | `skills.py:316-391` |
| 已有分发通道 4：visible-sdd 节点合同携带 `sdd_protocol` 指针 | `monitor.py:2943-2944` |

### 1.2 pm-ai-shipping 实际内容（clone 后实读）

| skill | 行数/字节 | 内容 |
|---|---|---|
| `code-review` | 253 行 / 14.5KB + 3 个 references（correctness-taxonomy 144 行、security 68 行、performance 55 行） | 以 correctness 为核心引擎，perf/security 为同引擎子案例 |
| `intended-vs-implemented` | 42 行 / 3.8KB | 审「文档声称」与「代码实际」之差 |
| `shipping-artifacts` | 79 行 / 8.8KB | 定义 `documentation/` 意图文档集，是上一条的前置输入 |
| 合计 | **641 行 / 43KB ≈ 11k tokens**；reviewer 实际常用的核心（code-review SKILL + taxonomy）约 23KB ≈ 6k tokens | |

**可移植性实测**：`pm-ai-shipping` 里 `$ARGUMENTS`（Claude 专有插值）只出现在 `commands/` 的 5 个文件中，
**3 个 SKILL.md 一处都没有**。即这三个是全仓 69 个 skill 里最干净、最容易跨宿主复用的一批，符合 vibeguide
"统一 CLI 连接多 Agent"的初衷。

### 1.3 已实测确认的阻断点

```
vibe install（fetch=True，真实网络）→ status: pending, installed: False
  ├─ clone 成功、SHA 校验通过（vendor cloned: True）
  └─ 失败点唯一：skills.py:257 `manifest = stage / 'SKILL.md'`
     pm-skills 仓库根**没有** SKILL.md（它是 9 plugin × 69 skill 的 monorepo）
     实测：整棵树正常物化出 .claude-plugin/ AGENTS.md pm-ai-shipping/ …，只差根 manifest
```

**即：今天这 3 个 skill 装不进 vibeguide。** 这不是配置问题，是安装器只支持"仓库根即 skill"一种形状。

同时实测到解法可行：

```
git archive <sha>:pm-ai-shipping/skills/code-review  →  SKILL.md references/ …
```

子路径归档出来 **SKILL.md 正好落在归档根**，现有 manifest 检查和 `_safe_archive_members`
一行都不用改（git 2.50.1 实测）。

### 1.4 顺带发现的既有缺陷（本方案不修，只登记）

`VISIBLE_SDD_PROTOCOL_REF = "vibe_guide/protocols/visible-sdd-worker.md"`（`monitor.py:120`）是一个
**只在 vibeguide 自己仓库里解析得到的相对路径**。`initializer.py` 只物化 prd-guide 与 vibe-entry，
**从不物化 visible-sdd-worker.md**。所以在消费项目里，worker 被指向一个它读不到的文件，而交付门
仍然要求 `protocol` 逐字等于该字符串——协议内容事实上没有送达，只有字符串对上了。

这直接影响本方案的落点选择（见 §3 阶段 2c：方法论引用必须同时进 AGENTS.md 分发块，不能只写在协议文件里）。
建议单开 issue 处理物化缺失本身。

---

## 2. 这个 skill 到底效果如何（不吹不贬的评估）

### 2.1 好在哪：它和 vibeguide 踩过的坑是同一套哲学的更完整表述

`code-review` 的核心主张不是清单，是一个判据：**真缺陷几乎不在单个文件内，而在两个参与者之间的
"约定"上**（调用方↔被调方、生产者↔消费者、现在写入↔之后读取、两个本该产生同一状态的分支）。
按文件切分的审查看不见它们，因为两半从不同时在视野里。

逐条对上 vibeguide 自己写在 AGENTS.md 和失败记忆里的规则：

| code-review 的规则（原文要点） | vibeguide 已有的同源规则 |
|---|---|
| 跨边界缺陷必须**同时引用两侧**参与者，禁止只按文件分组 | AGENTS.md「跨模块约定必须有唯一真相来源」「测试必须覆盖跨模块契约，不只覆盖单模块逻辑」 |
| "Absorption is not prevention"——下游有缓存/重试/默认值恰好掩盖了缺陷，**不算反驳**；只有让执行**不可能**的机制才算 | 记忆《修之前先确认那段代码真的可达》《别借用为另一个文件写的判定》 |
| 测试通过、代码眼生、命名可疑、缺测试——**都只是待查线索，都不是证据，也都不是反驳** | 记忆《Reviewer 意见须实测复现》 |
| 第三种结局 `Unresolved`：关键契约或运行时事实未知时，**与 findings 分开列**，不算发现也不算通过 | AGENTS.md「未知状态不能当成无事项或成功」「记录 `blocked_unknown`」 |
| 明确告诉 review worker "你在读不在改"，并在收尾确认工作树未变 | `visible-sdd-worker.md` §2 git 写禁令清单、§4 reviewer 独立性违规 |
| "It works nearly always" 描述的是竞态，不是反驳 | AGENTS.md「95% 不够，必须追求极致可靠」 |

最有价值的一条是 **refutation 门**：一个候选要成为 finding，必须同时具备①有依据的约定 ②可行的执行
③具体矛盾 ④可观察后果 ⑤**已经检查过的最强反论**。这条恰好补上 vibeguide 现在最薄的一环——
`clearance {p0:0,p1:0,p2:0}` 目前只校验"数字是 0"，谁都能报 0，也谁都能靠堆未经反驳的疑点把返工拖到 3 轮上限。

它还点名了两类"强 agent 几乎从不上报"的缺陷（来自一次真实代码库植入缺陷的评测）：
**权威值调和**（请求值 ≠ 生效值，下游还在读请求值）和 **身份/关联**（结果找不回发起实体）。
这两类正是 vibeguide 自己 run log 里反复出现的形态。

### 2.2 不好在哪 / 不匹配处（必须在方案里处理，不能装看不见）

1. **它是静态审查，达不到 vibeguide 的证据标准。** 原文自己写明「A static review produces
   code-review findings, not confirmed exploits or measured regressions」。而 vibeguide 的 P0 是
   资金/数据正确性、要求实测复现。→ 两者是**组合关系不是替代关系**：skill 负责"找得准、不瞎报"，
   vibeguide 的"验证命令须为绿"仍然是独立一道门。方案里必须写死这一点。
2. **它不定义严重度刻度**（Report 契约里只有 `[Severity]` 占位）。→ 这其实是好事：P0–P3 可以直接绑进去，
   不冲突；但**必须外部给出映射表**，否则 agent 会按英文语感自定级别。
3. **Parallelism 一节让 reviewer 自己 fan out 子 worker、要求跑最强模型、禁止混模型。**
   vibeguide 有 `model_router.py` 和 `max_active_worker_sessions` 并发预算，而会话内嵌套子代理
   **不计入那个预算**。→ 成本可能失控。方案里必须显式封顶"一层，或直接串行"。
4. **`intended-vs-implemented` 要求存在 `documentation/*.md` 意图文档集**，否则按它自己的规则
   "文档缺失本身就是第一条发现"——vibeguide 项目没有这套文档，会导致**每个节点第一条发现都是同一句废话**。
   → 必须重新锚定：vibeguide 的**节点合同**（输入/输出/错误行为/验收示例，由 `node_spec.py` 产出、机器生成、必然存在）
   就是天然的 documented intent。重锚之后这个 skill 变成"审代码是否兑现了节点合同"，反而比原始形态更贴。
5. **全英文**，vibeguide 协议全中文。worker 读英文无障碍，但严重度映射必须用中文写死。
6. **69 个里只有这 3 个属于审阅层**，其余 66 个是产品方法论。不要因为这 3 个好就整包塞进 reviewer 上下文
   （43KB 已经不算小，整包会挤掉 diff 的位置）。

### 2.3 评估结论

这 3 个 skill 值得接，但**只值得接进 reviewer 这一个位置**，且需要 4 处适配（严重度映射、
intent 重锚到节点合同、fan-out 封顶、静态/实测分工写明）。它不是"顺手能用"，是"方法论层严丝合缝，
接口层要改 4 处"。

---

## 3. 结合方案

### 设计约束（来自 vibeguide 自己的规则，不可违反）

- AGENTS.md §7：**不把 Skill 源码复制进本项目**，只记来源与 SHA → 不 fork、不 vendor。
- prd-guide §0：**agent 出内容，vibe 出协议 + 校验** → 方法论属"内容"，归 agent 侧 skill；vibe 只加引用与校验。
- AGENTS.md §1：统一 CLI 连接多 Agent → 不走 Claude marketplace 分发路径。
- K2/K3：最简实现、只改必须改的 → 不重构 `review.py`，不动事件字段，不加 contract 字段。

### 阶段 0（前置，必做）：给 `vibe install` 加子路径能力

**为什么不能绕过**：唯一的替代是 fork pm-skills、把 3 个 skill 提到仓库根——那等于复制 Skill 源码
（违 §7），且要自己永久跟上游。加 subdir 是一次性的、对**所有** monorepo 型 skill 生效的通用能力。

**改动（估 ~80 行）**
- `SkillSpec` 增加可选字段 `subdir`（默认空，旧行为完全不变）。
- `_materialize_commit(vendor, commit, stage, subdir='')`：归档 spec 由 `commit` 变为 `f'{commit}:{subdir}'`。
  **已实测**：子路径归档的根就是 SKILL.md 所在层，`_safe_archive_members` 与 `skills.py:257` 的 manifest
  检查一行都不用改。
- `subdir` 入口校验：拒绝绝对路径、拒绝含 `..`、拒绝以 `/` 开头结尾、只允许 `[A-Za-z0-9_.-]` 与 `/`——
  与 `_github_path` / `_safe_archive_members` 同级的 fail-closed 风格。
- `.vibe/config.json` 的 skill 记录增加 `subdir`；`installed_tree_sha256` 仍覆盖实际物化树（不变）。
- 一个仓库的多个子目录 = 多条独立 skill 记录（`name` 不同，`source`+`commit` 相同）。

**测试先行（红→绿）**
1. 无根 SKILL.md 的仓库 + 合法 subdir → `installed`（今天是 `pending`，已实测）。
2. `subdir` 含 `..` / 绝对路径 / 符号链接目标 → `pending`，不落盘、不写记录。
3. 不带 `subdir` 的既有安装路径行为逐字不变（回归）。
4. 同仓多 subdir 装成多条记录，`vibe scan` 全部报 valid。

**规模**：1 个 PR，S1 估 10（轻规划）。

### 阶段 1：只装 3 个，不装 69 个

```bash
vibe install pm-ai-shipping/skills/code-review           # subdir
vibe install pm-ai-shipping/skills/intended-vs-implemented
vibe install pm-ai-shipping/skills/shipping-artifacts     # 可延后，见阶段 3
```
三条记录同锁 commit `8607e3b…`。**不装** `commands/*.md`（Claude 专有、含 `$ARGUMENTS`）、
**不装** `.claude-plugin/plugin.json`（marketplace 打包元数据）、**不装**其余 66 个产品方法论 skill。

**验收**：`.vibe/config.json` 出现 3 条记录；`vibe scan` 三条均 valid；
`$VIBE_HOME/skills/` 下三个目录各含 SKILL.md（`code-review` 另含 references/ 三个文件）。

### 阶段 2（核心）：把方法论绑进 reviewer——三处文本，零业务逻辑

**2a. `visible-sdd-worker.md` §1.2 扩写**（现在是一句话）

在"按 P0–P3 分级产出意见"之外补三条：
- 审查方法按 `code-review` skill 的 agreement 引擎推进：先确定跨边界约定 → 构造违反该约定的可行执行
  → 完成反驳 → 才算一条 finding。按文件逐个看不算审查。
- **节点合同**（输入/输出/错误行为/验收示例）就是本节点的 documented intent；按
  `intended-vs-implemented` 的方法审「合同声称」与「代码实际」之差，两侧都要引用到行。
  （不使用该 skill 原文的 `documentation/*.md` 锚点。）
- **fan-out 上限一层**；会话内不得再派子子代理，规模小时直接串行。审查子代理沿用 §2 的 git 写禁令清单。

**2b. 新增严重度映射表**（写进同一协议，中文，这是把两套契约焊死的那一块）

| code-review 的 finding 字段 | vibeguide 落点 |
|---|---|
| Expectation + Evidence | 被违反的约定 = 节点合同条款 / 跨模块契约 |
| Trigger + Defect | 复现路径；**vibeguide 要求实测复现，不接受纯静态推断**（静态审查产出的是候选，不是结论） |
| Impact | 决定 P 级：资金/数据正确性 → P0；逻辑缺陷/事务边界 → P1；边界条件/行为回归 → P2；其余 → P3 |
| Refutation | **必填**。未完成反驳的候选不得计入 P0–P2（只能落 P3 或 Unresolved） |
| Unresolved | 落 `blocked_unknown`，既不计入 clearance 也不算通过 |
| Coverage | 写进 §3 证据链；"零发现"必须表述为"在已审范围内无成立发现"，不得表述为"无缺陷" |

**这一步的真实收益**：`clearance {p0:0,p1:0,p2:0}` 今天只校验"数字是 0"。绑上"未反驳不得计 P0–P2"
之后，0 才第一次有了含义；同时堵住 reviewer 靠堆未验证疑点把返工拖满 3 轮的路径。

**2c. 新增第 5 个 AGENTS.md 分发块 `## Review Methodology`**（`scanner.py` `AGENTSMD_BLOCKS`）

**为什么必须有这一块**：§1.4 已证明消费项目拿不到 `vibe_guide/protocols/visible-sdd-worker.md`。
唯一可靠送达消费项目的通道是 AGENTS.md 分发块。

块内容 4–5 行，只写指针与三条硬规则：指向 `.vibe/proposals/skills/code-review/SKILL.md`；
节点合同即 intent；未反驳不计 P0–P2；未知落 `blocked_unknown`。
按 v4.9 既有规范同时提供 `REVIEW_METHODOLOGY_MARKER` 与版本化
`REVIEW_METHODOLOGY_CURRENT_SENTINEL`（教训来源：PR #81「证明块存在的标记本身也要版本化」；
以及 v4.9 finding「判定不得依赖英文头部套话」——sentinel 取中文正文片段）。

**测试**：`missing_agentsmd_blocks` 对新块的存在性判定；纯中文 AGENTS.md 不误判；
前 4 块已应用时新块仍可达（现有逐块判定已保证，补回归用例）。

**规模**：1 个 PR，约 120 行，几乎全是文本 + 测试。S1 估 11（轻规划）。

### 阶段 3（建议延后）：验收侧接 shipping-artifacts

把 `shipping-artifacts` / `derive-tests` 的意图文档集与测试覆盖图纳入 `.vibe/runs/` 的验收证据要求。

**建议延后的理由**：它要求项目具备一整套 `documentation/`，对存量项目是一次大改造；而阶段 2 已用
**节点合同**替代了 intent 来源，收益的大头已经拿到。等阶段 2 在 1–2 个真实 run 上跑过，再用实际数据
判断是否值得。

### 阶段 4（另一条线，不在本方案范围）

前段 66 个产品方法论 skill → `prd-guide` §2 五话题的引用补丁（2026-09-28 前两轮讨论的那条）。
**建议与本方案分开做**：受众不同（阶段 2 面向 reviewer 子代理，阶段 4 面向与用户对话的 agent）、
失败代价不同（阶段 4 影响产品决策质量，阶段 2 影响代码正确性判定）。可以并行推进，但不要合成一个 PR。

---

## 4. 明确不做什么

- 不 fork、不 vendor pm-skills 任何文本进 `vibe_guide/`（§7）。
- 不改 `review.py` 的 bundling 逻辑——它负责聚合，不负责方法论（K3 只改必须改的）。
- 不新增事件字段，因此不触碰 `state._EVENT_DATA_KEYS`（记忆：允许名单外的事件键落盘时被静默丢弃）。
- 不给节点 contract 加字段，避免 contract digest 漂移触发 fail-closed（`monitor.py` 交付门）。
  阶段 2 全部是协议文本 + AGENTS.md 块，不进 digest。
- 不自动追踪上游 main：config.json 锁 SHA，升级走一次显式 `vibe install` 新 SHA。
- 不把这 3 个 skill 装成 Claude marketplace plugin（会把能力绑死在单一宿主，违 §1）。

## 5. 风险

| 风险 | 处置 |
|---|---|
| 上游迭代导致方法论漂移 | SHA 锁定 + Freshness: Review-on-use；升级是显式动作 |
| reviewer 上下文变大（+6k tokens） | 只装 3 个不装 69 个；references 按需读（skill 自身设计就是"选中子案例才读对应 reference"） |
| 会话内嵌套 fan-out 绕过并发预算 | 阶段 2a 写死"一层封顶" |
| agent 按英文语感自定严重度 | 阶段 2b 中文映射表为唯一判据 |
| 静态发现被当成实测结论 | 阶段 2b 写死"静态产出是候选"；vibeguide 的验证命令门不放宽 |
| 与 Claude Code 内置 `/code-review` 同名 | 引用一律用完整路径 `.vibe/proposals/skills/code-review/SKILL.md`，不用裸 slash 名 |

## 6. 未验证项（按能力合同如实登记）

- 阶段 0 的 `subdir` 代码**尚未编写**；`git archive <sha>:<subdir>` 的行为已在本机 git 2.50.1 实测通过，
  但接进 `install_skill` 全链路后的行为未验证。
- 上游 commit `8607e3b` 之后的变化未跟踪。
- 本方案**未在任何真实 DAG run 上验证过收益**；阶段 2 完成后需要至少 1 个真实节点跑通才能给出效果结论。
- `intended-vs-implemented` 重锚到节点合同的实际效果**为设计推断，未实测**。
