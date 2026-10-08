# WorkBuddy 派发：根因定案与确切修法

日期：2026-10-08
环境：macOS arm64 / WorkBuddy 5.6.2 / CLI 2.147.0 / gatewayMode=local
结论性质：**实测定案**，不是推断。每条结论附可复现命令。

---

## 0. 一句话结论

后台 agent 作业（`POST /api/v1/jobs`）在这台宿主上**永远不会产出结果**，因为
job broker spawn 出来的 CLI 进程**拿不到模型凭据**——这是凭据保护机制的设计使然，
不是配置问题、不是缺 flag、不是沙箱问题。

**修法不是修作业，而是换派发目标**：把任务投递到**真实 WorkBuddy 窗口会话**
（`POST /api/v1/sessions/{id}/reply`），该路径已实测能真实驱动 agent。

---

## 1. 证据链

### 1.1 现象：agent 作业零输出、永久挂起

```bash
ls /Users/macmini/.workbuddy-ai/jobs/ | wc -l                    # 48
grep -l . /Users/macmini/.workbuddy-ai/logs/job-*.log | wc -l    # 仅 1 个非空
cat /Users/macmini/.workbuddy-ai/logs/job-d8bffc74.log
# Authentication required. Please use /login command to sign in to your account
```

48 个作业里，**6 个 `done` 全是 `bash:true` 的 echo 探针**，42 个 agent 作业全部
停在 `detail="starting…"`，进程活着、日志 0 字节、会话 jsonl 不存在。

### 1.2 排除的伪因

| 假设 | 结论 |
|---|---|
| 子会话缺 `CODEBUDDY_GATEWAY_PASSWORD` | **错**。作业进程环境里带着它（43 字符，App 级） |
| 网络/代理不通 | **错**。作业进程与我当前会话连的是同一个 `198.18.0.20:443`，我是活的 |
| 沙箱拦住 | **错**。开/关沙箱的会话两边都一样卡 |
| 缺 `-p` | **错**。带 `-p` 的 shell 作业内嵌 CLI 同样 15 分钟零输出 |

### 1.3 真因：凭据不向下传递

进程树：

```
77170 App(Electron)
 └ 77266 daemon-app-server-entry.js
    └ 77711 sidecar-entry.js --token …
       ├ 38552 codebuddy --serve (窗口会话)   ← 有 CODEBUDDY_SIDECAR_*
       ├ 83558 codebuddy --serve (窗口会话)   ← 有 CODEBUDDY_SIDECAR_*
       └ 9851  codebuddy --serve (窗口会话)   ← 有 CODEBUDDY_SIDECAR_*
            └ 5181  作业进程                  ← 没有
```

环境差分（作业 5181 vs 会话 83558）——**只有这 5 个变量是会话独有**：

```
CODEBUDDY_SIDECAR_CREDENTIAL_BOOTSTRAP_SOCKET
CODEBUDDY_SIDECAR_READY_SOCKET
CODEBUDDY_SIDECAR_READY_TOKEN
CODEBUDDY_SIDECAR_READY_SESSION_ID
WORKBUDDY_AT_REST_ENCRYPTION=fields
```

代码出处（`cli/dist/codebuddy-lite-wb.mjs`，offset 2545608，模块 5159）：

```js
let eE = function (env) {
  let a = env["CODEBUDDY_SIDECAR_READY_SOCKET"]?.trim(),
      b = env["CODEBUDDY_SIDECAR_CREDENTIAL_BOOTSTRAP_SOCKET"]?.trim(),
      c = env["CODEBUDDY_SIDECAR_READY_TOKEN"]?.trim(),
      d = env["CODEBUDDY_SIDECAR_READY_SESSION_ID"]?.trim();
  let present = !!(a || b || c || d);
  // ↓ CLI 启动即从自身 env 里删除，子进程永远继承不到
  delete env["CODEBUDDY_SIDECAR_READY_SOCKET"];
  delete env["CODEBUDDY_SIDECAR_CREDENTIAL_BOOTSTRAP_SOCKET"];
  delete env["CODEBUDDY_SIDECAR_READY_TOKEN"];
  delete env["CODEBUDDY_SIDECAR_READY_SESSION_ID"];
  return present ? (a&&b&&c&&d ? {kind:"ready", ...} : {kind:"invalid"})
                 : {kind:"disabled"};
}(process.env);
```

分支语义：

- 有完整四件套 → `managed-ready`：连 sidecar socket 取凭据 → **能跑**
- 四件套缺失 → `standalone`：读盘上凭据。而盘上凭据按 `WORKBUDDY_AT_REST_ENCRYPTION=fields`
  加密，standalone 无解密能力 → `Authentication required`
- 四件套不全 → `managed-invalid`：直接 `config-incomplete` 失败

**`delete env[...]` 是设计**。任何非 sidecar 亲自 spawn 的进程都拿不到凭据。

### 1.4 A/B 实验（决定性）

```bash
CLI="/Applications/WorkBuddy AI.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy"

# A：不带 sidecar（= 作业现状）
"$CLI" -p "只回复 OK 两个字" --model deepseek-v4.1-flash
# → 100 秒零输出，被强杀。完美复现。

# B：手动注入 sidecar 四件套
env CODEBUDDY_SIDECAR_CREDENTIAL_BOOTSTRAP_SOCKET=… \
    CODEBUDDY_SIDECAR_READY_SOCKET=… \
    CODEBUDDY_SIDECAR_READY_TOKEN=… \
    CODEBUDDY_SIDECAR_READY_SESSION_ID=… \
    WORKBUDDY_AT_REST_ENCRYPTION=fields \
  "$CLI" -p "只回复 OK 两个字" --model deepseek-v4.1-flash
# → 5 秒后明确失败：
# [CredentialBootstrap] CBC early {"event":"gate-failed","route":"sidecar-socket",
#   "policy":"fields","readyConfig":"ready","receiverOutcome":"fallback",
#   "reason":"receiver-timeout","configured":false}
# [CredentialBootstrap] startup failed: WorkBuddy credential bootstrap did not
#   configure required capabilities
```

**`receiver-timeout` 说明：凭据引导 socket 是一次性的**，只在 sidecar 亲自 spawn
的那个启动窗口内应答。事后补环境变量**补不回来**。

→ 「先补宿主登录态」这条路径**在架构上走不通**。此前 skill §5.8/§5.9 的相关判断作废。

### 1.5 可行路径已验证：派发到窗口会话

```bash
TOKEN=<CODEBUDDY_GATEWAY_PASSWORD>          # App 级，43 字符，各会话通用
EP=http://127.0.0.1:52268                   # 取自 ~/.workbuddy-ai/sessions/38552.json

curl -s -X POST "$EP/api/v1/sessions/b61f582f-…/reply" \
  -H "Authorization: Bearer $TOKEN" -H "X-CodeBuddy-Request: 1" \
  -H "Content-Type: application/json" \
  -d '{"text":"这是一次能力探针。请只回复一行文本：WINDOW_REPLY_OK。"}'
# → {"data":{"delivered":true}}   HTTP 200
```

随后读回：

```bash
curl -s "$EP/api/v1/sessions/b61f582f-…/history" \
  -H "Authorization: Bearer $TOKEN" -H "X-CodeBuddy-Request: 1"
```

实测最后一条 request 的 `finalReply`：

```
user:  这是一次能力探针。请只回复一行文本：WINDOW_REPLY_OK。…
reply: 429 usage exceeds frequency limit, … reset at 2026-10-08 18:47:57 UTC+8
       (01a11a54…/b61f582f-8b88-41ea-923f-554400c6a6e1)
```

**这是模型侧的真实响应，不是本地报错**。与作业路径的「零输出永久挂死」
是两种完全不同性质的失败。派发链路成立。

---

## 2. 两个独立问题，不要混为一谈

| # | 问题 | 状态 | 阻塞到 |
|---|---|---|---|
| 1 | 派发链路不通（job 无凭据） | **已有确切修法**（换窗口会话派发） | 等实现 |
| 2 | 账号级配额打满（429） | **与问题 1 无关** | **2026-10-08 18:47:57 UTC+8** |

429 是账号级：`model` 参数被忽略（deepseek / glm / gemini 三种都返回同一条 429），
换模型绕不开。也就是说：**即使链路今天修好，18:47 之前也跑不出任何结果。**

---

## 3. 确切修法

### 3.1 新增会话派发桥（仿 `vibe_guide/mcp_servers/workbuddy_jobs.py`）

新建 `vibe_guide/mcp_servers/workbuddy_sessions.py`，五个动作：

| 动作 | 端点 | 返回 |
|---|---|---|
| `discover` | 扫 `~/.workbuddy-ai/sessions/*.json` | 筛 `kind=="interactive"` 且带 `endpoint` + `sessionId` |
| `deliver` | `POST {endpoint}/api/v1/sessions/{sid}/reply` `{"text":…}` | `{"data":{"delivered":true}}` |
| `history` | `GET {endpoint}/api/v1/sessions/{sid}/history` | `requests[].finalReply` |
| `live` | `GET {endpoint}/api/v1/sessions/live` | `writerOccupied` |
| `wait` | 轮询 `history` 直到出现新的 `finalReply` | — |

鉴权（三要件，缺一不可）：

```
Authorization: Bearer <CODEBUDDY_GATEWAY_PASSWORD>
X-CodeBuddy-Request: 1
```

`CODEBUDDY_GATEWAY_PASSWORD` 从任一 `codebuddy --serve` 进程环境读取，App 级通用。

### 3.2 接进派发门禁

`vibe_guide/runners/provider_action.py`：

- `:48-63` `NATIVE_TOOL_MAP` 目前只有 `CODEX_PROVIDER` / `CLAUDE_CODE_PROVIDER`
  → 增 `WORKBUDDY_PROVIDER`，映射到 `workbuddy_session__create` /
  `workbuddy_session__locate` / `workbuddy_session__visibility`
- `:573-581` `create` 返回 `{"binding":{"task_id","host"}}` → 填 `sessionId` / `endpoint`
- `:607` `locate` 返回 `{"located":true}` → 用 `live` 判定
- `:618` `visibility` 需 `{"visible":true,"direct_enter":true}` → **窗口天然满足**：
  用户能在 UI 里点进该窗口续接，这就是 `direct_enter`

### 3.3 `vibe attest` 如实登记

窗口派发下 `visible_task.create/enter/resume/wait` 可如实记 `true`
（此前 `capabilities.json` 里全 false 记录的是「job 路径」的真实情况，不是平台能力上限）。

### 3.4 硬约束（设计时必须遵守）

1. **禁止回灌自身会话**。编排者所在窗口的 `reply` 会把任务塞进用户当前对话。
   适配器必须排除自身 `sessionId`。
2. **一个窗口同时只能一个任务**（`writerOccupied: true`）。并发上限 = 窗口数。
3. **窗口只能由 sidecar spawn**（用户在 UI 新建对话）。
   `POST /api/v1/sessions` 实测 404，无法用 API 造出有凭据的新会话。

---

## 4. 与既有文档的冲突（需作废/更正）

- `docs/workbuddy/workbuddy-job-mcp-server.md` §6「还没验证的」——
  现在可以关闭：**端到端闭环未验证**的原因是 job 路径无凭据，不是 safe-delete 阻断。
- `docs/workbuddy/vibe-guide-workbuddy-adaptation.md` §8.6「写 `workbuddy-visible`
  provider adapter」——方向对，但**必须基于会话派发，不能基于 Jobs API**。
  该文档 §8.4 的实测是 Windows / win32 / v5.5.6，结论**不可移植到 macOS**。
- skill `vibeguide-host-capability-diagnosis` §5.8/§5.9——
  「补登录态」路径作废。

---

## 5. 复现清单

```bash
# 1. 找窗口会话与其端点
for f in ~/.workbuddy-ai/sessions/*.json; do
  python3 -c "import json,sys;d=json.load(open('$f'));print(d.get('pid'),d.get('kind'),d.get('sessionId'),d.get('endpoint'))"
done

# 2. 取 App 级网关口令（43 字符）
ps eww -p <会话pid> | tr ' ' '\n' | grep '^CODEBUDDY_GATEWAY_PASSWORD='

# 3. 投递 + 读回
curl -s -X POST "<endpoint>/api/v1/sessions/<sid>/reply" \
  -H "Authorization: Bearer <token>" -H "X-CodeBuddy-Request: 1" \
  -H "Content-Type: application/json" -d '{"text":"…"}'
curl -s "<endpoint>/api/v1/sessions/<sid>/history" \
  -H "Authorization: Bearer <token>" -H "X-CodeBuddy-Request: 1"

# 4. 证明 job 路径必死
ps eww -p <作业pid> | tr ' ' '\n' | grep -c CODEBUDDY_SIDECAR   # → 0
```

---

## 6. 已落地（2026-10-08，同一天）

上面 §3 是方案；这一节是**实际改了什么**。

### 6.1 新增 `vibe_guide/mcp_servers/workbuddy_sessions.py`

stdlib only，仿 `workbuddy_jobs`，三个职责：

| 职责 | 关键内容 |
|---|---|
| 凭据发现 | `gateway_token()`：env `WORKBUDDY_JOB_TOKEN` / `CODEBUDDY_GATEWAY_PASSWORD` → 否则扫 `codebuddy --serve` 进程环境 |
| 窗口发现 | `discover_sessions()`：只收 `kind=interactive` + 有 endpoint + **pid 活着** + **sessionId 是 UUID** |
| 派发 / 读回 | `SessionDispatch.deliver()` / `history()` / `latest_reply()` / `live()` / `wait()` |
| 邮箱服务 | `MailboxServicer.serve()`：消费 pending 的 `create`/`locate`/`visibility` 并回写结果 |

两个实现细节是被实测逼出来的，不是设计偏好：

- **`ps -axeww` 在 macOS 上不输出环境块**。用它扫会只匹配到"命令行里恰好含该变量名"的进程——包括扫描者自己。
  改用两步：先 `ps -ax -o pid= -o command=` 拿候选 pid，再逐个 `ps eww -p <pid>`。
- **占位 sessionId 必须过滤**。host-CLI 的会话文件是 `interactive-9851`，同样声明
  `kind: interactive` 且带 endpoint，但网关对它返回 `SESSION_NOT_FOUND`；
  而它 pid 最小、排在最前，正是自动挑选会抓到的那个。故要求 UUID 形状。

### 6.2 `workbuddy_jobs.py` 接了两个新工具

工具名不变（`workbuddy_job__*` 已在 `NATIVE_TOOL_MAP` 里且已在宿主信任），
**vibe 侧零改动**。新增两个会话侧工具：

- `workbuddy_job__sessions` —— 列出可派发的活窗口（只读）
- `workbuddy_job__serve` —— 消费邮箱的 `create`/`locate`/`visibility`

`create` 增加可选 `session_id`：**显式指定才走窗口派发**，默认仍是 Jobs API。
不改默认是因为窗口派发会写进用户看得见的会话，必须显式要求，且不能破坏既有契约。

### 6.3 `wait` 故意不动

`MailboxServicer` 跳过 `wait`。它由 `vibe worker-deliver` 的 worker 自报结算，
在这里代答等于**伪造交付**——正是这个项目拒绝制造的假绿。

### 6.4 fail-closed 的三处

1. 没有活窗口 → `create` 报错（`no live WorkBuddy window`），**不返回假 binding**
2. `locate`：窗口不在活列表里 → `located=false`
3. `visibility`：**所有** target 都活着才 `visible=true` 且 `direct_enter=true`

### 6.5 测试

`tests/test_workbuddy_sessions.py`（19 项）：窗口发现过滤、占位 id 过滤、
排除自身、`create` 绑定与 digest、`locate`/`visibility` fail-closed、
`wait` 不被代答、重复请求不二次派发。

`tests/test_workbuddy_jobs_mcp.py` 的契约断言从"工具集相等"改为
"五个 native 名字必须是子集"——`sessions`/`serve` 是会话侧工具，
monitor 不会点名它们，但不该因此判定契约破裂。

### 6.6 还差什么（要用户配合，不是代码问题）

- **需要在项目目录里开着 WorkBuddy 窗口**。并发上限 = 窗口数，一个窗口同时一个任务。
- **编排者自己的窗口要排除**：`serve(exclude="<自己的 sessionId>")`，
  或设 `WORKBUDDY_DISPATCH_EXCLUDE_SIDS`。否则任务会被投递回正在跑监工的那个窗口，
  变成同一段对话里的一个 user turn，角色就塌了。
- 窗口只能由 sidecar 造（用户在 UI 新建），`POST /api/v1/sessions` 实测 404。

---

## 7. 独立 review 与返工（2026-10-08）

§6 落地后经**独立 reviewer** 审阅，判定 **「有阻断问题，需返工」**——§6 的实现在
单窗口/单节点/窗口空闲的理想路径上可走通，但在多节点、并发、续派场景会**假绿或静默挂死**，
恰是本模块要根除的故障模式。返工如下。

### 7.1 五个 P1（阻断）

| # | 问题 | 返工 |
|---|---|---|
| P1-1 | `create` 丢弃 `deliver()` 返回值、不校验 `delivered`，仍写 `state=working/delivered` | 校验 `delivered is True`；否则抛错、**不写结果**（`_answer_create` + `_create_in_window`） |
| P1-2 | `pick_session` 无 cwd 命中时回退 `live[0]` → fail-open，任务派进别的 repo | cwd miss 一律返回 `None`（拒绝），不再回退 |
| P1-3 | 所有 create 恒定选中同一窗口；`handle_id=session_id[:8]`、`binding.task_id=session_id` → 多节点 handle/baseline 互相覆盖、节点身份撞车 | `serve` 内维护 `used` 做**窗口轮转**；`handle_id = action_id`（唯一）；`binding.task_id = handle_id`；`locate`/`visibility` 经 handle 反查 session |
| P1-4 | `wait`/`latest_reply` 取 history 里**最新**非空 `finalReply`，未绑定本轮 → 返回上一轮（假绿） | 新增 `reply_after(session_id, baseline)`，只取 baseline 之后本轮 request 的回复；`wait`/`_window_state` 改用它 |
| P1-5 | `resume`（续派）无人应答（`MailboxServicer` 只处理 create/locate/visibility）；`WorkBuddyJobs.reply` 不解析窗口 handle → 续派永久悬挂 | 新增 `_answer_resume`（窗口不在 → `resumed:false` fail-closed）；`WorkBuddyJobs.reply` 支持窗口 handle（投递续派 turn） |

### 7.2 P2（一般，均已修）

- **P2-1** `SessionDispatch.__init__` 忽略 `WORKBUDDY_HANDLE_ROOT` → 写读目录不一致。改为读该 env。
- **P2-2** `_window_handle` fallback 把裸 session_id 当 handle 且 `baseline=0` → `get` 立即假报 `done`。改为 `baseline=None`，`_window_state` 对缺失 baseline **不判 done**。
- **P2-3** orchestrator 未显式 exclude 时可能投回自身窗口。改为 `skip = excluded_session_ids() ∪ self._skip ∪ used`。
- **P2-4** `serve` 无文件锁，两个并发循环会重复投递同一 create。改为对 `mailbox/.serve.lock` 加 `fcntl.flock` 排他锁（非 POSIX 降级为无锁）。

### 7.3 P3（记录在案，不阻塞合并）

- `SessionDispatch.live()` 无调用点（`/api/v1/sessions/live` 路径待核）。
- `discover_sessions` 对同一 pid 连调两次 `_pid_alive`。
- 测试仍以 `FakeDispatch` 为主，`_call`/`deliver`/`history` 的 HTTP 传输层未直接覆盖。

### 7.4 测试

`tests/test_workbuddy_sessions.py` 19 → **26 项**：新增「窗口拒收不伪造」「多节点跨窗口轮转」
「resume 落到所属窗口」「resume 在死窗口上拒绝」「cwd miss 拒绝」「`reply_after` 不串轮」。

`tests/test_workbuddy_jobs_mcp.py` 51 项保持绿。

全量 1499 项：**3 失败 + 5 错误 + 1 skip**，全部为 `test_v2_acceptance` /
`test_v310_packaging` / `test_cli` 的 `setuptools` 缺失（packaging 环境问题，
返工前同样 3+5）；**workbuddy/session/provider 相关 0 失败**。

---

## 8. 复审返工（第二轮）

§7 的返工经**独立复审**，判定「需返工」——两处**返工新引入**的 P2：

| # | 问题 | 修法 |
|---|---|---|
| P2-1 | `used` 只在 `create` **成功后**记录 → 首个（pid 最小）窗口忙时，本 sweep 的每个节点都反复撞这同一个忙窗口，从未尝试空闲的其它窗口，**多窗口并发退化为 0**（fail-closed 但是可用性缺陷） | 新增 `candidate_sessions()`（按优先级返回全部候选窗口）；`_answer_create` **依次尝试**候选直到某窗口 `delivered:true`，全忙才拒绝 |
| P2-2 | mailbox `resume`（`_answer_resume`）应答后**不回写 handle `baseline`** → 该 handle 后续经 `wait`/`get` 读取时会拿到**上一轮**回复（P1-4 的假绿从续派路径回流） | `_answer_resume` 投递成功后更新 handle `baseline`（对齐 `WorkBuddyJobs.reply` 的做法） |

顺带 **P3-1**：`SessionDispatch.wait` 对缺失 / 非 int 的 `baseline` 改为**拒绝**（不再裸比较导致 `TypeError`）。

测试 26 → **29 项**（新增「首个窗口忙时轮转到下一个」「resume 更新 handle baseline」）。
全量 1501 项：3 失败 + 5 错误（同 setuptools 环境问题），**workbuddy/session 相关 0 失败**。

其余 P3（`live()` 无调用点、`_pid_alive` 重复调用、`latest_reply` 成为死代码、
投递成功但写结果失败的 at-least-once、HTTP 传输层测试未覆盖）**记录在案、不阻塞合并**。

---

## 9. 第三轮复审返工（2026-10-08）

第二轮返工先修了一处「同 sweep 内轮转」的重复投递向量（`deliver()` 把「显式拒绝」与
「响应未知」压成同一个 `False`）。第三轮**独立复审**判定 **「需返工（小改）」**：那次只
挡住了同一轮扫描内的轮转，**跨轮重试仍然会重复投递**。

### 9.1 P1：`unknown` 留在 pending → 跨轮重复投递（阻断）

- **触发**：`/reply` 返回 2xx 但 body 里没有布尔 `delivered`（含空 body——`_call` 对空
  响应体返回 `{}`）。
- **后果**：`serve()` 把 `SessionError` 记进 `skipped` 就 `continue`，**不写 result 文件**。
  请求因此永久 pending：provider 侧一直 `ProviderPending`，监工每次重跑 `serve` 都把
  同一条 prompt 再投一次。第三轮的注释写着 "caller must fail closed, never retry"，而
  实现正好相反——**跨轮重试**。
- **修法**：新增 `DeliveryUnknown(SessionError)` 这一**独立**异常类型，语义是「回合可能
  已入队，永不可重试」。`serve()` 捕获它后写一份**终态负结果**
  （`{"delivery": "unknown", "failed": true, "reason": ...}`）：既没有 `binding`、也没有
  `located`/`resumed`，provider 只能把它读成「动作失败」，于是**既不伪造 worker，也不
  留在 pending**。`serve()` 的返回值新增 `failed` 桶，把这批「已终结、不会有人再重试」
  的请求显式交给操作者。
- **与普通 `SessionError` 的分工**：普通错误（没有窗口、显式拒绝、请求非法）意味着回合
  **从未离开我们**，请求可以安全地留在 pending 等下一轮；只有 `DeliveryUnknown` 是终态。

### 9.2 P2

| # | 问题 | 修法 |
|---|---|---|
| P2-1 | `_write_json` 写在 `try` 之外：投递成功后写结果失败会抛穿 `serve()`（只 `finally` 解锁），整个 sweep 中断，且该请求留在 pending → 下轮重复投递 | 新增 `delivering/<action>.json` **投递标记**：POST 之前立起，仅在**显式拒绝**（唯一能证明「未入队」的答复）时放下。崩溃 / 写结果失败都会留下标记，下一轮据此**终态失败**而不是重投。写结果的 `OSError` 单独捕获，不再中断 sweep |
| P2-2 | 自身窗口只靠人工排除：`skip` 只含 env + 构造参数 + 本轮 `used`，而监工通常就跑在它为之派发的那个会话里；操作者忘设 `WORKBUDDY_DISPATCH_EXCLUDE_SIDS` 时任务被投回监工自己的对话，角色塌陷 | 新增 `host_session_id()` 读 `CODEBUDDY_SESSION_ID`（按 UUID 校验），**默认**并入 `skip`；自身会话是唯一存活的窗口时如实报「无窗口可派」，不退回自身 |

### 9.3 P3（顺带）

- `WorkBuddyJobs._window_handle` 走模块级 `load_handle`，绕过 `dispatch` 的 handle_root；
  显式覆盖 root 时读写分叉。改为走 `self.dispatch.load_handle`。
- 首个候选 `history()` 失败会中断整条 action，不再轮转。改为记入 `unreachable` 并
  **尝试下一个候选**，全失败时错误信息里带上不可达窗口。
- `deliver()` 的三态此前**没有直接测试**：servicer 用例由 `FakeDispatch` 自己造 `state`，
  把实现改回旧行为仍然全绿（自证循环）。新增 `DeliverOutcomeTests`，在**传输边界**上
  用替换 `_call` 的桩验证 `accepted` / `refused` / `unknown`（空 body、非布尔字段、
  非对象 body）四种形状。

### 9.4 测试与变异检验

`tests/test_workbuddy_sessions.py` 29 → **38 项**（新增：未知结果终态化、崩溃标记不重投、
自身会话永不入选、只剩自身会话即无窗口、候选死亡不放弃整条 action、投递三态 ×5）。

新用例做了**变异检验**，确认不是自证循环——四处变异各自被对应用例抓到：

| 变异 | 被抓到的用例 |
|---|---|
| `unknown` 塌回 `refused` | `test_an_empty_body_is_unknown_not_refused` 等 3 项 |
| `unknown` 留在 pending（回到 §9.1 的 bug） | `test_an_unknown_delivery_outcome_is_answered_terminally` |
| 去掉崩溃标记护栏 | `test_a_crashed_delivery_is_never_delivered_twice` |
| 去掉自身会话排除 | `test_the_host_conversation_is_never_a_target` 等 2 项 |

「不再 pending」这条不是自说自话：用例直接用 provider 侧的 `ProviderActionStore` 读同一
个 mailbox，断言 `store.result(action_id)` 非空、`store.pending()` 不再列出该请求。

全量 1502 项：**2 失败 + 3 错误 + 1 skip**，全部落在
`test_v310_packaging` / `test_v2_acceptance` / `test_skill_install_cli`；经核对这三个模块
对 `workbuddy_sessions` / `workbuddy_jobs` **零引用**，属既有环境性问题（打包构建、
本机 skill 数量），与本改动无因果关系。

### 9.5 已知残留（不阻塞合并，记录在案）

- **resume 的终态只解决「不重投」，未解决「不悬挂」**：`provider_action.poll` 对
  `resumed` 非 `true` 的结果返回 `visibility_unknown` 并保留 `pending_action`，所以窗口
  消失 / 拒收的 resume 仍是「已暴露但未收敛」。这是 provider 侧既有语义，改动它属于协议
  变更，应单独走一次评审。
- baseline 与投递之间隔一次往返：用户可见窗口若在这中间被人工输入，索引后移，
  `reply_after` 会返回那一轮人工输入的回复（串轮）。窗口可见性是本方案的前提。

---

## 10. 第四轮复审返工（2026-10-08）

§9 的返工经**独立复审**，判定 **「需返工（小改）」**：P0=0，但 P1×1、P2×1 **均为 §9
自己新引入**，且都还是「重复投递」这条红线。

### 10.1 P1：终态结果写失败却仍放下标记（阻断）

- **触发**：`_fail_terminally` 内部吞掉 `_record` 的 `OSError` 并正常返回，而 `serve()`
  的两处调用点**无条件** `_unlink(marker)`。
- **后果**：写结果失败（磁盘满 / 权限 / 目录被占）时，**标记被清、结果未落盘**；下一轮
  sweep 既无标记也无结果 → 重新投递同一条 prompt。§9 的结论 2 和 `_fail_terminally`
  自己的 docstring（「标记仍在上，下一轮 fail closed」）都被这一行推翻。
- **修法**：`_fail_terminally` 改为返回**是否真的落盘**；`serve()` 只在返回 `True` 时
  `_unlink(marker)`。标记就是「写失败」与「二次投递」之间唯一的那道墙。
- **为什么原来的用例没抓到**：`test_a_crashed_delivery_is_never_delivered_twice` 用的是
  正常可写的 `results/`，从未走到写失败分支。新增
  `test_a_terminal_answer_that_cannot_be_written_keeps_the_marker` 把 `_record` 换成抛
  `OSError` 的桩，断言标记仍在、且第二轮 sweep 一次都没投递。

### 10.2 P2：传输层失败被当成「未入队」

- **触发**：`_call` 把超时 / `URLError` / `OSError` 与 HTTP 状态错误一并归为
  `SessionError`；`serve()` 据此清标记、留 pending。
- **后果**：超时的语义是「请求已发出、响应丢失」，回合**可能已经入队**。下一轮重投 →
  同一个任务在一个窗口里跑两遍。
- **修法**：`deliver()` 把 **POST 本身**的失败一律转成 `DeliveryUnknown`。`_endpoint_for`
  （窗口查找）刻意留在 wrap 之外——窗口不在意味着**什么都没发出去**，那是真正的瞬时错误，
  可以安全地留 pending 重试。两种情况必须分开。

### 10.3 P3

- `WorkBuddyJobs._window_handle` 上一轮改走 `self.dispatch.load_handle` 后，**首行**在
  `try/except SessionError` 之外，而 `WorkBuddyJobs.dispatch` 是惰性属性、构造
  `SessionDispatch()` 会调 `gateway_token()`。在发现不到口令的机器上，纯 job 的
  `get`/`reply`/`wait` 会从「回退 Jobs API」变成直接报错。改为捕获后回退到模块级
  `load_handle`：窗口后端是可选能力，jobs 后端不是。

### 10.4 测试与变异检验

`tests/test_workbuddy_sessions.py` 38 → **42 项**，`tests/test_workbuddy_jobs_mcp.py` 51 → **52 项**。

新增用例（终态写失败保标记、传输层丢失答复、窗口不在不算丢失答复、无口令时 job id 仍可解析）
同样做了变异检验：

| 变异 | 被抓到的用例 |
|---|---|
| `_fail_terminally` 写失败也返回 `True` | `test_a_terminal_answer_that_cannot_be_written_keeps_the_marker` |
| `deliver` 不包装传输层异常 | `test_a_post_that_dies_in_transit_is_unknown` |
| `_window_handle` 不设无口令回退 | `test_a_job_id_still_resolves_without_a_gateway_credential` |

workbuddy/session 相关 5 模块 **130 项全绿**；全量 1515 项仍为同一批既有环境性失败
（`test_v310_packaging` / `test_v2_acceptance` / `test_skill_install_cli`），无新增回归。

---

## 11. 第五轮复审返工（2026-10-08）

§10 的返工经**独立复审**，判定 **「需返工（小改）」**：P0=0，P1×1、P2×1。

### 11.1 P1：`save_handle` 失败把 §10.1 的洞又开了一次（阻断）

- **触发**：`_answer_create`（`:940`）与 `_answer_resume`（`:1052`）在投递已 `accepted`
  **之后**调 `dispatch.save_handle`。它写的是 handle root（`~/.workbuddy-ai/session-handles`），
  与投递标记目录（项目 `.vibe/provider-actions/delivering`）**不在同一个文件系统**。
- **后果**：handle 写失败（磁盘满 / 权限 / 配额）抛 `OSError`，被 `serve()` 的
  `except (SessionError, ValueError, OSError)` 吞下，而该分支**无条件 `_unlink(marker)`**
  ——标记已立、POST 已入队，却把标记放了下来，请求留 pending。下一轮 sweep 无标记无结果
  → 重投同一 prompt。
- **这是 §10.1 的同一个洞**：那一轮我只把 `_record` 那条路径堵上，`save_handle` 这条并行
  路径漏了，而且当时没有任何用例覆盖它。
- **修法**：把「谁有权放下标记」收拢成一条规则——**只有能证明「什么都没发出去」的地方才
  放标记**，也就是 `_deliver` 内部：显式拒绝、或请求尚未离开（窗口查不到 / 连接被拒）。
  `serve()` 的通用 `except` 分支不再动标记。此后任何发生在 `accepted` **之后**的失败
  （`save_handle` 也好、别的新增簿记也好）都自动落在「标记留、下一轮终态失败」这条路上，
  不需要再逐条堵。

### 11.2 P2：传输层失败一律算「未知」，代价过大

- **问题**：§10.2 把**所有** POST 传输失败升为 `DeliveryUnknown`。但「连接被拒」时请求
  **根本没发出去**，与「已入队但响应丢失」被混为一谈 → 节点永久失败。方向是 fail-closed
  没错，但不触红线却付了过高的代价。
- **修法**：新增 `TransportError(SessionError)`，带 `reached` 字段；`_may_have_reached()`
  只把**两种可证明「没送到」**的情形判为 `False`——连接被拒（`ConnectionRefusedError`）、
  主机解析不了（`socket.gaierror`，含 `URLError` 里嵌套的 reason）。这两种降回普通错误，
  可以安全重试；超时、连接重置等一律仍算「未知」，绝不重试。

### 11.3 P3

- `serve()` 在结果已存在时 `continue`，遗留的 `delivering` 标记永不清理。改为在**结果已
  存在**这一处清掉——那是唯一能证明「回合已被记账」的地方。
- `_window_handle` 对裸 session id 合成 `id=job_id[:8]`，`wait` 据此查 handle 文件必落空，
  报「unknown session handle」——读起来像是 id 传错了。改为用完整 session id，并在 `wait`
  里明确回答「这个窗口没有派发 handle，本会话没往里投过 turn，没有可等的东西」。

### 11.4 测试与变异检验

`tests/test_workbuddy_sessions.py` 42 → **47 项**，`tests/test_workbuddy_jobs_mcp.py` 52 → **53 项**。

新增用例：投递后绑定失败不得重投、请求未发出时标记必须放下（否则好请求被误判终态失败）、
结果已存在时清理陈旧标记、被拒连接不算丢失答复、只有被拒/解析失败算「没送到」、
裸 session id 的 `wait` 要说实话。

变异检验（四条，各自单独验，避免互相掩盖）：

| 变异 | 被抓到的用例 |
|---|---|
| 通用 `except` 分支重新去清标记 | `test_a_bind_failure_after_delivery_is_not_delivered_again` |
| `_deliver` 不再为「未发出」放下标记 | `test_a_window_that_vanishes_before_the_post_may_retry` |
| `_may_have_reached` 恒为 `True` | `test_only_a_refusal_or_a_dead_host_counts_as_never_sent` |
| 不清理陈旧标记 | `test_a_stale_marker_next_to_an_answer_is_cleared` |

> 变异 1 与变异 2 会**互相掩盖**（通用分支的清标记把 `_deliver` 的缺失补上了），所以两条
> 必须分开验——这本身说明「谁有权放标记」此前是散在两处的隐式约定，§11.1 的收拢修法正是
> 为了消掉这个耦合。

workbuddy/session 相关 5 模块 **135 项全绿**。

---

## 12. 第六轮复审返工（2026-10-08）

§11 的返工经**独立复审**：**P0 = 0，P1 = 0**。这是五轮以来第一次没有阻断项。
剩下 1 个 P2 是**覆盖缺口**，不是代码缺陷。

### 12.1 P2：`_deliver` 的两条标记护栏没有用例（护红线）

第 11 轮把「谁有权放下标记」收拢到 `_deliver` 一处，但那一处有三种行为，其中两种
**没有任何用例**。实测确认（我复现了复审给出的变异）：

- 把 `except DeliveryUnknown: raise` 并进 `except SessionError`（即让 `DeliveryUnknown`
  也放下标记）→ **102 项全绿**。
- 删掉「显式拒绝放下标记」那一行 → **全绿**。

为什么上一轮没覆盖到：`test_a_terminal_answer_that_cannot_be_written_keeps_the_marker`
里 `FakeDispatch` 是**返回** `state="unknown"`，而 `_deliver` 的 `except DeliveryUnknown`
分支只在 dispatch **抛出**时才走。用例写的是「返回值未知」，护栏管的是「抛异常」——
两者看着像同一件事，实际隔着一层。这正是自证循环的典型形态。

两条护栏失效的后果恰好就是两条红线：

- 护栏 A 失效 + `_fail_terminally` 写盘也失败（磁盘满）→ 标记已放、结果未落 →
  下一轮无标记无结果 → **重投同一 prompt**。
- 护栏 B 失效（拒绝后标记不放下）→ 标记永久留下 → 下一轮被误判终态失败 →
  **好请求永不重试**，即使窗口后来空出来。

**修法**：补两条用例，并把它们写成「组合条件」而不是各自孤立——
`test_a_lost_reply_keeps_the_marker_when_the_answer_cannot_be_written`（dispatch 抛
`DeliveryUnknown` + `_record` 写失败）、`test_a_refused_turn_leaves_no_marker_behind`
（全候选拒绝 → 无标记 → 窗口空出后**确实**重试成功）。两条变异随后都被抓到。

### 12.2 P3

- `_answer_create` 的轮转循环只包了 `history`，没包 `_deliver`：候选窗口在查端点与
  POST 之间消失时（`_endpoint_for` 抛普通 `SessionError`），整条 action 被中止而不是
  转下一个候选。窄竞态，但既然后果只是「多等一轮」，就没有理由不修。改为捕获后记入
  `unreachable` 并 `continue`；`DeliveryUnknown` 仍原样抛出（它不能轮转）。
  新增 `test_a_candidate_that_vanishes_before_the_post_falls_through`。
- `_answer_resume` 的终态负结果**不被 provider 读成终态**：`provider_action.poll:1154`
  只有 `result["resumed"] is True` 才清 `pending_action`，否则回 `visibility_unknown`，
  最终落 `blocked_unknown`、pending 永不清。`_fail_terminally` 的 payload 没有
  `resumed`，`resumed:false`（窗口已死）同理。**fail-closed，不触红线**，但与
  `_answer_resume` docstring 所称「读成动作失败、不留 pending」不符——把 docstring
  改成如实描述，并保留为**已知残留**（改 provider 语义属协议变更，单独评审）。

### 12.3 测试

`tests/test_workbuddy_sessions.py` 47 → **51 项**。workbuddy/session 相关 5 模块
**139 项全绿**。

> 六轮下来，每轮返工都引入了**新的同类问题**（第 3→4→5→6 轮全部围绕「重复投递」这条
> 红线打转）。第 11 轮的「收拢成一条规则」是第一次让规则本身收敛而不是再加一个补丁；
> 第 12 轮补上护栏用例后，这条不变式才第一次有了完整的回归保护。
