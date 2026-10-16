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
# 1. 建集（本机，从 ~/.claude/projects 里捞真实判定行）
PYTHONPATH=. python3 eval/decision/build_from_history.py

# 2. 评测（harness 在 decision-gateway 仓，不在本仓）
cd ~/Documents/Codex/decision-gateway
DG_BACKEND=systemone python3 eval/run_eval.py \
  --dataset /Users/macmini/Desktop/vibeguide/eval/decision/route.jsonl \
  --report /Users/macmini/Desktop/vibeguide/eval/decision/route.report.json

# 3. 温度缩放拟合
python3 calibrate/temperature.py /Users/macmini/Desktop/vibeguide/eval/decision/route.report.json
```

## 评测集怎么建的（详见脚本 docstring）

`expected` = **真实判定行**，不是重跑打分。`vibe-entry` 协议要求每次入口评分后输出
一行可验证结果，形如 `S1：11→轻规划（不触碰 vibe）`；这行就是生产实际做的判定。
脚本把这类行从助手消息正文里抽出来，与它前面那条用户请求配对。

三条避坑设定：

1. **只扫助手自己的 prose**（`message.content` 里 `type == "text"` 的块）。
   读 `vibe-entry.md` 会让协议里的示例行以 tool result 的形式进转录，扫它等于
   从文档里造标签。
2. **标签自洽校验**：`<=8 → simple`、`9-15 → light_plan`、`>15 → complex`。
   不自洽的行丢弃并计数。
3. **冲突标签剔除**：同一请求在不同时间被判定成不同档位的，两边都剔除。
   否则会留下模型不可能全对的样本，白白压低准确率。

档位词表用**代码实际档位名** `simple` / `light_plan` / `complex`，与
`vibe_guide/planner.py` 的 `ROUTE_SIMPLE_MAX_SCORE`（8）/ `ROUTE_LIGHT_PLAN_MAX_SCORE`（15）
同源，不另建翻译层。问法（`question.instructions` 与 `criteria`）按 issue #128 的要求冻结，
**改动必须重跑全部评测**。

## 首次实测（2026-10-16，Kev 0.8B / systemone-http，n = 92）

```text
评测集：92 条 = simple 37 / light_plan 34 / complex 21
accuracy      = 0.3152
ECE           = 0.1835        （issue #128 要求 <= 0.05 → 未达标）
avg_confidence= 0.1455
latency p50   = 59.2 ms
latency p95   = 136.5 ms
温度缩放拟合：T = 5.0（网格上界）
              NLL 1.1259 → 1.0979
              ECE 0.1151 → 0.0375（校准后达标）
```

对照 issue #128 §4 的验收断言：

- [x] `run_eval.py` 报告出了 accuracy / ECE / p95 延迟
- [ ] **ECE ≤ 0.05：原始概率未达标（0.1835）**；温度缩放后 0.0375，说明概率可用，
      但按 issue 规则「否则只允许用排序，不许用概率阈值」——原始概率不能直接开阈值
- [ ] `calibrate/temperature.py` 拟合出 T = 5.0，**尚未写入业务配置**（属接入侧）
- [ ] 降级路径实测一次（kill 服务后走规则兜底）**未做**，需要单独授权

## 已知缺口（都是实测数字，不是估计）

- **92 条 < ≥100 门槛**。217 个真实判定行里，配对可用 114 个「请求-档位」对；
   去重后 92、其中 22 行因冲突标签被剔除。**154 个判定行配不上请求**——它们的请求
  不在同一条转录里（心跳/交接/续接会话产生的评分）。要提高条数，只能靠后续真实
  使用继续攒（`decision-gateway` 的 `eval/append_case.py` 就是干这个的）。
- **准确率 ≈ 随机**（三分类随机约 0.33，实测 0.3152）。即使置信度校准好了，
  排序本身的区分度也不足以做路由预判；这是本 issue 接入侧要正面回答的问题。
- **T 落在网格上界 5.0**：`calibrate/temperature.py` 的搜索范围是 0.5–5.0，
  最优值在区间端点，说明还没收敛，需要扩网格后重拟。
- **两个 ECE 定义不同**：`run_eval.py` 用的是答案自身的 confidence（0.1835），
  `calibrate/temperature.py` 用 `max(probabilities)`（0.1151）。同一份数据两个数，
  后续要把口径统一。
- **配对是启发式**：判定行归给它前面最近的一条用户消息。协议要求评完立即输出该行，
  所以正常情况成立；但续接会话里判定行可能挨着很久以前的消息，产生的错配
  在下游无法与正确配对区分。这一条没有可自动核验的信号，只能靠人工抽检。
