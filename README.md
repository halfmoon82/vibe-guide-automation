# Vibe Coding 辅助开发向导

这是一个可安装、无运行时第三方依赖的本地 CLI。接收方 Agent 只需要交付物和本文件即可执行基础预检及 fake/local runner 闭环；真实 provider、远端权限、Change Request 和 Deploy 始终保持独立的 `unknown` 能力边界。

## 安装与交付物核验

项目使用单一版本 `2.0.0`，同时支持源码包、wheel 和 sdist。构建机可以在临时目录生成三种形态并记录 SHA-256：

```bash
python3 -m pip install --upgrade build
python3 -m build --sdist --wheel --outdir /tmp/vibe-guide-dist .
tar -czf /tmp/vibe-guide-source-0.1.0.tar.gz \
  --exclude=.git --exclude=.venv --exclude=dist .
shasum -a 256 /tmp/vibe-guide-dist/* /tmp/vibe-guide-source-0.1.0.tar.gz
```

不要把开发机的 `.vibe` 快照、绝对路径或临时 overlay 放进接收方环境。接收方在新目录创建干净 Python 3.9+ 虚拟环境，然后从选定产物安装：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install /path/to/vibe_guide-2.0.0-py3-none-any.whl
# 或：.venv/bin/python -m pip install /path/to/vibe-guide-2.0.0.tar.gz
# 源码目录也可执行：.venv/bin/python -m pip install /path/to/source
```

## 接收方 smoke

```bash
.venv/bin/vibe --help
.venv/bin/python -m vibe_guide --help
.venv/bin/vibe scan --json       # 只读，不创建 .vibe/
.venv/bin/vibe doctor --json     # provider 能力没有结构化证据时显示 unknown
.venv/bin/vibe init              # 退出码 3，不写入
.venv/bin/vibe init --confirm    # 最小结构，重复执行幂等
```

本地 fake/local runner 闭环（不代表真实 Agent 能力）：

```bash
.venv/bin/vibe plan --request "local fake flow" --plan-id demo
.venv/bin/vibe monitor --plan demo --authorize AUTHORIZE
.venv/bin/vibe resume --plan demo
.venv/bin/vibe status --plan demo --json
```

闭环状态依次为 `planned → delivered → accepted`，开发者与 reviewer 证据在 `.vibe/plans/demo/plan.json` 中保留。未提供精确 `AUTHORIZE` 时 monitor fail-closed，不启动 runner；找不到计划或 provider 事实时返回 `unknown`，不会猜测为成功或失败。

## 能力边界

`scan` 和 `doctor` 是只读命令。`init` 只在明确 `--confirm` 后写入最小 `.vibe` 状态，并使用原子替换；不会覆盖既有 `AGENTS.md`。本包不执行 commit、push、MR、merge、Deploy，也不保存凭据。真实 provider 的登录、可见任务、创建/续接和远端权限必须在真实平台单独核验。
