# V4.3 入口预筛与复杂计划合同前置门禁 Spec

## 1. 版本与基线

- Target: `v4.3`
- Direct baseline: merged PR23, `1c5ed0e66be0f31a4c484bf2f2f33385e426e330`
- PRD: `docs/superpowers/specs/2026-09-09-v42-entry-preflight-and-complex-plan-gate-prd.md`
- Baseline contract: verified native desktop worker dispatch is required before worker creation.
- Historical V4.2 runs, plans, authorizations and evidence remain read-only.

## 2. Compatibility contract

1. Preserve existing public signatures, JSON status values, `.vibe/state.json` exact V4 fields, `Plan`, `DAGNode`, `IntegrationAcceptanceContract`, `route_task()`, `append_integration_review_node()`, `validate_integration_review_node()` and `verify_workflow()` behavior.
2. Add the V4.3 entry decision as a wrapper/projection over existing S0/S1 and preflight functions. Do not create a second threshold table.
3. Existing legitimate plans remain readable. A plan classified as complex still requires a valid integration contract and independent integration review before new dispatch.
4. New strict contract validation applies to plans explicitly marked with the V4.3 protocol marker. Invalid known structure is `blocked_invalid`; missing, expired or unverifiable runtime evidence is `blocked_unknown`.
5. No code, plan, state, task registry, authorization, or historical run is rewritten by a read-only preflight.
6. V4.3 does not require the legacy `.vibe/session-contract.json` or `capability_contract` evidence file to be present or unexpired. V4.3 still requires plan/digest validation and PR23's verified native desktop dispatch path before creating a worker.

## 3. Interfaces

### 3.1 Complexity decision

```python
classify_plan_complexity(spec: dict, route_context: dict) -> ComplexityDecision
```

`ComplexityDecision` is JSON-safe and contains `route`, `complexity_band`, `is_complex`, `reason`, and `evidence_refs`. It consumes existing `classify_s0()`, `route_task()` and `RouteResult`; existing S1 thresholds remain `<=8 simple`, `9-15 light_plan`, `>15 complex`.

### 3.2 Entry preflight

```python
run_entry_preflight(paths, request, origin="user_entry") -> EntryPreflightResult
```

The result contains `project_root`, `session_contract_digest`, `route`, `complexity_band`, `needs_s1`, `monitor_allowed`, `status`, `reason`, `source_status`, and `evidence_ref`. It creates no worker, provider task, writer lease, or run.

### 3.3 Complex contract validation

```python
validate_complex_plan_contract(spec, plan, protocol="v4.3") -> ContractValidation
```

The validator reuses `IntegrationAcceptanceContract.from_dict()`, `build_integration_acceptance_contract()`, `validate_dag()` and `validate_integration_review_node()`. It must not coerce strings into lists/objects or silently add a missing top-level complexity field.

### 3.4 Workflow projection

Existing workflow records remain authoritative. V4.3 adds automatic calls at real stage boundaries and projects the same chain into the existing run snapshot. It does not append workflow records to the exact four-field project state contract, and it does not mark future stages complete.

### 3.5 DAG visualization

```python
render_dag_graph(plan, audit=None, format="mermaid") -> DagGraph
```

`DagGraph` is generated from the canonical plan nodes, `depends_on`, `integration_after`, `parallel_group`, node status and read-only/reviewer metadata. It must expose stable node IDs, directed dependency edges, parallel groups, blocked reasons and the current plan revision. Mermaid is the default display format; a machine-readable source remains the audit source. The renderer must not infer edges or maintain a second DAG.

## 4. Protocol marker and compatibility

The new plan source may opt into V4.3 with a project-relative protocol marker, for example `protocol: "v4.3-entry-preflight"`. The marker is part of the plan digest. Missing marker selects the existing reader; it is not permission to bypass complex validation. The Spec implementation must document the exact marker schema before code changes and must test both marked and legacy inputs.

## 5. Error mapping

| Condition | Public result | Source evidence |
| --- | --- | --- |
| Missing/malformed required field or wrong contract type | `blocked_invalid` | parser/schema/contract validator |
| Plan contract or node digest drift | `blocked_invalid` | recomputed digest |
| Missing/expired/timeout provider or session evidence | `blocked_unknown` | structured runtime observation |
| Existing legacy CLI status | preserve existing status and exit code; add structured detail | compatibility adapter |

## 6. Acceptance contract

- The PRD JSON contract parses directly with the existing `IntegrationAcceptanceContract`.
- A marked V4.3 plan without `route` or `complexity_band` fails at plan publication.
- A legacy legitimate plan remains readable; a newly dispatched complex plan cannot bypass contract or integration review.
- A hand-authored reserved `integration-review` node is rejected; a persisted valid generated node is validated and not appended twice.
- No invalid plan creates a developer, reviewer or integration-reviewer task.
- PR23 native desktop dispatch verification remains a prerequisite after all V4.3 checks pass.
- Existing V4.2-focused tests are recorded as baseline; V4.3 failures are reported separately.
- Clean install/upgrade and rollback checks use the actual package metadata and artifacts; no README, tag name or local fixture alone proves delivery.
- A V4.3 project with an absent or expired legacy session capability file can publish and enter Monitor when its plan, authorization, digest and native desktop dispatch checks pass.
- Every new, revised, resumed or audited DAG renders a graph from the same canonical data used for scheduling; graph output identifies parallel groups, hard dependencies, blocked nodes and read-only Review nodes.

## 7. Issue boundaries

The implementation DAG below is the only authorized decomposition. Each node must preserve one writer, an explicit file allowlist, a distinct read-only reviewer, and the baseline commit reference. No worker is started from this Spec.

## V4.4 兼容性注记

V4.4 保留 V4.3 的 S0/S1 入口预检和授权边界，但不再把 `required-workflow.json` 的缺失或生成失败当作 dispatch 硬阻塞。workflow 结果继续作为 append-only 审计证据；当前 plan、授权、绑定证明、lease 和验收证据仍必须有效。旧运行状态只读隔离，不能直接恢复为当前执行状态。
