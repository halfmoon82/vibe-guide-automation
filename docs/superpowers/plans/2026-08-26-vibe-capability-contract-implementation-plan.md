# Vibe Guide Capability Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 V2 分支中实现一个由初始化生成、由监工和 worker 共同读取的 capability contract，阻止 LLM 自报能力直接造成错误中断。

**Architecture:** 新增一个纯标准库能力合同模块，负责有限状态、摘要、原子持久化、有效期和只读查询。`init` 生成合同和 AGENTS.md 补丁建议；`workflow_gate` 在 V2 入口校验合同；Monitor/provider action 仅传递合同摘要和事实上下文，不把自然语言自报当成状态证据。缺合同或未知探测保持 unknown，不伪造 unavailable。

**Tech Stack:** Python 3.9+；`dataclasses`、`json`、`hashlib`、`pathlib`、`tempfile`、`os`、`datetime`、`unittest`；不增加运行时依赖。

## Global Constraints

- 现有 `AGENTS.md` 不直接覆盖、追加或删除；初始化只生成 `.vibe/proposals/agentsmd/proposal.md`。
- `.vibe/session-contract.json` 不保存 token、cookie、原始 provider 文本或无关绝对用户路径。
- `unknown_timeout`、探测失败、空响应和格式异常不得转换为 unavailable 或成功。
- 监工和 worker 使用同一合同摘要；task scope 不得冒充平台全局能力。
- 一个节点一个有效 writer；本任务不创建第二个 monitor/worker，也不执行 push、MR、merge 或 Deploy。
- 所有新增 JSON 写入原子替换、拒绝 symlink、可被重复 init 安全读取。
- 既有基线失败（缺失 `model_router`、两个 reauthorization 测试失败）单独记录，不通过修改无关代码掩盖。

---

### Task 1: Capability contract model and persistence

**Files:**
- Create: `vibe_guide/capability_contract.py`
- Create: `tests/test_capability_contract.py`
- Modify: `vibe_guide/contracts.py`

**Interfaces:**
- `CAPABILITY_STATUSES: frozenset[str]`
- `CapabilityFact(name: str, status: str, scope: str, route: str, evidence_ref: str, checked_at: str, expires_at: str)`
- `CapabilityContract(schema_version: int, project_digest: str, provider: str, host_id: str, scope: str, capabilities: dict[str, CapabilityFact], contract_digest: str, expires_at: str)`
- `build_contract(project_root: Path, provider: str = "unknown", host_id: str = "unknown", facts: dict = None, now: datetime = None) -> CapabilityContract`
- `save_contract(paths: ProjectPaths, contract: CapabilityContract) -> Path`
- `load_contract(paths: ProjectPaths, now: datetime = None) -> CapabilityContract`
- `capability_status(contract: CapabilityContract, name: str, now: datetime = None) -> str`
- `contract_path(paths: ProjectPaths) -> Path`

- [ ] **Step 1: Write failing tests** for valid status round-trip, digest stability, missing fact returning `unknown`, stale fact returning `stale`, malformed/symlink rejection, and atomic idempotent save.
- [ ] **Step 2: Run the focused tests** with `python3 -m unittest tests.test_capability_contract -v` and verify failure because the module is absent.
- [ ] **Step 3: Implement the minimal dataclasses and validation** with Python 3.9-compatible typing; reject unknown statuses, empty evidence references, invalid timestamps and path escapes.
- [ ] **Step 4: Implement canonical digest and atomic save/load**; the digest must exclude itself and be recomputed on load. Refuse symlinked contract paths.
- [ ] **Step 5: Implement `capability_status`** so missing, malformed, expired and unknown observations remain `unknown`/`stale`, never `unavailable`.
- [ ] **Step 6: Re-run focused tests** and confirm the red-green cycle, then commit only Task 1 files.

### Task 2: Init-time contract and AGENTS proposal

**Files:**
- Modify: `vibe_guide/initializer.py`, `vibe_guide/scanner.py`, `vibe_guide/diagnostics.py`
- Modify: `tests/test_initializer.py`, `tests/test_v2_diagnostics.py`

**Interfaces:**
- `build_agentsmd_patch` must include the capability truth rules when the existing project does not contain them.
- `init_project(paths, confirm)` returns the new contract path in `InitResult.paths` on first creation and remains unchanged on repeat initialization.
- Init-time facts are limited to project/runtime observations and are scoped `init`; task/provider capabilities remain `unknown` until a bound session observation exists.

- [ ] **Step 1: Write failing tests** for first init creating `.vibe/session-contract.json`, repeat init preserving its bytes, and missing AGENTS capability rules producing a proposal without modifying `AGENTS.md`.
- [ ] **Step 2: Run `python3 -m unittest tests.test_initializer tests.test_v2_diagnostics -v`** and verify the new assertions fail.
- [ ] **Step 3: Add the exact capability-rule block to the proposal builder** without changing existing proposal-only semantics or unrelated AGENTS text.
- [ ] **Step 4: Build the init contract** from the existing `scan_project` observations, using only bounded safe facts and `unknown` for provider/task routes.
- [ ] **Step 5: Extend initialization path validation** to include the contract file and preserve symlink/no-write guarantees.
- [ ] **Step 6: Re-run initializer and diagnostics tests**, verify idempotence and no AGENTS mutation, then commit only Task 2 files.

### Task 3: Shared session gate and provider context

**Files:**
- Modify: `vibe_guide/workflow_gate.py`, `vibe_guide/cli.py`, `vibe_guide/adapters/task_provider.py`, `vibe_guide/runners/provider_action.py`
- Modify: `tests/test_cli.py`, `tests/test_adapters.py`, `tests/test_end_to_end.py`

**Interfaces:**
- `require_capability_contract(paths: ProjectPaths) -> CapabilityContract` raises a bounded `PermissionError` only for invalid/missing V2 contract and reports the reason as unknown, never as provider unsupported.
- `session_contract_prompt(contract: CapabilityContract) -> str` returns a bounded redacted prompt fragment containing only digest, scope and capability statuses.
- Existing provider action request identity and child binding remain unchanged; prompts add the contract digest/status context without persisting raw provider output.

- [ ] **Step 1: Write failing tests** for V2 monitor/resume requiring a valid contract, provider prompts carrying the same digest for supervisor/worker, missing contract becoming `blocked_unknown`, and natural-language capability claims not being accepted as evidence.
- [ ] **Step 2: Run `python3 -m unittest tests.test_cli tests.test_adapters tests.test_end_to_end -v`** and verify the new contract assertions fail.
- [ ] **Step 3: Add contract loading to `require_entry`** after the existing state/session gate; keep non-V2 legacy paths unchanged.
- [ ] **Step 4: Add the bounded contract fragment to provider create/resume prompts** and bind it to the current run/task scope; do not change worker identity or writer selection.
- [ ] **Step 5: Make CLI output distinguish `capability_contract_missing`, `capability_contract_invalid`, and `blocked_unknown` without claiming unavailable.
- [ ] **Step 6: Re-run focused adapter/CLI/end-to-end tests** and verify existing provider action digests remain stable except for the explicit prompt context.

### Task 4: Documentation, full verification, and branch handoff

**Files:**
- Modify: `README.md`
- Create: `docs/superpowers/plans/2026-08-26-vibe-capability-contract-verification.md`
- Test: all existing tests plus capability-contract tests

- [ ] **Step 1: Document init, contract status meanings, monitor/worker shared context, and the boundary that runtime failures may still block.**
- [ ] **Step 2: Run the full verification commands:**
  - `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v`
  - `python3 -m vibe_guide --help`
  - `git diff --check`
- [ ] **Step 3: Record exact pass/fail counts and pre-existing baseline failures** in the verification report; do not call the branch complete if new failures remain.
- [ ] **Step 4: Inspect `git diff --stat`, `git status --short`, and staged paths; ensure no credentials, `.vibe` runtime state, or unrelated files are staged.**
- [ ] **Step 5: Commit only the capability-contract implementation, tests, documentation and verification report.**
- [ ] **Step 6: Leave merge/push/monitor acceptance to the user-authorized later gate; report branch name, commits, tests, known baseline failures and unverified external provider boundaries.**
