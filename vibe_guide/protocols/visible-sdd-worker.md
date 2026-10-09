# Vibe Guide · 可见 SDD worker 会话协议（visible-sdd topology）

> 本文件随 vibe 包发布，面向被监工派发的 worker 会话（宿主 agent）。
> 当一个 DAG 节点以 `visible-sdd` topology 绑定到单个可见会话时，该会话内的开发/审查流程按本协议执行；监工只读收口证据，不进入会话内代劳。

## 1. 固定流程（双角色，同一会话身份）

worker 会话收到节点合同后，必须在**同一会话身份**内按固定顺序推进，不得更换会话、不得跳过角色：

```text
dev 子代理实现 → review 子代理审查 →（存在 P0–P2）返工回 dev 子代理 → review 子代理复审 → P0–P2 清零后收口
```

1. **dev 子代理**：按节点合同实现，只写文件白名单内的文件，完成后跑合同列明的定向验证。
   **派发时必须把子代理钉在合同的 `worktree` 上**：宿主有 `cwd` 参数就传 `cwd`；WorkBuddy 的 Agent
   工具没有 `cwd`，只能在提示里要求它**每条命令先 `cd <worktree>`**、写文件用绝对路径——漏一条命令
   就落回主项目树（见 prd-guide §6.3）。review 子代理同理。唯一例外是第 5 节的 `vibe worker-deliver`：
   它必须在主项目目录执行。
2. **review 子代理**：**独立上下文**、**只读**、**非作者视角**审查 dev 的 diff，按 P0（资金/数据正确性）、P1（逻辑缺陷/事务边界）、P2（边界条件/行为回归）、P3（建议）分级产出意见。review 子代理不得是 dev 子代理本人，不得复用 dev 的实现上下文。
3. **返工**：存在 P0–P2 时逐条核实（reviewer 意见也要验证，不盲从）→ 由 dev 子代理修复 → 重跑定向验证（须为绿）。
4. **复审**：返工后回到 review 子代理复审，保留审查身份与证据链；最多 3 轮，超限仍存 P0–P2 时转 blocked 并上报监工。
5. **收口**：某轮复审无 P0–P2 且验证绿后，按节点合同收口（提交/开 PR 等以授权卡为准）。

## 2. review 子代理只读红线（git 写禁令清单）

派给 review 子代理的任务提示必须**显式禁止一切 git 写操作**。以下命令清单一字不差写进提示，review 子代理不得执行：

- `git checkout` / `git switch`：禁止（不得切换分支或检出文件）
- `git reset`：禁止（任何模式）
- `git clean`：禁止
- `git stash`：禁止
- 以及一切改变工作区、索引或引用的命令（`git add` / `git commit` / `git push` / `git merge` / `git rebase` / `git cherry-pick` 等）

跨分支查看只允许只读形式：

```bash
git show <ref>:<path>     # 看其他分支上的文件内容
git diff <ref>...<ref>    # 看分支间差异
```

审查所需的一切材料由 worker 会话在派发提示中直接给出（diff、文件内容、验证输出），review 子代理不应需要任何写操作。

## 2.5 审查方法论（review 子代理必读）

**第 0 步：覆盖面（先于三步法）**。三步法管的是「单条意见站不站得住」，管不了「有没有看全」——
一个只审了 3 个文件、每条意见都无懈可击的 review，与审完全部 14 个文件的 review，在意见质量上无法区分。
所以审查开始前必须先取得**确定性文件全集**：

```bash
ocr delegate preview --commit <sha>             # 单提交
ocr delegate preview --from <base> --to <head>  # 分支范围
```

该命令由确定性工程（而非模型）选出可审查文件并按规则排除测试/生成物。对清单**逐文件**给出处置：
审了，或跳过并写明理由（如「生成代码」「纯文档」「规则排除」）。清单拿不到时（工具不可用、
范围不是 git 变更），在交付里显式声明 `coverage.mode = "none"` 并写明理由——**豁免的是工具，
不是交代范围的义务**。交付时必须回报覆盖面（见第 5 节 `coverage` 字段），监工校验
`listed = reviewed + 跳过数` 是否自洽。

review 子代理产出意见前，每条候选意见必须走完「约定 → 可行执行 → 反驳」三步，
未完成反驳的陈述只是候选，不是 finding：

1. **约定（约定）**：先锚定约束来源——节点合同即 intent（合同写明的输入、输出、错误行为与验收示例就是本轮意图的完整表述，不必另找设计文档）；合同之外再参考合同声明的 `environment_facts`、AGENTS.md 与被改文件的邻近约定。约定来源 = 合同 + 合同声明的 `environment_facts` + AGENTS.md + 邻近约定，四者缺一即约定锚定不全。
2. **可行执行（可行执行）**：确认该候选问题在 diff 的实际执行路径上可达，能用一条具体输入或状态触发；纯理论担忧不构成 finding。
3. **反驳（Refutation）**：主动构造反例尝试驳倒这条候选意见——如果它被驳回（例如合同本就允许、已有守卫覆盖），丢弃或降级为 P3 观察；**未完成反驳不得计入 P0–P2**。

未知与拿不准不按猜测定级：无法确认的事实记录为 `blocked_unknown`，交由监工收口，不得伪造通过或伪造违规。

审查与返工的 fan-out 上限为一层：dev/review 子代理不得再自行派发嵌套子代理扩大并行；需要更多视角时在原子代理内顺序处理。

### 严重度映射表

| 级别 | 定义 | 判定机制 |
| --- | --- | --- |
| P0 | 资金/数据正确性受损、安全边界被绕过 | 约定违例 + 可行执行路径 + Refutation 失败 |
| P1 | 逻辑缺陷、事务边界错误、会改变交付语义 | 约定违例 + 可行执行路径 + Refutation 失败 |
| P2 | 边界条件、行为回归、合同外但不致命的行为变化 | 约定违例 + 可行执行路径 + Refutation 失败 |
| P3 | 建议性观察，不进入清零门槛 | 未走通三步或被 Refutation 驳回的候选 |

方法论参考：完整 skill 可读项目主目录 `.vibe/proposals/skills/<名>/SKILL.md`（如
pm-ai-shipping 的 code-review / intended-vs-implemented），仅作参考不强制逐字引用；
读不到时按本节硬规则继续审查，不得因此阻塞或降级标准。

## 2.6 环境事实与已知缺陷（派发提示强制节）

**环境事实与已知缺陷**：派发 review 子代理前，worker 必须将节点合同的 `environment_facts` **逐项原文**写入派发提示；合同该字段为空且本次改动不涉及第三方组件时，必须显式写「本节点无环境事实声明」。**缺失本节的派发视为 review 材料不全，该轮审查结论不得作为 clearance 证据。**

### 验证清单纪律（硬规则）

**「页面能打开/组件在产物里/路由可解析」不得作为验收项**；验收必须表述为「目标交互动作跑通」（具体输入 → 预期行为）。编译/构建绿不构成行为证据。以代理指标充当验收的交付按材料不全处理，不得进入 clearance。

## 3. 证据链写回

- 每一轮**实现结论**（dev：改了什么、验证命令与结果）、**审查结论**（review：P0–P3 分级意见）、**返工结论**（逐条处置：接受修复/驳回理由、复验结果）都必须写入本会话的交付记录，不得只留在子代理内部。
- 会话交付中每轮证据需可区分轮次（第 1 轮审查、第 1 轮返工、第 2 轮复审……），旧证据保留，新证据追加。
- 监工负责把会话交付中的结论**收口进 `.vibe/runs/<run-id>/events.jsonl`**；worker 会话不直接改写 events.jsonl，除非节点合同明确授权。
- 无法验证的项记录为 `blocked_unknown`，不得伪造完成。

## 4. reviewer 独立性违规

- review 子代理**代改业务代码**（任何对白名单文件的写入）= **违规**。
- 发现违规时：该轮审查结论作废，节点转 **blocked**，并在会话交付中**记录**违规事实（谁、改了哪些文件、时间）供监工收口。
- 违规后不得以"改得对"为由追认；修复只能由 dev 子代理完成、由 review 子代理复审。

## 5. 交付 payload 契约（监工校验字段）

worker 会话的交付事件由监工**先校验、后落账**：以下字段缺失或形状非法时，本次交付被记为
`acceptance_rejected`（纯审计事件，不入生命周期），会话 handle 保留，监工在同一会话上继续等待——
**worker 修正后重报即可，run 不会因此砖化**。只有节点合同 digest 漂移（篡改/合同被改）仍然
fail-closed，不可重报。

交付事件必须携带两块结构：

1. **`in_session_review`（本会话内 SDD 审查证据，必需）**：

   ```json
   {
     "protocol": "vibe_guide/protocols/visible-sdd-worker.md",
     "evidence_ref": "<本轮审查证据的非空引用，如 会话#轮次>",
     "clearance": {"p0": 0, "p1": 0, "p2": 0},
     "coverage": {
       "mode": "ocr",
       "listed": 14,
       "reviewed": 13,
       "skipped": [{"file": "backend/.../FooMapper.xml", "reason": "纯 XML 映射，无逻辑"}]
     },
     "environment_facts_ref": "none",
     "blocked_unknowns": []
   }
   ```

   - `protocol` 必须逐字等于本文件路径（`vibe_guide/protocols/visible-sdd-worker.md`），引用别的协议
     等于这次交付没有经过本协议规定的会话内审查；
   - `clearance` 三个键都必须是整数 `0`（布尔不算）——P0–P2 未清零就返工，不要交付；
   - `evidence_ref` 必须是非空字符串，指向第 3 节写入交付记录的那轮审查结论。
   - `coverage`：**必需字段**，本轮审查的覆盖面分母，来自第 2.5 节第 0 步的确定性文件清单。
     - `mode: "ocr"`：`listed`（清单文件数，非负整数）、`reviewed`（实际审查数，非负整数）、
       `skipped`（数组，逐项 `{"file": <非空>, "reason": <非空>}`）。监工校验
       `listed == reviewed + len(skipped)`；不等、类型不对或 `skipped` 条目缺理由，一律
       `acceptance_rejected`（可修正重报）。同一文件不得重复计入 `skipped`（重复即拒）——
       `skipped` 是逐文件处置，不是计数。
     - `mode: "none"`：未取得确定性清单时使用，必须带非空 `reason`。**这是显式豁免，不是省略字段**——
       字段缺失一律拒收，豁免的是工具而不是交代范围的义务。
     - 反面判据：`listed` 只写 `reviewed` 的数字（把分母当分子）会让校验直接不通过；覆盖面不完整
       而靠调低 `listed` 绕过，等于把「漏审」写成「没这些文件」，属于伪造证据。
   - `environment_facts_ref`：字符串，指向派发提示中「环境事实与已知缺陷」一节对应的内容来源，监工门校验其存在；合同无环境事实声明时填 `"none"`，不得留空或省略字段。
   - `blocked_unknowns`：可空数组，缺省视为空；**非空时监工不得直接 acceptance**，必须产生一条人工/监工处置事件（逐项：已确认无虞 / 已转 P0–P2 返工 / 已补环境事实后复审）后方可继续。

2. **`delivery_evidence`（交付证据门，引擎要求时必需）**：一个嵌套对象，含
   `completion_marker`、`delivery_path`、`thread_status`（只认 `complete` / `completed` /
   `DELIVERED`）。摊平成顶层三个字段不算，门读不到。

被拒后如何重报：按上面的形状补齐字段，在同一会话里再次交付即可；旧的 `acceptance_rejected`
记录保留在事件日志里作为审计痕迹，不阻塞后续 acceptance。

被拒后如何重报之外还有一条主动通道：`vibe worker-deliver` 可以把合法交付当场落盘并完成
对应 pending 的 `wait` 请求，监工下一次 resume 即消费；`--payload` 的形状与上面的交付事件相同
（嵌套 `delivery_evidence`，visible-sdd 另带 `in_session_review`），`--generation` 照抄派发指令里的值（返工续派时以续派指令给出的新值为准，旧值或不存在的更大值都会被当场拒绝；`--run-id`、`--node`、角色与派发不符同样当场拒绝），
并且必须在主项目目录执行（在 worktree 里执行会写进 worktree 自己的 `.vibe`，监工看不到）。自报时监工
还没发出 `wait` 也不会丢：交付先存档，监工下一次发出同一代的 `wait` 时自动领取；返工后的新一代
不会领到上一代的存档。同一 payload 重复自报幂等，不产生
第二个事件。监工侧校验门位置不变，也不信任唤醒信号的内容。

developer 自报时还会**按 git 实况核对落点**（不看自报内容）：合同 `worktree` 必须存在且在合同分支上，
主项目树里不得有合同白名单文件的未提交改动（含已暂存、未跟踪）。任一不满足即当场拒绝并写明原因（例如
`the main project has uncommitted changes to this node's allowlisted files: app.py`）——最常见的原因是漏了
`cd <worktree>`、把活干在主项目里，但也可能是别的 writer 或一次未提交的合并，先看清主项目树再处理：
若是本节点干错了地方，把改动挪进合同 worktree、恢复主项目树后再自报。git 读不出来时报
`cannot verify delivery landing`，属于环境问题。以下情况不核对：reviewer（只读）；合同 worktree 就是主项目根（`.`）；
派发没有 `child_binding`；同一 payload 的幂等重报。错落的改动若已提交到主项目分支：系统按派发时的主项目 HEAD
（记在 `.vibe/provider-actions/dispatch-heads/`，同一代只记一次）核对。若本节点 worktree 自派发以来白名单文件没有任何改动或提交，
而主项目此后有提交改过这些文件，就报
`the main project committed this node's allowlisted files after it was dispatched`。
派发之前已合并的提交不算；只要 worktree 里有本代的改动，监工期间合并的其他节点（白名单重叠）也不算。
被拒时**不要回滚不是你提交的 commit**：在合同 worktree 里重做，并把要撤的 commit 告诉监工。
主项目 HEAD 已不再是派发基线的后代（切了分支、改写了历史）时，报 `cannot verify delivery landing`，由监工重新派发。
**已知盲区**：记录这项之前就已派发的节点、派发时读不到主项目 HEAD 的节点，不做提交历史核对；
只出报告、而白名单与期间合并重叠的节点会被误拒，重新派发（新一代、新基线）即可。

## 6. 与本协议无关的事项

- 本协议不定义节点合同格式、授权卡签发或监工调度，那些由 vibe 核心与 prd-guide 协议负责。
- deploy、release、生产数据变更永远不在 worker 会话权限内，无论审查结论如何。
