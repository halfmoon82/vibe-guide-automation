# Task 2 实施报告：会话 S0/S1 入口与稳定物化

## 改动

- 新增 `vibe_guide/session_entry.py`：提供 `build_session_entry`、`stable_plan_id`、`default_s1_context` 和 `materialize_session_entry`。显式有效五维 S1 优先；缺失或无效 S1 使用有界、确定性的文本推导值；明确跨设计、实现、测试、部署等动作的请求可稳定进入 `complex`。
- `vibe_guide/planner.py` 新增 `parse_s1_context`，将可选 S1 解析与用户入口解耦；无效输入返回 `None`，不把工程字段升级成阻断。
- `vibe_guide/cli.py` 的 `plan` 入口允许缺少 `--s1`、`--plan-id`、`--node-spec`。复杂请求会稳定生成 plan/node spec 草案并原子物化到 `.vibe/plans/<plan-id>/`，执行明确延后到授权边界；此阶段不探测 provider、不创建 writer、不启动 Monitor。
- 新增 `tests/test_v45_session_entry_task2.py` 覆盖稳定性、S1 优先级、有界默认值、缺字段 CLI 入口和草案物化。

## 验证

- `python3 -m unittest tests.test_v45_session_entry_task2 -v`：10/10 通过。
- 定向回归：`tests.test_planner` 通过；既有 `tests.test_cli` 与 `tests.test_end_to_end` 有若干环境/基线失败（版本元数据、未初始化 V2 会话门、provider/进程权限），与本任务改动无直接关系，未修改。
- `git diff --check`：待提交前运行。

## 边界与 concerns

- 草案物化不生成授权卡、运行状态、任务登记、lease 或 provider 任务；这些属于后续授权/监工任务。
- 旧 CLI 在显式提供 `--node-spec` 时继续沿用原子发布路径；仅缺 node spec 的 V4.5 入口走草案路径。
- 空请求和非法 plan id 在 CLI 层返回结构化 `blocked`，不会生成空 objective 或路径穿越目标。
- 计划目录及其父目录出现 symlink 时物化 fail-closed；已授权/运行中的同名计划不会被草案入口覆盖。
- 在 canonical resolve 前额外拒绝原始 `.vibe` 根及 `plans` 链路 symlink，避免写入项目外目录。
