# V4.2 Closed-Loop Supervision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a single, non-bypassable V4.2 Monitor lifecycle in which one authorization covers Provider engineering recovery and the supervisor autonomously advances the DAG until delivery, review, or a genuine external/design boundary.

**Architecture:** Keep Monitor as the only complex-DAG state-transition authority. Add a durable retry/repair record and supervisor recovery loop that reuses the original task identity, writer lease, worktree, branch, generation, and cursor. Make V4.2 project state and package metadata consistent, while retaining legacy states as read-only migration inputs.

**Tech Stack:** Python 3.9 standard library, existing `vibe_guide` Monitor/Supervisor/state/adapter modules, `unittest`, wheel/sdist packaging.

## Global Constraints

- Complex DAG start, resume, retry, repair, rework, review, and acceptance must pass through `Monitor`.
- One user authorization covers listed Provider create/poll/wait/resume, engineering retry, same-identity repair, snapshot repair, review/rework, and current-DAG successors.
- Provider engineering failures never terminate the DAG or require renewed user authorization.
- External login, credentials, system permissions, remote approval, deploy, product changes, and new scope remain independent boundaries.
- No second writer, implicit successor, direct runner path, SDD-only downgrade, or historical evidence promotion.
- V4.2 initialized state is exactly `workflow_version=4`, `execution_mode=sdd_first`, `session_gate=s0_required`, `capability_contract_required=true`.
- Preserve unrelated working-tree changes; do not commit, push, merge, deploy, or modify upper CFO files.

---

### Task 1: Lock the V4.2 contract with Red tests

**Files:**
- Create: `tests/test_v42_closed_loop_contract.py`
- Modify: `tests/test_v42_engine_attestation.py`
- Modify: `tests/test_v41_sdd_downgrade_rejection.py`

**Interfaces:**
- Consumes: `vibe_guide.initializer.init_project`, `vibe_guide.workflow_gate.require_entry`, `vibe_guide.monitor.Monitor`.
- Produces: failing tests for exact V4.2 state, single complex entry, no direct runner/SDD bypass, and engineering-vs-external Provider classification.

- [ ] **Step 1: Write failing tests**

```python
def test_init_materializes_v42_sdd_first_state():
    result = init_project(ProjectPaths(root), True)
    state = json.loads((root / ".vibe/state.json").read_text())
    assert state == {
        "workflow_version": 4,
        "execution_mode": "sdd_first",
        "session_gate": "s0_required",
        "capability_contract_required": True,
    }

def test_complex_entry_rejects_v2_state_even_with_valid_contract():
    with pytest.raises(PermissionError, match="v42_state_required"):
        require_entry(paths, "entry", "monitor")

def test_provider_timeout_is_retryable_without_new_authorization():
    snapshot = monitor.tick(run_id, timeout_runner)
    assert snapshot.nodes[node_id]["status"] == "retry_pending"
    assert snapshot.nodes[node_id]["active_task"]["task_id"] == original_task_id

def test_external_permission_is_boundary_not_engineering_failure():
    snapshot = monitor.tick(run_id, permission_runner)
    assert snapshot.nodes[node_id]["status"] == "blocked_unknown"
    assert snapshot.nodes[node_id]["retryable_action"] is None
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3 -m unittest tests.test_v42_closed_loop_contract -v`

Expected: failures showing current initialization writes V2 state and the new lifecycle contract is absent.

- [ ] **Step 3: Keep the Red tests minimal**

Use real temporary `ProjectPaths`, a deterministic fake runner that emits timeout/permission observations, and assert persisted state rather than mock call counts.

- [ ] **Step 4: Do not implement production code in this task**

The task is complete only when the tests fail for the intended missing behavior.

---

### Task 2: Make initialization, upgrade, and version metadata genuinely V4.2

**Files:**
- Modify: `vibe_guide/initializer.py`
- Modify: `vibe_guide/workflow_gate.py`
- Modify: `vibe_guide/migration.py`
- Modify: `vibe_guide/__init__.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_initializer.py`
- Modify: `tests/test_migration.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: existing V2/V3 migration readers and capability-contract persistence.
- Produces: `require_v42_sdd_first(paths) -> dict` and V4.2 state written atomically by init/upgrade; package and module version both `4.2.0`.

- [ ] **Step 1: Run Task 1 Red tests**

Run: `python3 -m unittest tests.test_v42_closed_loop_contract -v`

Expected: the state and version assertions remain RED.

- [ ] **Step 2: Implement strict V4.2 state validation**

Add a read-only validator that requires all four exact fields and rejects missing, conflicting, or unknown execution modes with `PermissionError("v42_state_required")`. Make complex `require_entry` and Monitor entry call it before capability or Provider work.

- [ ] **Step 3: Update init and upgrade atomically**

Write the four V4.2 fields for new projects. For V2/V3 projects, preserve original state and history in migration evidence, write a verified backup, and only publish the V4.2 state after migration validation succeeds. Ambiguity or partial migration returns structured `blocked_unknown` without destructive replacement.

- [ ] **Step 4: Align metadata**

Set `vibe_guide.__version__` to `4.2.0` and ensure wheel/sdist metadata reads the same value. Keep legacy compatibility tests explicit about historical fixtures rather than current package version.

- [ ] **Step 5: Run focused tests**

Run: `python3 -m unittest tests.test_v42_closed_loop_contract tests.test_initializer tests.test_migration tests.test_cli -v`

Expected: PASS for V4.2 state and metadata behavior; unrelated historical packaging assertions remain separately classified if they still target older versions.

---

### Task 3: Remove complex execution bypasses and centralize transitions

**Files:**
- Modify: `vibe_guide/cli.py`
- Modify: `vibe_guide/monitor.py`
- Modify: `vibe_guide/runners/local.py`
- Modify: `vibe_guide/runners/provider_action.py`
- Modify: `vibe_guide/adapters/task_provider.py`
- Create: `tests/test_v42_no_bypass.py`

**Interfaces:**
- Consumes: `require_v42_sdd_first`, `Monitor.start`, `Monitor.resume`, `Monitor.tick`.
- Produces: one guarded complex execution dispatcher; direct complex runner/SDD/adapter calls fail before Provider I/O with `complex_monitor_required`.

- [ ] **Step 1: Write failing bypass tests**

```python
def test_cli_complex_monitor_is_the_only_dispatch_path():
    result = run_cli(["monitor", "--plan", plan_id, "--authorize", token, "--json"], root)
    assert result.payload["dispatcher"] == "monitor"

def test_direct_complex_runner_is_rejected_before_provider_call():
    with self.assertRaisesRegex(PermissionError, "complex_monitor_required"):
        local_runner.start(complex_request)

def test_sdd_only_override_cannot_start_complex_dag():
    with self.assertRaisesRegex(PermissionError, "complex_monitor_required"):
        adapter.start(sdd_only_request)
```

- [ ] **Step 2: Run tests to confirm RED**

Run: `python3 -m unittest tests.test_v42_no_bypass -v`

- [ ] **Step 3: Add the centralized dispatcher guard**

Route complex CLI operations to Monitor only. Add a shared guard used by local/provider runners and adapters that checks the V4.2 state, execution engine attestation, executable authorization, and dispatcher origin before any Provider request or writer lease acquisition.

- [ ] **Step 4: Preserve explicit simple/light behavior**

Do not add the complex guard to simple/light routes or read-only `status`, `scan`, and diagnostics paths.

- [ ] **Step 5: Run focused tests**

Run: `python3 -m unittest tests.test_v42_no_bypass tests.test_v41_sdd_downgrade_rejection tests.test_monitor tests.test_adapters -v`

---

### Task 4: Implement Provider engineering self-healing on the same identity

**Files:**
- Modify: `vibe_guide/monitor.py`
- Modify: `vibe_guide/supervisor.py`
- Modify: `vibe_guide/state.py`
- Modify: `vibe_guide/adapters/task_provider.py`
- Create: `tests/test_v42_provider_self_healing.py`

**Interfaces:**
- Consumes: `RunSnapshot`, `TaskBinding`, Provider action store observations, supervisor lease.
- Produces: `classify_provider_failure(observation) -> {"kind": "engineering"|"external"}`, durable `retryable_action` records, and heartbeat-driven same-task recovery.

- [ ] **Step 1: Write failing tests**

```python
def test_timeout_retries_same_task_and_cursor():
    first = supervisor.run_once()
    second = supervisor.run_once()
    assert second.nodes[node_id]["status"] == "retry_pending"
    assert second.nodes[node_id]["active_task"]["task_id"] == task_id
    assert second.nodes[node_id]["active_task"]["cursor"] == cursor

def test_pending_client_thread_never_becomes_formal_thread():
    snapshot = supervisor.run_once()
    assert snapshot.nodes[node_id]["active_task"]["thread_id"] is None
    assert snapshot.nodes[node_id]["retryable_action"]["setup_identity"] == setup_identity

def test_worker_exit_is_repaired_without_successor():
    snapshot = supervisor.watch(interval=0, max_cycles=3)
    assert snapshot.nodes[node_id]["developer_identity"] == original_identity
    assert snapshot.nodes[node_id]["successor_created"] is False

def test_external_auth_boundary_does_not_consume_engineering_retry():
    snapshot = supervisor.run_once()
    assert snapshot.nodes[node_id]["status"] == "blocked_unknown"
    assert snapshot.nodes[node_id]["retryable_action"] is None
```

- [ ] **Step 2: Run tests to confirm RED**

Run: `python3 -m unittest tests.test_v42_provider_self_healing -v`

- [ ] **Step 3: Add failure classification**

Classify timeout, empty response, 429, disconnect, pending setup, worker exit, snapshot interruption, and repairable binding drift as engineering. Classify explicit login/credential/system-permission/remote-approval requirements as external. Preserve unknown evidence as `blocked_unknown` without inventing a category.

- [ ] **Step 4: Persist and replay retry records**

Store `attempt`, `reason_class`, `next_retry_at`, `same_task_required`, `binding_digest`, and `last_observation_ref` in the node snapshot and event log. Replay unapplied events before polling or dispatching.

- [ ] **Step 5: Enforce same-identity recovery**

On every retry, revalidate plan/node/task/generation/worktree/branch/lease/cursor and contract digest. Repair only the original binding; if identity cannot be proven, quarantine and remain `blocked_unknown` without successor creation.

- [ ] **Step 6: Remove fixed-failure termination**

Supervisor heartbeat continues `retry_pending` and `repairing` indefinitely with bounded backoff. Only explicit stop, external boundary, design change, or scope/authorization invalidation may end the run.

- [ ] **Step 7: Run focused tests**

Run: `python3 -m unittest tests.test_v42_provider_self_healing tests.test_pending_setup_recovery tests.test_supervisor_lifecycle tests.test_v39_supervisor_liveness tests.test_monitor -v`

---

### Task 5: Make delivery, review, and recovery evidence close the loop

**Files:**
- Modify: `vibe_guide/monitor.py`
- Modify: `vibe_guide/state.py`
- Modify: `vibe_guide/review.py`
- Modify: `vibe_guide/cli.py`
- Create: `tests/test_v42_closed_loop_acceptance.py`

**Interfaces:**
- Consumes: retry/repair records, current authorization epoch, reviewer binding and evidence files.
- Produces: strict downstream unlock and read-only status/replay fields for unresolved nodes and recovery history.

- [ ] **Step 1: Write failing acceptance tests**

```python
def test_missing_marker_does_not_unlock_successor():
    snapshot = monitor.tick(run_id, delivered_without_marker_runner)
    assert snapshot.nodes[successor]["status"] == "planned"

def test_reviewer_p1_reworks_original_developer():
    snapshot = monitor.tick(run_id, reviewer_finding_runner)
    assert snapshot.nodes[node_id]["active_task"]["task_id"] == original_task_id
    assert snapshot.nodes[node_id]["status"] == "rework"

def test_parent_exit_recovery_preserves_lease_and_progress():
    recovered = supervisor.recover_or_start()
    assert recovered["active_supervisors"] == 1
    assert recovered["run_id"] == run_id

def test_status_never_publishes_provider_self_report_as_completion():
    payload = run_cli(["status", "--plan", plan_id, "--run-id", run_id, "--json"], root).payload
    assert payload["unresolved_nodes"]
```

- [ ] **Step 2: Run tests to confirm RED**

Run: `python3 -m unittest tests.test_v42_closed_loop_acceptance -v`

- [ ] **Step 3: Enforce evidence predicates**

Require current task identity, authorization epoch, contract digest, delivery marker/path, independent reviewer, and P0–P2 clearance before accepting a node or scheduling descendants.

- [ ] **Step 4: Preserve immutable history**

Append recovery, rework, reauthorization, and evidence events without rewriting earlier evidence. Make `status` expose canonical state, retry reason, unresolved nodes, and whether new authorization is required.

- [ ] **Step 5: Run focused tests**

Run: `python3 -m unittest tests.test_v42_closed_loop_acceptance tests.test_completion_evidence_gate tests.test_v41_integration_closeout tests.test_v41_execution_recovery -v`

---

### Task 6: Package, install, and full verification

**Files:**
- Modify only if required by verification: `pyproject.toml`, `setup.py`, `vibe_guide/__init__.py`, `vibe_guide.egg-info/PKG-INFO`
- Create: `tests/test_v42_clean_install.py`

**Interfaces:**
- Consumes: completed V4.2 source, initializer, Monitor, Supervisor, and package metadata.
- Produces: clean-install evidence, exact version parity, and a final bounded verification report.

- [ ] **Step 1: Write clean-install tests**

```python
def test_clean_install_reports_one_version_and_v42_init_state():
    wheel, sdist = build_artifacts_in_clean_directory()
    for artifact in (wheel, sdist):
        env = install_in_fresh_venv(artifact)
        assert env.run("python -c 'import vibe_guide; print(vibe_guide.__version__)'").stdout.strip() == "4.2.0"
        assert env.run("vibe init --confirm --json").returncode == 0
        assert json.loads(env.read(".vibe/state.json"))["workflow_version"] == 4
```

- [ ] **Step 2: Run the clean-install tests**

Run: `python3 -m unittest tests.test_v42_clean_install -v`

- [ ] **Step 3: Run the complete verification set**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m vibe_guide --help
git diff --check
```

- [ ] **Step 4: Classify every remaining failure**

Separate current V4.2 defects from historical version-fixture failures, host permission failures, missing optional test fixtures, and unverified real Provider/App lifecycle. Do not report a green result while any current V4.2 test or clean-install check fails.

- [ ] **Step 5: Produce final evidence report**

Record changed files, exact test counts, clean-install results, unresolved external Provider evidence, current Git status, and explicit non-actions: no commit, push, MR, merge, deploy, credential, or system-permission change.

Implementation status is tracked in the SDD progress ledger.
