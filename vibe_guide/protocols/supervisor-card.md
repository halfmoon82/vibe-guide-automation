# 监工卡（每班一次，短版运行手册）

完整协议见 prd-guide §6；本卡只给每一拍的动作。

## 每一拍
1. 只跑心跳指令里的那一条 `supervisor-beat`，按输出 state 走，不自己重算。
2. `idle`：本轮结束，不要再调用，等待下一次唤醒。
3. `stop`：与上次相同，立即结束本轮。
4. `work`：服务信箱一轮（请求、交付、结果）。输出写「推进由守护程序完成」时不要手工
   `resume`；只有输出给出 `resume` 命令（守护程序不在线）才用它推进。
5. `decision`：把未答决策卡片呈现给用户，等答案；心跳不暂停。
6. `unknown`：按 work 处理并写明原因；同一原因连续出现就停下报告。
7. `contract_drift`：停止推进，走同一运行的重新授权。
8. `rotate`：新开会话接班（仅非 Claude Code）；Claude Code 永不换班，
   `context_pressure` 只是提示，同会话继续。

## 回写
- 信箱结果用 `complete_mailbox_action` 写；形状不对会在写入前被拒绝，
  错误里列出缺少或多出的字段名，按提示补齐后重写。
- 开发者交付必须带 `delivery_evidence`。

## 手动停止会话
- 要停 claude-bg 会话，用 vibe 的停止入口，不要用 `claude stop`。vibe 自己发起的停止会先落盘记录；没有记录的 stopped 视为意外停止，只自动重试一次，第二次仍 stopped 则判 failed。
- codex、kimi-code、workbuddy：未验证能否取得停止发起方，只记录 vibe 自己发起的停止，其余不当意外处理，stopped 仍判 failed。

## 禁止
- 不在 idle/stop 后再调用 beat。
- 不把自报当成证据；未知状态不当成功。
