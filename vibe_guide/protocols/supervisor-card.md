# 监工卡（每班一次）

见 prd-guide §6。

## r4 采纳门
仅完整规则显式采纳、同 plan/run 重授权且 effective 后覆盖旧规则；部分/未知不启用。监工在下一心跳或唤醒诊断、具体帮助并复核原失败步骤。事实/判断/未知、已试行动、责任方、下一动作及验证条件随换班保留；通知去重不停止处理。缺材料找原责任方，必需材料未知不 accepted、不解锁。撤销屏障先落盘，旧回执仅对账。

daemon 只调度/记事实/消费，不另建修复 LLM、不跨平台。失活先核 owner、进程/启动事实及冲突；未知不得双启。新 owner 两个连续有效周期才算接班。产品 runtime 防同名包遮蔽，系统权限另授权。

恢复唯一认领，绑定 owner epoch、角色/任务/代次及授权/合同；超时先对账，不清 active 重发。至少三次真实完成失败才考虑接续；发送接受、轮询、格式拒收、review finding 不计。失格另隔离；均须可信终止/退役、释放写权、保留成果/root，idle/archive 不足。原生结果经 complete_native_mailbox_action + request_digest 留 raw/投影，冲突追加，setup pending 不猜身份。阻塞实例见 blocked-engineering-examples.md；scope 走受保护事务。不可见降级需新外部授权，平台交接不豁免。

## 每一拍
只运行给定 `supervisor-beat`，按 state：
- `idle`/`stop`：结束本轮，等唤醒，不再 beat。
- `work`/`unknown`：服务信箱一轮，记未知原因；仅输出给出 `resume` 时执行，否则由守护程序推进。
- `decision`：呈现未答卡片；心跳不停，工程 worker_questions 自答。
- `contract_drift`：停推进，同 run 重授权。
- `rotate`：新会话接班；Claude Code 不换班，context_pressure 仅提示。

## 信箱
旧规则失活只报，重启归 launchd/产品经理；r4 按门恢复。消息只送一次；`vibe daemon inbox --peek` 只读。

## 回写
vibe/worker-deliver/supervisor-address 均带 `--project-root <绝对路径>`（或 VIBE_PROJECT_ROOT，参数优先）。信箱用 complete_mailbox_action；按字段提示修形状，开发交付带 delivery_evidence。

## locate
照抄 native_args，不要多传。Codex navigate_to_codex_page 仅 `{"threadId": <task_id>}`；真实可定位才回 `{"located": true}`。

## 边界与停止
产品/验收/设计变化、deploy、凭据/权限、授权外不可逆动作、新依赖、不可见降级才问；工程问题按 prd-guide §4 自决留痕。旧规则交接后台先排空并披露；r4 需授权。
停 claude-bg 用 vibe 停止入口，不用 claude stop；自停先落盘，无记录 stopped 重试一次再 failed。codex/kimi-code/workbuddy 停止来源未验证，只记 vibe 自停；stopped 判 failed，不证明退役。

## 禁止
自报不作证据，未知不当成功。不执行 monitor --authorize、vibe authorize、带 digest 的 decision-answer。review_finding 带 target_node_id；原范围内或 scope-expand 无新增文件直接返工。
