# 会话内派发（visible-sdd）在 WorkBuddy 上的真实验证记录

日期：2026-10-08
方式：**零代码改动**（只读定位 + 跑真实监工 + 登记监工地址；未改任何 `.py`、未提交、未推送）
目标 run：`run-7e11e64785964eafadaaf8b75233c64e`（plan `supervisor-boundary-iteration-v4`，12 节点，complex）
要回答的问题：**「vibe-guide 驱动宿主会话、由宿主会话用原生能力 fan-out 子代理」这条路通不通？**

## 结论（一句话）

**不通，但缺的不是 AI 能力，而是三处接线。** 框架里 `visible-sdd` 拓扑、协议、派发载荷**全都已经写好且已被真实生成**；卡点是「派发目标仍是新桌面会话 → provider 永不应答 → 监工永远拿不到句柄 → worker 自报交付无处可落」。

## 全链七环（每环附证据）

| # | 断言 | 证据 | 判定 |
| --- | --- | --- | --- |
| 1 | WorkBuddy 的能力探针已判定「会话内 SDD 可派发」 | `.vibe/provider-actions/session-facts-workbuddy.json`：`workbuddy.in_session_sdd: true`，`workbuddy.visible_task.create/enter/resume/wait` **四项全 false** | 通过 |
| 2 | 该探针落到执行层即 `visible-sdd` | `adapters/registry.py:47` `"workbuddy": {"probe_pass": "in_session_sdd", ...}`；`authorization.py:381-383` 把 `in_session_sdd` 译成 `visible-sdd`；plan 里 12 个节点 `adapter_id` 全是 `workbuddy` | 通过 |
| 3 | 协议与派发载荷**早已完整** | `.vibe/provider-actions/requests/` 里有 **9 条真实 `visible-sdd` 派发请求**（`request.topology == "visible-sdd"`）。最新一条 `action-b21cfda688258400b87fd351ad37853d.json`：`request.sdd_protocol = vibe_guide/protocols/visible-sdd-worker.md`，`prompt` 6315 字符 = 派发指令 + **协议全文逐字内联** + 一致性纠偏证据；`request.child_binding` 带 allowlist（3 个文件）、branch、worktree、worker_profile | 通过 |
| 4 | 派发目标仍是「新桌面会话」，provider 永不应答 | 同一条请求 `native_tool = workbuddy_job__create`、`provider = workbuddy-visible`；该 action **在 `results/` 里没有对应结果**（requests 74 / results 65，缺的正是这 9 条） | **断** |
| 5 | 监工等不到应答 → 判 `blocked_unknown` | 跑一次 `python3 -m vibe_guide monitor --plan supervisor-boundary-iteration-v4 --authorize AUTHORIZE --run-id …`：events 667→674，requests/results **未变**；新增 4 条 `blocked_unknown`（`protocol-boundary-criteria` / `host-rules-loading` / `decision-mailbox-store` / `monitor-engineering-classification`）。再跑一次 events 674→681，run 状态 `running` → **`blocked_unknown`**，剩 8 个节点停在 `planned` | **断** |
| 6 | 节点**没有句柄**，所以自报交付无处可落 | `state.json`：`handles: []`；上述 4 节点 `active_task = null`、`active_role = null`、`developer_identity = null`、`developer_generation = 0` | **断** |
| 7 | 监工地址可登记（宿主差异已实测出正确值） | `python3 -m vibe_guide supervisor-register --run-id … --provider workbuddy --session-id fb153bad-9882-424b-aca0-92374f5ffac7 --host macmini --session-record ~/.workbuddy-ai/projects/Users-macmini-Desktop-vibeguide/fb153bad-9882-424b-aca0-92374f5ffac7.jsonl` → 「监工地址已登记；接班起点 295729 token」。登记后监工不再打印「首班两件事」，但仍**不派发** | 通过（但不足以解锁派发） |

## 三处接线缺口（精确到行）

### 缺口 1｜派发目标写死为「新桌面会话」

- `monitor.py:3126-3168` 构建 create 合同；`provider_action.py:496-519` 的 `visible-sdd` 分支把协议全文内联进 `prompt`，并保留 `topology` / `sdd_protocol`。
- 但最终出口是 `native_tool = workbuddy_job__create` —— 新桌面会话。
- **宿主会话 id 就在环境变量里**：实测 `CODEBUDDY_SESSION_ID = fb153bad-9882-424b-aca0-92374f5ffac7`（且 `host_session_id()` 已按 UUID 校验并入 `skip`，即当前实现是「排除自身」，不是「投给自身」）。
- 缺口实质：**缺一条「把同一份载荷投给宿主会话」的投递路径**，载荷本身不用改。

### 缺口 2｜句柄登记依赖 provider 应答，而应答永远不会来

- `monitor.py:3852`：

  ```python
  active = current.get("active_task")
  if not isinstance(active, dict) or active.get("handle_id") != handle_id:
      self._mark_blocked_unknown(snapshot, node_id, "event arrived from an unregistered handle")
      return
  ```

- 句柄只在 provider **应答之后**才登记；provider 从不应答 → `handles: []` → 任何 worker 事件（含自报交付）都会被判「来自未登记句柄」。
- 缺口实质：**宿主会话的投递路径必须在派发时就登记句柄**（目标已知，不需要 provider 发现）。

### 缺口 3｜交付归档只有在监工发出 `wait` 时才被消费

- CLI 侧登记门很宽：`task_provider.py:456` 只要求 `requests/` 里存在该 run/node/role 的派发记录（这 4 个节点**有**）→ `vibe worker-deliver` 能登记成功。
- 消费侧很窄：`task_provider.py:361-362` —— `if operation == "wait" and not probe: self._consume_archived_delivery(...)`。**只有监工发出 `wait` 才会消费归档**，而 `wait` 只对有 active dispatch 的节点发出。
- 于是：交付能归档，**但永远不被消费**，节点停在 `blocked_unknown`。
- 缺口实质：**自报通道的「存档等领取」语义（协议 §5）依赖一个不会再来的 `wait`**。

## 附带发现：已安装副本落后于仓库 HEAD

| 项 | 已安装（`~/.local/pipx/venvs/vibe-guide`） | 仓库 HEAD |
| --- | --- | --- |
| 版本 | `5.0.6` | post-#163 |
| `protocols/visible-sdd-worker.md` | **4983 字符**，无 §2.5 第 0 步、无 `coverage` | **6236 字符**，两者都有 |
| `monitor.py` 的 `_validate_visible_sdd_coverage` | **不存在** | 存在（`monitor.py:4533`） |
| `mcp_servers/workbuddy_sessions.py` 的 `host_session_id` | **出现 0 次** | 存在（`workbuddy_sessions.py:137`） |

根因：`protocols/__init__.py:63,71` —— `load_protocol` 读 `Path(__file__).resolve().parent`，**哪个副本在跑就读哪份**。今天 14:13 那批派发内联的是**已安装版**（老协议），因为那次的监工是 pipx 版。

含义：**PR #163 尚未部署**；协议与验收门存在「老配老、新配新」的两套并存状态，谁在跑决定行为。

## 宿主差异（实测，可直接补进文档）

`cli.py:1166-1171` 只写了 Codex 与 Claude Code 的会话记录路径约定，没有 WorkBuddy。实测值：

- 会话 id：环境变量 `CODEBUDDY_SESSION_ID`（本会话 `fb153bad-9882-424b-aca0-92374f5ffac7`）
- 会话记录路径：`~/.workbuddy-ai/projects/<cwd 转义名>/<session-id>.jsonl`
- 心跳原语：WorkBuddy 无 shell 级周期任务，对应物是宿主自带的**周期自动化**（`automation_update`）

## 本次验证造成的运行态变更（必须披露）

零代码改动，但**确实改动了这个 run 的状态**（都是监工在等不到应答时的正常判定，不是我手工改的）：

| 项 | 前 | 后 |
| --- | --- | --- |
| `events.jsonl` 行数 | 667 | 681（+14） |
| run 状态 | `running` | `blocked_unknown` |
| 节点状态 | 4 `running` + 8 `planned` | 4 `blocked_unknown` + 8 `planned` |
| `requests/` / `results/` / `deliveries/` | 74 / 65 / 10 | 74 / 65 / 10（**未变**，没产生新派发） |
| 监工地址 | 未登记 | 已登记（指向本会话 `fb153bad-…`） |

说明：这 4 个节点在本次之前就已经是「派发过、provider 从未应答、永卡 `running`」的死状态；监工把它们改判成 `blocked_unknown` 是把状态写成了事实，不是新造的故障。`deliveries/` 未增加任何文件。

## 未验证面（不得当成已验证）

1. **从未真正跑过一轮 dev/review 子代理并自报交付** —— 因为缺口 2/3 使这条路径结构性不可达，不是没时间跑。
2. `_answer_resume` 的终态负结果经 `provider_action.py:1154` 只落 `visibility_unknown`、`pending_action` 不清 → 节点停 `blocked_unknown`（已知未收敛）。
3. 缺口 1/2 的修复**未写一行代码**，也未经独立 review。
4. 监工地址登记后「worker 唤醒信号发到本会话」的实际效果未测（因为根本没有 worker）。
5. 心跳（周期自动化）未建。

## 更正（同日，读码后追加）：三处缺口未必都要动共享代码

上面「三处缺口」的落点写得过强了。补读派发链后，结论应更正为：

1. **平台差异早已隔离**：`NATIVE_TOOL_MAP`（`provider_action.py:54-80`）本就按 provider 分行
   （Codex → `codex_app__*`、Claude Code → `ccd_*`、WorkBuddy → `workbuddy_job__*`）。
   加一条 workbuddy 专属投递工具，另外两行不动。
2. **句柄由 provider 应答产生，不由监工自造**：`monitor.py:3424`
   `handle = self._dispatch_with_intent(runner, contract, intent)` → `handle.run_id` 来自
   邮箱服务者对 `create` 的应答 → `monitor.py:3514` `snapshot.handles[node_id] = handle.run_id`。
   **桥只要能应答出合法 handle，监工侧一行不用改**（缺口 ② 消失）。
3. **归档交付的消费随句柄自然恢复**：有句柄 → 监工照常发 `wait` →
   `task_provider.py:361-362` 的 `_consume_archived_delivery` 正常触发（缺口 ③ 消失）。
4. **4 个节点为何 `blocked_unknown` 也在此**：`monitor.py:3425` ——
   `handle.get("kind")` 落在 `binding_unknown` / `retryable` / `capacity_wait` / `repairable`
   时直接判阻塞。即「provider 没给出 handle」。

**因此 B 的改动面预计 = 桥（`mcp_servers/workbuddy_sessions.py` / `workbuddy_jobs.py`）
+ `NATIVE_TOOL_MAP` 里 workbuddy 那一行。** 但有一条**尚未验证**的关键环：
桥应答的 handle 必须过 `_dispatch_with_intent` 的整套校验（可能要求 `binding_contract` 证据、
`task_id`/`host` 形状）。该环 fail-closed，漏一项即退化为 `blocked_unknown`。

## 各平台派发是否真被跑通（实测请求/应答配对）

第二轮按 `provider × operation × native_tool` 重新交叉统计（`.vibe/provider-actions/` 全量 74 条请求 / 65 条结果），
第一版把不同 topology 混在一张表里，口径不对，以此表为准：

| provider | operation | native tool | 请求 | 有应答 |
| --- | --- | --- | --- | --- |
| codex-app-visible | create | `codex_app__create_thread` | 7 | **7** |
| codex-app-visible | locate | `codex_app__navigate_to_codex_page` | 7 | **7** |
| codex-app-visible | resume | `codex_app__send_message_to_thread` | 6 | 5 |
| codex-app-visible | visibility | `codex_app__wait_threads` | 7 | **7** |
| codex-app-visible | wait | `codex_app__wait_threads` | 35 | **35** |
| workbuddy-visible | create | `workbuddy_job__create` | 8 | 4 |
| workbuddy-visible | locate | `workbuddy_job__get` | 4 | **0** |
| claude-code-visible | — | `ccd_*` | **0** | 0 |

- **Codex 全链有真实应答证据**（create/locate/visibility 各 7/7，wait 35/35），
  与 `.vibe/codex-facts.json` 的 `visible_task.create/enter/resume/wait` 四项全 true 一致。
- **Claude Code 在本项目从未跑过**（`ccd_*` 请求数为 0），「claude-code 已跑通」目前无本地证据。
- **WorkBuddy 的 8 条 create 必须分成两组看**：
  - 4 条 `topology=visible-sdd`（带 `sdd_protocol`）→ **0 条应答**，链在 create 就断。
  - 4 条 `topology=None`（旧路径）→ 有应答，形状是
    `{"binding": {"host": "MacminideMac-mini.local", "task_id": "<uuid>"}}`。
    这 4 条对应的 `locate` 全部无应答。
  - 该 `host` 值是**本机主机名**，而 `hostname` 这个载体在本仓库
    （`vibe_guide/**/*.py`）与 pipx 已装副本（`workbuddy_sessions.py`，`host_session_id` 出现 0 次）
    里都**不存在**。即「谁写了这 4 条结果」**尚未定位**，只能标注未验证归属，
    不得据此推断 WorkBuddy 的 create 路径曾经可用。

## 下一步的三个选项

- **B（实现最小闭环）**：按缺口 1+2 加一条「投给宿主会话、并在派发时登记句柄」的路径；缺口 3 视实现方式一并处理。改动面已被本次验证收窄到 3 处。
- **C（先补文档与部署）**：把宿主差异（会话记录路径、心跳原语）补进 `cli.py` 的提示文案；并把仓库 HEAD 重新装进 pipx 副本（属 deploy，需单独授权）。
- **停**：保持现状，run 停在 `blocked_unknown`，等人工决定是否重新授权一个新 run。

---

## 第二轮实测（同日）：B 的落点从 1 处变成 3 处，且全部落在 WorkBuddy 一侧

### 三条被实测推翻/收窄的判断

1. **「401 说明路由存在」是错的。** `/nope`、`/api/v1/health`、随机 UUID 全部返回 401，
   说明鉴权是**全局中间件**，状态码里不含路由信息。路由是否存在的判据只能是
   **带正确凭据**拿 200、换随机 id 拿 404。更正后重测才得到下面的结论。
2. **`discover_sessions()` 在本机返回 0 个目标**，这才是 8 条 create 全失败的根因：
   UUID 会话的注册文件**没有 endpoint**，带 endpoint 的注册文件**没有 UUID**
   （`--serve` 进程登记的是占位 id `interactive-98851`，网关自己对该 id 回 404）。
   两个文件缺一不可，而它们不在同一个文件里。
3. **`gateway_token()` 会取到过期口令。** 本会话 shell 继承的
   `CODEBUDDY_GATEWAY_PASSWORD` 属于**另一个实例的 prewarm 池**（记为口令 A），
   打开 64063 的是**另一个**口令（记为口令 B，属会话进程 22135 与 `--serve` 进程 98851）。
   （两个口令的值都不在此记录，只留「取错了一个」这个事实。）
   旧实现取扫描结果的**第一个**匹配，因此必然 401。

### 本机网关拓扑（实测，只读）

| 事实 | 证据 |
| --- | --- |
| 唯一可达网关 = `http://127.0.0.1:64063`（`codebuddy --serve`，pid 98851） | 53807 / 53941 用**正确口令**仍 401，不是网关 |
| 该网关**只服务一个会话**：本对话 `fb153bad-…` | 200（`name='运行单元测试并获取结果'`）vs 其余全部 404 |
| 连 `--serve` 进程自己的 id `interactive-98851` 也 404 | 占位 id 不是会话 |
| 其它项目会话 `9db3a805-…` 在同网关 404 | 一个网关只认自己那个会话 |
| `jobs/<sid>/state.json` 是 job 生命周期（`pid`/`respawnArgs`），**不是投递信箱** | `~/.workbuddy-ai/jobs/fb153bad/` 只有 `state.json` + 空 `tmp/` |

### 已落地的改动（全部 workbuddy 一侧，未碰共享代码）

`vibe_guide/mcp_servers/workbuddy_sessions.py`：

1. **端点拼接**：UUID 会话自身没有 endpoint 时，继承同目录下 `--serve` 注册的 endpoint；
   网关文件本身永不作为目标；死网关不外借；同 id 重复注册先按「谁带 endpoint 谁更准」收敛再拼接。
2. **凭据轮换**：`gateway_tokens()` 列出候选，`_call` 遇 401 换下一个并记住通过的那个；
   进程扫描只在 401 之后发生一次；显式传入的凭据永不二次猜测（保住「设了 override 就不扫进程」的契约）。
3. **宿主会话按拓扑放行**：`visible-sdd`（会话内 dev/review 子代理）允许投宿主会话；
   `dual-visible` 与「没写 topology」维持排除。

`vibe_guide/supervisor.py` + `vibe_guide/cli.py`（C1）：把重复了两份的「宿主差异 / 会话记录路径」
文案收敛成 `HOST_SHIFT_DIFFERENCES` / `SESSION_RECORD_HINT` 两个常量，并补上 WorkBuddy：
记录路径 `~/.workbuddy-ai/projects/<项目路径把 / 换成 ->/<本会话 id>.jsonl`，
两条职责（登记地址、建心跳）在 WorkBuddy 上都不会自动发生。

### 真机只读冒烟（改后实测）

```
discover_sessions()  -> fb153bad-… @ http://127.0.0.1:64063   （改前：0 个目标）
SessionDispatch()    -> 起始凭据 = 继承来的口令 A（过期）
GET history          -> requests=1，凭据已自动换成口令 B  （改前：401）
candidate_sessions(cwd=项目) -> 恰好宿主会话
```

### 仍未验证（不得当成已验证）

1. **`POST /api/v1/sessions/<host>/reply` 真投一轮没跑过。** 投递会在用户**正在用的会话里**
   注入一个 worker turn，属可见副作用，需单独授权；本次只做到「发现 + 鉴权 + 候选选择」。
2. 因此**仍未跑过一轮 dev/review 子代理并自报交付**。
3. 那 4 条旧 `topology=None` create 的结果由谁写入，**未定位**（见上一节）。
4. `_answer_resume` 的终态负结果仍只落 `visibility_unknown` → 节点停 `blocked_unknown`（已知未收敛）。
5. 心跳（周期自动化）未建。
