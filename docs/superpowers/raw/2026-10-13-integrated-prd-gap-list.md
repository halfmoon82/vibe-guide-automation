# 整合 PRD 落地差距清单（2026-10-13）

基线：main 94a5785（含 #182 原生合并、#183 安全修正）。对照：[监工整合 PRD](../specs/2026-10-13-supervisor-integrated-prd.md)。
来源：独立审查员按 PRD 静态核对 main 代码；两处安全类偏差（N5、N8）已由 #183 修复，不在下表。

## 待补（P1：PRD 要求未满足）

| # | PRD 条目 | 现状 | 要做 |
| --- | --- | --- | --- |
| 1 | N4 挂起与决策类别 | 枚举仍是 `product_design / external_authorization / deploy`；没有别名归一；决策信箱无类别；旧挂起显示 unknown | 改为两类规范值 + 两个兼容别名、落盘写规范值、`decision-raise` 共用枚举、旧记录按 `product_acceptance` 读；同步 prd-guide 与提示文案（AC-B4） |
| 2 | §5.3 五态兼容 | 没有 `--accept-decision`，旧版心跳也会收到 `decision` | 加参数；新版一行心跳带上；旧调用同一情形回 `work`（AC-D8） |
| 3 | N1 心跳间隔 | 全仓没有 10/30/60 分钟、没有等答降频落盘、不可注入 | 心跳指令单一来源给出间隔；等答降频写盘；可注入时钟（AC-D5、AC-D9） |
| 4 | §5.5 无决策记录的 blocked_design | 十余处直接写 `blocked_design`，不查决策记录 | 无决策记录按 `supervisor_lifecycle` 处理 |
| 5 | §5.2 唯一接管 | `SupervisorLease` 无 epoch/CAS；successor 重建不经租约 | 租约 epoch + CAS，successor 登记走同一路径（A5） |
| 6 | #148 关键词分类 | 「permission denied」「authorization digest」等仍按文本判 `binding_unknown` | 这两类进入可自动恢复路径；结构化 `credential_required` 保留（AC-B2） |

## 待补（P2：部分实现或缺验收）

| # | PRD 条目 | 现状 | 要做 |
| --- | --- | --- | --- |
| 7 | §5.5 回答校验 | 摘要参数可选；选项变化生成新 ID，无 `decision.invalidated` | 摘要必填；选项变化作废旧答案并重新上浮（AC-D4） |
| 8 | #152 对齐门例外 | 未实现 | init 决策项、标记块、备份回滚、临时 HOME 测试（AC-H1、AC-H3） |
| 9 | #151 挂起理由 | 不校验是否引用决策 ID 或授权缺口 | 加校验 |

## 未核实（需在实施中补测）

#147 重新授权只重做受影响节点；#153 三轮未收敛换 reviewer、`blocked_unknown` 恢复动作；B1 两类反例（AC-B3）；§5.9 Codex 判定逐字不变；§5.10 分诊钩子；A13 实测与 N3 切换。

## 已知限制（P3，后续 issue）

批准开关 `acceptance_scope_rule_approved` 放在项目配置里，与 `AGENTS.md` 补丁是否写入没有机器关联；本仓库 `.vibe/` 不进版本库，README 写明它是产品经理的批准凭据。通用产品中其他项目可能提交 `.vibe/`，后续改为核对 `AGENTS.md` 补丁标记。
