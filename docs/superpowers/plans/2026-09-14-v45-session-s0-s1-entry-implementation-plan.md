# V4.5 会话级入口与授权即开工实施计划

> **For agentic workers:** 按任务逐项实现并在每项后运行定向测试。

**Goal:** 实现 S0/S1 入口、授权卡 allow/deny、authorize 自动物化并启动 Monitor，以及可视化 DAG。

**Architecture:** 入口运行时是事实源；authorize 原子完成授权审计、运行状态物化和 Monitor 启动；旧 V4.2/V4.3/V4.4 合同通过兼容投影保留。

## 全局约束

- S1 阈值：<=8、9-15、>15。
- `remote_git_actions` 必须由用户在 allow/deny 中选择。
- 不出现二次启动命令、start_monitor=false 或缺物化计划的用户阻断。
- deploy、发布、生产写入、凭据、外部通信永不由本卡授权。
- 一个 Issue 一个 writer；重复 authorize 幂等且不创建第二 writer/run/lease。

## 任务

### Task 1：入口 Skill 与初始化登记
修改 `vibe_guide/skills.py`、`vibe_guide/initializer.py`，补充本地/远端来源三态和 proposal 回归。

### Task 2：会话 S0/S1 入口与稳定物化
修改 `vibe_guide/planner.py`、`vibe_guide/cli.py`，新增 `vibe_guide/session_entry.py`；缺 plan/node spec 时稳定生成，不要求用户填写。

### Task 3：候选归属与恢复
修改 `vibe_guide/task_registry.py`、`vibe_guide/workflow_gate.py`；保留原 task/writer/worktree/branch/cursor，禁止第二 writer。

### Task 4：兼容适配
修改 adapters、binding_contract、workflow_gate；保持旧状态、Intent/Proof、legacy 输入和 native probe 语义。

### Task 5：授权即开工与 DAG 图
修改 `vibe_guide/authorization.py`、`vibe_guide/cli.py`、`vibe_guide/dag.py` 及相关测试：卡片生成 allow/deny 选项；`authorize <card-path>` 自动物化并启动 Monitor；输出 Mermaid 图；重复调用幂等。

验证：定向 unittest、完整 unittest、CLI help、`git diff --check`。
