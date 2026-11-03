# PRD：平台能力账本、当前平台识别与跨平台交接（5.2.6）

- 计划编号：`session-dd89468c829181ed`（`vibe plan` 已路由为 complex，S1=19：steps 5、domains 5、uncertainty 3、failure_cost 3、toolchain 3；可并行节点 ≥3，跨会话）
- 基线：`v5.2.5`（tag `7688137`）。本文所有行号指这个提交。
- 产品 spec：同目录 `2026-11-02-platform-ledger-and-handoff-product-spec.json`
- 证据来源：[outputs/platform-ledger-iteration-plan-2026-11-02.md](../../../outputs/platform-ledger-iteration-plan-2026-11-02.md)（本机 59 个 run、18 份能力登记、三个宿主的会话原始记录）

## 1. 问题

vibe 号称一套 CLI 连接多个平台，但它既不知道自己现在跑在哪个平台上，也没有一处地方如实记录每个平台能做什么、做不到什么。结果是：

1. **授权卡上的平台可能是错的。** socialmedia 的 run-63e7cfc2 在 Codex 上 8/8 完成：节点合同、20 个任务登记、3 班监工全是 Codex。但它的授权卡写的是 `workbuddy`。原因：run 已经全部验收（最后一次 `accepted` 是第 968 条事件）之后，一次 WorkBuddy 会话接管时对它跑了 `vibe monitor --platform-handoff`（第 972 条事件 `authorization_reauthorized change_reason=platform_handoff`），交接把一个已经没有未完成节点的 run 的授权卡平台改成了 WorkBuddy。审计时看不出这个 run 是在哪个平台完成的。（`system_inferred`，事件序号已核对）
2. **授权卡说的和实际派的不一样。** 5.2.5 那次 Claude Code 运行，授权卡里整合评审一项的 `mode` 是 `visible`、拓扑是 dual-visible（备注文字里写了「同样由后台会话执行」），任务登记里实际是 `mode=background`。字段和实际派发对不上。（`system_inferred`）
3. **代码里写的平台能力和实测不符，而且散在多处。** 包括派发方式矩阵、唤醒通道表、换班规则、推送拦截表、清单 yaml、项目级 `push-wake-verification.json`，彼此不引用。例如：WorkBuddy 被写成「没有唤醒通道」，实际上子代理完成后主会话会收到通知（但这个通知只到主会话，不是 worker 主动发的唤醒）；WorkBuddy 的 automations 心跳在 vibe 里完全没有记录；Claude Code 推送算不算已验证只记在单个项目的 `push-wake-verification.json` 里，换个项目就要重新登记。（`system_inferred`）
4. **能力登记没有时间，也没有版本。** 宿主升级后，旧结论仍被当成事实。WorkBuddy 10-30 已升到 5.9.4，而「Jobs 起不来」的结论是在 5.8.4 上测出来的。（`system_inferred`）
5. **跨平台交接从来没在真机上走完。** 交接代码连修了四版。但旧平台还有 worker 在跑时，系统会悄悄制造第二个写入者；派发方式变了也没有任何记录。（`system_inferred`）
6. **5.2.5 之后登记的 7 个问题，加上 2 个只修了一部分的问题**，让 Claude Code 后台运行仍要靠人推进：
   - 守护程序不会自己接收交付（#382）；
   - reviewer 有时发不了唤醒（#380）；
   - 旧 run 的会话决定本 run 能不能归档（#377）；
   - 网关间歇报 400 时节点被卡死（#376）；
   - 测试读本机配置（#378）；
   - 开发任务推送拦不住（#379）；
   - 返工时开发者不知道哪些文件会被交付门自动放行（#381）；
   - 部分填写的回写模板被收下（#344 第 1 条）；
   - 同一代派发留着旧请求时，重派失败（#363 E 条）。

   （`user_confirmed`：产品经理要求把这 9 个 issue 一并纳入）

## 2. 目标

用户在哪个平台上发起，vibe 就能自己认出这个平台。授权卡按一本带出处、带日期的平台能力账本，如实写出这次怎么派发、哪些看不到、靠什么唤醒监工、推送能拦住多少。派发严格照授权卡执行。交接到另一个平台时，系统先确认旧平台已经没人在写，把派发方式的变化记下来，然后继续。在 Claude Code 后台运行时，不需要监工手工 `resume` 也能推进到收口。

## 3. 用户场景

| 谁 | 什么情况 | 做什么 | 得到什么 |
|---|---|---|---|
| 产品经理 | 在 Claude Code、Codex 或 WorkBuddy 会话里提一个复杂需求 | `vibe plan` 发布、念授权卡 | 授权卡写对了当前平台，并且说清：这次是后台还是可见、哪些任务看不到、推送拦截是「部分拦截」还是「只靠提示词」 |
| 产品经理 | 一个 run 做到一半，想换到另一个平台继续 | 在新平台会话里执行交接 | 旧平台还有 worker 在跑时，系统拒绝交接，并告诉他是哪几个节点、怎么排空；排空后交接直接完成，派发方式的变化写进交接记录，不再单独问他 |
| 监工 | Claude Code 后台运行，worker 交付了，或 reviewer 发不出唤醒 | 什么都不用做 | 守护程序在两个周期内接收交付并推进 |
| 监工 | 网关间歇拒绝模型（400） | 什么都不用做 | 系统先在同一任务上退避续接，续接不了才换新会话；连续 3 次失败才升级，并写明原因是 provider 400 |
| 开发者（worker） | 授权卡是「允许远端动作」，自己在做一个开发节点 | 照派发提示干活 | 提示词和启动参数里都有推送禁令；返工时知道哪些文件会被自动放行，不用再问监工 |
| 维护者 | 宿主升级了一个版本 | 跑 `vibe doctor` | 看到哪些能力结论需要复核 |

## 4. 成功标准

1. 在 Claude Code、Codex、WorkBuddy 上各发布一次计划，都不带 `--adapter`：授权卡的平台和当前会话一致；故意传错 `--adapter` 时被拒绝。（`system_inferred`）
2. 三个平台各跑一个 2 节点 run：任务登记的派发方式，和授权卡上写的逐条一致，包括 Claude Code 上的整合评审。（`system_inferred`）
3. 代码里不再有第二份平台能力表：派发方式、唤醒通道、换班、推送拦截都从同一本账本读。模型档位命名不在本迭代范围（非目标里写明不改模型选档）。（`system_inferred`）
4. 交接时旧平台有 worker 在跑：交接被拒，并列出节点和排空办法；排空后交接成功，同一节点只有 1 个写入者。（`user_confirmed`，D2）
5. 跨平台交接时派发方式变了：交接不停下来等确认，交接记录和新授权卡里都能读到这次变化。（`user_confirmed`，D1）
6. Claude Code 后台跑 30 分钟，期间不手工 `resume`：所有交付都在 2 个守护周期内被接收。（`system_inferred`）
7. 9 个开放 issue 的验收例子全部有测试覆盖并通过；能在真机上验证的，在验收期真机验证。（`user_confirmed`）
8. 全量测试的结果不受本机 `~/.vibe-guide/config.json` 影响，也不受宿主环境变量（`CLAUDECODE`、`CODEX_THREAD_ID`、`CODEBUDDY_SESSION_ID` 等）影响：在 Claude Code 会话里跑和在普通终端里跑，结果一致。（`system_inferred`）
9. 对一个已经没有未完成节点的 run 执行交接：被拒绝或什么都不做，授权卡保留实际执行的平台。（`system_inferred`）

成功标准 1、6 和 WorkBuddy 5.9.4 的 Jobs 重测只能在真机上验证，放在验收期做，不归任何开发节点。成功标准 2 分两层：夹具版由 platform-e2e 节点覆盖，真机版放在验收期做。

## 5. 非目标

- 不接入 Cursor、Grok、DeepSeek Harness、Kimi Code 的真实派发；它们在账本里标「未验证」，授权卡照实显示。（`user_confirmed`，D3）
- 不追求可见性：交接后变成后台派发也接受，只做披露。（`user_confirmed`，D1）
- 不新增 `supervisor-read`，不做宿主层输出截断（沿用 5.2.5 决定）。（`user_confirmed`）
- 不重构 `monitor.py`；不改模型路由的选档规则；不修 AC-B3/AC-E1 的 3 条有意保留的基线失败。（`system_inferred`）
- 不部署、不发布、不动凭据、不新增第三方依赖、不改用户级规则文件。（`user_confirmed`）

## 6. 产品决定

| 编号 | 问题 | 选定 | 来源 |
|---|---|---|---|
| D1 | 交接后新平台的派发方式或可见性变了，要不要停下来确认？ | **不确认，只披露。** 差异写进交接记录和授权卡。作为可见降后台必须问产品经理这条规则的例外，只限 `platform_handoff`。要同步改的地方：AGENTS.md §6（两类边界）与 §12（拓扑或可见性变化使授权失效）、监工卡、prd-guide §4/§7、worker 派发提示里的同一条规则 | `user_confirmed`（2026-11-02 原话：「我不强求可见性，而且你给我差异我也只能接受，就不用每次单独确认了」） |
| D2 | 交接时还有 worker 在旧平台跑怎么办？ | **交接前必须排空。** 不排空就拒绝交接。排空的两种办法：等它交付；或者在旧平台停掉，再用一条受支持的命令在新平台登记终止证据 | `user_confirmed` |
| D3 | 没有运行记录的平台要不要纳入？ | **不纳入**，账本标未验证 | `user_confirmed` |

工程默认（监工自定，不需要产品经理拍板）：能力登记超过 14 天，或宿主版本和登记时不同，授权卡和 `doctor` 就提示「需复核」，但不阻断。某次派发的实际结果和账本矛盾时，把那一格降为未验证，并要求在本平台重新登记。

## 7. 代码现状（`v5.2.5`，只读核对）

| 关注点 | 位置 | 现状 |
|---|---|---|
| 当前平台怎么定 | `vibe_guide/node_spec.py:313-345` `normalize_node_spec` | 只看 `--adapter`；不给时要求项目里恰好只有一份能力登记，多份就失败。不读宿主环境 |
| 宿主环境变量 | `vibe_guide/scanner.py:103-109`、`:255-267` | 全仓库只有这里读 `CLAUDECODE`，而且只用来决定要不要写 `CLAUDE.md`；按测试 `test_the_session_marker_is_not_part_of_the_project_verdict`，doctor/scan 的结论不能取决于谁在跑。`WORKBUDDY_CONFIG_DIR` / `CODEBUDDY_CONFIG_DIR` 只是配置目录覆盖，不能当身份证据。能当身份证据的只有会话/线程编号：Claude Code 的 `CLAUDE_CODE_SESSION_ID`（加 `CLAUDECODE`）、Codex 的 `CODEX_THREAD_ID` / `CODEX_SESSION_ID`、WorkBuddy 的 `CODEBUDDY_SESSION_ID` |
| 能力登记 | `vibe_guide/attest.py:62-89`、`adapters/task_provider.py:384-500`（`publish_capabilities` 组装记录） | 只存 facts、provenance、project_id，没有时间、宿主版本、会话 id。旧版单文件 `capabilities.json` 仍会被读取和覆盖 |
| 派发方式矩阵 | `vibe_guide/adapters/registry.py:25-49` `DISPATCH_TOPOLOGY_MATRIX` | 只有 probe_pass / probe_unknown 两列，没有出处 |
| 唤醒通道 | `vibe_guide/supervisor.py:1805-1815` `WAKE_CHANNELS` / `WAKE_NO_CHANNEL_REASON`；`.vibe/push-wake-verification.json`（`supervisor.py:1973` 读取） | WorkBuddy 写「没有推送通道」。Claude Code 的通道 `SendMessage` 已登记，`WAKE_NO_CHANNEL_REASON["claude"]` 那句「unverified」是走不到的死字符串；算不算已验证由各项目的 `push-wake-verification.json` 决定，它还决定授权卡兜底检查是 60 还是 10 分钟 |
| 换班 | `vibe_guide/supervisor.py:635` `provider_never_rotates` | 硬编码 claude 家族 |
| 推送拦截 | `vibe_guide/authorization.py:121-160` `REMOTE_GIT_BAN_TEXT` / `remote_git_denied` / `REMOTE_GIT_ENFORCEMENT` | 只在授权卡是 `deny` 时加禁令；`allow` 时开发 worker 什么都拿不到（#379 根因） |
| 每个节点的派发方式 | `vibe_guide/authorization.py:398-447` `dispatch_topology_for_node`；`:803-821` `acceptance_contract_projection` | 优先读合同里的 `dispatch_topology`，但没有任何地方写入它；抽查真实计划全为空，每次派发都按当前登记重判。验收投影只把 `adapter_id/worker/reviewer_worker/model/reasoning/route_digest` 当路由字段，交接门（`monitor.py:3837-3844`）拒绝其余任何合同变化——直接把 `dispatch_topology` 写进合同，会让每次交接都被判成改了验收合同 |
| 整合评审派发 | `vibe_guide/cli.py:603-640` `_background_worker_entries`、`:1014-1054` `_public_runner` | 授权卡给整合评审的 `mode` 是 `visible`，只在备注文字里说明「同样由后台会话执行」；实际以后台派发。问题在 `mode` 字段，不在披露文字 |
| 交接 | `vibe_guide/cli.py:919-985`、`handoff_rebind.py`、`monitor.py:3966-3982`、`monitor_handoff.py` | 交接前不检查有没有在跑的 worker，也不检查 run 是否已经没有未完成节点；重授权时直接清空 handles，把旧身份记进 `handoff_superseded`，之后派 successor 时才要求旧任务是终态，证不了就把节点标 `blocked_unknown` |
| 归档判断看活会话（#377） | `vibe_guide/live_sessions.py:50-83` | 两路合并：会话编号在本 run `tasks.json` 里，或会话的 cwd 落在本 run 任一节点工作树里。后一路是为了接住已启动但还没写进 `tasks.json` 的会话；副作用是旧 run 同名工作树里的会话也被算进来 |
| 守护程序推进（#382） | `vibe_guide/supervisor.py:288-301` `run_once`；`daemon.py:604-616`、`_parked()` | 每拍调 `monitor.resume(..., poll_handles=False)` 再调 `tick`。为什么手工 `vibe resume` 能马上接收交付而守护程序不能，**未查明**（`unverified`）。一条线索：守护程序判定停车后不再 tick，`_parked()` 在 `blocked_grace` 之后即使有未消费交付也会停车 |
| 网关 400（#376） | `vibe_guide/runners/claude_bg.py:492`；`adapters/claude_bg.py:834-837` `status()` | 会话 `failed` 映射为 `failed_retryable`，下游报 `unrecognized runner event` 并落 `blocked_unknown`。`status()` 只返回状态，不读会话的失败原因，所以现在拿不到「400」这个信息 |
| 回写占位符（#344-1） | `vibe_guide/runners/provider_action.py:373-388` `_identity_fields`、`evidence.py:137-142` | 写前校验明确不看 wait 结果里的证据文本；交付门只判非空。在 v5.2.5 上实测，部分填写的模板被收下 |
| 同代重派（#363-E） | `vibe_guide/adapters/task_provider.py:575-590` | 同一个 action_id 的旧请求内容不同，就抛 `identity drift`，没有 supersede 路径。典型现场是旧 create 根本没被消费：没有结果、没有绑定，拿不出「旧任务已终止」的证据 |
| 测试隔离（#378） | `tests/` 共 244 个 TestCase 文件，只有 13 个提到 `VIBE_HOME`；用 `python3 -m unittest discover -s tests` 跑，没有 conftest 也没有 `tests/__init__.py` | 没有统一夹具，也没有一个能在所有测试之前生效的入口 |
| 打包 | `setup.py:8` `package_data`、`MANIFEST.in` | 只收 `adapters/manifests/*.yaml` 和 `protocols/*.md`；新增的数据文件不会进 wheel/sdist |
| 自动放行默认值 | `vibe_guide/config.py:97-108` `_auto_scope_paths` | 项目没写 `auto_scope_paths` 时为空；本项目 `.vibe/config.json` 没有写。AGENTS.md §6 规定登记本身就是产品经理授权 |

## 8. 节点

产品 spec 里写的是验收语言；完整合同见 product-spec.json。

| 节点 | 一句话 | 覆盖 | 依赖 |
|---|---|---|---|
| test-isolation | 测试结果不随本机配置和宿主环境变量变化 | #378 | — |
| platform-ledger | 一本随包发布的账本代替多处散表；按实测改正 | 问题 3、4 | — |
| host-detect | 按会话编号认出当前平台；能力登记带时间、版本、会话；已全部验收的 run 拒绝交接 | 问题 1、4 | platform-ledger |
| daemon-consumes | 守护程序自己接收交付；唤醒失败有兜底；活会话只数本 run（含刚启动未绑定的） | #382 #380 #377 | host-detect |
| card-and-handoff | 派发方式授权时写进合同并作为路由字段；授权卡与实际派发一致；交接前排空（D2）、差异披露（D1）并同步各处规则 | 问题 2、5 | daemon-consumes |
| transient-failure | 网关 400：先同任务续接，不行再换会话；保留 provider_http_400 | #376 | card-and-handoff |
| worker-boundaries | allow 模式也禁止 worker 推送；返工提示写清自动放行；项目登记 protocols/__init__.py | #379 #381 | transient-failure |
| mailbox-leftovers | 部分填写模板被拒；同代重派在旧任务已终止或从未创建时自动顶替 | #344 #363 | worker-boundaries |
| platform-e2e | 三个平台 × 三种起点的端到端测试，主断言是授权卡等于实际派发 | 成功标准 2、4、5、9 | mailbox-leftovers（`integration_after` test-isolation） |

**排期说明：除了第一波的 test-isolation 和 platform-ledger 可以同时开工，其余基本是一条线。** 原因是 `monitor.py`、`cli.py`、`authorization.py`、`supervisor.py`、`runners/provider_action.py`、`adapters/task_provider.py` 这几个共用文件几乎每个节点都要改，按规则写范围重叠就必须串行。这不影响结果，只影响工期。

## 9. 风险

- **宿主环境变量会互相继承。** 例如在 WorkBuddy 会话里起一个 Claude CLI 子进程，可能同时带两家的变量。识别顺序必须先看更具体的宿主（WorkBuddy 和 Codex），再看 Claude Code；识别不出来就保持未知，不猜。（`system_inferred`）
- **把派发方式写进节点合同，会改变合同摘要。** 已授权的旧 run 不能因为升级就被判成授权失效。只对新发布的计划写入；旧计划沿用实时判断，并在 `doctor` 里提示。派发方式必须登记成路由字段，否则交接门会把每次交接都判成改了验收合同。（`system_inferred`）
- **D2 的代价。** 交接常常是因为旧平台用不了了；如果旧平台打不开，又判断不了 worker 是否已停，交接就会一直被拒。所以必须提供一条在新平台登记终止证据的命令。（`system_inferred`）
- **账本改动影响进行中的 run。** 账本改了某平台的唤醒结论，会改变授权卡的兜底检查间隔。进行中的 run 升级后运行参数必须不变，只有新发布的计划才按新账本生成。（`system_inferred`）
- **修 #377 不能丢掉现有保护。** 按工作树路径匹配会话，是为了接住已启动但还没写进任务登记的会话；改成只数登记会话，可能在还有写入者时就把 run 归档。新规则必须同时覆盖本 run 自己发出的派发。（`system_inferred`）
- **#382 的根因还没定位。** 修复范围可能比预计的大。`daemon-consumes` 要先交一份定位结论，再改代码。（`unverified`）
- **真机验收要在三个宿主上各跑一遍**，WorkBuddy 的 Jobs 探针要在 5.9.4 上重测。（`system_inferred`）
