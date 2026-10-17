# eval/decision — 入口路由决策评测（issue #128）

对 `decision-gateway` 的 `/v1/systemone` 后端（Kev 0.8B）做「请求该走哪条路径」的
choice 评测，回答的问题是：**本地快思考模型能否复现 vibe 自己的入口路由判断。**

## 目录里有什么

| 文件 | 入库 | 说明 |
|---|---|---|
| `build_from_history.py` | 是 | 从本机会话记录构建评测集（本页下半） |
| `route.jsonl` | **否** | 评测集本体；仓库是公开的，而它是真实用户请求 |
| `route.report.json` | **否** | `run_eval.py` 出的报告 |

`route.jsonl` 与 `route.report.json` 已在 `.gitignore` 里。要重跑：

```bash
# 1. 建集（本机，从 ~/.claude/projects 里捞真实判定行；--force 覆盖旧集）
PYTHONPATH=. python3 eval/decision/build_from_history.py --force

# 2. 评测（harness 在 decision-gateway 仓，不在本仓）
cd ~/Documents/Codex/decision-gateway
DG_BACKEND=systemone python3 eval/run_eval.py \
  --dataset /Users/macmini/Desktop/vibeguide/eval/decision/route.jsonl \
  --report /Users/macmini/Desktop/vibeguide/eval/decision/route.report.json

# 3. 温度缩放拟合
python3 calibrate/temperature.py /Users/macmini/Desktop/vibeguide/eval/decision/route.report.json
```

> ⚠️ 第 1 步会**整集重写**。用 `decision-gateway` 的 `eval/append_case.py` 手工回填的
> 行不在转录里，会被 `--force` 冲掉——回填过就要先备份再重建，或重建后重新回填。
>
> ⚠️ 本页所有漏斗与实测数字对应 **2026-10-16 冻结快照**（118 行）。历史语料是增长的
> 目标：在任何其它日期重建都会得到不同分布的数据集（2026-10-17 实测 `--dry-run`
> 127 行），重建后必须**重跑全部评测**才能引用数字——与 issue #128「问法冻结后改动
> 必须重新评测」同口径。

## 评测集怎么建的（详见脚本 docstring）

`expected` = **真实判定行**，不是重跑打分。`vibe-entry` 协议要求每次入口评分后输出
一行可验证结果，形如 `S1：11→轻规划（不触碰 vibe）`（`vibe_guide/protocols/vibe-entry.md`
§2）；这行就是生产实际做的判定。脚本把这类行从助手消息正文里抽出来，与它被打分的那条
请求配对。

设定（每条都有实测依据，不是猜的）：

1. **只扫助手自己的 prose**（`message.content` 里 `type == "text"` 的块）。读
   `vibe-entry.md` 会让协议里的示例行以 tool result 的形式进转录，扫它等于从文档里
   造标签。有回归测试钉住（`tests/test_decision_dataset_build.py`）。
2. **受认两种判定行书写**：① 协议规定的可验证行 `S1：11→轻规划（…）`（常并入
   `[对齐门]` 卡片的 `S1：` 字段）；② 评分明细结论行 `S1 评估: 步骤(5)+范围(3)+…= 15
   → 轻规划执行`——分数与档位同行齐全、同样自洽可验，实测 55 行（2026-10-16 快照数）、
   与形状 ① 零共现，v1 完全漏收。
3. **紧邻配对**：判定行与它**紧邻上一条用户消息**配对。v1 用「最近一条被接受的用户
   消息」做锚点，被拒的消息（心跳、宿主注入、短消息）不更新锚点——于是**心跳回合
   自己的判定被错配到上一条真实用户请求上**。v1 漏斗实测：217 个配对判定只有 87 个
   干净，130 个在请求与判定行之间插过机器/注入/短消息；v1 报的 11 组「冲突标签」里
   10 组在改为紧邻配对后消失（错配的 artifact），同时暴露出 1 组真实标签噪声（同一
   请求在不同会话被打了不同分，即下方漏斗的 `conflicting_labels 1`，已双剔）——冲突
   不全是错配，但大部分是；v1 数据集有 9 行带错配标签。判定行紧前的消息是机器生成
   （`vibe supervisor-beat` 心跳、跨会话投递、Stop hook 反馈、`<system-reminder>`/
   `<task-notification>` 注入、OpenClaw Guardian 定时指令等）时，整条丢弃并计数。
4. **短消息不过滤长度**。v1 的 `--min-chars 8` 把「跑完了，继续」「同意续授权」这类
   真实判定丢了，还制造错配锚点；短消息也是生产真实打分的请求，退化样本交给去重和
   冲突规则处理。
5. **标签自洽校验**：`<=8 → simple`、`9-15 → light_plan`、`>15 → complex`，不自洽
   丢弃并计数。
6. **冲突标签剔除**：同一请求在不同时间被判定成不同档位的，两边都剔除——否则留下
   模型不可能全对的样本，白白压低准确率。
7. **脱敏后入集**：`state` 逐字保留用户请求，但凭据/PII 形状机械替换（11 位手机号、
   `pass=…`、`host=rm-…` RDS 地址 → `[REDACTED:…]`），路由判定不依赖这些值。替换处
   有占位符，行数计入构建报告（`rows_redacted`）。脱敏在去重/冲突检测**之前**执行
   （两条仅凭据不同的请求脱敏后即同一请求，回归测试钉住）。

档位词表用**代码实际档位名** `simple` / `light_plan` / `complex`，与
`vibe_guide/planner.py` 的 `ROUTE_SIMPLE_MAX_SCORE`（8）/ `ROUTE_LIGHT_PLAN_MAX_SCORE`（15）
同源，不另建翻译层。问法（`question.instructions` 与 `criteria`）按 issue #128 的要求冻结，
**改动必须重跑全部评测**。

## v2 建集漏斗（2026-10-16，构建器输出原文）

```text
decision_lines        424   （两种书写合计）
  inconsistent          4   （分数与档位说法不符，如「= 16 → 轻规划执行」）
  machine_paired      274   （紧前消息是心跳/注入/定时指令，判定不属于用户请求）
paired_decisions      146   （真实用户判定）
  duplicate_pairs      26   （同请求同档位重复出现）
  conflicting_labels    1   （「1」跨会话被判成 complex 和 light_plan，两边剔除）
rows_dropped_conflict  2
rows_redacted          1
= 最终 118 行 = simple 37 / light_plan 57 / complex 24   ✅ ≥100 门槛达标
```

v1（同源数据、旧配对规则）：92 行，其中 9 行标签来自心跳错配。v1 的 217 个「可配对」
判定里有 130 个是脏的——v2 的紧邻配对 + 机器识别把这一层挖了出来。

## 实测（2026-10-16，Kev 0.8B / systemone-http）

两轮都跑在构建器当前版本产出的数据集上；v1 那轮跑的是含 9 行错配标签的 92 行集，
数字仅存档，**以 v2 为准**。

```text
                 v1（n=92，含错配）      v2（n=118，当前）
accuracy             0.3152               0.4322
ECE（答案 confidence） 0.1835               0.2997
avg_confidence       0.1455               0.1325
latency p50          59.2 ms              68.7 ms
latency p95         136.5 ms              88.3 ms
温度缩放 T            5.0（网格上界）        0.9
NLL                1.1259 → 1.0979      1.0623 → 1.0621
ECE（max prob）     0.1151 → 0.0375      0.036 → 0.053
```

> 注：`route.report.json` 行内 probabilities 为**校准前**概率（由 NLL 复算值与上表
> 「校准前」一列一致确认）；校准后数字（ECE 0.053 / NLL 1.0621）出自
> `decision-gateway` 仓 `calibrate/temperature.py` 的输出。

### v2 的逐个类分解（比总分更重要）

```text
混淆矩阵（expected → answer）     simple  light_plan  complex
  simple           (37)              0        32         5
  light_plan       (57)              0        45        12
  complex          (24)              0        18         6

模型 answer 分布：light_plan 95 / complex 23 / simple 0（一次都没答 simple）
无脑全猜 light_plan 基线：57/118 = 0.4831  >  模型实测 0.4322
```

**模型比平凡策略还差**：它的输出塌缩到 light_plan（80% 的请求），simple 类完全不可达，
总分 0.4322 低于「永远猜多数类」的 0.4831。接入侧据此可以下结论：本地快思考模型
**不能**复现 vibe 的入口路由判断，连做预筛都不够格——这不是调阈值能救的，是模型本身
的区分度问题（对照：v1 数据集类分布更均衡，0.3152 ≈ 三分类随机 0.33，同一结论）。

对照 issue #128 §4 的验收断言：

- [x] `run_eval.py` 报告出了 accuracy / ECE / p95 延迟
- [ ] **ECE ≤ 0.05**：`run_eval.py` 口径（答案自身 confidence）0.2997，未达标；
      `calibrate/temperature.py` 口径（max prob）校准前就已 0.036 ≤ 0.05。**同一份报告
      两个口径差 8 倍**，按 issue 规则「原始概率未达标就只允许用排序」——原始概率不能
      直接开阈值；口径统一前任何 ECE 数字都不能单独引用
- [x] `calibrate/temperature.py` 拟合出 T = 0.9（v1 的 5.0 是网格上界未收敛，v2 落在
      区间内；按 max-prob 口径温度缩放几乎无收益，NLL 仅 1.0623 → 1.0621）
- [ ] T = 0.9 **尚未写入业务配置**（属接入侧）
- [ ] 降级路径实测一次（kill 服务后走规则兜底）**未做**，需要单独授权

## 已知缺口（都是实测数字，不是估计）

- **配对仍有残留不确定性**：紧邻配对 + 机器识别解决了主路径，但「请求在 A 文件、
  判定行进 B 文件」的跨文件续接仍配不上（记为 orphans/machine_paired）；助手在散文里
  **引用**自己早前卡片的判定行理论上会造重复标签（去重兜底），这一条没有可自动核验的
  信号，只能靠人工抽检。
- **accuracy 不可用**（见上：低于多数类基线）；接入侧要正面回答的问题是：要么换模型，
  要么明确路由判断永远不带本地预筛。
- **两个 ECE 定义不同**（0.2997 vs 0.036）：`run_eval.py` 用答案自身的 confidence，
  `calibrate/temperature.py` 用 `max(probabilities)`。同一份数据两个数，后续要把口径统一。
- **数据集增长依赖真实使用**：`append_case.py` 手工回填与 `--force` 重建互斥（见上方
  警告）；日常应只回填不重建，重建前先导出回填行。
- **类分布不均**（light_plan 48%）：v2 的增量主要来自评审明细行，天然偏轻规划。后续
  回填时留意 simple/complex 的配额，否则多数类基线会继续虚高。
