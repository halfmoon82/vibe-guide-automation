# Vibe Coding 辅助开发向导 Implementation Plan

> **Revision 2 — 2026-08-24:** 显式独立 developer/reviewer 是通用产品的首选合同。七个平台优先在对应桌面 App 中创建用户可见、可进入、可续接的独立任务；Codex App 使用 `create_thread`。无等价桥接的平台可明确降级为 background subagent，但不能冒充完整可见自动化。当前项目的旧内部 subagent 拓扑已停止。任务对上限按同时活跃并发量计算；完成并归档的任务保留证据但释放名额。

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 在当前“开发辅助”目录实现一个统一 CLI 和桌面 Agent 会话适配层，完成项目扫描/初始化、Skill 安装、S0/S1 分流、PRD/Spec/DAG 规划和一次授权后的可恢复监工核心。

**Architecture:** 使用 Python 3.9+ 的小型模块化 CLI。`models.py` 和 `contracts.py` 先定义配置、DAG、授权和运行事件的稳定接口；扫描/初始化/Skill 安装、规划引擎、监工状态机和 Agent 适配器分别作为可独立测试的模块。项目运行状态保存在 `.vibe/`，调度器通过事件日志、原子快照和 `tasks.json` 恢复。核心通过平台无关的 `VisibleTaskProvider` 管理 developer/reviewer；各适配器负责创建、定位、续接和等待其桌面 App 中的显式独立任务。Codex App provider 使用 `create_thread`。provider 明确不支持时可回退 `BackgroundTaskProvider`，但状态、授权和交付必须保留降级标识。

**Tech Stack:** Python 3.9+；`argparse`、`dataclasses`、`json`、`pathlib`、`subprocess`、`hashlib`、`tempfile`、`unittest`；配置解析使用 `PyYAML>=6.0`；CLI 入口通过 `pyproject.toml` 的 console script 暴露为 `vibe`。

## Global Constraints

- 只允许新增或修改 `/Users/smy/Desktop/CFO/黑客松/开发辅助/` 内文件；不得纳入上层 CFO 仓库其他目录的现有变更。
- 不使用 `git add .` 或 `git add -A`；每个任务只暂存自己的白名单文件。
- `architecture-skill-pack` 必装；`akasha-grimoire` 和 `aligning-with-johari-windows` 可选；Skill 源码只从 GitHub 拉取，不复制进本项目。
- 已有 `AGENTS.md` 只生成补丁建议，不直接覆盖或追加。
- `scan` 只读；`init`、Skill 安装和知识库初始化必须在用户确认后执行。
- S0 只做规则预筛；S1 默认 `<=8` 直接执行、`9-15` 轻规划、`>15` 复杂流程。
- DAG 默认优先并行；只有硬依赖阻塞启动，联调关系记录为 `integration_after`。
- 一次授权覆盖当前 DAG 已列明的全部非 deploy 动作；deploy 永远单独授权。
- 技术完成、Review 通过、交付授权和最终发布必须分开记录。
- DAG 节点以 `planned` 进入执行；developer 交付只到 `delivered`，独立 Review 后的 `accepted` 才能解锁硬依赖；`complete` 只表示所有节点均 accepted 的运行状态，`start_pending` 仅是私有启动意图。
- 未知状态不得转换为无事项或成功；无法验证时进入 `blocked_unknown`。
- 实现缺陷优先退回同一 worker；设计变化只重建受影响 DAG 后缀。
- 一致性纠偏按“用户当前明确决定 → 已批准 PRD/Design Spec → 授权卡 → Issue 合同 → 下层实现”取证；若只有一个答案且仍在已授权项目、DAG 和非 deploy 边界内，记录纠偏、更新受影响后缀或合同并自动继续。只有多种结果仍会实质改变产品、范围或方向变化、需要外部/deploy/系统权限授权，或证据无法区分安全结果时暂停。
- 桌面 App 适配必须检测权限并如实降级，不能绕过沙箱或伪造完整自动化。
- 七个平台的完整可见自动化路径中，开发 Issue 和独立 Review 必须分别映射为对应 App 的可见独立任务；可见、可进入、可追溯是验收条件。
- 每个任务登记 provider、平台任务 ID、host、worktree、branch、`status_file`、`handoff_file` 和 cursor/token；Codex 具体登记 `threadId`、`hostId` 和 cursor。返工回到原 developer，复审回到原 reviewer。
- 只有 adapter 明确无等价可见任务能力时，才可把 developer/reviewer 降级为 background subagent；授权卡和交付必须披露限制，不能标为完整可见自动化。
- 任务创建也是授权动作；只有新版授权卡确认后才能调用 `create_thread`。设计或执行拓扑变化使授权失效。
- 可见 worker 因工具丢失而无法继续时，先记录并验证原 thread aborted/archived、cursor、零写入、冻结 HEAD、clean writer worktree 和唯一 writer；随后仅允许一个 visible successor 复用原 writer root。App 自动 host worktree 不得成为第二 writer root，也不得因此创建额外 worktree。

## Implementation DAG

```text
N0 共享模型、CLI 骨架和文件边界
├── N1 扫描/doctor/初始化/Skill 安装
├── N2 S0/S1、产品决策、PRD/Spec/Issue/DAG 规划
├── N3 授权、状态快照、事件日志、监工调度核心
└── N4 Agent 能力检测与桌面会话适配器
N1 ─┐
N2 ─┼── N5 端到端集成、验收夹具和用户文档
N3 ─┤
N4 ─┘
```

N1、N2、N3、N4 在 N0 的契约完成后可并行开发；N3 使用 N0 的 runner/事件占位接口，不等待具体 Agent 适配器；N5 只在四条工作流都通过定向测试后启动。

### Revision 2 可见任务执行 DAG

代码依赖关系仍为 `N0 → N1/N2/N3/N4 → N5`，但每个 Issue 拆为“可见 developer → 可见 reviewer”两个不同的桌面 App 任务。当前项目使用 Codex App `create_thread` 实施；迁移后的执行图为：

```text
M0 更新设计/计划/项目规则（本次主会话完成）
├── R0 可见 reviewer：重新核验 N0@7d8ba5d
├── R1 可见 reviewer：重新核验 N1@22819a9
├── D2 可见 developer：从 N2@9916c2c 续作/补报告 → R2 可见 reviewer
├── D3 可见 developer：从封存对象 41c16c26… 续作 → R3 可见 reviewer
└── D4 可见 developer：实现 N4 → R4 可见 reviewer

R1 + R2 + R3 + R4 → D5 可见 developer：N5 集成 → R5 可见 reviewer：全分支验收
```

首批 ready 集合为 `R0、R1、D2、D3、D4`。授权容量为同时活跃最多 5 对 developer/reviewer，即同一时刻最多 10 个可见独立任务；只创建 DAG 已 ready 的任务。Issue 的 developer/reviewer 完成、P0–P2 清零且证据登记后关闭或归档并释放名额，历史任务身份和 cursor 继续保留。R0 是 N0 的补充复核证据；N5 的硬门只要求 N1–N4 分别通过可见 reviewer，但最终 R5 必须覆盖 N0–N5 全分支。任一 Review 的 P0–P2 问题回到原 developer，修复后回到原 reviewer，不新建替代任务。

### 迁移保留证据

| 节点 | 保留成果 | 当前处理 |
|---|---|---|
| N0 | commit `7d8ba5d`，原报告和 Review 包 | 不重复实现；由新建可见 R0 重新核验 |
| N1 | commits `21c961d`、`22819a9`，报告和 Review 包 | 不重复实现；由新建可见 R1 重新核验 |
| N2 | commit `9916c2c`，定向测试 15 项通过，尚无正式报告/Review | D2 从该 commit 继续补齐交付，再交 R2 |
| N3 | 未完成文件封存为 Git stash object `41c16c26a66fb91be2df9d24900b387cd926a983` | D3 在原隔离 worktree 恢复后继续；旧 worker 已停止 |
| N4/N5 | 尚未开始 | 仅在新版授权后创建可见任务 |

旧内部 worker/reviewer 不再拥有 writer 或 Review 权；不得与新可见任务同时运行。

## File Responsibility Map

**Create:**

- `pyproject.toml`：包元数据、`vibe` console script、运行依赖和开发命令。
- `vibe_guide/__init__.py`：版本常量和公开包入口。
- `vibe_guide/__main__.py`：`python -m vibe_guide` 入口。
- `vibe_guide/cli.py`：七个 CLI 子命令的参数解析和退出码映射。
- `vibe_guide/models.py`：项目路径、Skill 引用、Agent 能力、计划、DAG 节点、授权和运行事件的数据类。
- `vibe_guide/contracts.py`：scanner、planner、runner、adapter 的 Protocol 接口。
- `vibe_guide/paths.py`：项目根、`.vibe/`、用户级 `VIBE_HOME` 和安全相对路径解析。
- `vibe_guide/scanner.py`：本地环境、Git、Agent、Skill、AGENTS.md 和知识库扫描。
- `vibe_guide/doctor.py`：能力检查、版本校验和降级报告。
- `vibe_guide/initializer.py`：项目内 `.vibe/knowledge/` 初始化和 `AGENTS.md` 补丁建议。
- `vibe_guide/skills.py`：GitHub Skill clone/fetch、提交 SHA 校验、非覆盖安装和安装记录。
- `vibe_guide/planner.py`：S0/S1 分流、产品决策卡、PRD/Spec/Issue 草稿和计划就绪门。
- `vibe_guide/dag.py`：硬依赖、`integration_after`、契约和并行组校验及 ready 节点计算。
- `vibe_guide/authorization.py`：授权卡生成、DAG 版本绑定、授权摘要和授权失效判断。
- `vibe_guide/state.py`：原子快照、追加事件、节点状态转换和恢复读取。
- `vibe_guide/monitor.py`：ready 调度、唯一 writer 租约、Review/返工轮转和 blocked 状态。
- `vibe_guide/adapters/base.py`：Agent 适配器基类和能力声明。
- `vibe_guide/adapters/registry.py`：七类 Agent 适配器发现、选择和降级排序。
- `vibe_guide/adapters/manifests/*.yaml`：七类 Agent 的可配置探针、调用方式和能力声明，不写入平台私有凭据。
- `vibe_guide/runners/fake.py`：测试用 runner，用于不启动真实 Agent 的状态机测试。
- `vibe_guide/runners/local.py`：按已确认的适配器命令启动本地 worker，并保存 PID/退出码/日志路径。
- `tests/test_models.py`：数据类、序列化和契约校验。
- `tests/test_scanner.py`：扫描结果、Git/Agent/Skill/AGENTS.md 识别。
- `tests/test_initializer.py`：项目知识库初始化、补丁建议和幂等性。
- `tests/test_skills.py`：GitHub 源校验、SHA 记录和目标不覆盖。
- `tests/test_planner.py`：S0/S1、产品决策未闭合阻塞和 PRD/计划就绪门。
- `tests/test_dag.py`：硬依赖/契约依赖/集成依赖、并行组和 ready 计算。
- `tests/test_authorization.py`：授权摘要、DAG 版本变化失效和 deploy 排除。
- `tests/test_state.py`：快照恢复、事件顺序和原子写入。
- `tests/test_monitor.py`：唯一 writer、并行调度、同 worker 返工和 blocked_unknown。
- `tests/test_adapters.py`：能力探针、最高权限结果和降级适配。
- `tests/test_cli.py`：命令退出码、JSON 输出和授权前不启动 runner。
- `tests/fixtures/`：虚拟项目、虚拟 Agent 探针、示例 PRD/DAG 和 fake runner 事件。
- `README.md`：安装、扫描、规划、授权、监工和恢复的产品经理语言说明。
- `docs/superpowers/plans/2026-08-24-vibe-coding-guide-implementation-plan.md`：本实施计划。

## Task 0: Establish Shared Contracts and CLI Skeleton (N0)

**Files:**

- Create: `pyproject.toml`
- Create: `vibe_guide/__init__.py`
- Create: `vibe_guide/__main__.py`
- Create: `vibe_guide/cli.py`
- Create: `vibe_guide/models.py`
- Create: `vibe_guide/contracts.py`
- Create: `vibe_guide/paths.py`
- Test: `tests/test_models.py`
- Test: `tests/test_cli.py`

**Interfaces:**

- `ProjectPaths.from_cwd(cwd: Path) -> ProjectPaths`
- `DAGNode(id: str, title: str, depends_on: list[str], integration_after: list[str], parallel_group: Optional[str], contract: dict, status: str)`
- `Plan(plan_id: str, version: int, prd_path: str, node_ids: list[str], status: str)`
- `AgentCapabilities(agent_id: str, shell: bool, subprocess: bool, worktree: bool, background: bool, session_resume: bool, level: str)`
- `Runner.start(contract: dict, worktree: Path) -> RunHandle`
- `Runner.poll(handle: RunHandle) -> list[RunEvent]`
- `Runner.stop(handle: RunHandle) -> None`
- `python -m vibe_guide <command> [--json]` returns `0` on success, `2` on usage error, `3` on blocked/needs confirmation, `4` on unknown state.

- [ ] **Step 1: Write failing model and CLI contract tests**

  Add tests that construct a `DAGNode`, serialize/deserialize a `Plan`, resolve the project root from the current directory, and assert `vibe scan --json` returns exit code `0` with a JSON object. Assert `vibe monitor` without an authorization token returns exit code `3` and does not call a runner.

- [ ] **Step 2: Run the contract tests and verify failure**

  Run: `python3 -m unittest discover -s tests -v`

  Expected: FAIL because package modules and CLI entrypoint do not yet exist.

- [ ] **Step 3: Implement the minimal package and shared contracts**

  Define dataclasses with explicit string statuses, JSON-safe conversion, and validation for node IDs, duplicate dependencies, and unsupported statuses. Use `typing.Optional` instead of Python 3.10 union syntax so the package remains Python 3.9 compatible. Keep CLI handlers as thin dispatch functions; do not put scanner, planner, or monitor logic in `cli.py`.

- [ ] **Step 4: Run the contract tests and verify pass**

  Run: `python3 -m unittest discover -s tests -v`

  Expected: PASS for `tests/test_models.py` and `tests/test_cli.py`.

- [ ] **Step 5: Commit the isolated N0 change**

  Stage only `pyproject.toml`, `vibe_guide/`, and the two N0 test files after the repository-specific commit authorization is available.

## Task 1: Read-only Scan, Doctor, Initialization, and External Skills (N1)

**Depends on:** N0 contracts

**Files:**

- Create: `vibe_guide/scanner.py`
- Create: `vibe_guide/doctor.py`
- Create: `vibe_guide/initializer.py`
- Create: `vibe_guide/skills.py`
- Create: `tests/test_scanner.py`
- Create: `tests/test_initializer.py`
- Create: `tests/test_skills.py`
- Create: `tests/fixtures/scan-project/`

**Interfaces:**

- `scan_project(paths: ProjectPaths) -> ScanReport`
- `doctor(report: ScanReport) -> DoctorReport`
- `build_agentsmd_patch(existing: Optional[str], report: ScanReport) -> PatchProposal`
- `init_project(paths: ProjectPaths, confirm: bool) -> InitResult`
- `install_skill(spec: SkillSpec, vibe_home: Path, fetch: bool) -> SkillInstallResult`

- [ ] **Step 1: Write failing tests for scan and initialization**

  Cover: project root detection, existing `AGENTS.md` preserved, missing `AGENTS.md` produces a proposal, missing `.vibe/knowledge/` is reported, repeated `init_project(confirm=True)` is unchanged, and `init_project(confirm=False)` performs no writes.

- [ ] **Step 2: Write failing Skill installer tests**

  Use a local Git fixture repository to verify source URL normalization, commit SHA recording, existing target preservation, and failed fetch returning `pending` without claiming installation.

- [ ] **Step 3: Run N1 tests and verify failure**

  Run: `python3 -m unittest tests.test_scanner tests.test_initializer tests.test_skills -v`

  Expected: FAIL because scanner, initializer, and Skill installer modules are absent.

- [ ] **Step 4: Implement read-only scan and doctor**

  Detect only observable facts: Python/Git versions, Git root/remote, candidate Agent commands, existing rules, `.vibe/` state, and configured Skill records. Do not infer login, merge authority, approval, or deployment permission from command existence.

- [ ] **Step 5: Implement idempotent initializer**

  On confirmation create only missing `.vibe/knowledge/`, `.vibe/proposals/agentsmd/`, and minimal config/state files. Write the proposed AGENTS patch under `.vibe/proposals/agentsmd/`; never edit an existing AGENTS file.

- [ ] **Step 6: Implement external Skill installer**

  Clone or fetch into `<VIBE_HOME>/vendor/<skill-name>`, verify the requested commit SHA, and create a non-overwriting link/copy under `<VIBE_HOME>/skills/`. Persist source, SHA, timestamp, and validation state; never print credentials.

- [ ] **Step 7: Run N1 tests and verify pass**

  Run: `python3 -m unittest tests.test_scanner tests.test_initializer tests.test_skills -v`

  Expected: PASS, including the no-write scan path and target-collision path.

## Task 2: S0/S1, Product Decision Cards, and DAG Planning (N2)

**Depends on:** N0 contracts; may run in parallel with N1, N3, and N4 after N0

**Files:**

- Create: `vibe_guide/planner.py`
- Create: `vibe_guide/dag.py`
- Create: `tests/test_planner.py`
- Create: `tests/test_dag.py`
- Create: `tests/fixtures/plans/`

**Interfaces:**

- `classify_s0(message: str) -> S0Result`
- `score_s1(context: TaskContext) -> S1Score`
- `route_task(score: S1Score) -> Literal['simple', 'light_plan', 'complex']`
- `create_decision_card(question: ProductQuestion) -> DecisionCard`
- `approve_prd(prd: PRD, decisions: list[DecisionCard]) -> PRDResult`
- `validate_dag(nodes: list[DAGNode]) -> DAGValidation`
- `ready_nodes(nodes: list[DAGNode]) -> list[DAGNode]`
- `render_plan_artifacts(plan: Plan, output_dir: Path) -> PlanArtifacts`

- [ ] **Step 1: Write failing S0/S1 and product decision tests**

  Assert obvious one-step requests route to `simple`, multi-step build requests enter S1, scores `8`, `9`, `15`, and `16` route to the exact configured bands, and an unresolved product decision prevents PRD status from becoming `approved`.

- [ ] **Step 2: Write failing DAG parallelism tests**

  Assert a node with only `integration_after` is ready, a node with incomplete `depends_on` is not ready, duplicate IDs and cycles fail validation, and independent nodes in one `parallel_group` are returned together.

- [ ] **Step 3: Run N2 tests and verify failure**

  Run: `python3 -m unittest tests.test_planner tests.test_dag -v`

  Expected: FAIL because planner and DAG modules are absent.

- [ ] **Step 4: Implement S0/S1 and decision gates**

  Keep S0 rule matching deterministic and cheap. Store the five S1 scores and rationale. Render product decision cards in plain language with options, impact, recommendation, and explicit decision status.

- [ ] **Step 5: Implement DAG validation and ready scheduling**

  Separate hard dependencies from integration dependencies. Validate contracts before a node is ready; do not turn shared-file risk into a hard dependency. Produce `dag.yaml` plus human-readable plan output.

- [ ] **Step 6: Run N2 tests and verify pass**

  Run: `python3 -m unittest tests.test_planner tests.test_dag -v`

  Expected: PASS, including placeholder-contract parallelism and design-change blocking.

## Task 3: Authorization, State, Event Log, and Monitor Core (N3)

**Depends on:** N0 contracts; consumes N2 interfaces but can begin with N0 placeholder plan objects

**Files:**

- Create: `vibe_guide/authorization.py`
- Create: `vibe_guide/state.py`
- Create: `vibe_guide/task_registry.py`
- Create: `vibe_guide/monitor.py`
- Create: `vibe_guide/runners/fake.py`
- Create: `tests/test_authorization.py`
- Create: `tests/test_state.py`
- Create: `tests/test_task_registry.py`
- Create: `tests/test_monitor.py`

**Interfaces:**

- `build_authorization_card(plan: Plan, nodes: list[DAGNode], capabilities: AgentCapabilities) -> AuthorizationCard`
- `authorize(card: AuthorizationCard, confirmation: str) -> AuthorizationRecord`
- `is_authorization_valid(record: AuthorizationRecord, plan: Plan) -> bool`
- `append_event(paths: ProjectPaths, event: RunEvent) -> None`
- `save_task_binding(paths: ProjectPaths, binding: TaskBinding) -> None`
- `load_task_binding(paths: ProjectPaths, issue_id: str, role: str) -> TaskBinding`
- `load_snapshot(paths: ProjectPaths, run_id: str) -> RunSnapshot`
- `Monitor.start(record: AuthorizationRecord, runner: Runner) -> RunSnapshot`
- `Monitor.resume(run_id: str, runner: Runner) -> RunSnapshot`
- `Monitor.tick(run_id: str, runner: Runner) -> RunSnapshot`

- [ ] **Step 1: Write failing authorization, task registry, and state tests**

  Assert authorization cards list every allowed action, explicitly exclude deploy, bind to the plan version, invalidate on plan changes, append events without rewriting history, recover the last valid snapshot after process interruption, and preserve exact developer/reviewer task identities without allowing duplicate writers.

- [ ] **Step 2: Write failing monitor tests with fake runner**

  Cover: two independent ready nodes start together; a hard-dependent node waits; one node owns one writer lease; a review finding returns to the same worker; a transient unknown becomes `blocked_unknown` rather than `completed`; `monitor` without authorization never calls `Runner.start`.

- [ ] **Step 3: Run N3 tests and verify failure**

  Run: `python3 -m unittest tests.test_authorization tests.test_state tests.test_task_registry tests.test_monitor -v`

  Expected: FAIL because state, authorization, and monitor modules are absent.

- [ ] **Step 4: Implement atomic state and event persistence**

  Write snapshots and `tasks.json` to a temporary file in the same directory and replace atomically. Append JSONL events with sequence numbers. Persist `threadId`, `hostId`, worktree, branch, status/handoff paths and cursor. Use an exclusive lease file per node/worktree and clear only leases owned by the current run.

- [ ] **Step 5: Implement authorization binding**

  Canonicalize the plan ID/version, node IDs, file scope, worker scope, action set, and deploy exclusion before hashing. Store only the digest and non-secret metadata; never store tokens or credentials.

- [ ] **Step 6: Implement monitor scheduling and review loop**

  Compute ready nodes from `dag.ready_nodes`, start all non-conflicting nodes, poll runner events, transition nodes through delivery/review/accepted, and send in-contract findings back to the same worker. Preserve prior evidence on rework.

- [ ] **Step 7: Run N3 tests and verify pass**

  Run: `python3 -m unittest tests.test_authorization tests.test_state tests.test_task_registry tests.test_monitor -v`

  Expected: PASS, including parallel dispatch, unique writer, resume, and unknown-state behavior.

## Task 4: Agent Capability Adapters and Session Bridge (N4)

**Depends on:** N0 contracts; may run in parallel with N1, N2, and N3

**Files:**

- Create: `vibe_guide/adapters/base.py`
- Create: `vibe_guide/adapters/task_provider.py`
- Create: `vibe_guide/adapters/registry.py`
- Create: `vibe_guide/adapters/manifests/codex.yaml`
- Create: `vibe_guide/adapters/manifests/claude-code.yaml`
- Create: `vibe_guide/adapters/manifests/cursor.yaml`
- Create: `vibe_guide/adapters/manifests/grok.yaml`
- Create: `vibe_guide/adapters/manifests/workbuddy.yaml`
- Create: `vibe_guide/adapters/manifests/kimi-code.yaml`
- Create: `vibe_guide/adapters/manifests/deepseek-harness.yaml`
- Create: `tests/test_adapters.py`
- Create: `tests/fixtures/agents/`

**Interfaces:**

- `Adapter.detect(environment: Environment) -> DetectionResult`
- `Adapter.capabilities(environment: Environment) -> AgentCapabilities`
- `Adapter.session_prompt(trigger: str, plan_id: Optional[str]) -> str`
- `Adapter.monitor_command(plan_id: str, json_output: bool) -> list[str]`
- `Adapter.downgrade_reason(capabilities: AgentCapabilities) -> Optional[str]`
- `AdapterRegistry.detect_all(environment: Environment) -> list[DetectionResult]`
- `VisibleTaskProvider.create(role: str, issue_id: str, contract_path: Path) -> TaskBinding`
- `VisibleTaskProvider.resume(binding: TaskBinding, contract_path: Path) -> None`
- `VisibleTaskProvider.wait(binding: TaskBinding, cursor: Optional[str]) -> TaskUpdate`
- `VisibleTaskProvider.visibility(binding: TaskBinding) -> VisibilityResult`
- `BackgroundTaskProvider.create(role: str, issue_id: str, contract_path: Path) -> TaskBinding`

- [ ] **Step 1: Write failing adapter tests**

  Use fake command probes to assert each manifest has an ID, probe definition, session prompt, capability mapping, and visible-task provider declaration. Assert a fully capable environment with verified create/enter/resume/wait yields `full`; missing visible-task capability selects an explicitly labelled `background` downgrade and cannot claim visible automation; a no-subprocess environment yields `guide`.

- [ ] **Step 2: Run adapter tests and verify failure**

  Run: `python3 -m unittest tests.test_adapters -v`

  Expected: FAIL because adapter modules and manifests are absent.

- [ ] **Step 3: Implement manifest-driven capability detection**

  Do not guess proprietary app paths or APIs. Read probes from manifests, report observed command/path/permission facts, and map them to the three capability levels. Session prompts must contain only the short trigger and current plan reference. 七个平台优先使用可创建、进入、续接和等待显式独立任务的 provider；Codex App 映射到 user-owned thread。只有确认无等价桥接时才使用明确标识的 background provider。

- [ ] **Step 4: Implement session bridge commands**

  Return stable JSON for detected Agent, capability level, authorization-card URL/path, and the exact local CLI command the App can invoke. Keep prompts product-manager readable and avoid embedding platform-specific workflow rules in the core.

- [ ] **Step 5: Run adapter tests and verify pass**

  Run: `python3 -m unittest tests.test_adapters -v`

  Expected: PASS, including highest-permission and downgrade cases.

## Task 5: Local Runner and End-to-End Acceptance (N5)

**Depends on:** N1, N2, N3, N4

**Files:**

- Create: `vibe_guide/runners/local.py`
- Modify: `vibe_guide/cli.py`
- Create: `tests/test_cli.py` (extend with integration cases)
- Create: `tests/test_end_to_end.py`
- Create: `README.md`
- Create: `tests/fixtures/e2e-project/`

**Interfaces:**

- `LocalRunner.start(contract: dict, worktree: Path) -> RunHandle`
- `LocalRunner.poll(handle: RunHandle) -> list[RunEvent]`
- `LocalRunner.stop(handle: RunHandle) -> None`
- `run_cli(argv: list[str], cwd: Path) -> CLIResult`

- [ ] **Step 1: Write failing end-to-end tests**

  Build a temporary project and fake Agent command that: scans without writing, initializes after confirmation, routes a simple request directly, creates a complex plan with two parallel placeholder nodes, refuses to monitor before authorization, runs after authorization, resumes after a simulated interruption, and stops at `blocked_design` when the contract changes.

- [ ] **Step 2: Run the end-to-end tests and verify failure**

  Run: `python3 -m unittest tests.test_end_to_end -v`

  Expected: FAIL because local runner wiring and final CLI dispatch are incomplete.

- [ ] **Step 3: Implement LocalRunner and CLI wiring**

  Launch only commands returned by a confirmed adapter manifest, capture PID/exit code/log path, convert process events to `RunEvent`, and keep deploy outside the default action set. Wire `scan`, `init`, `doctor`, `plan`, `monitor`, `status`, and `resume` to JSON and human-readable output.

- [ ] **Step 4: Run targeted and full tests**

  Run: `python3 -m unittest discover -s tests -v`

  Expected: all tests pass, including scan no-write, PRD decision gate, placeholder parallelism, authorization, resume, downgrade, and blocked-design cases.

- [ ] **Step 5: Run packaging and CLI smoke checks**

  Run: `python3 -m pip install -e .` then `vibe --help` and `vibe scan --json` in `tests/fixtures/e2e-project`.

  Expected: the console script starts, prints valid JSON, and does not modify the fixture during `scan`.

- [ ] **Step 6: Run final diff and sensitive-path checks**

  Run: `git diff --check -- /Users/smy/Desktop/CFO/黑客松/开发辅助` and a scoped scan for credentials, absolute user paths, `knowledge/email/private/`, and files outside the current directory.

  Expected: no whitespace errors, no credentials, no forbidden paths, and no unrelated parent-workspace files staged.

## Verification Matrix

| Requirement | Verification | Planned task |
|---|---|---|
| Seven Agent adapters | Manifest and capability tests | N4 |
| Architecture Skill required, optional Skills external | Installer fixture and SHA tests | N1 |
| Existing AGENTS preserved | Initializer no-write tests | N1 |
| Project knowledge initialization | Idempotence tests | N1 |
| S0/S1 routing | Boundary score tests | N2 |
| Product decisions before monitor | PRD approval gate test | N2 |
| Parallel placeholders | DAG ready/contract tests | N2 |
| One-time non-deploy authorization | Digest and exclusion tests | N3 |
| Unique writer and same-worker rework | Fake runner monitor tests | N3 |
| Cross-platform developer/reviewer visible independent tasks | Generic task registry, provider contracts, exact continuation and per-App capability tests | N3/N4/N5 |
| Unknown state is not no-op | `blocked_unknown` test | N3 |
| Desktop session and permission downgrade | Adapter fixture tests | N4 |
| Resume after interruption | Snapshot recovery and E2E test | N3/N5 |
| End-to-end CLI flow | Temporary-project acceptance tests | N5 |

## Execution Gate

The implementation worker must not start before:

1. The user confirms this DAG and its task boundaries.
2. The monitor displays the exact authorization card for the selected plan.
3. The user explicitly authorizes the listed non-deploy actions.

Deploy, system-permission changes, and actions not listed in the authorization card remain outside this authorization.

### Revision 2 重新授权卡

旧授权状态：**invalidated**，原因是 developer/reviewer 从内部 subagent 改为 Codex App user-owned thread，属于产品设计与执行拓扑变化。

重新授权后允许：

- 按新版 DAG 同时最多保持 5 对（10 个）活跃 Codex App 左侧可见独立任务；完成并归档后释放并发名额，只启动 ready 节点；
- 使用已登记的精确 thread 继续开发、Review、返工和复审；
- 使用上述隔离 worktree/branch，并恢复 N3 的精确封存对象；
- 在当前项目白名单内编辑、测试、生成报告和本地 commit；
- 由监工等待可动作终态、核验证据、更新 DAG 状态，并持续轮转到 R5 完成。

明确不包含：push、创建 MR、merge、deploy、系统权限变更、扩大项目/文件/数据范围、替用户做新的产品取舍。任何一项出现时再次暂停。
