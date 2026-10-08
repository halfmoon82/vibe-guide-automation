# Issue #155 监工轻装可行性实测

## 结论

截至 2026-10-08，本节点没有证明 Codex Desktop 监工能够把首次输入从约 30k 降到约 17k token。本次真实默认会话的首次模型调用为 **39,031 input tokens**，其中 **38,656 cached input tokens**；没有创建“轻装监工”对照线程，因此不存在可比较的 after 数值，也不声称 token 降低。

当前可行性判断：

- **Codex CLI：部分可行，Desktop 路径未验证。** 官方文档与本机帮助均证明 CLI 可以指定工作目录；但当前 Desktop `create_thread` 合同仍以项目为目标，本节点没有得到可设置独立 cwd、额外可写目录或关闭技能发现的结构化参数证据。
- **Claude Code CLI：参数层面可行，生命周期未验证。** 本机 `claude 2.1.118` 暴露 `--bare`、`--disable-slash-commands`、`--setting-sources`、`--add-dir`、`--resume` 和 `--session-id`；本节点未启动 Claude 会话，因此没有首次 token、创建、resume 或登记实测。
- **目标未达标。** 现有证据不足以让 VibeGuide 生成并启用轻装监工步骤；后续实现必须先补一组受控对照实测。

## 绑定

- run：`run-ce79518f6b1e489db738ffef70437a20`
- plan / revision：`session-f9a554fff82f6766` / `1`
- project digest：`f84bf7410acd05aa9649810b3f56569819dba06a394fa2308d0837017fcfd727`
- decision digest：`f88feda89b4bd6b6b005f14616a91abe9ba9244f4321131e5a962e4df1eb93d8`
- authorization digest：`cda7d896c01581973b29a070f04ee28adc80acaca0a6818a3623dc02977770ef`
- Issue contract digest：`67d1438be1e55747aa2ff4e0de81f57a0189e6a640450312f0a1b799570bf894`
- capability contract digest：`c9366e3596287d4fdf4a1549fe8f2902874922c8b6e0cf44de68e9fec79e9134`
- generation：`3`（generation 2 自报被运行时判为 stale，未登记为有效交付）
- writer root：`/Users/smy/Desktop/CFO/黑客松/开发辅助/.worktrees/issue-155-6e2448a8`
- branch / base HEAD：`node/issue-155-6e2448a8` / `c2a04abea77d1bb456d695f5e83a3627503a5c93`
- developer thread：`01a11ada-c87f-7d81-a3e3-17df01e60115`
- supervisor thread：`01a11ad3-0b56-7080-9e5c-99163ab527f0`

## 实测环境

当前 developer rollout：

- 路径：`/Users/smy/.codex/sessions/2026/10/08/rollout-2026-10-08T17-31-48-01a11ada-c87f-7d81-a3e3-17df01e60115.jsonl`
- `session_meta.cwd`：`/Users/smy/Desktop/CFO/黑客松/开发辅助`
- `session_meta.originator`：`Codex Desktop`
- `session_meta.source`：`vscode`
- Desktop 内嵌 CLI version：`0.159.2`
- PATH 上的 `codex --version`：`codex-cli 0.153.4`
- 模型：`gpt-5.6-sol`，reasoning `medium`

两个版本不同，因此本机 PATH CLI 的参数只能证明 CLI 入口，不能自动外推为当前 Desktop 线程能力。

## 首次上下文测量

rollout 第一条 `token_usage_record` 的原始计量：

| 指标 | 实测值 |
|---|---:|
| input tokens | 39,031 |
| cached input tokens | 38,656 |
| cache write input tokens | 0 |
| output tokens | 214 |
| reasoning output tokens | 78 |
| total tokens | 39,245 |
| model context window | 258,400 |

同一 rollout 在首次调用前可见的消息字符数：

| 消息 | 字符数 |
|---|---:|
| Desktop/app/工具等 developer 上下文 | 44,334 |
| multi-agent developer 上下文 | 2,264 |
| multi-agent 禁用补充 | 271 |
| 项目规则与 worker 请求 user 消息 | 10,617 |
| 客户端时间 developer 补充 | 271 |

这些字符数只是输入组成的观测，不按字符数反推各组成 token，也不把缓存命中解释为未计量。

项目路径上的规则文件实测大小：

- `/Users/smy/.codex/AGENTS.md`：3,446 bytes
- `/Users/smy/Desktop/CFO/AGENTS.md`：46,476 bytes
- 本 worktree `AGENTS.md`：18,363 bytes

rollout 没有提供逐文件 token 归因，所以上述文件大小不能证明各自占用多少 input tokens。

## Codex 能力证据

### 工作目录与规则发现

OpenAI 官方文档 `https://learn.chatgpt.com/docs/config-file/config-advanced#project-root-detection` 明确：Codex 从 working directory 向上查找到 project root，并在该过程中发现 `.codex/` 配置层和 `AGENTS.md`；默认 `.git` 标记项目根。文档还说明 `project_root_markers = []` 可把当前工作目录当作项目根。

本机 `codex --help` 和 `codex resume --help` 均暴露：

- `-C, --cd <DIR>`：指定工作根；
- `--add-dir <DIR>`：给主 workspace 之外增加可写目录；
- `-p, --profile <CONFIG_PROFILE_V2>`：叠加专用配置档；
- `codex resume <SESSION_ID>`：续接既有会话。

因此 CLI 方案可以设计为“轻量 cwd + 显式项目目录访问”，但本节点没有在真实 Desktop 监工创建路径上验证 `-C`、`--add-dir` 或 `project_root_markers=[]` 是否能被传入并保持 `.vibe/` 与 worker worktree 写权限。

### 技能目录

本机 `codex features list` 显示：

- `skill_search` 为 stable 且启用；
- `skip_host_skill_discovery` 为 under development 且未启用；
- `plugins` 与 `remote_plugin` 为 stable 且启用。

当前 `codex --help`、`codex exec --help` 和 `codex resume --help` 没有“关闭全部技能目录”的公开参数。`codex exec --ignore-user-config` 只声明不加载 `$CODEX_HOME/config.toml`，不能等价为不发现技能。结论保持为 **Desktop/按线程关闭技能目录未验证**，而不是“不支持”。

## Claude Code 能力证据

本机 `claude --version` 为 `2.1.118`。`claude --help` 明确暴露：

- `--bare`：跳过 hooks、LSP、plugin sync、auto-memory、background prefetch、keychain reads 和 `CLAUDE.md` 自动发现；技能仍可用 `/skill-name` 显式解析；
- `--disable-slash-commands`：关闭全部 skills；
- `--setting-sources <sources>`：限制 user/project/local 设置来源；
- `--add-dir <directories...>`：增加目录访问；
- `--resume` / `--session-id`：续接或绑定会话。

这些是 CLI 参数存在性证据，不是 VibeGuide 对 Claude 创建、resume、任务登记或首次 token 成本的运行证据。

## 生命周期核验

| 环节 | 本次结果 | 证据边界 |
|---|---|---|
| 创建 | 通过（默认 Codex Desktop 会话） | rollout `session_meta.id=01a11ada-c87f-7d81-a3e3-17df01e60115`；不是轻装 cwd 对照线程 |
| 预检 | `unknown` | 通过绝对 `runtime-bridge.py cli supervisor-preflight` 执行，返回 `session record path is missing` |
| resume | 当前方案未验证 | 本机 CLI 暴露 resume；本 run 事件中存在其他节点的 `monitor.resume`，但不能替代轻装线程 resume 实测 |
| 登记 | 当前线程未完成 | 读取时 `tasks.json` 没有 `issue-155` binding，`state.json` 的 `developer_identity` 仍为 `null` |
| 项目访问 | 默认会话通过 | 当前 cwd 可读取主项目 `.vibe/` 与 writer worktree；独立轻量 cwd + `--add-dir` 未验证 |

第一次交付尝试使用派发时的 generation 2，被运行时以 `current generation is 3` 拒绝；监工随后把同一 native thread 绑定到 generation 3。该拒绝保留为恢复证据，不算成功登记，也没有创建第二 writer。

当前线程创建成功不等于“预检、resume、创建、登记”全链路通过。尤其是登记缺失和 preflight `unknown` 必须保留到监工后续拉取与结构化绑定完成。

## 后续最小实测合同

后续若获得同一 DAG 下的明确测试入口，应建立两个不写业务代码的监工线程：

1. 默认项目 cwd 对照组；
2. 独立轻量 cwd 实验组，只含监工角色说明，并显式增加项目根访问。

两组都必须从各自原生 rollout 读取首次 `token_usage_record`，并分别完成结构化 preflight、resume、创建子任务和 `tasks.json` 登记。只有实验组首次 input tokens 实测接近或低于 17k，且四项生命周期检查均通过，才可把方案判为达标。技能关闭必须使用平台结构化配置或公开 CLI 参数；不得通过移动、删除或覆盖用户技能与规则目录实现。

## 未执行

- 未创建额外 writer、额外测试线程或 Claude 会话。
- 未修改代码、配置、AGENTS/CLAUDE 规则、安装项、凭据或系统权限。
- 未 commit、push、创建 PR/MR、merge、deploy、release 或执行外部通信。
- 未把 GitHub Issue 中的约 30k 拆解或 17k 目标当作本次实测结果。

ISSUE_155_DELIVERY_COMPLETE
