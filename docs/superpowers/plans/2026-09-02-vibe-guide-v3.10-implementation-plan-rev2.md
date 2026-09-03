# Vibe Guide V3.10 门禁分层与自愈 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver `v3.10.0` as a cross-platform install/upgrade package whose supervisor absorbs engineering drift, while authorized post-review Issues can automatically create and merge PR/MR without Deploy.

**Architecture:** Keep the existing CLI, planner, authorization, monitor, adapter, migration and packaging modules. Add a narrow self-healing policy layer to classify engineering observations as `auto_corrected`/`degraded`, isolate only the affected node or external action, and preserve the frozen target contract. Extend the authorization envelope for explicitly listed `create_pr`, `create_mr`, `merge_local` and `merge_remote`; keep Deploy, credentials, system permissions and destructive operations outside the normal card.

**Tech Stack:** Python 3.9–3.13, standard library, setuptools/wheel/build, `unittest`, existing provider adapters and local fake runners.

## Global Constraints

- Product goal and acceptance scope come from `docs/superpowers/specs/2026-09-02-vibe-guide-v3.10-prd-design-rev2.md`.
- Engineering binding, provider gaps and target drift must not block unrelated local nodes or the whole DAG.
- Authorization-time target contract contains provider, repository/project, target branch, Issue/PR/MR type, source branch, file scope and merge method.
- Supervisor self-healing may not change product target, authorized repository/branch, file scope or unique writer.
- `create_pr`, `create_mr`, `merge_local` and `merge_remote` are allowed only when listed in the user-confirmed card.
- Deploy, credentials, system permissions, destructive writes and target changes remain separately authorized and excluded.
- Use isolated worktrees for writers; reviewer is read-only and distinct from developer.
- Unknown provider observations are retained as evidence; they are not converted into success or global unavailability.
- Preserve V3.9 Rev4 and `E2E_MAILBOX` boundaries; do not modify their files.
- Do not commit, push, create remote PR/MR, merge or deploy until a later explicit authorization card authorizes those actions.

## File Map

- `vibe_guide/authorization.py`: action-scope validation and target-bound external-action authorization.
- `vibe_guide/monitor.py`: engineering observation classification, self-healing, degradation and node-local isolation.
- `vibe_guide/models.py`: target contract, self-healing evidence and user-visible status fields.
- `vibe_guide/planner.py`: authorization-time target collection and persistence.
- `vibe_guide/installation.py`, `vibe_guide/migration.py`, `vibe_guide/initializer.py`: install/upgrade and backup-first migration.
- `vibe_guide/capability_contract.py`, `vibe_guide/capability_wizard.py`: layered/bundled capability catalog and explicit sensitive authorization.
- `vibe_guide/adapters/registry.py`, `vibe_guide/adapters/task_provider.py`: provider-neutral Agent entry and degraded fallback metadata.
- `vibe_guide/cli.py`: interactive/JSON rendering and shared state-machine entry points.
- `tests/test_v310_*.py`: red/green contract, self-healing, target authorization, package and upgrade evidence.

## DAG Execution Order

`V310-INSTALL-CONTRACT` is the only foundation. After it, `V310-MIGRATION`, `V310-CAPABILITY`, `V310-ROUTING` and `V310-SELF-HEAL` run in parallel. `V310-CLI` and `V310-ADAPTERS` then integrate in parallel. `V310-MERGE` follows their contracts but remains independent of package work. `V310-PACKAGE` is the final release-evidence node.

### Task 1: Install and upgrade state contract

**Files:**
- Modify: `vibe_guide/installation.py`, `vibe_guide/models.py`
- Test: `tests/test_installation.py`

**Interfaces:** `InstallStateMachine.run(mode, target) -> InstallResult`; `InstallResult.to_dict() -> dict` with stable phase/status/error/evidence fields.

- [ ] Step 1: Add failing tests for fixed phases, layered/bundled modes, atomic JSON output and recoverable failure.
- [ ] Step 2: Run `python3 -m unittest tests.test_installation -v` and confirm the new assertions fail.
- [ ] Step 3: Implement the smallest shared state machine and JSON-safe result model; do not probe or require a visible provider.
- [ ] Step 4: Run the focused test and `python3 -m vibe_guide --help`; expect PASS.
- [ ] Step 5: Record the contract evidence in the node handoff.

### Task 2: Backup-first V2 to V3.10 migration

**Files:**
- Modify: `vibe_guide/migration.py`, `vibe_guide/initializer.py`
- Test: `tests/test_migration.py`

**Interfaces:** `migrate_v2_to_v310(source, destination) -> MigrationResult`; `MigrationResult.to_dict() -> dict`.

- [ ] Step 1: Add failing tests for v2.0.0 fixture migration, unknown-field preservation, excluded `E2E_MAILBOX` path, idempotence, backup restore and invalid input.
- [ ] Step 2: Run `python3 -m unittest tests.test_migration -v` and confirm failure.
- [ ] Step 3: Implement backup manifest, copy-preserving migration and atomic result persistence; never delete the source.
- [ ] Step 4: Run the focused test twice against the same fixture and verify identical output hashes.
- [ ] Step 5: Record source, backup and destination evidence.

### Task 3: Layered and bundled capability authorization

**Files:**
- Modify: `vibe_guide/capability_contract.py`, `vibe_guide/capability_wizard.py`
- Test: `tests/test_capability_wizard.py`

**Interfaces:** `build_capability_catalog(scan_report) -> list[CapabilityItem]`; `authorize_capabilities(mode, selections) -> CapabilityAuthorization`.

- [ ] Step 1: Add failing tests for three layers, one-time bundled local approval, optional selection, and sensitive capability separation.
- [ ] Step 2: Run `python3 -m unittest tests.test_capability_wizard -v` and confirm failure.
- [ ] Step 3: Implement catalog and authorization persistence using current scan evidence only.
- [ ] Step 4: Verify bundled mode never grants credentials, system permissions, platform login or Deploy.
- [ ] Step 5: Record unknown/permission-denied/timeout states without converting them to unavailable.

### Task 4: Persisted task grading and authorization-time target contract

**Files:**
- Modify: `vibe_guide/planner.py`, `vibe_guide/models.py`
- Test: `tests/test_v310_routing_binding.py`

**Interfaces:** `route_task(context) -> RouteResult`; `collect_target_contract(environment, user_selection) -> TargetContract`; `TargetContract.to_dict() -> dict`.

- [ ] Step 1: Add failing S0/S1 boundary tests and tests requiring one target prompt before authorization when repository/branch cannot be uniquely inferred.
- [ ] Step 2: Run the focused routing tests and confirm failure.
- [ ] Step 3: Persist the complexity band and frozen target contract in the plan/authorization projection.
- [ ] Step 4: Verify later monitor entry points reuse the stored contract and never re-ask normal engineering fields.
- [ ] Step 5: Record target fields and digest in plan evidence.

### Task 5: Supervisor self-healing and node-local degradation

**Files:**
- Modify: `vibe_guide/monitor.py`, `vibe_guide/models.py`, `vibe_guide/state.py`
- Test: `tests/test_v310_self_healing.py`, `tests/test_v310_routing_binding.py`

**Interfaces:** `classify_observation(observation) -> ObservationDisposition`; `self_heal_binding(snapshot, node_id, observation) -> HealingResult`; `isolate_affected_action(snapshot, node_id, reason) -> None`.

- [ ] Step 1: Add failing tests for missing visible locate evidence, worktree/branch drift, stale cursor, provider timeout, successful repair, degraded fallback and unrelated-node continuation.
- [ ] Step 2: Run the focused tests and confirm failure.
- [ ] Step 3: Implement read-current-facts → reconcile frozen contract → repair/reuse original task → degrade fallback → append evidence event.
- [ ] Step 4: Verify self-healing cannot widen allowlists, change target repository/branch, create a second writer or grant new authorization.
- [ ] Step 5: Run state replay and snapshot recovery tests; expect unrelated ready nodes to continue.

### Task 6: CLI and Agent session integration

**Files:**
- Modify: `vibe_guide/cli.py`, `vibe_guide/adapters/registry.py`, `vibe_guide/adapters/task_provider.py`
- Test: `tests/test_v310_cli.py`, `tests/test_v310_adapters.py`

**Interfaces:** `run_install_or_upgrade(request, json_output) -> dict`; `TaskProviderAdapter.describe_upgrade_entry() -> dict`; `TaskProviderAdapter.invoke_upgrade(request) -> dict`.

- [ ] Step 1: Add failing interactive/JSON parity tests and adapter delegation tests.
- [ ] Step 2: Run both focused test files and confirm failure.
- [ ] Step 3: Route both entry points through the shared state machine; render `准备中`/`自动修复中`/`已启动`/`需要你决定` without exposing engineering fields as questions.
- [ ] Step 4: Verify provider gaps affect only provider-backed actions and are shown as structured evidence.
- [ ] Step 5: Run CLI help, scan, doctor and JSON fixtures.

### Task 7: Authorized PR/MR creation and restricted merge

**Files:**
- Modify: `vibe_guide/authorization.py`, `vibe_guide/change_requests.py`, `vibe_guide/monitor.py`
- Test: `tests/test_v310_merge_authorization.py`

**Interfaces:** `can_create_change_request(evidence, authorization) -> bool`; `can_auto_merge(evidence, authorization) -> bool`; `execute_change_request(action, target_contract) -> ChangeRequestEvidence`.

- [ ] Step 1: Add failing tests for authorized `create_pr`, `create_mr`, `merge_local`, `merge_remote`, final-review/P0–P2 checks, target drift self-healing and Deploy exclusion.
- [ ] Step 2: Run `python3 -m unittest tests.test_v310_merge_authorization -v` and confirm failure.
- [ ] Step 3: Extend action-scope validation to permit only explicitly card-listed PR/MR/merge actions; keep push, Deploy, credentials and system permissions excluded.
- [ ] Step 4: Implement target-bound evidence recording and monitor self-heal before external execution; isolate only the external action when ambiguity remains.
- [ ] Step 5: Verify local merge never claims remote success and every external result is separately recorded.

### Task 8: Cross-platform package and release evidence

**Files:**
- Modify: `pyproject.toml`, `setup.py`, `vibe_guide/__init__.py`, `README.md`
- Test: `tests/test_v310_packaging.py`

**Interfaces:** package metadata reports `3.10.0`; clean-install harness returns wheel/sdist/source and upgrade evidence.

- [ ] Step 1: Add failing tests for metadata/tag consistency, wheel/sdist/source installation, v2.0.0→v3.10.0 upgrade and rollback evidence.
- [ ] Step 2: Run the focused packaging tests and confirm failure.
- [ ] Step 3: Implement only the required metadata, entry points and documentation updates.
- [ ] Step 4: Build wheel and sdist, then test each in a clean environment under Python 3.9–3.13 where available.
- [ ] Step 5: Run `python3 -m unittest discover -s tests -v`, `python3 -m compileall vibe_guide`, and `git diff --check`.
- [ ] Step 6: Record release evidence; do not publish or deploy without separate authorization.

## Plan Self-Review

- Spec coverage: installation/upgrade (Tasks 1–2, 8), capability authorization (Task 3), grading/target contract (Task 4), self-healing and node-local degradation (Task 5), Agent/CLI parity (Task 6), PR/MR and merge automation (Task 7), package evidence (Task 8).
- Parallelism: Tasks 2–5 run after Task 1 in parallel; Tasks 6 and 7 integrate independently; Task 8 is the only release收口 node.
- Placeholder scan: every step contains concrete files, interfaces, commands and expected outcomes.
- Authorization boundary: plan generation and audit do not authorize workers, commit, push, PR/MR, merge or Deploy.
