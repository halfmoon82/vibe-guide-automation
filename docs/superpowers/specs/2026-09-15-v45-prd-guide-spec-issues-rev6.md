# V4.5 PRD Guide Spec/Issue 合同（revision 6）

状态：修订草案，待授权。

## ISSUE-06：PRD Guide、代码现状规划与目标追踪

沿用 revision 5 合同：自然语言入口、来源状态、只读代码分析、planning-brief.md、目标到运行时验收追踪。保留 Task 2 S0/S1 稳定入口。

## ISSUE-07：授权卡路径到计划物化与执行入口

`authorize <card-path> remote_git_actions=allow|deny` 读取卡片绑定的 PRD、Spec/Issue、DAG、plan、planning brief、节点合同及 SHA；校验 revision、项目根和证据一致后，物化 `.vibe/plans/<plan-id>/`，初始化 run/state/tasks/events，并进入现有 Monitor 授权流程。缺源、缺证据或不一致时 fail-closed；不得从卡片猜测节点合同。

文件白名单：`vibe_guide/cli.py`、`vibe_guide/authorization.py`、`vibe_guide/session_entry.py`、`vibe_guide/paths.py`、相关定向测试。

## ISSUE-08：授权卡权限一致性校验

签发和执行前校验 `remote_git_actions_options`、选择值、allowed_actions、permissions 一致；allow 才开启 commit/push/PR/MR/merge，deny 禁止；worker、Monitor、测试、Review、返工范围必须与卡片一致；deploy、发布、生产写入、凭据、外部通信恒为禁止。矛盾卡不得签发或执行。

文件白名单：`vibe_guide/authorization.py`、`vibe_guide/cli.py`、相关定向测试。

## ISSUE-05：授权即开工与整合回归

等待 ISSUE-01/03/04/06/07/08 accepted 后收口；执行阶段再验证 engine/provider/runtime/lifecycle。

## 工程缺陷补充（纳入 ISSUE-07）

ISSUE-07 同时覆盖：`init` 运行时状态前置检查；授权卡路径与 plan-id 双模式解析；缺失 `.vibe/plans/<plan-id>/plan.json` 时返回明确 `blocked_unknown`；禁止把 plan-id 当 JSON 文件读取；从授权卡绑定源生成并校验 `plan.json`、`nodes.json` 后才进入 Monitor。
