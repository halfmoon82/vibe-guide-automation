# Vibe Coding 辅助开发向导

Vibe Guide 是一个本地 CLI：先扫描项目和规划任务，再用一次精确授权启动可恢复的开发、独立 Review 与返工流程。它把技术交付、Review 接受、push、MR、merge 和 deploy 分开记录；默认授权永远不包含 deploy。

## 安装

Python 3.9 legacy 环境（已验证为 pip 21.2、setuptools 58，且未预装 wheel）使用 setuptools 自带的 editable `develop` 命令，不依赖环境偶然存在的 wheel：

```bash
python3 -m venv .venv
.venv/bin/python setup.py develop
.venv/bin/vibe --help
```

现代 Python/pip 使用隔离构建；`pyproject.toml` 明确提供 setuptools 和 wheel 构建依赖，不要求用户预装：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/vibe --help
```

两条路径都不升级或修改系统 Python。legacy 命令仅用于声明的 Python 3.9 兼容路径；现代环境不再关闭 build isolation。

也可以直接运行：

```bash
python3 -m vibe_guide --help
```

## 七个命令

```text
vibe scan                         只读扫描项目，不创建 .vibe/
vibe init --confirm               确认后初始化最小项目状态；重复执行不改写
vibe doctor                       报告可观察的环境、Skill 和 Agent 命令事实
vibe plan --request <请求>        先走 S0；需要时使用显式 S1 与 node-spec
vibe monitor --plan <ID>          没有精确授权时拒绝启动
vibe status --plan <ID>           读取当前快照，不轮询外部 provider
vibe resume --plan <ID>           从快照、任务登记和事件证据继续
```

每个命令都支持 `--json`。默认文本面向产品经理；JSON 适合桌面 App 适配器和自动化调用。退出码为：`0` 命令成功执行，`2` 参数错误，`3` 需要确认或因设计变化阻塞，`4` 外部/运行状态未知。

## 扫描和初始化

`scan` 只返回可观察事实，不推测登录、审批、merge 或 deploy 权限，也不写任何文件。

```bash
vibe scan --json
vibe init                 # 退出 3，不写入
vibe init --confirm       # 创建缺失的最小 .vibe/ 结构
```

已有 `AGENTS.md` 不会被覆盖。缺失规则时只在 `.vibe/proposals/agentsmd/` 生成建议。外部 Skill 的安装不由 `init` 隐式触发。

## 简单任务和复杂计划

明显简单的请求直接走轻量路径，不生成 PRD 或 DAG：

```bash
vibe plan --request "修正标题错别字" --json
```

疑似复杂的请求必须显式提供五维 S1 分数和已关闭产品决策的 node-spec。CLI 不替用户猜节点拆分：

```bash
vibe plan \
  --request "实现两个可并行模块并完成独立审查" \
  --s1 4,4,4,4,4 \
  --plan-id example-plan \
  --node-spec plan-source.json \
  --json
```

node-spec 是 JSON 对象，包含 `title`、`objective`、已批准的 `decisions`、Agent `capabilities` 和 `nodes`。每个节点沿用公开 `DAGNode` 合同，至少提供输入、输出、错误行为和验收示例。复杂计划会原子发布 PRD、Spec、Issue、DAG、机器可读 plan/nodes 和授权卡；已有同名计划不会被覆盖。

## 授权、监工和恢复

触发 `monitor` 本身不构成授权：

```bash
vibe monitor --plan example-plan --json
```

上面的命令退出 `3`，且不会启动 runner。确认授权卡范围后，使用精确确认词：

```bash
vibe monitor --plan example-plan --authorize AUTHORIZE --json
vibe status --plan example-plan --json
vibe resume --plan example-plan --json
```

节点从 `planned` 开始。provider 的 `complete` 只映射为 developer 的 `delivered`；随后必须由不同 reviewer 任务给出 `accepted`，硬依赖才会解锁。全部节点 accepted 后，run 才是 `complete`。

恢复以 `.vibe/runs/<run-id>/state.json`、`tasks.json` 和 `events.jsonl` 为准。已登记的活动 handle 不会因重复 `resume` 创建第二 writer。计划或节点合同变化会持久化为 `blocked_design` 并使旧授权失效；provider unknown/timeout 会保持 `blocked_unknown`，不会转成成功或无事项。

已确认规则能够唯一判断、且仍处于当前项目、plan revision、授权文件/action 和非 deploy 边界内的实现纠偏，由监工自动执行并记录，不会再次作为产品取舍询问。纠偏证据必须绑定已批准决定、授权和 Issue 合同；未绑定文本不能冒充用户决定。合同变化后的 `monitor --plan <ID> --authorize AUTHORIZE` 会在同一 run 上审计旧授权与变更原因，保留原任务身份和 cursor，登记新授权后续接修正 DAG；旧任务终止或 continuation 无法证明时仍会 fail closed。

桌面 App 原生能力不能由 Python 直接调用时，public CLI 使用 `.vibe/provider-actions/` 的 provider-neutral request/result bridge。`monitor/resume` 会写入有界、digest 绑定的 `create/locate/visibility/resume/wait` 请求；App 会话按 `native_tool` 调用公开能力并回写绑定结果。Codex 映射到 `create_thread`、`navigate_to_codex_page`、`send_message_to_thread` 和 `wait_threads`。在真实 task ID、host、定位和可见性全部核验前，状态保持 `blocked_unknown`；node-spec 中的 provider/mode/thread/host 自声明不会成为证据。

授权 action 是 closed allowlist；未列明、deploy-like、外部安装和系统权限动作一律拒绝。`files` 必须是规范化的项目内相对路径列表，并与授权卡的 file scope 精确绑定。授权卡还绑定同时活跃的 developer/reviewer pair 上限；只有 reviewer 接受、P0–P2 清零且证据登记后才归档 pair 并释放容量。

## LocalRunner 边界

`LocalRunner` 只能执行构造时由适配层登记的精确命令；adapter ID 或任一命令参数不一致都会在启动前拒绝。它不使用 shell 字符串，不持久化命令参数、stdout、stderr、token、credential 或 provider 原文，只保存有界的 PID、退出状态、命令名称、安全事件引用和确认命令来源。

本仓库的端到端测试使用临时项目与 fake Agent 命令，验证本地进程、授权、developer→reviewer、恢复、unknown 和 deploy 排除。这些证据不证明真实 Codex、Claude Code、Cursor、Grok、WorkBuddy、Kimi Code 或 DeepSeek Harness 的登录、权限、任务可见性、创建/续接行为，也不证明 push、MR、merge 或 deploy 已发生。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m vibe_guide --help
python3 -m compileall -q vibe_guide tests
git diff --check
```
