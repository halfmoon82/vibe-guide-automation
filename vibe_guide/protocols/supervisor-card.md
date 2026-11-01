# 监工卡（每班一次，短版运行手册）

完整协议见 prd-guide §6；本卡只给每拍动作。

## 每一拍
1. 只跑心跳指令里的 `supervisor-beat`，按输出 state 走，不自己重算。
2. `idle`：本轮结束，不要再调用，等待下一次唤醒。
3. `stop`：同上，立即结束本轮。
4. `work`：服务信箱一轮（请求、交付、结果）。输出写「推进由守护程序完成」时不手工 `resume`；仅输出给出 `resume` 命令（守护程序不在线）才用。
5. `decision`：呈现未答决策卡片等答案；心跳不暂停。`worker_questions` 工程问题自答，不呈现给用户。
6. `unknown`：按 work 处理并写明原因；同因连续出现且只有用户能解才停下报告。
7. `contract_drift`：停止推进，走同一运行的重新授权。
8. `rotate`：新开会话接班（仅非 Claude Code）；Claude Code 永不换班，`context_pressure` 只是提示。

## 守护程序与收件箱
- 输出含「守护程序失活」只上报那行和原因，不执行 `daemon start`；重启归 launchd 或产品经理。
- 输出里的「产品经理待处理消息」只送达一次，照 `decision` 呈现；`vibe daemon inbox` 读未读，`--peek` 只看不推进。

## 项目根
- `vibe` 子命令都带 `--project-root <绝对路径>`（或 `VIBE_PROJECT_ROOT`，参数优先），不依赖当前目录；
  给 worker 的 `worker-deliver`、`supervisor-address` 示例必须带它。相对路径或不存在的目录被拒绝。

## 回写
- 信箱结果用 `complete_mailbox_action` 写；形状不对写入前被拒，错误列出缺少或多出的字段名，补齐后重写。
- 开发者交付带 `delivery_evidence`。

## locate
- 照抄信箱 `native_args` 形状，不要多传：Codex `navigate_to_codex_page` 只传 `{"threadId": <task_id>}`，多传 hostId/page 即参数错误。
- `{"located": true}` 只在真实可定位时回写。

## 决策边界
- 只有改变 PRD 决策（产品范围、验收判定、设计）、部署发布、凭据权限、授权卡外不可撤销动作、新增第三方依赖、可见降级为后台不可见，才问用户。
- 其余工程问题（基线、方案取舍、重试，路径范围按 prd-guide §4 判据）自行决定，理由写进证据后继续；不提问、不替 worker 转问、不等人。拿不准选可回退的做法。

## 停止会话
- 停 claude-bg 用 vibe 的停止入口，不用 `claude stop`。vibe 自停先落盘；无记录的 stopped 视为意外，只重试一次，再 stopped 判 failed。
- codex、kimi-code、workbuddy：未验证停止发起方，只记 vibe 自己发起的停止，stopped 仍判 failed。

## 禁止
- idle/stop 后不再调用 beat。
- 不把自报当成证据；未知状态不当成功。
- 不执行 `monitor --authorize`、`vibe authorize`、带 digest 的 `decision-answer`（产品经理命令）。
- 整合审查 `review_finding` 带 `target_node_id`；文件在其原范围内、`scope-expand` 回“无新增文件”都直接返工，不问用户。
