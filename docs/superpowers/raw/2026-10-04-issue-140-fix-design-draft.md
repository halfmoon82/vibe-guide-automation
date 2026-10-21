# Issue #140 修复设计稿（草案，待产品确认）

> 来源：issue #140「review 六道门的检查面全部是『形状』与『自报』，不含环境事实」
> 知识库：`wiki/vibe-guide/review-gates-structural-blindspot.md`（命中且有效）
> 状态：**草案**——尚未进入 PRD/Spec，待产品经理对下列取舍拍板。

## 0. 问题复述（一句话）

六道门全部只校验「形状/自报」，没有任何一道门读得到**环境事实**（第三方库实装版本下的 API 契约），也没有任何一道门要求「目标交互动作跑通」；因此「实现写得对、但用到的第三方 API 在本版本不存在」这类缺陷**结构上必然穿门**。

## 1. 修复总思路

盲区的根因是「每一层都对，但没有一层为跨层的环境事实负责」。修复不是再加一道形状门，而是把**环境事实变成合同的一等公民**，并让它顺着既有的六道门流动：

```
合同声明环境事实（门1 校验形状）
  → worker 派发提示强制携带（门3「约定」的来源扩大）
    → reviewer 拿得到事实（门4「可行执行」有了外部依据）
      → 行为类不变量由引擎真执行 test_command（门6 之前的事实证据）
```

四条改动互相咬合，只做其中一条都会留洞（例如只改合同不改派发协议，事实到不了 reviewer 手里）。

## 2. 六项建议的处置与落点

| # | issue 建议 | 处置 | 落点 |
|---|---|---|---|
| 1 | 环境事实写进合同 | **采纳**（核心） | `contracts.py` schema + `check_contract_closure()` |
| 2 | 行为类不变量强制可执行 DOM 级验证 | **部分采纳**（降为机制，不指定 jsdom） | 见 §3.3「行为级 invariant 标记」 |
| 3 | 真正执行 `test_command` | **采纳**（监工侧执行，非自报） | `monitor.py` acceptance 前置步骤 |
| 4 | `entrypoint` 符号校验多语言化 | **采纳** | `brief.py:132` |
| 5 | 派发提示模板化「环境事实与已知缺陷」 | **采纳** | `protocols/visible-sdd-worker.md` |
| 6 | 验证清单纪律（删「页面能打开」） | **采纳**（纯协议文本） | `protocols/visible-sdd-worker.md` + PRD 引导协议 |
| 附 | `test_command`/`expected_red` 零执行 | 随 #3 一并解决 | 同 #3 |
| 附 | payload 形状门（evidence_ref 不打开） | **本 issue 不修**，另立 issue | 见 §6 范围外 |

## 3. 具体设计

### 3.1 合同新增 `environment_facts` 字段（建议 1）

`IssueContract` 增加可选字段（`from_mapping` 目前 `set(data) - required - optional` 严格拒绝未知键，加进 `optional` 即向后兼容）：

```python
environment_facts: List[Dict[str, str]] = field(default_factory=list)
# 每项: {"fact": "element-plus 实装 2.3.7，el-radio 无 value prop（2.6.0 才引入）",
#        "source": "node_modules/element-plus/package.json + components/radio/src/radio.ts",
#        "verified_at": "2026-10-04"}
```

- `check_contract_closure()` 新增形状校验：每项必须有非空 `fact` 与 `source`；`source` 指向仓内路径时校验文件存在。
- **触发规则**（写进 PRD 引导协议与合同校验警告）：`owned_paths`/`call_chain` 涉及第三方 UI 组件用法变更时，`environment_facts` 为空 → 合同闭环**不通过**（fail）还是**警告**（warn）是一个产品取舍点，见 §5 待决项 A。
- 门 3 的「约定」定义同步改：`protocols/visible-sdd-worker.md` §2.5 第 1 步改为「合同 + 合同声明的 `environment_facts` + AGENTS.md + 邻近约定」。

**为什么这是一句话抬高两道门**：门 3 的「约定来源」从合同内自洽扩到合同+环境；门 4 的「可行执行」拿到了它原本缺的外部事实——reviewer 不再需要自己猜 2.3.7 的 props。

### 3.2 派发提示模板化（建议 5）

`visible-sdd-worker.md` 第 2 节（派发提示要求）增加强制一节：

> **环境事实与已知缺陷**：派发 review 子代理前，worker 必须将节点合同的 `environment_facts` **逐项原文**写入派发提示；合同该字段为空且本次改动不涉及第三方组件时，必须显式写「本节点无环境事实声明」。**缺失本节的派发视为 review 材料不全，该轮审查结论不得作为 clearance 证据。**

配套：交付 payload 的 `in_session_review` 增加 `environment_facts_ref`（字符串，允许 `"none"`），监工门校验其存在——这让「派发时有没有带事实」从不可见变为可审计（仍是自报，但漏报会变成形状门可拦的缺失）。

### 3.3 引擎真执行 `test_command`（建议 3 + 附带项 1）

现状：`test_command`/`expected_red` 全包零执行，填 `vue-tsc` 全绿也能过。

设计：**监工在 acceptance 前置一步 `contract_test_execution`**：

1. 节点进入交付验收前，监工在该节点的 **writer worktree**（合同指定、唯一 writer）内逐条执行 invariants 的 `test_command`；
2. 超时（默认 120s，合同可声明上限）、非零退出码 → 记 `blocked_unknown` + 拒收，不伪造通过；
3. 退出码 0 → 把 `{command, exit_code, output_tail, executed_at, worktree}` 写入 `events.jsonl`，作为交付证据的一部分；
4. **安全边界**：`test_command` 来自已经用户授权卡授权的合同，执行范围限定合同 writer worktree，与既有授权语义一致，不新增授权面；执行前在授权卡摘要中显式列出将执行的命令清单（透明化）。
5. `expected_red` 同步获得语义：TDD 节点在实现前由监工执行一次 `test_command` 并**要求失败**（红），实现后要求通过（绿）；两次结果都落事件日志。这同时补掉「expected_red 零执行」。

**行为级 invariant 标记**（建议 2 的落地形式）：invariant 增加可选 `"kind": "behavioral"`。标记为 behavioral 的 invariant，其 `test_command` 不得是 `vue-tsc`/`tsc`/`vite build` 等纯编译构建命令（黑名单可配置），必须是能执行交互断言的命令（jsdom/vitest/playwright 等由项目自选——**机制强制、工具不指定**，回应 issue「假定可跑 jsdom 未验证」的保留意见）。同时在协议与 PRD 引导中写明：**编译/构建绿不构成行为证据**。

### 3.4 `entrypoint` 符号校验多语言化（建议 4）

`brief.py:132` 目前 `("def " + symbol.split(".", 1)[0]) not in source` 对前端恒失败。改为按扩展名分派：

- `.py`：`def <sym>` 或 `class <sym>`（保持现有严格度）；
- `.ts/.tsx/.js/.jsx`：`export (const|function|class|interface|type) <sym>` 或 `function <sym>`；
- `.vue`：`<sym>` 作为词出现在 `<script` 块内（覆盖 `defineExpose`、顶层 `const`）；
- 未知扩展名：退化为「符号基名作为独立词出现在源码中」，并在 evidence 里记录降级，**不再静默只剩文件存在**。

### 3.5 验证清单纪律（建议 6）

- `visible-sdd-worker.md` 增加硬规则：**「页面能打开/组件在产物里/路由可解析」不得作为验收项**；验收必须表述为「目标交互动作跑通」（具体输入 → 预期行为）。
- PRD 引导协议（`protocols/prd-guide.md`）的验收示例同步更新，给出反例（页面能打开 ✅→❌）与正例（点击「按生成数量」后仅该项选中且可提交）。

### 3.6 `blocked_unknown` 收口门（issue 隐含、知识库已点名）

现状：`blocked_unknown` 不进 clearance 三计数，reviewer「无法确认 2.3.7 的 props」时这条通道恰好是缺陷的合法逃生口。

设计：`in_session_review` payload 增加 `blocked_unknowns: []`（可空数组，缺省视为空）；**非空时监工不得直接 acceptance**，必须产生一条人工/监工处置事件（逐项：已确认无虞 / 已转 P0–P2 返工 / 已补环境事实后复审）后方可继续。这让「拿不准」从静默降级变为必须显式收口。

## 4. 实施分期建议

| 期 | 内容 | 理由 |
|---|---|---|
| P1 | §3.1 合同字段 + §3.2 派发模板 + §3.5 验证纪律 | 纯 schema + 协议文本，风险最低，直接堵住实证缺陷的穿门路径 |
| P2 | §3.4 符号校验多语言化 + §3.6 blocked_unknown 收口 | 独立小改，可并行 |
| P3 | §3.3 引擎执行 test_command/expected_red | 最重（监工新增执行器、超时、授权卡清单），需独立测试先行 |

每期均可独立交付、独立验证；P1 单独上线即能让 issue 的实证案例（element-plus `:value`）在门 3/门 4 被拦住。

## 5. 待产品经理拍板的取舍（进入 PRD 前必须闭合）

- **A. 环境事实缺失时的严格度**：涉及第三方 UI 组件但 `environment_facts` 为空 → 合同直接 fail，还是先 warn 一个版本再 fail？（fail 更堵洞，但会给存量流程加摩擦）
- **B. `test_command` 引擎执行的命令来源信任**：合同经授权卡授权即视为可执行，是否足够？是否需要命令白名单（如禁止 `rm`/`curl`）或逐条列入授权卡？
- **C. behavioral invariant 的编译命令黑名单**：内置默认名单（tsc/vue-tsc/vite build/webpack）是否够用，是否允许项目 `.vibe/config.json` 覆盖？
- **D. 范围确认**：payload 形状门（evidence_ref 不打开、protocol 逐字判等）本 issue 明确**不修**、另立 issue，是否同意？

## 6. 范围外（明确不做）

- payload 内容指纹/证据打开校验（同族但杠杆点不同，另立 issue）；
- 对 vibe-guide 5.0.5 以外版本的复核；
- 熟小麦仓其余 `el-radio` 用法的全量排查（属业务仓，不属本仓）。

## 7. 测试先行清单（按项目 §8 必覆盖项映射）

- 合同 schema：`environment_facts` 缺 fact/source 被拒；未知键仍被拒（回归）；旧合同无该字段可通过（兼容）；
- 门 1 闭环：source 指向不存在文件被拒；
- brief 符号校验：`.vue` entrypoint 带 `:symbol` 不再恒失败；python 严格度回归；
- 监工执行：test_command 非零退出 → 拒收 + blocked_unknown；超时 → 拒收；exit 0 → 证据落日志；
- behavioral 黑名单：`vue-tsc` 作 behavioral invariant 的 test_command 被拒；
- blocked_unknown 非空 → acceptance 暂停、处置后可继续；
- 快照恢复：新增字段后旧 state.json 可恢复。
