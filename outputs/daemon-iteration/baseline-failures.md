# v5.2.1 基线失败分类

基线：main 8d670fe（干净 detached worktree，Python 3.14.6，pytest 9.0.2）。

## 实测结果

| 运行方式 | 结果 |
|---|---|
| `python3 -m unittest discover -s tests` | 3158 个测试，3 失败，0 错误，3 跳过 |
| `python3 -m pytest tests` | 3206 个用例，3 失败，0 错误，3 跳过 |

**与合同数字不符**：PRD 写的是"9 个失败和 18 个错误"（来源标注 system_inferred，是推断值，不是实测）。本机在 8d670fe 上两种运行方式都只复现出 3 个失败、0 个错误。没有找到另外 6 个失败和 18 个错误；它们若存在，依赖本机不具备的条件（其他环境、其他 Python 版本或不同的并发/顺序），此处无法分类，不编造。

补丁后（22 个文件落地）全量 pytest：3 失败，3276 通过，3 跳过。失败集合与基线完全相同，没有新增失败，新增通过 73 个。

## 3 个失败的分类

全部在 `tests/test_integrated_prd_acceptance.py`，三个都是**真实缺陷（功能未实现）**，不是环境原因。依据：断言针对仓库内容和行为，不依赖网络、时间、权限或本机工具。

| 测试 | 失败信息 | 分类 | 原因 |
|---|---|---|---|
| `ACE1EntryTriageTests.test_ac_e1_evaluation_scaffold_reports_accuracy_ece_p95` | `scripts/` 下应有且只有一个 `*triage*` 评测脚手架，实际 0 个 | 真实缺陷 | 入口分诊评测脚本在仓库里不存在 |
| `ACE1EntryTriageTests.test_ac_e1_gateway_down_leaves_a_degradation_record` | 分诊服务关闭时 `vibe plan` 的 payload 和 `.vibe` 都没有降级记录（只写了 `.vibe/session-gates.json`） | 真实缺陷 | 分诊降级记录未实现 |
| `ACB3VisibilityDowngradePreviewTests.test_ac_b3_preview_asks_pm_to_authorize_a_visibility_downgrade` | 夹具前置条件 `topology_summary["background"]` 期望 0，实际 1 | 真实缺陷 | 签字卡的拓扑摘要把一个节点算作 background；与可见性降级预览的验收夹具不一致 |

三个失败都属于 PRD 验收测试套件里尚未交付的功能，与本补丁涉及的 daemon/收口代码无关。
