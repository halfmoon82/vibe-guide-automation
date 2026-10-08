# Issue #154 完工唤醒实测证据

实测日期：2026-10-08（Asia/Shanghai）  
运行：`run-ce79518f6b1e489db738ffef70437a20`  
节点：`issue-154` generation 4  
基线：`c2a04abea77d1bb456d695f5e83a3627503a5c93`

## 验收判据

GitHub #154 要求分别实测 Codex 和 Claude Code 的 worker 完工唤醒；只有平台唤醒可靠时，后续节点才可将该平台默认心跳从 10 分钟放宽到 30 分钟。缺失工具或没有端到端接收证据时必须记为“未验证”，不能用 mock、自报或文件存在代替。GitHub #157 要求在不改变 PRD 验收判定的前提下自动继续，但不放宽授权、可见性或证据边界。

## 实测环境与绑定

- 唯一 writer root：`/Users/smy/Desktop/CFO/黑客松/开发辅助/.worktrees/issue-154-31e1a0dd`
- 分支：`node/issue-154-31e1a0dd`
- developer thread：`01a11ad9-70db-7601-9ad5-4b593b03474c`，host `local`
- 当前监工 thread：`01a11ad3-0b56-7080-9e5c-99163ab527f0`，host `local`
- 能力合同：`c9366e3596287d4fdf4a1549fe8f2902874922c8b6e0cf44de68e9fec79e9134`
- 唤醒信号不携带可信验收结论；监工仍从自报交付和 native wait 重新校验。

## Codex 桌面实测

| 样例 | 真实观测 | 判定 |
| --- | --- | --- |
| 地址查询 | 通过本 run 绑定的绝对 `runtime-bridge.py cli supervisor-address --json` 查询，返回 `status=ok`、provider `codex`、当前 supervisor thread `01a11ad3-0b56-7080-9e5c-99163ab527f0`。 | 成功；证明有可定位的当前目标。 |
| 推送能力 | 当前宿主工具清单包含 `mcp__codex_app__send_message_to_thread`；节点完工时按合同固定顺序，在自报落盘和地址复查后向上述当前监工发送固定格式信号。 | 发送尝试是本 generation 的真实完工动作；最终接收/处理证据由监工 native 任务日志核对。 |
| 失败样例 | 本 generation 未观察到发往当前监工的结构化失败；合同又禁止向无关或伪造 thread 故意发送。 | `unverified`；没有为凑样例制造越权外部通信。 |

Codex 的“可发送”不等于“已证明可靠”。本轮只有一次合法目标的完工样本，且失败路径和监工处理延迟没有完整分布证据，不足以承诺 30 分钟心跳间隔。

## Claude Code 实测状态

| 样例 | 真实观测 | 判定 |
| --- | --- | --- |
| 成功样例 | 当前 worker 会话的工具清单中没有 `ccd_session_mgmt__send_message`，也没有可进入的 Claude supervisor 会话绑定。 | `unverified`。 |
| 失败样例 | 2026-09-18 的真实 Claude 可见派发记录明确写明 `resume` / `ccd_session_mgmt__send_message` 未单独验证；另一条 `claude --bg` 路径当时因 `Not logged in`失败，而登录或新增凭据不在本节点授权中。 | 认证失败是真实历史样本，但不能证明桌面 `send_message` 本身不可靠；整体仍是 `unverified`。 |

本轮不安装 Claude 工具、不修改登录或凭据、不创建后台降级任务，也不以 Codex 结果类推 Claude。

## 结论与后续约束

- Codex：完工地址查询与真实推送路径可用，但本轮证据不足以将“可用”升格为“可靠”。
- Claude Code：当前会话无原生发送工具，真实完工唤醒仍未验证。
- 因两平台都没有满足“成功/失败样例齐全且端到端可靠”的门槛，后续 #156 **必须保留 10 分钟默认心跳**，不得改为 30 分钟。
- 后续若要放宽，必须对每个平台补齐：发送 API 成功、监工会话实际收到并开始处理、明确失败后心跳能在 10 分钟内兜底，以及多次样本下的延迟与丢失率。

## 证据引用

- 当前合同：`.vibe/runs/run-ce79518f6b1e489db738ffef70437a20/worker-contracts/issue-154-developer.json`
- 宿主工具清单：`.vibe/runs/run-ce79518f6b1e489db738ffef70437a20/setup-evidence/session-tool-inventory.json`
- 当前监工登记：`.vibe/runs/run-ce79518f6b1e489db738ffef70437a20/supervisor-registry.json`
- GitHub #154 冻结证据：`.vibe/runs/run-ce79518f6b1e489db738ffef70437a20/setup-evidence/issue-154-github.json`
- Claude 历史真实探针：`docs/superpowers/raw/2026-09-18-claude-code-visible-dispatch-probe.md`

## 一致性绑定

```json
{"authorization_digest":"cda7d896c01581973b29a070f04ee28adc80acaca0a6818a3623dc02977770ef","decision_digest":"f88feda89b4bd6b6b005f14616a91abe9ba9244f4321131e5a962e4df1eb93d8","issue_contract_digest":"ba5a3fe93c7575890fc5b3ec60d48a075cab2059ee3c7ada8971ccaf332eca81","plan_id":"session-f9a554fff82f6766","plan_version":1,"project_digest":"f84bf7410acd05aa9649810b3f56569819dba06a394fa2308d0837017fcfd727","schema_version":1}
```

`ISSUE_154_DELIVERY_COMPLETE`
