# V4.2 Monitor Engine Attestation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate verifiable V4.2 Monitor engine attestation during preflight and bind it into authorization and start/resume validation.

**Architecture:** Add a provider-neutral attestation module that hashes the current plan/revision/engine/mode/provider capability facts. Plan publication writes the attestation before the authorization card; card digest includes its evidence reference. Monitor validates the attestation against the card before any Run or provider action.

**Tech Stack:** Python 3 standard library, dataclasses, SHA-256, JSON atomic writes, `unittest`.

## Global Constraints

- Do not create Run, worker, task thread, or provider action during attestation generation.
- Preserve V4.2 rev2 artifacts as read-only history.
- Missing, malformed, expired, cross-plan, cross-revision, or digest-mismatched attestation remains `blocked_unknown`.
- Do not authorize commit, push, PR/MR, merge, deploy, credentials, or system permissions.

---

### Task 1: Attestation contract

**Files:**
- Create: `vibe_guide/engine_attestation.py`
- Test: `tests/test_v42_engine_attestation.py`

**Interfaces:**
- `create_engine_attestation(plan_id: str, plan_revision: int, execution_engine: str, engine_mode: str, provider: str, capability_facts: Mapping[str, bool], provenance: str, now: str) -> dict`
- `validate_engine_attestation(attestation: Mapping, plan_id: str, plan_revision: int, expected_digest: str | None = None) -> None`

- [ ] **Step 1: Write the failing tests**

```python
def test_create_attestation_has_stable_digest_and_evidence_ref():
    result = create_engine_attestation("p", 1, "vibeguide_monitor", "dag", "codex", {"codex.worktree": True}, "live:test", "2026-09-06T00:00:00Z")
    assert result["evidence_ref"].startswith("engine-attestation:")
    assert len(result["digest"]) == 64

def test_validate_rejects_wrong_revision():
    attestation = create_engine_attestation("p", 1, "vibeguide_monitor", "dag", "codex", {"codex.worktree": True}, "live:test", "2026-09-06T00:00:00Z")
    with pytest.raises(ValueError, match="revision"):
        validate_engine_attestation(attestation, "p", 2)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python3 -m unittest tests.test_v42_engine_attestation -v`
Expected: import or attribute failure because the module does not exist.

- [ ] **Step 3: Implement the minimal contract**

Canonicalize the signed payload with sorted JSON, require the exact engine/mode pair, require non-empty provider/provenance and boolean capability facts, derive `evidence_ref` from the digest prefix, and reject invalid identity or digest.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `python3 -m unittest tests.test_v42_engine_attestation -v`
Expected: all attestation tests pass.

### Task 2: Generate and persist attestation during plan publication

**Files:**
- Modify: `vibe_guide/cli.py`
- Modify: `vibe_guide/authorization.py`
- Test: `tests/test_v42_engine_attestation.py`

**Interfaces:**
- Plan publication writes `<plan>/engine-attestation.json` before card serialization.
- `build_authorization_card(..., engine_attestation=...)` uses the validated evidence reference rather than `unverified:legacy`.

- [ ] **Step 1: Add failing publication tests**

```python
def test_complex_plan_publishes_verified_engine_attestation(tmp_path):
    result = run_plan_fixture(tmp_path, plan_id="v42-attestation")
    assert result["authorization_card"]["engine_evidence_ref"].startswith("engine-attestation:")
    assert load_json(tmp_path / ".vibe/plans/v42-attestation/engine-attestation.json")["plan_id"] == "v42-attestation"
```

- [ ] **Step 2: Run RED**

Run: `python3 -m unittest tests.test_v42_engine_attestation -v`
Expected: the card still contains `unverified:legacy` and no attestation file exists.

- [ ] **Step 3: Implement minimal publication wiring**

Read `ProviderActionStore.capabilities()`, call `create_engine_attestation`, atomically write `engine-attestation.json`, pass it to authorization-card construction, and include the evidence reference in the digest input. Keep remote Git actions unchanged.

- [ ] **Step 4: Run focused CLI/card tests**

Run: `python3 -m unittest tests.test_v42_engine_attestation tests.test_authorization -v`
Expected: all pass.

### Task 3: Enforce attestation at Monitor start and resume

**Files:**
- Modify: `vibe_guide/monitor.py`
- Modify: `vibe_guide/cli.py`
- Test: `tests/test_v42_engine_attestation.py`

**Interfaces:**
- Monitor binding validation loads the current plan attestation and calls `validate_engine_attestation` before provider dispatch.

- [ ] **Step 1: Add failing start/resume tests**

```python
def test_monitor_rejects_missing_attestation_before_provider_call():
    monitor, record, runner = fixture_monitor_without_attestation()
    with pytest.raises(PermissionError, match="engine evidence"):
        monitor._execution_engine_binding(record)
    assert runner.calls == []
```

- [ ] **Step 2: Run RED**

Run: `python3 -m unittest tests.test_v42_engine_attestation -v`
Expected: the old implementation only checks the string and does not load or validate the attestation.

- [ ] **Step 3: Implement fail-closed validation**

Load the attestation as a regular non-symlink file, validate plan/revision and card evidence reference, and map missing/malformed/mismatch errors to `PermissionError("execution_engine_unverified: ...")`.

- [ ] **Step 4: Run focused monitor tests**

Run: `python3 -m unittest tests.test_v42_engine_attestation tests.test_v41_monitor_dispatch tests.test_monitor -v`
Expected: all pass; legacy/simple paths remain compatible.

### Task 4: Verification and artifact hygiene

**Files:**
- Modify only files from Tasks 1–3.

- [ ] **Step 1:** Run `python3 -m unittest discover -s tests -v` and record exact totals, separating frozen baseline failures.
- [ ] **Step 2:** Run `python3 -m vibe_guide --help`.
- [ ] **Step 3:** Run `git diff --check`.
- [ ] **Step 4:** Verify no `current-run.json`, worker task, provider request, or remote Git artifact was created for this work.

