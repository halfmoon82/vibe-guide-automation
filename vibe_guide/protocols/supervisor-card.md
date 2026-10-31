# 监工卡（每班一次，短版运行手册）

完整协议见 prd-guide §6；本卡只给每拍动作。

## 每一拍
1. 只跑心跳指令里的那一条 `supervisor-beat`，按输出 state 走，不自己重算。
2. `idle`：本轮结束，不要再调用，等待下一次唤醒。
3. `stop`：同上，立即结束本轮。
4. `work`：服务信箱一轮（请求、交付、结果）。输出写「推进由守护程序完成」时不要手工 `resume`；仅当输出给出 `resume` 命令（守护程序不在线）才用它推进。
5. `decision`：呈现未答决策卡片等答案；心跳不暂停。`worker_questions` 工程问题先自答，不呈现给用户。
6. `unknown`：按 work 处理并写明原因；同因连续出现且只有用户能解才停下报告，工程原因自处。
7. `contract_drift`：停止推进，走同一运行的重新授权。
8. `rotate`：新开会话接班（仅非 Claude Code）；Claude Code 永不换班，`context_pressure` 只是提示，同会话继续。

## 守护程序与收件箱
- 输出含「守护程序失活」只把那行连原因上报，不执行 `daemon start`、不重启；重启归 launchd 或产品经理手工。
- 输出里的「产品经理待处理消息」只送达一次，照 `decision` 呈现给用户；
  `vibe daemon inbox` 读未读消息，`--peek` 只看不推进。

## 项目根
- 所有 `vibe` 子命令都可带 `--project-root <绝对路径>`（或环境变量 `VIBE_PROJECT_ROOT`，参数优先），
  不依赖进程当前目录；给 worker 的 `worker-deliver`、`supervisor-address` 示例必须带它。
  相对路径或不存在的目录会被拒绝。

## 回写
- 信箱结果用 `complete_mailbox_action` 写；形状不对在写入前被拒，错误里列出缺少或多出的字段名，按提示补齐后重写。
- 开发者交付必须带 `delivery_evidence`。

## locate
- 照抄信箱 `native_args` 形状，不要多传参数：Codex `navigate_to_codex_page` 只传 `{"threadId": <task_id>}`，多传 hostId/page 即参数错误（#316）。
- `{"located": true}` 只在真实可定位时回写。

## 决策边界
- 只有改变 PRD 决策（产品范围、验收判定、设计）、部署发布、凭据权限、授权卡外不可撤销动作，以及新增第三方依赖、可见降级为后台不可见，才问用户。
- 其余工程问题（基线、方案取舍、重试，路径范围按 prd-guide §4 判据）你自行判断决定，理由写进证据后继续；不提问、不替 worker 转问、不等人。拿不准选不改变 PRD 决策、可回退的做法。

## 手动停止会话
- 停 claude-bg 用 vibe 的停止入口，不用 `claude stop`。vibe 自停的停止先落盘；无记录的 stopped 视为意外，只自动重试一次，再 stopped 判 failed。
- codex、kimi-code、workbuddy：未验证停止发起方，只记录 vibe 自己发起的停止，其余不当意外处理，stopped 仍判 failed。

## 禁止
- 不在 idle/stop 后再调用 beat。
- 不把自报当成证据；未知状态不当成功。
