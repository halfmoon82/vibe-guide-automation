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
