# 决策信箱 + 通知桥：PRD 输入（需求讨论共识稿）

- 日期：2026-10-05
- 状态：**需求讨论已闭合，待 `vibe plan --request` 进正式复杂流程**；本文是 PRD 输入，不是已批准 PRD
- 触发事故：issue #140 run 中 contract-env-facts 的 A/B 决策（digest 登记授权）跨第 3→7 班未获回答，靠换班交接文本口口相传，第 7 班被静默心跳刷没，用户第 8 班才拍到
- 讨论记录来源：第 7/8 班监工会话 + 监工会话侧边聊天（用户贴回）

## 1. 问题证据（已逐条代码核实）

1. **决策卡点没有标准落点。** 全链路三处放决策信息，全是非结构化：
   - worker 的 `known_gaps` 是交付存档 JSON 里的自由文本，monitor 不解析、不持久化成一级记录（`grep known_gaps vibe_guide/` 零命中，纯约定）；
   - `blocked_design` 状态存在（`vibe_guide/monitor.py:5112`），但只存状态字符串，不携带 question/options/推荐，无 CLI 回答入口；
   - `supervisor_preflight`（`vibe_guide/supervisor.py:246`）把 `blocked_design` 归入 idle-safe，心跳不催办，无 decision 分支。
   - 结果：决策内容只活在 worker 会话（会归档）和监工会话聊天记录（换班轮换即丢失）。
2. **没有外部通知通道。** 代码里唯一 notify 相关物是 `mcp_servers/workbuddy_jobs.py`（平台任务派发，非用户 IM）；`.vibe/config.json` 无通道配置；授权卡默认把对外通信列为排除动作——产品边界上就没开口子。
3. **决策等答期间心跳空转。** 预检误报 work、每轮加载全量监工上下文、rollout 增长加速 rotate，换班成本远高于空转本身。

## 2. 产品决策（用户已拍板）

1. 做**决策信箱**：决策是一级磁盘事实，会话归档/换班/轮换免疫；用户在监工会话内拍板，监工写回。
2. **心跳降频不暂停**：decision-wait 时监工把自己心跳 RRULE 调低频（如 10 分钟→2 小时），答案消费后恢复；**禁止 PAUSED**——IM 桥与 `vibe decision-answer` 都在桌面 App 工具链之外，没有任何机制能唤醒被暂停的 App 侧心跳，暂停=只能等用户回电脑前戳一下，违背"用户不在电脑旁也能推进"的初衷。
3. 通知层**只做接口不做落地**：一个可选联通向导项 + 一个出站 hook + 一条入站 CLI；bot 选型、推送文案、凭据全在用户侧，vibe 仓库永远不含 IM SDK/webhook server/bot 模板。
4. **决策来源双入口**（补丁一）：worker `decision_requests[]` 投递 + 监工 `vibe decision-raise` 发起——本次 A/B 就是监工构造的（授权范围问题，worker 只报了 known_gaps），只认 worker 投递会漏掉最难的形态。
5. **重渲染策略**（补丁二）：新决策在监工会话立即渲染一次；未答期间每个降频周期末尾重发一次（带"已等待 N 小时"）；答案落盘即停。
6. **答案时效**（补丁三）：选项内容参与 request_digest；选项变化则已答结果自动作废、决策重新上浮，不拿过期答案推进。
7. 本能力**独立迭代**，不搭 #140 的车（会使现授权卡失效、违反"只改当前任务直接涉及的文件"、S1 远超 15 分）；#140 的 A/B 按现有手工约定收口。

## 3. 第一层设计：决策信箱（治本）

- 新增 `.vibe/decisions/{requests,results}/`，复用 ProviderActionStore 四键绑定形状（schema_version / decision_id / request_digest / payload）。
- `worker-deliver` payload 增加结构化 `decision_requests[]`（question / options[{id,label,impact}] / recommendation / 证据绑定）；monitor 校验后落一级记录 + 事件，节点转 `blocked_design`。
- 监工入口 `vibe decision-raise`（同一信箱、同一上浮路径）。
- CLI：`vibe decision-list`、`vibe decision-answer --id --choice [--via 标注] [--note]`；answer 四道校验：存在、仍 pending、choice ∈ options、digest 匹配当前选项。
- `supervisor-preflight` 增加 **decision 分支**：有未答决策时心跳把卡片**从磁盘渲染**进监工会话；答后下一轮自动 `resume` 推进。
- 预检能区分"有可服务项"与"纯等用户"：后者当轮只回一字结束，等价 idle。
- 降频状态写在盘上（信箱本身即事实），换班后新监工从盘上读到并恢复频率，不会忘。
- 决策正路径不依赖心跳：桥/用户写答案后可顺手 `vibe resume`（纯本地）立即推进；心跳只兜底。

## 4. 第二层设计：通知桥（接口化，无具体落地）

### 4.1 配置（可选键，缺省休眠）

```json
"notify_bridge": {
  "enabled": true,
  "command": "/abs/path/to/user/bridge",
  "timeout_seconds": 10
}
```

### 4.2 出站：全系统唯一 hook

新 pending decision 落盘时，以项目根为 cwd 调用 `<command> push <decision.json 绝对路径>`。payload 即信箱请求文件原文：

```json
{
  "schema_version": 1,
  "decision_id": "dec-…",
  "request_digest": "sha256…",
  "question": "…",
  "options": [{"id": "A", "label": "…", "impact": "…"}],
  "recommendation": "A",
  "evidence": {"run_id": "…", "node": "…", "delivery_ref": "…"},
  "asked_at": "…",
  "answer_cli": "vibe decision-answer --id dec-… --choice <A|B> --digest sha256…"
}
```

- 退出语义：0=桥已收到（≠送达用户）；非零/超时/命令不存在 → 记 UNKNOWN，不阻断 run、不解释为成功或永久不可用。
- 每决策落地推一次，不周期重推（催办重发是第一层会话内渲染职责）。
- 逃逸口：桥可自行轮询 `.vibe/decisions/requests/` 而不被 vibe 调用，两种接法同一套文件。

### 4.3 入站：无 hook，单条 CLI

`vibe decision-answer --id <id> --choice <option_id> [--via feishu-bot] [--note "…"]` → 写 `.vibe/decisions/results/<id>.json`（四键 + answered_at + answered_via 自由标注，仅记录无信任语义）。

### 4.4 向导项

能力目录新增 `id=notify_bridge`，**layer=`sensitive_external`**（走 `_SENSITIVE` 显式授权、默认 pending）。init/wizard 中用户选定 → 追问桥命令路径写 config；不选 → 功能休眠。

### 4.5 边界（写进合同）

- 桥命令只是引用，凭据禁入 `.vibe`（桥自管 env/keychain）；
- `decision.json` 只含问题/选项/证据引用，不含文件内容、凭据、私隐；
- 出站 push 属授权卡"对外通信"类，run 级授权单独列出，默认排除；
- 桥失败=UNKNOWN，沿用能力合同真相条款。

## 5. 协议层同步

- prd-guide §6：心跳指令加 decision 分支、降频/恢复纪律、换班交接把未答决策列为头等事项；
- prd-guide §7 / visible-sdd-worker.md：worker 自报格式（decision_requests[]）；
- supervisor-preflight 输出契约：decision 状态语义。

## 6. 成功标准（草案）

1. 构造一个含 decision_requests 的 worker 交付：决策一级落盘、节点转 blocked_design、预检报 decision、心跳把卡片渲染进监工会话；
2. 会话内 `vibe decision-answer` 后下一轮自动 resume 推进；
3. 决策未答期间监工归档+换班，新班开工即从盘上渲染同一决策（不再需要交接文本传递）；
4. 选项变更后旧 answer 被拒并重新上浮；
5. decision-wait 心跳按降频 RRULE 运行，答案消费后自动恢复；
6. 配置 notify_bridge 后新决策触发一次桥调用；桥缺失/失败仅记 UNKNOWN，run 不受影响；
7. 未配置桥时全功能退回会话内回答，无回归。

## 7. 明确不做（Out of Scope）

- 不做任何具体 IM bot 实现、模板或示例脚本；
- 不存凭据、不建 webhook server；
- 不 PAUSED 心跳（只降频）；
- 不改动本次 #140 run 的任何既有授权与节点。
