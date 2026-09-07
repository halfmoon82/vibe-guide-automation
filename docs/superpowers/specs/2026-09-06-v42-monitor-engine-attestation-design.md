# V4.2 Monitor Engine Attestation 设计

状态：已确认方案 3，待实现

## 目标

为 V4.2 complex DAG 增加真实、结构化且可复核的 Monitor engine evidence 生成路径，替换 `unverified:legacy`，并让授权摘要与该证据绑定。

## 设计

1. 预检阶段读取当前 `plan_id`、`plan_revision`、`execution_engine=vibeguide_monitor`、`engine_mode=dag` 以及当前 Provider capability evidence，生成 `engine-attestation.json`。
2. attestation 保存 `evidence_ref`、输入摘要、生成时间和校验 digest；预检不创建 Run、worker、任务线程，也不执行 Git/Deploy。
3. 授权卡构建强制读取并校验 attestation，将 `engine_evidence_ref` 纳入授权摘要输入并重新计算 digest。
4. Monitor 启动和恢复时复核 attestation 与授权卡的 plan/revision/digest 绑定；缺失、过期、篡改或不一致保持 `blocked_unknown`。
5. 历史 V4.2 rev2 工件只读保留，不原地修补或复用。

## 测试与边界

- 无 attestation：阻断。
- 有效 attestation：允许进入授权准备，但不自动启动 Run。
- attestation 篡改、过期或跨 revision：阻断。
- 旧 rev2：行为不变。
- 不验证真实 worker/Run 生命周期，不包含 commit、push、PR/MR、merge、deploy、凭据或系统权限。

