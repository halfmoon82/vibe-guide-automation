# Vibe Coding 辅助开发向导 V3.8 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变 V3 产品路由、可见任务和授权边界的前提下，实现 V3.8 的 Manifest、授权前预检、Brief 门、契约闭合、并行所有权、基线健康、Review 闭包、证据回放和结构化 Git 目标。

**Architecture:** 以新增的 `manifest.py`、`preflight.py`、`brief.py`、`path_ownership.py`、`baseline_health.py`、`review.py` 和 `evidence.py` 承担单一职责；`state.py`、`task_registry.py`、`monitor.py`、`authorization.py` 和 `cli.py` 只通过明确的数据结构接入。Run Manifest 是当前运行事实源，其他持久化文件只引用它或保存不可变历史证据；所有外部 provider 能力继续由结构化运行时证据决定。provider 创建 visible task 前只声明 `provider_managed_runtime`，创建后在首次业务写入前登记并校验实际 task/host/worktree/managed_root/branch/HEAD/clean/base_sha/lease/cursor。

**Tech Stack:** Python 3.9 标准库，现有 `vibe_guide` 模块，JSON/JSONL 原子持久化，`unittest`，临时项目与 fake runner；真实 provider、桌面任务可见性、push、MR、merge 和 Deploy 不由本地测试冒充验证。

## Global Constraints

- Preserve S0/S1 thresholds and existing `simple`/`light_plan`/`complex` routing.
- A preflight `mismatch` or `unknown` returns `preflight_blocked` and creates no worker, run, worktree or provider action.
- `run-manifest.json` is the only current run fact source; state/tasks/authorization files hold references or history only.
- Every developer has one provider-managed valid writer worktree after runtime binding; every reviewer is a different read-only task.
- Timeout, empty response, malformed response and provider self-report remain `unknown`.
- `execution_epoch` changes invalidate old authorization and preserve old task/worktree/cursor evidence.
- Provider-created detached HEAD is a pre-write candidate only; no business write is allowed until the node-unique branch and actual binding are registered and verified.
- A single `baseline-health.json` is generated per run and reused by all nodes.
- Finding bundles return to the original developer/reviewer; the second same-class rework escalates to contract/call-chain review.
- Git actions require explicit `merge_target_branch`; `merge_to_main=false` and `deploy=false` remain the defaults.
- No commit, push, Change Request, merge, Deploy, external install or system permission action is executed by this plan.

---

### Task 1: Run Manifest and execution epoch

**Files:**
- Create: `vibe_guide/manifest.py`
- Modify: `vibe_guide/models.py`, `vibe_guide/state.py`, `vibe_guide/task_registry.py`
- Test: `tests/test_v38_manifest.py`, `tests/test_v38_recovery.py`

**Interfaces:**
- `RunManifest.from_mapping(data: Mapping[str, Any]) -> RunManifest`
- `RunManifest.to_dict() -> Dict[str, Any]`
- `RunManifest.digest() -> str`
- `load_run_manifest(paths: ProjectPaths, run_id: str) -> RunManifest`
- `save_run_manifest(paths: ProjectPaths, manifest: RunManifest) -> None`
- `advance_execution_epoch(manifest: RunManifest, reason: str) -> RunManifest`

- [ ] **Step 1: Write failing tests for the current fact source.** Assert that a manifest round trip preserves `plan_revision`, `execution_epoch`, base SHA, target branch and evidence reference; assert that a revision/epoch change invalidates the old authorization digest and that an unbound provider task remains pre-write blocked.
- [ ] **Step 2: Run the focused tests.** Run `python3 -m unittest tests.test_v38_manifest tests.test_v38_recovery -v`. Expected: FAIL because no V3.8 manifest model or atomic loader exists.
- [ ] **Step 3: Implement the smallest manifest model and atomic persistence.** Validate identifiers, SHA format, target branch, epoch and schema version; write through the existing atomic state helper; retain previous manifest hash and event reference.
- [ ] **Step 4: Add recovery binding checks.** Make resume reject a mismatched epoch, unknown writer, foreign provenance or unverified provider-managed binding without deleting old registry rows or creating a successor. Preserve task/host/worktree/branch/cursor when a verified continuation is possible.
- [ ] **Step 5: Run focused and existing state tests.** Run `python3 -m unittest tests.test_v38_manifest tests.test_v38_recovery tests.test_state tests.test_task_registry -v`. Expected: all tests pass with existing state behavior unchanged.

### Task 2: Path ownership and Baseline Health Manifest

**Files:**
- Create: `vibe_guide/path_ownership.py`, `vibe_guide/baseline_health.py`
- Modify: `vibe_guide/dag.py`
- Test: `tests/test_v38_path_ownership.py`, `tests/test_v38_baseline_health.py`

**Interfaces:**
- `validate_path_ownership(nodes: Sequence[DAGNode]) -> PathOwnershipResult`
- `build_baseline_health(root: Path, base_sha: str, commands: Sequence[Sequence[str]]) -> BaselineHealthManifest`
- `save_baseline_health(paths: ProjectPaths, run_id: str, manifest: BaselineHealthManifest) -> None`
- `load_baseline_health(paths: ProjectPaths, run_id: str) -> BaselineHealthManifest`

- [ ] **Step 1: Write failing tests for overlapping writes and baseline reuse.** Assert that intersecting `owned_paths` is a conflict, `read_paths` intersection is allowed, and repeated load returns the same digest and no second file.
- [ ] **Step 2: Run the focused tests.** Run `python3 -m unittest tests.test_v38_path_ownership tests.test_v38_baseline_health -v`. Expected: FAIL because the ownership and baseline contracts are absent.
- [ ] **Step 3: Implement normalized project-relative path comparison.** Reject traversal, absolute paths and symlink escapes; report both nodes, paths and serializing owner. Preserve read-only overlap.
- [ ] **Step 4: Implement one-run baseline health generation.** Record collection/import counts, command exit codes, missing modules/entries, runtime, dependency and workspace evidence, then classify each defect as `in_scope`, `out_of_scope` or `must_restore`.
- [ ] **Step 5: Run focused DAG tests.** Run `python3 -m unittest tests.test_v38_path_ownership tests.test_v38_baseline_health tests.test_dag -v`. Expected: all tests pass and existing cycle detection remains unchanged.

### Task 3: Preflight and Contract Closure

**Files:**
- Create: `vibe_guide/preflight.py`
- Modify: `vibe_guide/contracts.py`, `vibe_guide/authorization.py`, `vibe_guide/cli.py`
- Test: `tests/test_v38_preflight.py`, `tests/test_v38_contract_closure.py`

**Interfaces:**
- `run_preflight(context: PreflightContext) -> PreflightReport`
- `check_contract_closure(issue: IssueContract, project_root: Path) -> ContractClosureResult`
- `preflight_status(report: PreflightReport) -> str`
- `assert_authorizable(report: PreflightReport) -> None`

- [ ] **Step 1: Write failing tests for every blocking check.** Cover base SHA mismatch, remote target mismatch, occupied binding, old active writer, owned path overlap, missing real entrypoint, structured capability `unknown`, missing baseline manifest and absent merge target. Assert no task, worktree, provider request or Git side effect; preflight checks strategy, while runtime binding is checked after provider creation and before first write.
- [ ] **Step 2: Run the focused tests.** Run `python3 -m unittest tests.test_v38_preflight tests.test_v38_contract_closure -v`. Expected: FAIL because the preflight report and closure evaluator are absent.
- [ ] **Step 3: Implement read-only checks with structured evidence.** Return one record per check with `passed`, `mismatch` or `unknown`, observed/expected values, `evidence_ref` and `recovery_action`; never fill unknown from README or self-report.
- [ ] **Step 4: Implement the authorizable gate.** Return `preflight_blocked` for any critical mismatch/unknown and `ready_to_authorize` only when every critical item is passed; make the CLI refuse worker or authorization creation before this gate.
- [ ] **Step 5: Run focused, authorization and CLI tests.** Run `python3 -m unittest tests.test_v38_preflight tests.test_v38_contract_closure tests.test_authorization tests.test_cli -v`. Expected: all tests pass and unauthorized monitor behavior remains fail-closed.

### Task 4: Implementation Brief write gate

**Files:**
- Create: `vibe_guide/brief.py`
- Modify: `vibe_guide/monitor.py`, `vibe_guide/state.py`
- Test: `tests/test_v38_brief_gate.py`, `tests/test_monitor.py`

**Interfaces:**
- `validate_implementation_brief(brief: ImplementationBrief, manifest: RunManifest, contract: IssueContract) -> BriefValidation`
- `require_brief_before_write(node: DAGNode, manifest: RunManifest, brief: ImplementationBrief) -> None`

- [ ] **Step 1: Write failing tests for missing fields and drift.** Cover absent invariant, missing negative case, call-chain path outside allowlist, base SHA mismatch, wrong epoch, unregistered provider worktree and incorrect runtime binding; assert business files are unchanged and status is `brief_pending` or binding-pending.
- [ ] **Step 2: Run the focused tests.** Run `python3 -m unittest tests.test_v38_brief_gate -v`. Expected: FAIL because no brief validator protects the first write.
- [ ] **Step 3: Implement structural validation.** Require one invariant per acceptance, real entrypoint/positive/negative/test command, normalized owned/read paths and exact manifest binding; return concrete missing fields.
- [ ] **Step 4: Enforce the monitor write gate.** Permit `implementing` only after brief validation passes; preserve the existing developer/reviewer role separation and do not add a user confirmation round.
- [ ] **Step 5: Run focused and monitor regression tests.** Run `python3 -m unittest tests.test_v38_brief_gate tests.test_monitor tests.test_end_to_end -v`. Expected: all tests pass with no duplicate writer.

### Task 5: Review Matrix and Finding Bundle

**Files:**
- Create: `vibe_guide/review.py`
- Modify: `vibe_guide/monitor.py`
- Test: `tests/test_v38_review_matrix.py`

**Interfaces:**
- `build_review_matrix(manifest: RunManifest, brief: ImplementationBrief, closure: ContractClosureResult) -> ReviewMatrix`
- `bundle_findings(findings: Sequence[ReviewFinding]) -> List[FindingBundle]`
- `accept_review(matrix: ReviewMatrix) -> ReviewResult`

- [ ] **Step 1: Write failing tests for matrix coverage and root-cause grouping.** Assert that all invariants have real entrypoint, positive/negative case and evidence command; two findings with one root cause become one bundle; provider timeout remains unknown.
- [ ] **Step 2: Run the focused tests.** Run `python3 -m unittest tests.test_v38_review_matrix -v`. Expected: FAIL because the fixed matrix and bundle model are absent.
- [ ] **Step 3: Implement the matrix builder.** Enumerate the required provider, provenance, symlink, Guidance, lifecycle, allowlist, side-effect and Red→Green checks from the brief and closure; keep each row linked to immutable evidence.
- [ ] **Step 4: Implement bundle normalization.** Group by explicit root-cause key, retain all affected invariants, set severity, minimal repair boundary and one verification command; reviewer output stays read-only.
- [ ] **Step 5: Run review and existing monitor tests.** Run `python3 -m unittest tests.test_v38_review_matrix tests.test_monitor tests.test_end_to_end -v`. Expected: existing acceptance/review behavior remains intact.

### Task 6: Evidence replay and repeated-rework escalation

**Files:**
- Create: `vibe_guide/evidence.py`
- Modify: `vibe_guide/monitor.py`, `vibe_guide/task_registry.py`
- Test: `tests/test_v38_evidence_replay.py`, `tests/test_v38_rework_escalation.py`

**Interfaces:**
- `write_generation_evidence(paths: ProjectPaths, evidence: GenerationEvidence) -> None`
- `replay_summary(paths: ProjectPaths, run_id: str, issue_id: str) -> IssueSummary`
- `classify_rework(history: Sequence[ReviewResult]) -> ReworkDecision`

- [ ] **Step 1: Write failing tests for immutable evidence and same-worker return.** Assert generation-0 remains unchanged when generation-1 is added, original task/cursor/worktree/branch are retained, and the second same-class gap returns `contract_or_call_chain_review_required`.
- [ ] **Step 2: Run the focused tests.** Run `python3 -m unittest tests.test_v38_evidence_replay tests.test_v38_rework_escalation -v`. Expected: FAIL because the layered evidence and escalation logic are absent.
- [ ] **Step 3: Implement atomic layered evidence writes.** Store contract, brief, baseline reference, review generations, Red/Green handoff and summary with hashes, exit codes, base SHA and timestamps; never overwrite historical files.
- [ ] **Step 4: Implement rework classification.** Return original developer/reviewer continuation for first findings; on the second same-class finding pause and require contract/call-chain review; contract changes invalidate authorization and require a new preflight.
- [ ] **Step 5: Run recovery and registry regression tests.** Run `python3 -m unittest tests.test_v38_evidence_replay tests.test_v38_rework_escalation tests.test_state tests.test_task_registry tests.test_monitor -v`. Expected: all tests pass with no successor on uncertain identity.

### Task 7: Structured Git action targets

**Files:**
- Modify: `vibe_guide/authorization.py`, `vibe_guide/change_requests.py`
- Test: `tests/test_v38_git_targets.py`, `tests/test_authorization.py`, `tests/test_change_requests.py`

**Interfaces:**
- `validate_git_action_target(action: Mapping[str, Any]) -> None`
- `canonical_git_action(action: Mapping[str, Any]) -> Dict[str, Any]`

- [ ] **Step 1: Write failing tests for ambiguous merge actions.** Reject `{merge: true}`, missing `merge_target_branch`, implicit `main` and deploy-like fields; accept explicit `codex/v38-7` with `merge_to_main=false` and `deploy=false`.
- [ ] **Step 2: Run the focused tests.** Run `python3 -m unittest tests.test_v38_git_targets -v`. Expected: FAIL because the current action contract allows ambiguous targets.
- [ ] **Step 3: Implement canonical action normalization.** Require `commit`, `push`, `create_change_request`, `merge_to_target_branch`, `merge_target_branch`, `merge_to_main` and `deploy`; reject unknown target or contradictory main fields.
- [ ] **Step 4: Keep external execution out of this phase.** Wire only validation and serialization; do not call Git, provider, Change Request or Deploy APIs.
- [ ] **Step 5: Run existing authorization and Change Request tests.** Run `python3 -m unittest tests.test_v38_git_targets tests.test_authorization tests.test_change_requests -v`. Expected: all tests pass and existing explicit action behavior remains compatible.

### Task 8: V3.8 integration acceptance and documentation

**Files:**
- Modify: `vibe_guide/cli.py`, `vibe_guide/monitor.py`, `vibe_guide/dag.py`
- Test: `tests/test_v38_acceptance.py`, `tests/test_end_to_end.py`
- Documentation check: `README.md`

**Interfaces:**
- `validate_v38_artifact_set(prd_ref: str, spec_ref: str, issues_ref: str, dag_ref: str) -> ArtifactSetValidation`
- `run_v38_acceptance_fixture() -> AcceptanceReport`

- [ ] **Step 1: Write a failing end-to-end fixture.** Build a temporary project with one blocked preflight and one passing foundation path; assert the blocked path has zero task/worktree/provider/Git side effects and the passing path still cannot start without user authorization.
- [ ] **Step 2: Run the focused fixture.** Run `python3 -m unittest tests.test_v38_acceptance -v`. Expected: FAIL because V3.8 artifact validation and integrated gates are absent.
- [ ] **Step 3: Implement artifact-set validation.** Validate PRD@2, Spec@2, Issues@2 and DAG@3 references, complete FR/AC mapping, acyclic dependencies, provider-managed binding contract, exact allowlists and deploy/main exclusion.
- [ ] **Step 4: Add the acceptance report without changing README claims.** Report local test evidence separately from unverified real provider visibility, task lifecycle, push, MR, merge and Deploy; update README only if the existing command description becomes factually inconsistent.
- [ ] **Step 5: Run the full project verification.** Run `python3 -m unittest discover -s tests -v`, `python3 -m vibe_guide --help`, `python3 -m compileall -q vibe_guide tests` and `git diff --check`. Expected: all tests pass, help exits 0, compileall is silent, and the scoped diff has no whitespace errors.

## Plan self-review

- **Spec coverage:** V38-1 through V38-8 cover FR-801 through FR-811 and AC-801 through AC-808; the DAG repeats the same mapping, requires provider-managed runtime binding before first write and forbids old authorization reuse.
- **Placeholder scan:** no step depends on an unspecified file, provider result, future decision or generic “handle edge cases” instruction.
- **Type consistency:** the manifest, preflight, brief, review, evidence and Git helper names are used consistently across tasks and the Spec.
- **Boundary check:** this plan does not include worker creation, external provider action, commit, push, Change Request, merge, Deploy, install or system permission changes.

## Execution handoff

本计划目前为 `version 3`，因 provider-managed worktree 合同修订而使旧授权失效。用户已确认原 Spec/Issue/DAG 和本计划，但当前版本必须在预检后重新展示并重新授权；该确认仍不自动授予 Deploy、系统权限或未列明外部动作。
