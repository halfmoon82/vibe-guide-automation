# Vibe Coding 辅助开发向导

[English README](README.en.md)

> 如果这个项目对你有帮助，欢迎点一下右上角的 ⭐ Star，让更多人发现它。

Vibe Guide 是一个本地 CLI：先扫描项目和规划任务，再用一次精确授权启动可恢复的开发、独立 Review 与返工流程。它把技术交付、Review 接受、push、MR、merge 和 deploy 分开记录；默认授权永远不包含 deploy。

## 安装

当前发行版本为 `5.1.3`。版本真相以仓库中匹配的 `v5.1.3` tag、对应提交和 Release 资产为准；版本引用的写法约定：反引号 v 形式（如本句的 tag）只用于当前 release tag，历史版本写作 V4.5.0 这样大写、无反引号的形式，否则 release-tag-gate 会判为旧 tag 残留。`dist/` 目录只是本地构建缓存，不是下载入口。交付验收分别覆盖 wheel、sdist 和源码安装；本地构建只生成验证用产物，不创建 tag/Release、push、merge 或 deploy。

### 安装与初始化前提

- **运行环境**：Python ≥ 3.9，零第三方依赖（纯标准库）；pip 或 pipx 安装，wheel / sdist / 源码三种方式均可（三种安装方式均有测试钉住协议文件随包发布）。
- **装后验证**：`python3 -c "import vibe_guide; print(vibe_guide.__version__)"` 与 `vibe --help`。
- **初始化顺序**：进入项目先 `vibe scan --json`（只读）；确认初始化才 `vibe init --confirm`；AGENTS.md 规则块经 `vibe apply-agentsmd --confirm` 人工评审后合入。
- **宿主框架零要求**：入口协议完全自包含。宿主装有强制门/状态行类框架（如对齐门卡片）时，S1 评分并入其首个强制状态行作为 `S1：` 字段输出，不另起一轮评分或第二个门；未装则独立输出一行 S1 结果。
- **存量项目升级**：物化文件（`.vibe/proposals/skills/*/SKILL.md` 与 AGENTS.md 已合入块）永不自动改写；新版 init 检测到与随包内容不一致时给出漂移提示，人工 diff 后手动合并。

### 给 Agent 的安装协议

当用户要求安装或升级时，Agent 必须按以下顺序执行并回报证据：

1. 先查询最新 tag，不根据 README、`dist/` 文件名或历史记忆判断版本：
   `git ls-remote --tags --sort='-v:refname' https://github.com/halfmoon82/vibe-guide-automation.git 'refs/tags/v*'`
2. 选择最新稳定 tag（当前应为 `v5.1.3`），核对 tag、提交和可下载 Release 资产版本一致；无法核对时保持 `unknown`，不要猜测。
3. 在目标环境安装该版本，并验证：
   `python3 -c "import vibe_guide; print(vibe_guide.__version__)"`
   `vibe --help`
4. 安装完成后进入用户指定项目，先运行只读的 `vibe scan --json`；只有用户明确确认初始化时，才运行 `vibe init --confirm --json`。
5. 初始化后回报 `.vibe/session-contract.json` 和初始化结果；安装成功不等于项目已初始化，也不等于已授权监工、push、merge 或 deploy。

下载包后 Agent 不应假设安装器会自动运行向导或写入项目；初始化必须作为独立、可见、可追溯步骤执行。

升级与回滚必须显式指定已验证的版本：从 V2.0.0 安装后执行 `pip install --upgrade` 到 V3.10.0，确认版本和 CLI，再使用 V2.0.0 构建产物 `--force-reinstall` 回滚并重新确认版本。`.vibe/` 迁移另按 V3.10 安装流程执行；provider、缺失 Python 版本和远端 Release 能力未验证时保持未验证。

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

### 发版验收：三条会报假红的陷阱

发版自检时最容易踩的三个坑，按下面的写法抄命令，不要凭记忆简化：

1. **版本断言必须在干净目录跑**。在仓库根执行会 import 到仓库里的源码树而不是 pipx 安装版，仓库根可能导入本地源码；干净 cwd 应显示实际安装版本：

   ```bash
   cd /tmp && python3 -c "import vibe_guide; print(vibe_guide.__version__)"
   ```

   `vibe doctor --json` 的 `facts.install` 会给出当前解释器实际加载的包路径、是否落在 cwd 源码树（`in_cwd`）、是否在 site-packages 内（`in_site_packages`）、是否算安装版（`installed`）；不成立时 `hint` 带修复提示。这项只是信息项，不影响 `status`。

2. **pipx 装 wheel 必须用构建出来的原文件名**，改名后的副本（如 `cli-162b481.whl`）会被拒：

   ```bash
   pipx install --force dist/vibe_guide-5.1.3-py3-none-any.whl
   ```

3. **沙箱内 `pip install <sdist>` 会报 `EEXIST: mkdir pip-req-build-*`**（换 `TMPDIR` 仍复现），改成解包后按目录安装，新 venv 先装 setuptools / wheel：

   ```bash
   tar xzf dist/vibe_guide-5.1.3.tar.gz -C /tmp
   python3 -m venv /tmp/vibe-sdist-check
   /tmp/vibe-sdist-check/bin/pip install setuptools wheel
   /tmp/vibe-sdist-check/bin/pip install --no-build-isolation --no-deps /tmp/vibe_guide-5.1.3
   /tmp/vibe-sdist-check/bin/vibe --help
   ```

## 核心命令

```text
vibe scan                                    只读扫描项目，不创建 .vibe/
vibe init --confirm                          确认后初始化最小项目状态；重复执行不改写
vibe init --confirm --host-rules skip|apply|rollback [--host-rules-digest <摘要>]   回答「是否在用户级规则中加入 vibe 监工会话例外」
vibe doctor                                  报告可观察的环境、Skill 和 Agent 命令事实
vibe attest --adapter <id> --facts <json>    登记本会话实测到的能力（复杂计划发布前必做一次）
vibe plan --request <请求>                   先走 S0/S1 分流；complex 请求生成 draft
vibe plan --request <请求> --from-prd <spec> 用产品级 spec 发布复杂计划，工程字段自动派生
vibe plan --print-protocol                   打印 PRD 引导协议（agent 用）
vibe authorize --plan <ID> --authorize AUTHORIZE   记录一次用户授权（十节点门禁证据）
vibe monitor --plan <ID> --authorize AUTHORIZE     启动监工；没有精确授权时拒绝
vibe monitor --plan <ID> --preview-card            只读预览重新授权将要发布的授权卡；不写盘、不授权、不派发，不需要 token
vibe status --plan <ID>                      读取当前快照，不轮询外部 provider
vibe resume --plan <ID>                      从快照、任务登记和事件证据继续
vibe skill-install --source <GitHub> --sha <40hex> --name <名> [--subdir <子目录>] --confirm   按完整 SHA 拉取 GitHub skill，校验 subdir 合法性后物化到 .vibe/proposals/skills/<名>/ 并在 .vibe/config.json 登记；subdir 含 ..、绝对路径、首尾 / 或非法字符时拒绝且不落盘
vibe supervisor-preflight --run-id <run> [--accept-decision] --session-record <file>   监工心跳预检，只读磁盘，输出 idle/work/decision/rotate/unknown；只有带 `--accept-decision` 才会输出 decision，不带时同一情形（只剩未答决策）输出 work；<file> 直接用本会话日志（Codex rollout / Claude Code jsonl）
vibe supervisor-beat --run-id <run> --accept-decision --session-record <file>   心跳入口：预检 + 输出分支 + 下一拍间隔（按授权卡 `heartbeat_interval_minutes`：缺省 10，平台推送唤醒经验证登记后可选 30；只剩未答决策 60），并写入 `.vibe/runs/<run>/heartbeat-interval.json`，接班时 `supervisor-handoff` 会打印。推送唤醒登记文件 `.vibe/push-wake-verification.json`：`{"platforms": {"<provider>": {"success": [{"evidence_ref": "..."}], "failure": [{"evidence_ref": "..."}]}}}`，成功与失败各至少一条证据才算验证；文件读不到时按 10 分钟并在输出里写明原因
vibe supervisor-register --run-id <run> --provider <p> --session-id <id> --host <h> [--session-record <file>]   原子登记当前监工地址（保留历史，不含凭据）；给了日志就记下接班起点，换班按此后增长计算
vibe supervisor-hold --run-id <run> --node <id> --category <product_acceptance|external_authorization> (--decision <决策ID>|--authorization-gap <缺口>) --reason <原因>   登记/刷新"节点等人拍板"，预检据此判为空闲；必须引用一条决策或一个授权缺口；product_design/deploy 仍可作别名（落盘存规范名），无 category 的旧挂起按 product_acceptance 读取
vibe decision-answer --id <决策ID> --choice <选项> --digest <摘要>   回答决策；成功后提示「想马上续跑，去监工会话说继续；否则最晚下一次心跳自动接上」。已回答、节点仍停在 blocked_design 时预检输出 work（理由写明已回答待处理）；节点离开 blocked_design 后恢复正常判定，绑定在已答决策上的挂起不再引起反复 work；回答文件损坏或作废时预检输出 unknown 并写明原因
vibe supervisor-hold --run-id <run> --node <id> --release   解除"节点等人拍板"
vibe supervisor-handoff --run-id <run>             打印接班所需的全部内容（进度、等人事项、心跳指令、步骤），换班时新会话只跑这一条
vibe supervisor-address --run-id <run>             查询当前监工地址；未登记返回 unknown；带 --node/--role/--generation 时 wake 字段给出推送工具与填好的唤醒信号
vibe worker-deliver --run-id <run> --node <id> --generation <n> --payload '<json>'   worker 在主项目目录自报交付（嵌套 delivery_evidence），格式错误当场返回
```

新会话入口：agent 先按 `.vibe/proposals/skills/vibe-entry/SKILL.md` 的入口协议在会话内自评 S0/S1——<=8 直接执行、9-15 轻规划，均不触碰 vibe，评分后输出一行 S1 评分与档位；>15 且能拆出 3 个以上可并行节点、又需跨会话/跨中断续跑时才 `vibe scan` + `vibe plan --request --s1` 进入正式路由（不满足则按轻规划，评分行注明未满足的条件；拿不准按轻规划；产品经理显式要求或显式 `--s1` 升级仍进复杂模式）；「授权」「选A」这类确认/续接回复延续当前档位，不重新评分。该协议完全自包含，不依赖任何外部技能。

产品经理的完整路径（自评确认为复杂之后）：agent 按 `.vibe/proposals/skills/prd-guide/SKILL.md` 的协议引导写 PRD 与节点拆分（业务字段），`vibe attest` 登记会话能力，`vibe plan --from-prd` 发布，`vibe authorize` 授权，`vibe monitor` 派发。旧的 `--node-spec` 手写路径仍可用。

每个命令都支持 `--json`。默认文本面向产品经理；JSON 适合桌面 App 适配器和自动化调用。退出码为：`0` 命令成功执行，`2` 参数错误，`3` 需要确认或因设计变化阻塞，`4` 外部/运行状态未知。

## 扫描和初始化

`scan` 只返回可观察事实，不推测登录、审批、merge 或 deploy 权限，也不写任何文件。

```bash
vibe scan --json
vibe init                 # 退出 3，不写入
vibe init --confirm       # 创建缺失的最小 .vibe/ 结构
```

已有 `AGENTS.md` 不会被覆盖。缺失规则时只在 `.vibe/proposals/agentsmd/` 生成建议（能力真相、复杂请求入口、`New Session Entry`、`Engineering Principles` 四个规则块），由 `vibe apply-agentsmd --confirm` 经人工评审后合入。外部 Skill 的安装不由 `init` 隐式触发。

### 用户级规则：vibe 监工会话例外（必答决策项）

`init` 会提出一个必须回答的问题：「是否在用户级规则中加入 vibe 监工会话例外？」。没回答时 `init` 照常完成，输出里带 `host_rules.status = decision_required` 和回答命令，不写任何用户级文件。

```bash
vibe init --confirm --host-rules skip                                  # 不应用：只在 .vibe/host-rules.json 记下决定
vibe init --confirm --host-rules apply                                 # 只预览：展示将写入的差异和授权摘要，不写入
vibe init --confirm --host-rules apply --host-rules-digest <摘要>      # 授权：摘要与当前差异一致才写入
vibe init --confirm --host-rules rollback                              # 回滚：恢复原文件
```

- 只写已存在的用户级规则文件（`~/.claude/CLAUDE.md`、`~/.codex/AGENTS.md`、`~/AGENTS.md`），不新建；内容是一段带版本和校验值的标记块，文件其余部分不动。
- 写前在同目录留 `<文件>.vibe-host-rules.bak` 备份并校验；任一备份失败、文件或目录不可写、路径含符号链接、标记块被人工改过，都整体不写并说明原因，`init` 其余部分照常完成。
- 重复执行幂等：已是当前版本时不改文件、不新增备份。回滚时文件未再改动则按备份逐字节恢复；之后有人改过文件其他部分则只移除标记块。
- 例外只作用于带监工会话标记的会话（已用 `vibe supervisor-register` 登记、授权卡已签、当前回合由 `vibe supervisor-beat` 心跳或 `vibe supervisor-handoff` 接班发起）：这些回合按 light 处理，只有 vibe 边界判定需要用户的事项才停下；其他会话和其他项目的对齐门不变。
- `vibe doctor` 在 `facts.host_rules` 报告决定和每个文件状态；标记块被人工改过或不是当前版本时列为问题。

### V2 能力合同（监工与 worker 共用）

确认初始化后，`.vibe/session-contract.json` 是当前项目会话的唯一能力事实源；监工入口和 worker/provider action 都读取同一份合同。初始化只记录安全、有限且当场可验证的运行时事实，任务终端、浏览器控制和可见会话在未探测前保持 `unknown`。

合同中的能力状态包括 `verified_available`、`not_exposed`、`permission_denied`、`probe_failed`、`unknown_timeout`、`unknown` 和过期后的 `stale`。合同缺失、损坏或无法核验会安全映射为 `blocked_unknown`；过期能力只会降为 `stale` 并要求刷新，不会被解释成“没有工具/能力”或成功；`unknown_timeout` 也不会降级为 `unavailable`。

`AGENTS.md` 不会被初始化直接改写，能力真相规则只生成到 `.vibe/proposals/agentsmd/proposal.md`。规则要求监工和 worker 不得根据记忆、README、工具未被提及或自然语言自报判断能力存在与否，必须引用合同事实；没有证据时保持未知并重试或阻断。真实 provider 的权限、登录、任务可见性、创建/续接和外部故障仍须在真实平台核验，合同和本地测试不能替代该核验。

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

## V4.4 恢复与兼容性

V4.4 将工程故障限定在节点范围内，并保留同一任务身份。五类恢复状态是：`repairable`（修复 dirty checkout、detached HEAD 或 branch drift）、`retryable`（超时、断线、任务创建失败等可重试故障）、`capacity_wait`（429 或 provider 容量不足，按退避等待）、`binding_unknown`（无法证明原 task、writer、lease 或 cursor 身份，只阻塞该节点及其依赖闭包）和 `external_decision`（产品、权限、deploy、凭据或不可逆安全决策）。

修复必须回到原 developer task、generation、writer、worktree、lease、cursor 和合同摘要；不得创建 successor 或第二 writer。Review 的 P0–P2 缺陷回到原 developer 返工，再由同一 reviewer 复审。无关节点仍按硬依赖重新计算 ready set，`integration_after` 和 workflow 证据不会阻塞可执行节点。

迁移使用显式命令 `vibe migrate-state --preview`（只读预览）和 `vibe migrate-state --confirm`（生成迁移证据）。旧 V2–V4.3 snapshot、事件、授权 epoch 和 cursor 保存在历史 namespace，只读且不会恢复为当前可执行状态；迁移生成 `history_manifest.json` 与 `migration_evidence.json`，保留源文件哈希，无法完整解释时标记 `historical_incomplete`。安装、升级、迁移、回滚分别验证；回滚必须留下目标版本、来源构建产物和验证命令证据。

测试通过、worker 自报、timeout、分支名或本地 fixture 都不能单独证明真实 provider 能力、任务可见性、merge、发布或 deploy。当前执行必须以匹配的 plan、授权、BindingIntent/BindingProof、lease、provider 返回和验收证据为准；缺失或冲突证据保持 `unknown`，不得转成成功。

## 授权、监工和恢复

### DAG 运行时约束

每个 Agent Task 都是某个 Issue 的运行时实例；任务线程、Provider 返回值或 worker 自报不会替代 Issue/DAG 合同。节点完成独立验收后，Monitor 立即依据最新的 `depends_on` 状态重算 `ready_set`，已解锁的节点可以继续调度，不等待整批任务形成 barrier。

### V4.6 派发拓扑与并发上限

V4.6 起，DAG 真并行的载体是每节点一个可见 worker 会话（Codex 为 `create_thread` 创建的 user-owned thread）；监工只派发、等待、收口，不作任何节点的 writer。任务登记 `tasks.json` 用 `topology` 字段记录每个节点的派发拓扑：

- `visible-sdd`：每节点一个可见会话，会话内走 SDD 双角色——dev 子代理实现，review 子代理以独立上下文、只读审查（协议见 `vibe_guide/protocols/visible-sdd-worker.md`），返工与复审在同一会话身份内闭环；派发时 create 请求的 prompt 会内联随包协议全文（`VISIBLE_SDD_PROTOCOL_REF` 仅作版本指针），随包协议缺失时在派发前 fail-closed、不创建会话；
- `dual-visible`：保守默认，developer 与 reviewer 是两个不同的可见独立任务；平台能力 UNKNOWN 时 fail-closed 到本拓扑，不会升级为 `visible-sdd`；
- `background`：平台无可见桥接时的显式降级，必须在能力报告、授权卡和交付三处披露降级及限制（不可见、不可直接进入、返工续接受限）；`mode=background` 缺少披露时授权卡机器校验直接失败。

平台拓扑由适配器注册表的 `DISPATCH_TOPOLOGY_MATRIX` 按各平台 `in_session_sdd` 探针证据裁定。`in_session_sdd` 与 `visible-sdd` 分属两层、不互换：前者是适配层的名字——既是 manifest 能力探针字段名，也是 `DISPATCH_TOPOLOGY_MATRIX` 的裁定值；后者是 `topology` 字段的枚举值（派发层），由监工把裁定值翻译而来，描述节点实际派发拓扑；探针通过不等于 topology 已是 `visible-sdd`（以矩阵裁定为准）。

并发上限：`.vibe/config.json` 的 `max_active_worker_sessions` 控制同时活跃的 worker 会话数，默认 5（合法范围 1–64），与授权卡快照取较小者生效；显式但非法的值是配置错误，不会静默回落默认值。节点验收、P0–P2 清零且证据登记后归档会话，名额释放给后续 ready 节点。

单班监工：`.vibe/config.json` 设 `"supervisor_single_shift": true` 后，同一监工会话服务整个运行，预检不再按上下文增长换班，只在达到 20 万绝对上限（或 `--context-hard-cap` 临时值）时输出 `rotate`。默认 false，沿用增长换班。非布尔值是配置错误，预检报 `unknown`；配置文件整体读不了时按默认处理并带 `config_warning`；绝对上限换班不受配置影响。

按验收结果扩范围：`.vibe/config.json` 的 `acceptance_scope_rule_approved` 默认 false，此时监工自动扩范围只限 `tests/` 与 `auto_scope_paths`；只有产品经理批准并写入 `AGENTS.md` §6 的对应补丁后才设为 true，届时「所有 PRD 验收例子判定不变」的低风险文件也可自扩。

决策通知桥：项目 `.vibe/config.json` 只能写 `"notify_bridge": {"enabled": true}`；要执行的命令与超时只从用户级 `$VIBE_HOME/config.json` 的 `notify_bridge` 读取。项目配置里写 `command` 会被拒绝并记为 UNKNOWN。

### Claude Code 后台派发（降级后台）

配置后，在 Claude Code 上授权一次即可无人值守派发 worker：监工用 `claude --bg` 启动后台会话（provider 为 `claude-code-background`）。这是降级后台，有三条限制：桌面侧边栏看不到这些会话；只能在真实终端里 `claude attach <short-id>` 进入；返工续接的是同一个会话。未配置时行为不变，仍需逐个点卡片。

1. 准备一个只含 `apiKeyHelper` 的 Claude Code 设置文件（放在仓库外，不要把密钥写进任何 vibe 文件）。后台会话不认 `~/.claude/settings.json` 的 env 块里的 key，所以需要这个文件：

```json
{"apiKeyHelper": "<读取你密钥的脚本的绝对路径>"}
```

2. 在**用户级** `$VIBE_HOME/config.json`（默认 `~/.vibe-guide/config.json`）登记：

```json
{"claude_bg": {"settings_path": "<设置文件绝对路径>", "model": "sonnet"}}
```

vibe 只记路径，不读也不存密钥。`model` 默认 sonnet，监工每次派发仍显式传 `--model`（可按节点换）。项目 `.vibe/config.json` 的 `claude_bg` 只能写 `enabled`，写凭据、命令或模型会被拒绝（仓库会被别人克隆）。

3. 在 Claude Code 会话里用 `vibe attest` 登记能力事实时，把 `claude-code.background_task.launch` 记为 `true`（依据是本机实测 `claude --bg` 可启动）。登记成功会显示 `级别 background`；若第 2 步没配好，同一行会写明「后台启动器不可用：<原因>」，授权卡上也会列出。`vibe scan` 不报告派发方式，不用它判断。

查看后台 worker：在终端运行 `claude agents --json --all` 看所有后台会话及其 `state`（working / done / failed / blocked / stopped），用 `claude attach <short-id>` 进入（必须真实终端；非 TTY 下不带 `--json` 的 `claude agents` 会报错）。关闭终端标签不会结束会话。不要在后台会话运行时用前台 `claude --resume` 续它（会被拒绝，这是防双写）。

权限与提交身份：worker 以 `bypassPermissions` 运行，安全边界是 worktree 隔离与授权卡；本机若没配全局 git `user.name` / `user.email`，提交作者会是机器名。可在用户级 `claude_bg` 里写 `git_user_name` / `git_user_email` 只给 worker 的 worktree 设身份，前提是仓库已开 `git config extensions.worktreeConfig true`，否则启动会失败并报原因（不会改动主仓库的身份）。

已真机验收（2026-10-14，Claude Code 2.1.260）：后台派发 → 交付 → 审查发现缺陷 → 返工续接同一会话 → 复审通过，全程无人点击，约 30 秒；本机复跑：`VIBE_LIVE_CLAUDE_BG=1 python3 -m unittest tests.test_claude_bg_live -v`（默认跳过，会真实启动后台会话并消耗额度）。worker 必须在独立 worktree（`.worktrees/<节点>`）里运行：新版 Claude Code 的后台会话在仓库主目录会自己另开 worktree 写文件，交付落不到预期位置。

未验证、不得当作可用：`--session-id` 预指定会话 id、`-w` 与 `--bg` 合用、长任务/休眠/daemon 退出后的恢复、桌面内置 CLI 下的 `--bg`。

### WorkBuddy 派发桥（`workbuddy_job` MCP server）

监工自己不执行桌面动作：它把请求写进 `.vibe/provider-actions/requests/`，写明要调的**原生工具名**，由桌面会话真正去调。WorkBuddy 的五个名字是 `workbuddy_job__create` / `__get` / `__list` / `__reply` / `__wait`——本包随附一个纯标准库的 stdio MCP server 提供它们：

```bash
python3 -m vibe_guide.mcp_servers.workbuddy_jobs --print-tools   # 看五个工具定义
python3 -m vibe_guide.mcp_servers.workbuddy_jobs --selfcheck     # 探测控制面是否可达
```

注册到宿主 `~/.workbuddy-ai/mcp.json`（server 名必须是 `workbuddy_job`），写完后需在连接器管理页对自定义连接器点"信任"才生效。默认在首次调用时自动拉起 `codebuddy --serve` 本地网关并随进程退出而停止；也可用 `WORKBUDDY_JOB_ENDPOINT` / `WORKBUDDY_JOB_TOKEN` 复用已有网关，或 `WORKBUDDY_JOB_AUTOSTART=0` 禁止自动拉起（拿不到凭据即报错，不猜）。控制面不可达、鉴权失败、宿主守卫拦截都返回 `isError`，不会伪装成功。实测端点差异、环境变量与已知阻断见 `docs/workbuddy/workbuddy-job-mcp-server.md`。

### V4.1 复杂任务最终整合

复杂任务在授权前必须完成 PRD 阶段的范围与边界确认，并把需求、PRD、Spec/Issue、DAG 审计、计划确认、授权卡和用户授权纳入十节点强制链；节点只能通过结构化记录完成，显式跳过会记录用户指令、原因和后继策略：允许后继时继续，跳过授权节点则不能推导执行授权。所有业务节点完成后，独立 reviewer 执行只读的最终整合 Review，核验 PRD/Spec 合同、全量 diff 与 P0–P2 门禁。CLI 会区分“局部节点完成”“整合 Review 进行中”“整合 Review 返工”和“整合通过但外部动作未授权”。

该整合流程只适用于 `complex`；`simple` 与 `light_plan` 仍走轻量路径，不生成最终整合 Review。整合通过不等于 merge、push 或 deploy：授权卡的 `remote_git_actions` 仅是远端 Git 总开关，deploy、凭据和系统权限始终需要独立授权与验证。

当前版本中，确认授权卡后需要两步：`vibe authorize --plan <ID> --authorize AUTHORIZE` 记录十节点门禁证据，再 `vibe monitor --plan <ID> --authorize AUTHORIZE` 启动 Monitor。旧版 CLI 兼容路径仍可接受显式 `--authorize`：

```bash
vibe monitor --plan example-plan --json
```

没有授权的显式兼容调用仍退出 `3`，且不会启动 runner。确认授权卡范围后，确认动作应直接调用等价的 Monitor 启动路径：

```bash
vibe monitor --plan example-plan --authorize AUTHORIZE --json
vibe status --plan example-plan --json
vibe resume --plan example-plan --json
```

节点从 `planned` 开始。授权确认启动 Monitor 后，当前无硬依赖节点立即调度；provider 的 `complete` 只映射为 developer 的 `delivered`；随后必须由不同 reviewer 任务给出 `accepted`，硬依赖才会解锁。全部节点 accepted 后，run 才是 `complete`。

恢复以 `.vibe/runs/<run-id>/state.json`、`tasks.json` 和 `events.jsonl` 为准。已登记的活动 handle 不会因重复 `resume` 创建第二 writer。计划或节点合同变化会持久化为 `blocked_design` 并使旧授权失效；provider unknown/timeout 会保持 `blocked_unknown`，不会转成成功或无事项。

开发会话已经派发出去、却没登记进运行（节点停在 `blocked_unknown`、generation 0，同 run 同代的租约被隔离）时，用对账包 `{"kind": "adopted_dispatch", "node_id": "<节点>", "action_id": "<该节点的 create 请求 id>"}` 执行 `vibe reconcile --plan <ID> --run-id <run> --evidence <包路径>`，把原会话认回原运行：原 task_id 不变，租约改绑到当前授权，之后原会话照常用 `vibe worker-deliver --generation <原代数>` 交付。它只认磁盘上的结构化证据（本 run 的 create 请求与结果绑定、dispatch-head、唯一一份同代隔离租约、本 run 授权历史里出现过的派发授权）；缺一项或互相矛盾就保持 `blocked_unknown`，不会派出第二个 writer，也不会向会话发任何消息。认领由一条 `orphan_dispatch_adopted` 事件整体生效；中途中断时重跑同一条命令或直接 `resume` 即可续完，已认领后重跑不会再改动。认领前先暂停该 run 的监工心跳自动化与监工循环（有存活的监工进程持有 run 租约时会拒绝；若并发的 `resume` 用旧快照覆盖了认领结果，重跑同一条认领命令会在租约、任务登记和 handle 均未变时恢复它），授权已失效（计划目录存在失效标记）时也会拒绝，须先重新授权。

worker 用 `vibe worker-deliver` 自报的交付不带游标（游标只来自监工翻会话时回写的 `wait` 结果）；交付证据门对自报改认"自报回执"——同代存档被指向本任务 task/host 的同代 `wait` 领走、且结果就是存档里的交付——身份、host、worktree、branch 照旧必须齐。5.0.11 及更早版本不认回执，完整的自报交付会被判 `blocked_unknown`（原因 `current cursor is missing`）且不会自愈。升级后对这样的节点用对账包 `{"kind": "self_reported_delivery", "node_id": "<节点>"}` 执行 `vibe reconcile --plan <ID> --run-id <run> --evidence <包路径>`：它把同一份已绑定的结果按修复后的门重新判一次（visible-sdd 的会话内审查、合同测试等其余门全部照跑），先记一条 `self_report_delivery_reconciled`，再走正常的 `delivered` 路径；监工后来补发、还没有结果的同代 `wait` 会留审计标记后退役，不向会话发任何消息，也不编造游标。只认结构化证据：节点仍登记着该 developer 任务与 handle、任务登记没有游标、存档已被本任务同代 `wait` 领走且结果与存档一致、该代还没记过交付；缺一项、后补的 `wait` 已有结果、有存活监工持有 run 租约或授权已失效时都会拒绝并保持原状。原因文本落盘时被打码，它靠事件判断"只因游标被拦"：本任务开工后该节点只能出现 `blocked_unknown`、`retry_scheduled`、`unknown`、`timeout`、`acceptance_rejected`，其余任何节点事件即拒绝；登录/权限类轮询失败也只留下 `blocked_unknown`，事件上与缺游标无法区分，所以只在确认节点是因 `current cursor is missing` 被拦时使用（放行后验收门、会话内审查、合同测试照旧全部重判）。

已确认规则能够唯一判断、且仍处于当前项目、plan revision、授权文件/action 和非 deploy 边界内的实现纠偏，由监工自动执行并记录，不会再次作为产品取舍询问。纠偏证据必须绑定已批准决定、授权和 Issue 合同；未绑定文本不能冒充用户决定。合同变化后的 `monitor --plan <ID> --authorize AUTHORIZE` 会在同一 run 上审计旧授权与变更原因，保留原任务身份和 cursor，登记新授权后续接修正 DAG；旧任务终止或 continuation 无法证明时仍会 fail closed。

授权卡是「发布时才签」的：执行门把在盘卡绑到 run 快照的 authorized digest 上，所以「卡已重出、尚未授权」在本产品里不可表示——只重出卡会让公开执行门以 `plan-confirmation.invalid` 关门。合同或 PRD 变化后要重新授权时，先用 `vibe monitor --plan <ID> --preview-card` 只读地看一遍将要签的卡：它报的是「相对当前在盘卡会改动哪些字段」（这才是要审的东西），并单独自检「刷新本身是否只动了引擎凭证字段」。确认后再跑带 `--authorize` 的那条，一次原子完成重新授权与派发。预览里的卡 digest 只是预览态——引擎凭证的时间戳进入凭证与卡的摘要，发布时会重新观测，所以别拿预览 digest 当审批凭据。

计划发布超过 72 小时后，`engine-attestation.json` 会过期，`monitor --authorize` 会以 `execution_engine_unverified` 拒绝，而重新发布计划不是一条可用出路。同一 plan revision 上的重新授权会按发布路径同样的方式**重新观测**引擎凭证并重签，凭证先落盘、卡后发布，重新授权失败则逐字节还原，不需要重新发布计划。

桌面 App 原生能力不能由 Python 直接调用时，public CLI 使用 `.vibe/provider-actions/` 的 provider-neutral request/result bridge。`monitor/resume` 会写入有界、digest 绑定的 `create/locate/visibility/resume/wait` 请求；App 会话按 `native_tool` 调用公开能力并回写绑定结果。Codex 映射到 `create_thread`、`navigate_to_codex_page`、`send_message_to_thread` 和 `wait_threads`；其他 provider 必须先接入并验证自己的等价桌面控制面。未完成原生探针时不允许 background fallback；在真实 task ID、host、定位和可见性全部核验前，状态保持 `blocked_unknown`；node-spec 中的 provider/mode/thread/host 自声明不会成为证据。

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
