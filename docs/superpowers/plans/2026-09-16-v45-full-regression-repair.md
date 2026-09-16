# V4.5 完整回归修复计划

## 目标

在保留当前 V4.5 rev6 行为和工作区已有改动的前提下，使干净 worktree 的完整 unittest discover 通过，并保留 V4.5 定向测试通过。

## 根因分组与范围

1. 兼容模型与初始化：补齐 models.py 暴露的 PRD/PRDCheckpoint 合同；让旧版 state.json 的可迁移版本进入现有迁移路径。验证 test_prd_profiles、test_stage_handoff 和 V2 初始化诊断。
2. 监工夹具兼容：对旧 SimpleNamespace 快照读取新字段使用安全默认值，并修复启动失败时 retry/lease 的持久化路径。验证 V3.9/V4.1、Monitor 和端到端失败。
3. Provider 状态语义：保留当前运行时的结构化状态，同时提供旧测试和旧调用方需要的分类兼容映射；确保 timeout、pending setup、worker exit、未知和外部权限都保留同一 writer 与 retry/blocked 边界。验证 V4.2 self-healing 和 supervisor liveness。
4. 打包与版本隔离：检查当前包版本、历史版本测试的临时构建机制和安装入口，修复真实的版本漂移或构建残留；不得把当前版本伪装成旧版本。验证 V2/V3.10/V3.9 packaging 与 CLI metadata。
5. 进程清理与端到端授权：修复本地 runner 停止时对失效或非子进程组的安全处理，修复 provider action request 和重授权路径的字段传播。验证端到端及完整回归。

## 顺序与验收

每个分组先新增或收紧一个能复现根因的测试，确认测试先失败，再实现最小修复并运行该分组相关测试。所有分组完成后运行：

    python3 -m unittest discover -s tests -v
    python3 -m vibe_guide --help
    git diff --check

如果仍有失败，按新堆栈重新归类；不把历史基线失败改写成成功。
