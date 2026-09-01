"""Authorization-bound, write-ahead monitor for developer/reviewer DAG tasks."""

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional, Tuple
import uuid

from .authorization import (
    AuthorizationRecord,
    affected_node_closure,
    canonical_node_contracts,
    executable_contract_digest,
    is_authorization_valid,
    validate_runtime_contract,
)
from .contracts import RunEvent, RunHandle, Runner
from .models import DAGNode, Plan, BindingIntent, BindingObservation, BindingVerification, IssueComplexity, LocalModel
from .paths import ProjectPaths
from .planner import resolve_consistency
from .adapters.task_provider import ProviderActionStore, ProviderPending, ProviderUnavailable
from .state import (
    CONSISTENCY_CORRECTION_KEYS,
    RunSnapshot,
    acquire_writer_lease,
    append_event,
    load_events,
    load_snapshot,
    quarantine_writer_lease,
    read_writer_lease,
    redact_provider_text,
    release_writer_lease,
    save_snapshot,
)
from .task_registry import (
    TaskBinding,
    binding_contract_enabled,
    load_task_binding,
    runtime_binding_gate,
    save_task_binding,
)
from .workflow_gate import require_capability_contract, require_entry
from .diagnostics import validate_child_session_binding
from .models import WorkerProfile
from .model_router import ModelRouter
from .change_requests import ChangeRequest, LocalMergeEvidence, merge_local
from .checkpoint import (
    ContextBudgetEstimator,
    ContextBudgetPolicy,
    MonitorCheckpoint,
    resume_from_checkpoint,
    write_checkpoint,
)
from .brief import ImplementationBrief, validate_implementation_brief
from .manifest import RunManifest


def _user_status(record: Dict[str, Any]) -> str:
    """Keep internal recovery detail behind the four public statuses."""
    reason = str(record.get("reason") or "").casefold()
    if any(marker in reason for marker in ("product", "scope", "permission", "credential", "irreversible", "security", "产品", "范围", "权限", "凭据", "不可逆", "安全")):
        return "需要你决定"
    if record.get("binding_phase") in {"retry_pending", "binding_probe_pending", "binding_repair_pending", "binding_repairing", "blocked_unknown", "unknown", "timeout", "failed", "stopped"}:
        return "自动修复中"
    if isinstance(record.get("retryable_action"), dict) and not record.get("active_task"):
        return "自动修复中"
    status = record.get("status")
    if status in {"blocked_design", "blocked_deploy"}:
        return "需要你决定"
    if status in {"initialized", "planned", "brief_pending", "start_pending"}:
        return "准备中"
    if status in {"running", "review", "rework", "delivered", "accepted", "complete"}:
        return "已启动"
    return "自动修复中"


class Monitor:
    def __init__(
        self,
        paths: ProjectPaths,
        plan: Plan,
        nodes: List[DAGNode],
        context_policy: Optional[ContextBudgetPolicy] = None,
    ):
        self.paths = paths
        self.plan = plan
        self.nodes = {node.id: node for node in nodes}
        # Binding reads are scoped to one public monitor operation.  The
        # registry is still the durable source of truth; clearing this cache
        # at each operation boundary prevents stale cross-tick identities.
        self._binding_cache: Dict[Tuple[str, str, str], TaskBinding] = {}
        self.context_policy = context_policy

    def _reset_binding_cache(self) -> None:
        self._binding_cache.clear()

    def _load_task_binding(
        self, snapshot: RunSnapshot, node_id: str, role: str
    ) -> TaskBinding:
        key = (snapshot.run_id, node_id, role)
        binding = self._binding_cache.get(key)
        if binding is None:
            binding = load_task_binding(
                self.paths, node_id, role, run_id=snapshot.run_id
            )
            self._binding_cache[key] = binding
        return binding

    def _save_task_binding(self, binding: TaskBinding) -> None:
        save_task_binding(self.paths, binding)
        if binding.run_id is not None:
            self._binding_cache[(binding.run_id, binding.issue_id, binding.role)] = binding

    def _require_binding_gate(
        self, contract: Dict[str, Any], binding: TaskBinding, runner: Runner
    ) -> None:
        """Fail closed before a real runner can start or write.

        ProviderActionRunner exposes the same gate so both boundaries execute
        it.  A generic runner is checked here as well, preserving legacy
        contracts that do not opt into V3.9.
        """
        if not binding_contract_enabled(contract):
            return
        verification = runtime_binding_gate(contract, binding)
        if not isinstance(verification, BindingVerification):
            raise ValueError("local provider binding gate returned invalid verification")
        if verification.binding_state != "binding_verified" or not verification.business_write_allowed:
            raise ValueError(
                "provider binding gate blocked_unknown: missing={} conflicts={}".format(
                    ",".join(verification.missing), ",".join(verification.conflicts)
                )
            )
        # Refresh the supervisor-owned lease at the write boundary.  A cached
        # observation is not sufficient for generic runners that expose no
        # provider hook; a released/expired lease must fail closed.
        node_id = str(contract.get("node_id") or binding.issue_id)
        fresh_lease = read_writer_lease(self.paths, node_id, binding.worktree)
        observed = getattr(binding, "binding_observation", None)
        if fresh_lease is None or observed is None or observed.lease != fresh_lease:
            raise ValueError("supervisor lease is stale or unavailable")
        refreshed = runtime_binding_gate(contract, binding)
        if (
            not isinstance(refreshed, BindingVerification)
            or refreshed.binding_state != "binding_verified"
            or not refreshed.business_write_allowed
        ):
            raise ValueError("provider binding gate blocked after lease refresh")
        # Provider hooks are advisory consistency checks only.  A provider
        # cannot manufacture permission by returning an object with
        # ``verified=True`` or another untrusted shape.
        probe = getattr(runner, "provider_binding_probe", None)
        if callable(probe):
            provider_probe = probe(contract, binding)
            if (
                not isinstance(provider_probe, BindingVerification)
                or provider_probe.binding_state != "binding_verified"
                or not provider_probe.verified
                or provider_probe != refreshed
            ):
                raise ValueError("provider binding probe returned invalid verification")
        gate = getattr(runner, "binding_gate", None)
        if callable(gate):
            provider_verification = gate(contract, binding)
            if not isinstance(provider_verification, BindingVerification):
                raise ValueError("provider binding gate returned invalid verification")
            if provider_verification != refreshed:
                raise ValueError("provider binding gate disagrees with local evidence")

    def _preflight_binding_before_provider(self, contract: Dict[str, Any]) -> None:
        """Gate provider actions before ``task_binding`` can emit a request.

        Unknown provider task ids are the sole exception: an explicit
        ``binding_probe`` may be emitted, and is marked non-business in its
        request.  Any ordinary V3.9 start requires complete protected live
        evidence first.
        """
        if not binding_contract_enabled(contract):
            return
        verification = runtime_binding_gate(contract)
        if not isinstance(verification, BindingVerification):
            raise ValueError("provider binding preflight returned invalid verification")
        if contract.get("binding_probe") is True:
            # An unknown provider task id is the sole controlled exception:
            # invoke the local gate first, then permit a non-business probe
            # only when no untrusted evidence was supplied.
            if verification.verified:
                return
            if (
                verification.missing == ["binding_provenance"]
                and "binding_intent" not in contract
                and "binding_observation" not in contract
            ):
                return
            raise ValueError("provider binding probe preflight blocked_unknown")
        if not verification.verified:
            raise ValueError("provider binding preflight blocked_unknown")

    @staticmethod
    def _binding_recovery_requested(contract: Dict[str, Any]) -> bool:
        """Identify the narrow JSON-restored continuation recovery path."""
        if not binding_contract_enabled(contract):
            return False
        if contract.get("continuation") is not True or contract.get("binding_bootstrap") is not True:
            return False
        return not (
            isinstance(contract.get("binding_intent"), BindingIntent)
            and isinstance(contract.get("binding_observation"), BindingObservation)
        )

    def _preflight_binding_recovery(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        contract: Dict[str, Any],
        current: Dict[str, Any],
    ) -> None:
        """Validate only the durable identity needed to enter bootstrap.

        A persisted binding has intentionally lost its private lease/cursor
        provenance.  This preflight therefore never returns a verified gate;
        it only proves that recovery will address the same task and the same
        constrained worktree.  The runner bootstrap performs the fresh Git
        observation and the subsequent full protected-evidence gate.
        """
        try:
            binding = self._load_task_binding(snapshot, node_id, role)
            root = self._worktree_path(current).resolve(strict=True)
            bound = Path(str(binding.worktree))
            if not bound.is_absolute():
                bound = self.paths.root / bound
            bound = bound.resolve(strict=True)
            managed_root = contract.get("managed_root")
            branch = contract.get("branch")
            base_sha = contract.get("base_sha")
            if not isinstance(managed_root, str) or not managed_root:
                raise ProviderUnavailable("binding recovery managed_root is missing")
            if not isinstance(branch, str) or not branch:
                raise ProviderUnavailable("binding recovery branch is missing")
            if not isinstance(base_sha, str) or not base_sha:
                raise ProviderUnavailable("binding recovery base_sha is missing")
            managed = Path(managed_root).resolve(strict=True)
            root.relative_to(managed)
            if bound != root:
                raise ProviderUnavailable("binding recovery worktree drift")
            if binding.issue_id != node_id or binding.role != role:
                raise ProviderUnavailable("binding recovery task scope drift")
            if binding.run_id != snapshot.run_id:
                raise ProviderUnavailable("binding recovery run drift")
            if binding.task_id != contract.get("task_id"):
                raise ProviderUnavailable("binding recovery task identity drift")
            if binding.branch != branch:
                raise ProviderUnavailable("binding recovery branch drift")
            expected_host = contract.get("host_id")
            if expected_host not in (None, "") and binding.host != expected_host:
                raise ProviderUnavailable("binding recovery host drift")
            expected_project = contract.get("project_id")
            if expected_project not in (None, ""):
                persisted_intent = binding.binding_intent
                if isinstance(persisted_intent, BindingIntent) and persisted_intent.project_id != expected_project:
                    raise ProviderUnavailable("binding recovery project drift")
        except ProviderUnavailable:
            raise
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            raise ProviderUnavailable("binding recovery preflight blocked_unknown") from error

    def _run_binding_bootstrap(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        contract: Dict[str, Any],
        runner: Runner,
        current: Dict[str, Any],
    ) -> None:
        """Invoke the real runner bootstrap only for an explicit recovery."""
        if not binding_contract_enabled(contract) or (
            contract.get("binding_bootstrap") is not True
            and contract.get("continuation") is not True
        ):
            return
        bootstrap = getattr(runner, "binding_bootstrap", None)
        if not callable(bootstrap):
            raise ProviderUnavailable("binding bootstrap is unavailable")
        try:
            binding = self._load_task_binding(snapshot, node_id, role)
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            raise ProviderUnavailable("binding bootstrap has no same-task binding") from error
        result = bootstrap(contract, binding, self._worktree_path(current))
        if not isinstance(result, BindingVerification) or not result.verified:
            raise ProviderUnavailable("binding bootstrap remains blocked_unknown")
        contract["binding_intent"] = binding.binding_intent
        contract["binding_observation"] = binding.binding_observation

    def merge_change_request_local(
        self,
        run_id: str,
        change_request: ChangeRequest,
        local_facts: Optional[Dict[str, Any]] = None,
    ) -> LocalMergeEvidence:
        """Record a local-only Change Request outcome for an existing run.

        The method never invokes Git or a provider.  It only validates the
        persisted authorization and appends the evidence returned by
        :func:`merge_local` to the run event log.
        """
        snapshot = load_snapshot(self.paths, run_id)
        record = self._require_snapshot_authorization(snapshot)
        evidence = merge_local(change_request, record, local_facts)
        self._record(
            snapshot,
            "change_request_" + evidence.status,
            {
                "run_id": snapshot.run_id,
                "change_request": change_request.to_dict(),
                "evidence": evidence.to_dict(),
            },
        )
        save_snapshot(self.paths, snapshot)
        return evidence

    # Short alias for callers that already operate in the Monitor boundary.
    merge_local = merge_change_request_local

    def start(
        self, record: Optional[AuthorizationRecord], runner: Runner
    ) -> RunSnapshot:
        self._reset_binding_cache()
        self._require_record(record)
        state = self.paths.vibe / "state.json"
        capability_contract_digest = ""
        if state.is_file():
            try:
                require_entry(self.paths, "monitor:" + self.plan.plan_id, "monitor")
            except (OSError, ValueError, json.JSONDecodeError) as error:
                raise PermissionError("session_gate_blocked") from error
            capability_contract_digest = require_capability_contract(
                self.paths
            ).contract_digest
        elif self.paths.vibe.exists():
            raise PermissionError("session_gate_blocked: V2 state.json is missing")
        assert record is not None
        run_id = "run-" + uuid.uuid4().hex
        node_state: Dict[str, Dict[str, Any]] = {}
        for node_id, node in self.nodes.items():
            node_state[node_id] = {
                "status": "delivered" if node.status == "delivered" else "planned",
                "worker": node.contract.get("worker"),
                "worktree": node.contract.get("worktree", ".worktrees/" + node_id),
                "branch": node.contract.get("branch", "node/" + node_id),
                "status_file": node.contract.get("status_file", ""),
                "handoff_file": node.contract.get("handoff_file", ""),
                "evidence": [],
                "active_role": None,
                "active_task": None,
                "start_intent": None,
                "quarantine": None,
                "developer_identity": None,
                "reviewer_identity": None,
                "developer_generation": 0,
                "review_generation": 0,
                "reviewer_started": False,
                "pair_archived": False,
                "old_task_reconciled": False,
                "retryable_action": None,
                "binding_phase": None,
                "contract_overrides": {},
                "corrections": [],
                "contract_digest": executable_contract_digest([node]),
                "acceptance": None,
            }
        snapshot = RunSnapshot(
            run_id=run_id,
            plan_id=self.plan.plan_id,
            plan_version=self.plan.version,
            status="initialized",
            nodes=node_state,
            handles={},
            tasks={},
            authorization=record.to_dict(),
            authorization_digest=record.digest,
            node_contract_digest=record.node_contract_digest,
            capability_contract_digest=capability_contract_digest,
            event_sequence=0,
        )
        self._record(
            snapshot,
            "run_started",
            {
                "run_id": run_id,
                "authorization_digest": record.digest,
                "node_contract_digest": record.node_contract_digest,
                "capability_contract_digest": capability_contract_digest,
                "node_ids": sorted(self.nodes),
            },
        )
        save_snapshot(self.paths, snapshot)
        if not self._context_allows_dispatch(snapshot, runner):
            save_snapshot(self.paths, snapshot)
            return snapshot
        self._schedule_ready(snapshot, runner)
        self._refresh_run_status(snapshot)
        save_snapshot(self.paths, snapshot)
        return snapshot

    def resume(self, run_id: str, runner: Runner, poll_handles: bool = True) -> RunSnapshot:
        self._reset_binding_cache()
        require_entry(self.paths, "resume:" + str(run_id), "resume")
        snapshot = load_snapshot(self.paths, run_id)
        checkpoint_path = self.paths.root / ".vibe" / "runs" / run_id / "monitor_checkpoint.json"
        if checkpoint_path.is_file():
            # Validate the recovery package before touching leases or polling.
            resume_from_checkpoint(self.paths, run_id)
        current_capability_contract = require_capability_contract(self.paths)
        if (
            not snapshot.capability_contract_digest
            or snapshot.capability_contract_digest
            != current_capability_contract.contract_digest
        ):
            raise PermissionError("capability_contract_unknown: run binding mismatch")
        self._require_snapshot_authorization(snapshot)
        self._reconcile_unapplied_events(snapshot)
        for node_id, current in snapshot.nodes.items():
            if current.get("status") == "start_pending":
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "start intent has no provider-confirmed result",
                )
            elif current.get("status") in {"running", "review", "rework"}:
                if node_id not in snapshot.handles and not isinstance(
                    current.get("retryable_action"), dict
                ):
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "active task handle is missing during resume",
                    )
        # Poll durable handles before considering any new dispatch.  This is
        # the recovery ordering that prevents chat/context loss from creating
        # a second writer for an already-running provider task.
        if poll_handles:
            self._poll_active_handles(snapshot, runner)
        if self._context_allows_dispatch(snapshot, runner):
            self._schedule_ready(snapshot, runner, recover_missing_reviewer=True)
        self._refresh_run_status(snapshot)
        save_snapshot(self.paths, snapshot)
        return snapshot

    def reauthorize(
        self,
        run_id: str,
        record: AuthorizationRecord,
        runner: Runner,
        change_reason: str,
    ) -> RunSnapshot:
        """Continue the same run under a newly confirmed same-plan contract."""

        self._reset_binding_cache()
        self._require_record(record)
        snapshot = load_snapshot(self.paths, run_id)
        state = self.paths.vibe / "state.json"
        current_capability_contract_digest = snapshot.capability_contract_digest
        capability_contract_changed = False
        if state.is_file():
            current_capability_contract = require_capability_contract(self.paths)
            current_capability_contract_digest = current_capability_contract.contract_digest
            capability_contract_changed = (
                snapshot.capability_contract_digest
                != current_capability_contract_digest
            )
            if capability_contract_changed and change_reason not in {
                "capability_contract_changed",
                "executable_contract_changed",
            }:
                raise PermissionError(
                    "capability_contract_unknown: explicit recovery reason required"
                )
        self._reconcile_unapplied_events(snapshot)
        # The replay may already have applied this exact authorization and
        # capability transition.  Re-read the live contract after replay so
        # an interrupted call cannot append the same transition again.
        if state.is_file():
            current_capability_contract = require_capability_contract(self.paths)
            current_capability_contract_digest = current_capability_contract.contract_digest
            capability_contract_changed = (
                snapshot.capability_contract_digest
                != current_capability_contract_digest
            )
        if (
            snapshot.plan_id != self.plan.plan_id
            or snapshot.plan_version != self.plan.version
            or set(snapshot.nodes) != set(self.nodes)
        ):
            raise PermissionError("reauthorization must remain on the same plan revision")
        if snapshot.authorization_digest == record.digest and not capability_contract_changed:
            self._require_snapshot_authorization(snapshot)
            self._schedule_ready(snapshot, runner)
            self._refresh_run_status(snapshot)
            save_snapshot(self.paths, snapshot)
            return snapshot

        previous = self._snapshot_record(snapshot)
        previous_node_contract_digests = {
            node_id: snapshot.nodes[node_id].get("contract_digest")
            for node_id in snapshot.nodes
        }
        new_node_contract_digests = {
            node_id: executable_contract_digest([self.nodes[node_id]])
            for node_id in snapshot.nodes
        }
        changed_nodes = sorted(
            node_id
            for node_id in snapshot.nodes
            if previous_node_contract_digests[node_id]
            != new_node_contract_digests[node_id]
        )
        affected_nodes = affected_node_closure(
            list(self.nodes.values()), changed_nodes
        )
        authorized_node_contracts = {
            item["id"]: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in canonical_node_contracts(list(self.nodes.values()))
        }
        accepted_nodes = sorted(
            node_id
            for node_id, current in snapshot.nodes.items()
            if current.get("status") == "accepted"
        )
        retained_acceptances = {}
        invalidated_acceptances = {}
        for node_id, current in snapshot.nodes.items():
            if current.get("status") != "accepted":
                continue
            acceptance = current.get("acceptance")
            if not isinstance(acceptance, dict):
                raise RuntimeError("accepted node acceptance evidence is missing")
            evidence = {
                "contract_digest": acceptance.get("contract_digest"),
                "authorization_epoch": acceptance.get("authorization_epoch"),
            }
            if (
                node_id not in affected_nodes
                and previous_node_contract_digests.get(node_id)
                == new_node_contract_digests[node_id]
                and evidence["contract_digest"]
                == previous_node_contract_digests[node_id]
            ):
                retained_acceptances[node_id] = evidence
            else:
                invalidated_acceptances[node_id] = evidence
        for node_id, handle_id in list(snapshot.handles.items()):
            current = snapshot.nodes[node_id]
            active = current.get("active_task")
            if not isinstance(active, dict) or active.get("handle_id") != handle_id:
                raise RuntimeError("active task cannot be reconciled for reauthorization")
            if not self._stop_active_for_transition(
                snapshot,
                node_id,
                str(active["role"]),
                handle_id,
                runner,
                "stopped",
            ):
                save_snapshot(self.paths, snapshot)
                raise RuntimeError("active task stop cannot be proven for reauthorization")
            snapshot.nodes[node_id]["old_task_reconciled"] = True

        continuation: Dict[str, Any] = {}
        for node_id in sorted(snapshot.nodes):
            for role in ("developer", "reviewer"):
                key = "{}:{}".format(node_id, role)
                try:
                    binding = self._load_task_binding(snapshot, node_id, role)
                except FileNotFoundError:
                    continue
                snapshot.tasks[key] = binding.to_dict()
                continuation[key] = {
                    "task_id": binding.task_id,
                    "cursor": binding.cursor,
                }

        transition = {
            "run_id": snapshot.run_id,
            "previous_authorization": previous.to_dict(),
            "previous_authorization_digest": previous.digest,
            "previous_node_contract_digest": previous.node_contract_digest,
            "previous_capability_contract_digest": snapshot.capability_contract_digest,
            "new_authorization": record.to_dict(),
            "authorization_digest": record.digest,
            "node_contract_digest": record.node_contract_digest,
            "capability_contract_digest": current_capability_contract_digest,
            "previous_node_contract_digests": previous_node_contract_digests,
            "node_contract_digests": new_node_contract_digests,
            "authorized_node_contracts": authorized_node_contracts,
            "changed_nodes": changed_nodes,
            "affected_nodes": affected_nodes,
            "accepted_nodes": accepted_nodes,
            "retained_acceptances": retained_acceptances,
            "invalidated_acceptances": invalidated_acceptances,
            "change_reason": change_reason,
            "continuation": continuation,
        }
        self._record(snapshot, "authorization_reauthorized", transition)
        self._apply_reauthorization_transition(snapshot, transition)
        self._schedule_ready(snapshot, runner)
        self._refresh_run_status(snapshot)
        save_snapshot(self.paths, snapshot)
        return snapshot

    def tick(self, run_id: str, runner: Runner) -> RunSnapshot:
        self._reset_binding_cache()
        snapshot = load_snapshot(self.paths, run_id)
        self._require_snapshot_authorization(snapshot)
        self._reconcile_unapplied_events(snapshot)
        self._poll_active_handles(snapshot, runner)
        if self._context_allows_dispatch(snapshot, runner):
            self._schedule_ready(snapshot, runner)
        self._refresh_run_status(snapshot)
        save_snapshot(self.paths, snapshot)
        return snapshot

    def reconcile_evidence(self, run_id: str, package: Dict[str, Any]) -> RunSnapshot:
        """Promote a verified, same-run evidence package through normal events.

        This path is deliberately provider-free: it only reuses the original
        task bindings and appends ordinary ``delivered``/``accepted`` events.
        Any malformed, stale, mixed, or unverifiable package is rejected before
        the first event is appended.
        """
        self._reset_binding_cache()
        snapshot = load_snapshot(self.paths, run_id)
        self._require_snapshot_authorization(snapshot)
        self._validate_reconciliation_package(snapshot, package)
        if all(node.get("status") == "accepted" for node in snapshot.nodes.values()):
            return snapshot
        for item in package["nodes"]:
            current = snapshot.nodes[item["node_id"]]
            if current.get("active_task") is not None or item["node_id"] in snapshot.handles:
                raise ValueError("reconciliation target has an active writer")

        for item in package["nodes"]:
            node_id = item["node_id"]
            current = snapshot.nodes[node_id]
            developer = self._load_task_binding(snapshot, node_id, "developer")
            reviewer = self._load_task_binding(snapshot, node_id, "reviewer")
            # Establish an in-memory active registration for event application;
            # this is not a new task or writer and is never dispatched.
            dev_handle = str(item["developer"].get("handle_id") or "reconcile:" + node_id + ":developer")
            current["active_role"] = "developer"
            current["active_task"] = {
                "role": "developer", "task_id": developer.task_id,
                "generation": developer.generation, "handle_id": dev_handle,
            }
            snapshot.handles[node_id] = dev_handle
            previous_sequence = snapshot.event_sequence
            self._record(
                snapshot,
                "delivered",
                {"run_id": snapshot.run_id, "node_id": node_id,
                 "evidence": item["developer"].get("evidence_ref", "reconciliation")},
                current["active_task"],
            )
            snapshot.event_sequence = previous_sequence
            self._reconcile_unapplied_events(snapshot)
            current.setdefault("evidence", []).append(
                redact_provider_text(item["developer"].get("evidence_ref", "reconciliation"))
            )

            reviewer_handle = str(item["reviewer"].get("handle_id") or "reconcile:" + node_id + ":reviewer")
            current["active_role"] = "reviewer"
            current["active_task"] = {
                "role": "reviewer", "task_id": reviewer.task_id,
                "generation": reviewer.generation, "handle_id": reviewer_handle,
            }
            snapshot.handles[node_id] = reviewer_handle
            previous_sequence = snapshot.event_sequence
            self._record(
                snapshot,
                "accepted",
                {"run_id": snapshot.run_id, "node_id": node_id,
                 "contract_digest": current["contract_digest"],
                 "authorization_epoch": snapshot.authorization_digest,
                 "evidence": {"source": "reconciliation", "clearance": item["reviewer"]["clearance"]}},
                current["active_task"],
            )
            snapshot.event_sequence = previous_sequence
            self._reconcile_unapplied_events(snapshot)
            current.setdefault("evidence", []).append(
                redact_provider_text(item["reviewer"].get("evidence_ref", "reconciliation"))
            )
            previous_sequence = snapshot.event_sequence
            self._record(
                snapshot,
                "pair_archived",
                {"run_id": snapshot.run_id, "node_id": node_id,
                 "clearance": current.get("review_clearance", {"p0": 0, "p1": 0, "p2": 0})},
            )
            snapshot.event_sequence = previous_sequence
            self._reconcile_unapplied_events(snapshot)

        self._refresh_run_status(snapshot)
        save_snapshot(self.paths, snapshot)
        return snapshot

    def _validate_reconciliation_package(
        self, snapshot: RunSnapshot, package: Dict[str, Any]
    ) -> None:
        if not isinstance(package, dict):
            raise ValueError("reconciliation package is invalid")
        required = {
            "schema_version", "run_id", "plan_id", "plan_revision",
            "authorization_digest", "node_contract_digest", "nodes",
        }
        if set(package) != required or package.get("schema_version") != 1:
            raise ValueError("reconciliation package schema is invalid")
        if (
            package["run_id"] != snapshot.run_id
            or package["plan_id"] != self.plan.plan_id
            or package["plan_revision"] != self.plan.version
            or package["authorization_digest"] != snapshot.authorization_digest
            or package["node_contract_digest"] != snapshot.node_contract_digest
        ):
            raise ValueError("reconciliation run or contract digest mismatch")
        records = package["nodes"]
        if not isinstance(records, list):
            raise ValueError("reconciliation package nodes are invalid")
        ids = [item.get("node_id") if isinstance(item, dict) else None for item in records]
        all_accepted = all(current.get("status") == "accepted" for current in snapshot.nodes.values())
        expected_ids = {
            node_id for node_id, current in snapshot.nodes.items()
            if current.get("status") != "accepted"
        }
        if all_accepted and not records:
            return
        if all_accepted:
            expected_ids = set(snapshot.nodes)
        if any(not isinstance(node_id, str) for node_id in ids) or len(ids) != len(set(ids)) or set(ids) != expected_ids:
            raise ValueError("reconciliation package node set is invalid")
        if all_accepted:
            for item in records:
                if not isinstance(item, dict) or set(item) != {"node_id", "developer", "reviewer"}:
                    raise ValueError("reconciliation node record is invalid")
            return
        for item in records:
            node_id = item["node_id"]
            if set(item) != {"node_id", "developer", "reviewer"}:
                raise ValueError("reconciliation node record is invalid")
            current = snapshot.nodes[node_id]
            for role in ("developer", "reviewer"):
                claim = item[role]
                if not isinstance(claim, dict):
                    raise ValueError("reconciliation task evidence is invalid")
                binding = self._load_task_binding(snapshot, node_id, role)
                for key in ("task_id", "generation", "worktree", "branch", "status"):
                    if claim.get(key) != getattr(binding, key):
                        raise ValueError("reconciliation {} identity mismatch".format(role))
                self._validate_head(binding, claim.get("head"))
                if binding.task_id != current.get(role + "_identity"):
                    raise ValueError("reconciliation {} task identity is stale".format(role))
                if binding.worktree != str(current.get("worktree")) or binding.branch != str(current.get("branch")):
                    raise ValueError("reconciliation {} worktree or branch mismatch".format(role))
                self._validate_evidence_file(binding, claim.get("status_file"), "status_file")
                self._validate_evidence_file(binding, claim.get("handoff_file"), "handoff_file")
            reviewer_claim = item["reviewer"]
            clearance = reviewer_claim.get("clearance")
            if not isinstance(clearance, dict) or set(clearance) != {"p0", "p1", "p2"} or any(clearance[key] != 0 for key in clearance):
                raise ValueError("reviewer P0-P2 clearance is not zero")

    def _validate_evidence_file(self, binding: TaskBinding, value: Any, field: str) -> None:
        if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
            raise ValueError("reconciliation {} evidence is invalid".format(field))
        path_value = value["path"]
        digest = value["sha256"]
        if not isinstance(path_value, str) or not path_value or Path(path_value).is_absolute() or "\x00" in path_value:
            raise ValueError("reconciliation {} path is invalid".format(field))
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("reconciliation {} hash is invalid".format(field))
        expected = getattr(binding, field)
        if expected and path_value != expected:
            raise ValueError("reconciliation {} path does not match binding".format(field))
        root = self._worktree_path({"worktree": binding.worktree})
        candidate = (root / path_value).resolve(strict=False)
        try:
            candidate.relative_to(root.resolve())
        except ValueError as error:
            raise ValueError("reconciliation evidence path escapes worktree") from error
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("reconciliation evidence file is unavailable")
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != digest:
            raise ValueError("reconciliation evidence hash mismatch")

    def _validate_head(self, binding: TaskBinding, claimed: Any) -> None:
        if not isinstance(claimed, str) or len(claimed) != 40 or any(c not in "0123456789abcdef" for c in claimed.lower()):
            raise ValueError("reconciliation HEAD is invalid")
        worktree = self._worktree_path({"worktree": binding.worktree})
        try:
            observed = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "--verify", "HEAD"],
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            observed_branch = subprocess.run(
                ["git", "-C", str(worktree), "branch", "--show-current"],
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError("reconciliation HEAD is unavailable") from error
        if observed.lower() != claimed.lower():
            raise ValueError("reconciliation HEAD mismatch")
        if observed_branch != binding.branch:
            raise ValueError("reconciliation branch mismatch: {} != {}".format(observed_branch, binding.branch))

    def _poll_active_handles(self, snapshot: RunSnapshot, runner: Runner) -> None:
        for node_id, handle_id in list(snapshot.handles.items()):
            self._require_snapshot_authorization(snapshot)
            try:
                events = runner.poll(RunHandle(handle_id))
            except Exception as error:
                self._queue_active_retry(
                    snapshot,
                    node_id,
                    "runner poll failed ({})".format(type(error).__name__),
                )
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "runner poll failed ({})".format(type(error).__name__),
                    quarantine_lease=False,
                    retryable_same_task=True,
                )
                continue
            for event in events:
                if event.event in {"context_overflow", "context_exhausted", "overflow"}:
                    active = snapshot.nodes[node_id].get("active_task")
                    self._record_runner_event(snapshot, node_id, event, active or {})
                    self._checkpoint_context(snapshot, "provider reported context overflow", exhausted=True)
                    continue
                self._apply_event(snapshot, node_id, handle_id, event, runner)

    def _require_record(self, record: Optional[AuthorizationRecord]) -> None:
        if record is None or not is_authorization_valid(
            record, self.plan, list(self.nodes.values())
        ):
            raise PermissionError("a valid executable-contract authorization is required")
        if record.node_ids != tuple(sorted(self.nodes)):
            raise PermissionError("authorization node scope does not match monitor scope")

    def _snapshot_record(self, snapshot: RunSnapshot) -> AuthorizationRecord:
        try:
            return AuthorizationRecord.from_dict(snapshot.authorization)
        except (TypeError, ValueError) as error:
            raise PermissionError("snapshot authorization record is invalid") from error

    def _consistency_binding(
        self, record: AuthorizationRecord, node: DAGNode
    ) -> Dict[str, Any]:
        project_root = str(self.paths.root.resolve())
        return {
            "schema_version": 1,
            "project_digest": hashlib.sha256(project_root.encode("utf-8")).hexdigest(),
            "plan_id": self.plan.plan_id,
            "plan_version": self.plan.version,
            "decision_digest": record.decision_digest,
            "authorization_digest": record.digest,
            "issue_contract_digest": executable_contract_digest([node]),
        }

    def _require_snapshot_authorization(self, snapshot: RunSnapshot) -> AuthorizationRecord:
        record = self._snapshot_record(snapshot)
        if snapshot.plan_id != self.plan.plan_id or snapshot.plan_version != self.plan.version:
            raise PermissionError("snapshot plan no longer matches monitor plan")
        if snapshot.authorization_digest != record.digest:
            raise PermissionError("snapshot authorization digest is inconsistent")
        if snapshot.node_contract_digest != record.node_contract_digest:
            raise PermissionError("snapshot contract digest is inconsistent")
        self._require_record(record)
        return record

    def _apply_reauthorization_transition(
        self, snapshot: RunSnapshot, data: Dict[str, Any]
    ) -> None:
        previous = AuthorizationRecord.from_dict(data.get("previous_authorization"))
        replacement = AuthorizationRecord.from_dict(data.get("new_authorization"))
        if (
            previous.to_dict() != snapshot.authorization
            or data.get("previous_authorization_digest")
            != snapshot.authorization_digest
            or data.get("previous_node_contract_digest")
            != snapshot.node_contract_digest
            or data.get("previous_capability_contract_digest")
            != snapshot.capability_contract_digest
        ):
            raise ValueError("reauthorization previous lineage is inconsistent")
        self._require_record(replacement)
        if (
            data.get("authorization_digest") != replacement.digest
            or data.get("node_contract_digest")
            != replacement.node_contract_digest
            or data.get("change_reason")
            not in {"executable_contract_changed", "capability_contract_changed"}
        ):
            raise ValueError("reauthorization replacement lineage is inconsistent")
        replacement_capability_digest = data.get("capability_contract_digest")
        if replacement_capability_digest:
            if (
                not isinstance(replacement_capability_digest, str)
                or len(replacement_capability_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in replacement_capability_digest
                )
            ):
                raise ValueError("reauthorization capability contract lineage is invalid")
        elif snapshot.capability_contract_digest:
            raise ValueError("reauthorization capability contract lineage is missing")
        continuation = data.get("continuation")
        if not isinstance(continuation, dict):
            raise ValueError("reauthorization continuation evidence is invalid")
        for key, evidence in continuation.items():
            if not isinstance(key, str) or ":" not in key or not isinstance(evidence, dict):
                raise ValueError("reauthorization continuation evidence is invalid")
            node_id, role = key.rsplit(":", 1)
            if node_id not in snapshot.nodes or role not in {"developer", "reviewer"}:
                raise ValueError("reauthorization continuation identity is invalid")
            binding = self._load_task_binding(snapshot, node_id, role)
            if (
                set(evidence) != {"task_id", "cursor"}
                or evidence["task_id"] != binding.task_id
                or evidence["cursor"] != binding.cursor
            ):
                raise ValueError("reauthorization continuation cursor is inconsistent")
            snapshot.tasks[key] = binding.to_dict()

        previous_node_contract_digests = data.get("previous_node_contract_digests")
        node_contract_digests = data.get("node_contract_digests")
        if (
            not isinstance(previous_node_contract_digests, dict)
            or not isinstance(node_contract_digests, dict)
            or set(previous_node_contract_digests) != set(snapshot.nodes)
            or set(node_contract_digests) != set(snapshot.nodes)
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for mapping in (previous_node_contract_digests, node_contract_digests)
                for value in mapping.values()
            )
        ):
            raise ValueError("reauthorization node contract lineage is invalid")
        retained_acceptances = data.get("retained_acceptances")
        invalidated_acceptances = data.get("invalidated_acceptances")
        changed_nodes = data.get("changed_nodes")
        affected_nodes = data.get("affected_nodes")
        accepted_nodes = data.get("accepted_nodes")
        authorized_node_contracts = data.get("authorized_node_contracts")
        if not isinstance(retained_acceptances, dict) or not isinstance(
            invalidated_acceptances, dict
        ) or not isinstance(authorized_node_contracts, dict) or set(
            authorized_node_contracts
        ) != set(snapshot.nodes) or any(
            not isinstance(value, str)
            for value in authorized_node_contracts.values()
        ) or any(
            not isinstance(items, list)
            or any(not isinstance(node_id, str) for node_id in items)
            or len(items) != len(set(items))
            or items != sorted(items)
            or any(node_id not in snapshot.nodes for node_id in items)
            for items in (changed_nodes, affected_nodes, accepted_nodes)
        ):
            raise ValueError("reauthorization acceptance lineage is invalid")
        if set(retained_acceptances) & set(invalidated_acceptances):
            raise ValueError("reauthorization acceptance lineage overlaps")
        expected_changed_nodes = sorted(
            node_id
            for node_id in snapshot.nodes
            if previous_node_contract_digests[node_id]
            != node_contract_digests[node_id]
        )
        if changed_nodes != expected_changed_nodes:
            raise ValueError("reauthorization changed node lineage is invalid")
        expected_authorized_node_contracts = {
            item["id"]: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in canonical_node_contracts(list(self.nodes.values()))
        }
        if authorized_node_contracts != expected_authorized_node_contracts:
            raise ValueError("reauthorization authorized DAG proof is invalid")
        if affected_nodes != affected_node_closure(
            list(self.nodes.values()), changed_nodes
        ):
            raise ValueError("reauthorization affected suffix lineage is invalid")
        retained_ids = set(retained_acceptances)
        invalidated_ids = set(invalidated_acceptances)
        current_accepted_ids = {
            node_id
            for node_id, current in snapshot.nodes.items()
            if current.get("status") == "accepted"
        }
        if (
            accepted_nodes != sorted(retained_ids | invalidated_ids)
            or current_accepted_ids != set(accepted_nodes)
            or set(changed_nodes) - set(affected_nodes)
            or retained_ids & set(affected_nodes)
            or not invalidated_ids.issubset(set(affected_nodes))
            or retained_ids | invalidated_ids != set(accepted_nodes)
        ):
            raise ValueError("reauthorization affected suffix disposition is invalid")

        for node_id, evidence in retained_acceptances.items():
            if (
                not isinstance(evidence, dict)
                or set(evidence) != {"contract_digest", "authorization_epoch"}
                or evidence["contract_digest"]
                != previous_node_contract_digests[node_id]
                or evidence["contract_digest"] != node_contract_digests[node_id]
                or evidence["authorization_epoch"] != previous.digest
            ):
                raise ValueError("retained acceptance evidence is invalid")
        for node_id, evidence in invalidated_acceptances.items():
            if (
                not isinstance(evidence, dict)
                or set(evidence) != {"contract_digest", "authorization_epoch"}
                or evidence["contract_digest"]
                != previous_node_contract_digests[node_id]
                or evidence["authorization_epoch"] != previous.digest
            ):
                raise ValueError("invalidated acceptance evidence is invalid")

        snapshot.authorization = replacement.to_dict()
        snapshot.authorization_digest = replacement.digest
        snapshot.node_contract_digest = replacement.node_contract_digest
        snapshot.capability_contract_digest = replacement_capability_digest or ""
        snapshot.status = "running"
        snapshot.handles.clear()
        for node_id, current in snapshot.nodes.items():
            current["contract_digest"] = node_contract_digests[node_id]
            current["active_role"] = None
            current["active_task"] = None
            current["start_intent"] = None
            current["quarantine"] = None
            if current.get("status") == "accepted":
                if node_id in retained_acceptances:
                    evidence = retained_acceptances[node_id]
                    current["acceptance"] = {
                        "contract_digest": evidence["contract_digest"],
                        "authorization_epoch": replacement.digest,
                    }
                    continue
                if node_id in invalidated_acceptances:
                    current["acceptance"] = None
                    self._queue_reauthorization_continuation(
                        current,
                        self._continuation_requires_successor(
                            snapshot, node_id, "developer"
                        ),
                    )
                    continue
                raise ValueError("accepted node lacks reauthorization disposition")
            if int(current.get("developer_generation", 0)) > 0:
                self._queue_reauthorization_continuation(
                    current,
                    self._continuation_requires_successor(
                        snapshot, node_id, "developer"
                    ),
                )
            else:
                current["status"] = "planned"
                current["retryable_action"] = None

    def _continuation_requires_successor(
        self, snapshot: RunSnapshot, node_id: str, role: str
    ) -> bool:
        """Return whether reauthorization needs a new task identity.

        A provider-confirmed terminal binding is still a canonical task that
        can be continued.  Only a missing binding (or a non-terminal binding
        whose state must be reconciled by the scheduler) is a successor
        candidate.  Malformed registry data remains fail-closed: the
        scheduler will keep the node blocked rather than guessing.
        """
        try:
            binding = self._load_task_binding(snapshot, node_id, role)
        except FileNotFoundError:
            return True
        except (OSError, TypeError, ValueError):
            return True
        # A provider-confirmed delivery is a terminal developer handoff: the
        # same visible task can be resumed for authorized rework.  Treating
        # it as a successor candidate would require a stop/absence proof even
        # though no active writer remains, leaving ordinary delivered work
        # permanently blocked.  Unknown/running/review states remain
        # successor candidates and still require the fail-closed proof below.
        return binding.status not in {"delivered", "stopped", "failed", "archived"}

    @staticmethod
    def _queue_reauthorization_continuation(
        current: Dict[str, Any], successor_candidate: bool
    ) -> None:
        current["pair_archived"] = True
        current["status"] = "blocked_unknown"
        current["retryable_action"] = {
            "role": "developer",
            "phase": "rework",
            "continuation": True,
            "pending_schedule": True,
            "successor_candidate": successor_candidate,
        }

    def _recover_quarantined_delivered_developer(
        self, snapshot: RunSnapshot, node_id: str, record_event: bool = True
    ) -> bool:
        """Rebuild a lost continuation marker from a delivered binding.

        An interrupted reauthorization can persist the quarantine transition
        after the retry marker was lost.  A current-run delivered developer
        binding is sufficient evidence to continue that same visible task;
        it is not evidence for creating a successor or another writer.
        """
        current = snapshot.nodes[node_id]
        if (
            current.get("status") != "blocked_unknown"
            or not isinstance(current.get("quarantine"), dict)
            or current.get("active_task") is not None
            or current.get("active_role") is not None
            or current.get("start_intent") is not None
            or node_id in snapshot.handles
            or current.get("retryable_action") is not None
            # If reviewer dispatch already began, a delivered developer is
            # not a rework candidate.  Recovery must reconstruct the missing
            # reviewer binding and keep the node fail-closed until then.
            or current.get("reviewer_started") is True
        ):
            return False
        try:
            binding = self._load_task_binding(snapshot, node_id, "developer")
            generation = int(current.get("developer_generation", 0))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return False
        if (
            binding.status != "delivered"
            or not binding.task_id
            or binding.task_id != current.get("developer_identity")
            or generation <= 0
            or binding.generation <= 0
            or binding.generation > generation
        ):
            return False
        current["retryable_action"] = {
            "role": "developer",
            "phase": "rework",
            "continuation": True,
            "pending_schedule": True,
            "successor_candidate": False,
        }
        current["pair_archived"] = True
        current["quarantine"] = None
        current["reason"] = "quarantined delivered continuation recovered"
        if record_event:
            self._record(
                snapshot,
                "quarantine_continuation_recovered",
                {
                    "run_id": snapshot.run_id,
                    "node_id": node_id,
                    "role": "developer",
                    "task_id": binding.task_id,
                    "generation": generation,
                },
            )
        return True

    @staticmethod
    def _identity_from_contract(node: DAGNode, role: str) -> Optional[str]:
        for key in (
            role + "_task_id",
            role + "_platform_task_id",
            role + "_threadId",
            role + "_thread_id",
        ):
            value = node.contract.get(key)
            if value:
                return str(value)
        nested = node.contract.get(role + "_task")
        if isinstance(nested, dict):
            for key in ("task_id", "platform_task_id", "threadId", "thread_id"):
                if nested.get(key):
                    return str(nested[key])
        if role == "developer":
            for key in ("task_id", "platform_task_id", "threadId", "thread_id"):
                if node.contract.get(key):
                    return str(node.contract[key])
        return None

    def _prove_old_task_stopped_or_absent(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        retry: Dict[str, Any],
    ) -> Optional[str]:
        """Return the predecessor identity only when a successor is safe.

        ``None`` means the old task remains ambiguous.  An empty string is a
        durable absence proof: there is no current-run registry binding and
        no provider request that could have created a task.  A terminal
        binding is accepted only for an already provider-confirmed terminal
        state; running/blocked bindings never authorize a new writer.
        """
        current = snapshot.nodes[node_id]
        if retry.get("successor_candidate") is not True:
            return None
        if (
            current.get("active_task") is not None
            or node_id in snapshot.handles
            or current.get("start_intent") is not None
        ):
            return None
        role = retry.get("role")
        if role not in {"developer", "reviewer"}:
            return None
        try:
            binding = self._load_task_binding(snapshot, node_id, str(role))
        except FileNotFoundError:
            try:
                requested = ProviderActionStore(self.paths).has_request(
                    snapshot.run_id, node_id, str(role)
                )
            except (OSError, TypeError, ValueError):
                return None
            return None if requested else ""
        except (OSError, TypeError, ValueError):
            return None
        if binding.status in {"stopped", "failed", "archived"}:
            return binding.task_id or ""
        return None

    def _reject_replayed_old_task_reconciliation(
        self, snapshot: RunSnapshot, node_id: str, data: Dict[str, Any]
    ) -> None:
        """Consume an untrusted reconciliation event without starting a writer."""
        current = snapshot.nodes[node_id]
        current["status"] = "blocked_unknown"
        current["reason"] = "replayed old task reconciliation proof is invalid"
        current["quarantine"] = {
            "run_id": snapshot.run_id,
            "handle_id": snapshot.handles.get(node_id),
            "reason": current["reason"],
        }
        current["old_task_reconciled"] = False
        current["retryable_action"] = None

    def _schedule_ready(
        self,
        snapshot: RunSnapshot,
        runner: Runner,
        recover_missing_reviewer: bool = False,
    ) -> None:
        record = self._require_snapshot_authorization(snapshot)
        active_pairs = sum(
            1
            for node_id, current in snapshot.nodes.items()
            if current.get("status") not in {"stopped", "failed"}
            and not current.get("pair_archived")
            and (
                int(current.get("developer_generation", 0)) > 0
                or current.get("retryable_action") is not None
                or isinstance(current.get("active_task"), dict)
                or node_id in snapshot.handles
            )
        )
        for node_id, node in self.nodes.items():
            current = snapshot.nodes[node_id]
            was_active_pair = not current.get("pair_archived") and (
                int(current.get("developer_generation", 0)) > 0
                or current.get("retryable_action") is not None
            )
            if self._recover_quarantined_delivered_developer(snapshot, node_id):
                if was_active_pair:
                    active_pairs -= 1
            if recover_missing_reviewer:
                self._recover_missing_reviewer_successor(snapshot, node_id)
            retry = current.get("retryable_action")
            if isinstance(retry, dict) and retry.get("same_task") is True:
                # Unknown side effects are never eligible for successor
                # creation, even if an older persisted marker said so.
                if retry.get("successor") is not False or retry.get("successor_candidate") is True:
                    retry = dict(retry)
                    retry["successor"] = False
                    retry["successor_candidate"] = False
                    current["retryable_action"] = retry
            if (
                current.get("status") in {"blocked_unknown", "running"}
                and isinstance(retry, dict)
                and node_id not in snapshot.handles
            ):
                # A delivered developer handoff is a verified continuation
                # even when an interrupted reauthorization left a stale
                # quarantine marker behind.  Normalize the legacy marker
                # before applying the fail-closed quarantine branch so the
                # original visible task can be resumed in place.
                resumable_developer = (
                    retry.get("continuation")
                    and retry.get("role") == "developer"
                    and not self._continuation_requires_successor(
                        snapshot, node_id, "developer"
                    )
                )
                if resumable_developer:
                    retry = dict(retry)
                    retry["successor_candidate"] = False
                    current["retryable_action"] = retry
                    if (
                        isinstance(current.get("quarantine"), dict)
                        and current.get("active_task") is None
                    ):
                        current["quarantine"] = None
                if (
                    current.get("status") == "blocked_unknown"
                    and isinstance(current.get("quarantine"), dict)
                    and retry.get("successor_candidate") is not True
                    and retry.get("same_task") is not True
                    and current.get("active_task") is None
                    and not resumable_developer
                ):
                    current["retryable_action"] = None
                    current["reason"] = (
                        "quarantined task has no proven active binding to resume"
                    )
                    continue
                if retry.get("successor_candidate") is True and retry.get(
                    "continuation"
                ):
                    predecessor = self._prove_old_task_stopped_or_absent(
                        snapshot, node_id, retry
                    )
                    if predecessor is None:
                        current["status"] = "blocked_unknown"
                        current["reason"] = (
                            "old task stop or absence cannot be proven for successor"
                        )
                        continue
                    retry = dict(retry)
                    retry["continuation"] = False
                    retry["successor"] = True
                    retry["predecessor_task_id"] = predecessor or None
                    retry["old_task_reconciled"] = True
                    current["retryable_action"] = retry
                    current["old_task_reconciled"] = True
                    self._record(
                        snapshot,
                        "old_task_reconciled",
                        {
                            "run_id": snapshot.run_id,
                            "node_id": node_id,
                            "role": retry["role"],
                            "predecessor_task_id": predecessor or None,
                            "proof": "absent" if not predecessor else "stopped",
                        },
                    )
                pending_schedule = retry.get("pending_schedule") is True
                reviewer_recovery = retry.get("missing_binding_recovery") is True
                if pending_schedule and (
                    active_pairs >= record.active_pair_limit
                    or not all(
                        snapshot.nodes[dependency].get("status") == "accepted"
                        for dependency in node.depends_on
                    )
                ) and not reviewer_recovery:
                    continue
                if not acquire_writer_lease(
                    self.paths,
                    node_id,
                    str(current["worktree"]),
                    snapshot.run_id,
                ):
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "writer lease is already owned by another run",
                    )
                    continue
                if pending_schedule:
                    current["pair_archived"] = False
                    active_pairs += 1
                if self._start_task(
                    snapshot,
                    node_id,
                    str(retry["role"]),
                    str(retry["phase"]),
                    runner,
                    bool(retry.get("continuation")),
                    bool(retry.get("successor")),
                ):
                    if current.get("status") != "blocked_unknown":
                        current["retryable_action"] = None
                continue
            if current.get("status") == "delivered" and not current.get("reviewer_started"):
                if not acquire_writer_lease(
                    self.paths,
                    node_id,
                    str(current["worktree"]),
                    snapshot.run_id,
                ):
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "writer lease is already owned by another run",
                    )
                    continue
                self._start_task(snapshot, node_id, "reviewer", "review", runner, False)
                continue
            if current.get("status") != "planned":
                continue
            if not self._brief_allows_first_write(snapshot, node_id, node):
                continue
            if active_pairs >= record.active_pair_limit:
                continue
            if not all(
                snapshot.nodes[dependency].get("status") == "accepted"
                for dependency in node.depends_on
            ):
                continue
            if not acquire_writer_lease(
                self.paths,
                node_id,
                str(current["worktree"]),
                snapshot.run_id,
            ):
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "writer lease is already owned by another run",
                )
                continue
            if self._start_task(
                snapshot, node_id, "developer", "develop", runner, False
            ):
                active_pairs += 1

    def _brief_allows_first_write(
        self, snapshot: RunSnapshot, node_id: str, node: DAGNode
    ) -> bool:
        """Require a valid V3.8 implementation brief before a first write.

        The gate is deliberately checked before acquiring a writer lease or
        invoking the provider.  Nodes without the opt-in contract retain the
        legacy scheduling path.
        """
        if node.contract.get("brief_required") is not True:
            return True
        current = snapshot.nodes[node_id]
        raw = node.contract.get("implementation_brief")
        try:
            brief = ImplementationBrief.from_mapping(raw)
            validation = validate_implementation_brief(
                brief, RunManifest.from_mapping({
                    "plan_id": snapshot.plan_id,
                    "plan_revision": snapshot.plan_version,
                    "run_id": snapshot.run_id,
                    # A missing contract base is itself a mismatch.  Use a
                    # non-zero sentinel only to let the validator report the
                    # concrete ``base_sha`` difference without accepting it.
                    "base_sha": node.contract.get("base_sha", "f" * 40),
                    "target_branch": str(current.get("branch", "")),
                    "execution_epoch": int(node.contract.get("execution_epoch", 0)),
                    "authorization_digest": snapshot.authorization_digest,
                    "evidence_ref": "brief-gate",
                }),
                node,
                project_root=self.paths.root,
            )
        except (TypeError, ValueError, OSError) as error:
            validation = None
            missing = ["schema:" + type(error).__name__]
            evidence = {"status": "brief_pending", "checks": {}}
        else:
            missing = validation.missing
            evidence = validation.evidence
        if missing:
            current["status"] = "brief_pending"
            current["brief_evidence"] = {
                "missing": sorted(set(missing)),
                "checks": evidence.get("checks", {}),
            }
            current["reason"] = "implementation brief is not valid"
            return False
        return True

    def _recover_missing_reviewer_successor(
        self, snapshot: RunSnapshot, node_id: str
    ) -> None:
        """Queue a reviewer successor only from a fully proven recovery state.

        A historical ``reviewer_started`` flag is not proof that this run has
        a reviewer task to continue. Recovery may create a successor only when
        the delivered developer binding is current, the reviewer binding is
        explicitly absent, and neither a provider request nor an active
        task/handle/start intent could represent an unresolved side effect.
        """
        current = snapshot.nodes[node_id]
        if current.get("status") != "blocked_unknown":
            return
        if current.get("reviewer_started") is not True:
            return
        if current.get("retryable_action") is not None:
            return
        if (
            current.get("active_task") is not None
            or current.get("active_role") is not None
            or current.get("start_intent") is not None
            or node_id in snapshot.handles
        ):
            return

        try:
            developer = self._load_task_binding(snapshot, node_id, "developer")
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return

        try:
            self._load_task_binding(snapshot, node_id, "reviewer")
        except FileNotFoundError:
            pass
        except (OSError, TypeError, ValueError):
            return
        else:
            return

        try:
            requested = ProviderActionStore(self.paths).has_request(
                snapshot.run_id, node_id, "reviewer"
            )
        except (OSError, TypeError, ValueError):
            return
        if requested:
            return

        try:
            developer_generation = int(current.get("developer_generation", 0))
        except (TypeError, ValueError):
            return
        if (
            developer.status != "delivered"
            or developer.task_id != current.get("developer_identity")
            or developer.generation != developer_generation
        ):
            return

        predecessor = current.get("reviewer_identity")
        if predecessor is not None and (
            not isinstance(predecessor, str) or not predecessor
        ):
            return
        current["retryable_action"] = {
            "role": "reviewer",
            "phase": "review",
            "continuation": False,
            "successor": True,
            "successor_candidate": True,
            "pending_schedule": True,
            "missing_binding_recovery": True,
            "predecessor_task_id": predecessor,
        }
        current["reason"] = "reviewer successor queued after missing binding recovery"

    def _prepare_worker_profile(
        self, node: DAGNode, contract: Dict[str, Any], current: Dict[str, Any], role: str
    ) -> Optional[WorkerProfile]:
        """Resolve an evidence-bound profile for the real dispatch path.

        Legacy nodes keep their configured profile/default behavior.  A node
        that declares routing inputs must provide a complete IssueComplexity
        and structured LocalModel probes; no default model is synthesized.
        """
        routing_keys = {"routing_required", "issue_complexity", "model_probes", "models", "required_capabilities"}
        routing_requested = any(key in contract for key in routing_keys)
        raw = contract.get("worker_profile")
        if raw is None and isinstance(contract.get("child_binding"), dict):
            raw = contract["child_binding"].get("worker_profile")
        if role != "developer" and raw is None:
            raw = current.get("contract_overrides", {}).get("worker_profile")
        if raw is not None:
            try:
                profile = raw if isinstance(raw, WorkerProfile) else WorkerProfile.from_dict(raw)
            except (TypeError, ValueError) as error:
                raise ValueError("worker model profile is invalid") from error
            issue_data = contract.get("issue_complexity")
            probes = contract.get("model_probes", contract.get("models"))
            if routing_requested and contract.get("routing_required") is True:
                # An explicitly routed node cannot silently accept an
                # unrelated preconfigured/default profile.
                if issue_data is None or probes is None:
                    raise ValueError("worker model routing evidence is incomplete")
            if routing_requested and issue_data is not None and probes is not None:
                issue = issue_data if isinstance(issue_data, IssueComplexity) else IssueComplexity.from_dict(issue_data)
                if not isinstance(probes, (list, tuple)):
                    raise TypeError("model probes must be a list")
                models = [item if isinstance(item, LocalModel) else LocalModel.from_dict(item) for item in probes]
                expected = ModelRouter(worker=profile.worker).select(
                    issue, contract.get("required_capabilities", []), models
                )
                if (
                    profile.model != expected.model
                    or profile.reasoning != expected.reasoning
                    or profile.route_digest != expected.route_digest
                ):
                    raise ValueError("worker model profile conflicts with routing evidence")
            return profile
        if not routing_requested:
            return None
        issue_data = contract.get("issue_complexity")
        probes = contract.get("model_probes", contract.get("models"))
        if issue_data is None or probes is None:
            raise ValueError("worker model routing evidence is incomplete")
        issue = issue_data if isinstance(issue_data, IssueComplexity) else IssueComplexity.from_dict(issue_data)
        if not isinstance(probes, (list, tuple)):
            raise TypeError("model probes must be a list")
        models = [item if isinstance(item, LocalModel) else LocalModel.from_dict(item) for item in probes]
        profile = ModelRouter(worker=str(contract.get("worker") or node.contract.get("worker") or "developer")).select(
            issue, contract.get("required_capabilities", []), models
        )
        profile = WorkerProfile(
            profile.worker,
            profile.model,
            profile.reasoning,
            profile.fallbacks,
            profile.selection_basis,
            worktree=str(contract.get("worktree") or current.get("worktree", "")),
            branch=str(contract.get("branch") or current.get("branch", "")),
            allowlist=list(contract.get("files", [])),
            writer=str(contract.get("writer") or profile.worker),
            route_digest=profile.route_digest,
        )
        current.setdefault("contract_overrides", {})["worker_profile"] = profile.to_dict()
        return profile

    def _start_task(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        phase: str,
        runner: Runner,
        continuation: bool,
        successor: bool = False,
    ) -> bool:
        record = self._require_snapshot_authorization(snapshot)
        node = self.nodes[node_id]
        current = snapshot.nodes[node_id]
        identity_key = role + "_identity"
        predecessor_identity = current.get(identity_key) or "{}:{}".format(
            role, node_id
        )
        identity = predecessor_identity
        generation_key = "review_generation" if role == "reviewer" else "developer_generation"
        generation = int(current.get(generation_key, 0)) + 1
        if successor:
            identity = "{}:successor:{}".format(predecessor_identity, generation)
        current[generation_key] = generation
        if role == "reviewer":
            current["reviewer_started"] = True

        contract = dict(node.contract)
        contract.update(current.get("contract_overrides", {}))
        contract.update(
            {
                "node_id": node_id,
                "role": role,
                "worker": (
                    node.contract.get("reviewer_worker")
                    if role == "reviewer"
                    else current.get("worker")
                ),
                "phase": phase,
                "task_id": identity,
                "generation": generation,
                "continuation": continuation,
                "successor": successor,
                "action": phase,
                "run_id": snapshot.run_id,
                "consistency_binding": self._consistency_binding(record, node),
            }
        )
        retry = current.get("retryable_action")
        if successor:
            predecessor_task_id = None
            if isinstance(retry, dict):
                predecessor_task_id = retry.get("predecessor_task_id")
            if not predecessor_task_id:
                predecessor_task_id = current.get(identity_key)
            contract["predecessor_task_id"] = predecessor_task_id
        contract.setdefault("worktree", str(current["worktree"]))
        contract.setdefault("branch", str(current["branch"]))
        contract.setdefault(
            "files", list((contract.get("worker_profile") or {}).get("allowlist", []))
        )
        routing_requested = any(
            key in contract
            for key in ("routing_required", "issue_complexity", "model_probes", "models", "required_capabilities")
        )
        profile = self._prepare_worker_profile(node, contract, current, role)
        if profile is not None:
            contract["worker_profile"] = profile.to_dict()
            if routing_requested:
                contract["routing_required"] = True
        if binding_contract_enabled(contract):
            has_live_evidence = isinstance(contract.get("binding_intent"), BindingIntent) and isinstance(
                contract.get("binding_observation"), BindingObservation
            )
            if not has_live_evidence:
                if continuation:
                    contract.setdefault("binding_bootstrap", True)
                    current["binding_phase"] = "binding_repair_pending"
                else:
                    contract.setdefault("binding_probe", True)
                    current["binding_phase"] = "binding_probe_pending"
            else:
                current["binding_phase"] = "binding_verified"
        if snapshot.capability_contract_digest:
            contract["capability_contract_digest"] = snapshot.capability_contract_digest
        if (self.paths.vibe / "state.json").is_file():
            try:
                v2 = json.loads((self.paths.vibe / "state.json").read_text(encoding="utf-8")).get("workflow_version") == 2
            except (OSError, ValueError, json.JSONDecodeError):
                v2 = True
            if v2:
                profile_data = contract.get("worker_profile") or {}
                if not profile_data:
                    profile_data = {"worker": str(contract.get("worker", "worker")), "model": "default", "reasoning": "normal", "fallbacks": [], "selection_basis": {"issue_complexity_ref": node_id, "complexity_band": "standard", "risk_tags": [], "availability_evidence": "runtime"}, "writer": str(contract.get("worker", "writer")), "worktree": str(contract.get("worktree", ".")), "branch": str(contract.get("branch", "branch-" + node_id)), "allowlist": list(contract.get("files", [node_id + ".py"]))}
                profile = WorkerProfile(**profile_data)
                validate_child_session_binding(snapshot.run_id, str(self.plan.version), record.digest, node_id, role, profile)
                contract["child_origin"] = "worker_dispatch"
                contract["child_binding"] = {"parent_run_id": snapshot.run_id, "plan_revision": str(self.plan.version), "authorization_digest": record.digest, "node_id": node_id, "role": role, "writer": profile.writer, "worktree": profile.worktree, "branch": profile.branch, "allowlist": profile.allowlist, "worker_profile": profile.to_dict(), "model": profile.model, "reasoning": profile.reasoning, "route_digest": profile.route_digest, "capability_contract_digest": snapshot.capability_contract_digest}
        try:
            contract = validate_runtime_contract(
                contract,
                authorized_actions=record.allowed_actions,
                authorized_files=record.file_scope,
            )
            if self._binding_recovery_requested(contract):
                self._preflight_binding_recovery(
                    snapshot, node_id, role, contract, current
                )
            else:
                self._preflight_binding_before_provider(contract)
            self._run_binding_bootstrap(snapshot, node_id, role, contract, runner, current)
            binding = self._binding_for(
                snapshot,
                node_id,
                role,
                identity,
                generation,
                "start_pending",
                runner,
                contract,
                continuation,
                successor,
            )
            self._require_binding_gate(contract, binding, runner)
            if binding_contract_enabled(contract):
                # Registry persistence intentionally strips private provenance
                # tokens.  Carry the live, verified objects through this
                # in-process call so ProviderActionRunner.start() rechecks the
                # same evidence instead of trusting a downgraded JSON row.
                contract["binding_intent"] = binding.binding_intent
                contract["binding_observation"] = binding.binding_observation
            identity = str(binding.task_id)
            current[identity_key] = identity
            contract["task_id"] = identity
            self._save_task_binding(binding)
        except ProviderPending as error:
            current[generation_key] = generation - 1
            if role == "reviewer" and generation == 1:
                current["reviewer_started"] = False
            current["retryable_action"] = {
                "role": role,
                "phase": phase,
                "continuation": continuation,
                "successor": False,
                "same_task": True,
            }
            # An asynchronous bridge response is a retry condition, not
            # evidence that the provider capability is unavailable.
            current["status"] = "blocked_unknown" if successor else "running"
            if binding_contract_enabled(contract):
                current["binding_phase"] = "retry_pending"
            current["reason"] = str(error)
            if successor:
                current["active_task"] = None
                current["active_role"] = None
                current["start_intent"] = None
                self._mark_blocked_unknown(snapshot, node_id, str(error))
                save_snapshot(self.paths, snapshot)
            return False
        except ProviderUnavailable as error:
            current[generation_key] = generation - 1
            if role == "reviewer" and generation == 1:
                current["reviewer_started"] = False
            current["retryable_action"] = {
                "role": role,
                "phase": phase,
                "continuation": continuation,
                "successor": False,
                "same_task": True,
            }
            current["active_task"] = None
            current["active_role"] = None
            current["start_intent"] = None
            self._mark_blocked_unknown(snapshot, node_id, str(error))
            if binding_contract_enabled(contract):
                current["binding_phase"] = "binding_repair_pending"
            save_snapshot(self.paths, snapshot)
            return False
        except (OSError, TypeError, ValueError) as error:
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "task binding rejected before start ({})".format(
                    type(error).__name__
                ),
            )
            return False

        snapshot.tasks["{}:{}".format(node_id, role)] = binding.to_dict()
        intent = {
            "intent_id": uuid.uuid4().hex,
            "role": role,
            "phase": phase,
            "task_id": identity,
            "generation": generation,
        }
        current["status"] = "start_pending"
        if binding_contract_enabled(contract) and current.get("binding_phase") is None:
            current["binding_phase"] = "binding_verified"
        current["active_role"] = role
        current["active_task"] = {
            "role": role,
            "task_id": identity,
            "generation": generation,
            "handle_id": None,
        }
        current["start_intent"] = intent
        self._record(
            snapshot,
            "start_intent",
            {"run_id": snapshot.run_id, "node_id": node_id, **intent},
            current["active_task"],
        )
        # The intent and lease are durable before the external side effect.
        save_snapshot(self.paths, snapshot)
        self._require_snapshot_authorization(snapshot)
        try:
            handle = runner.start(contract, self._worktree_path(current))
        except Exception as error:
            if successor:
                current["retryable_action"] = {
                    "role": role,
                    "phase": phase,
                    "continuation": continuation,
                    "successor": False,
                    "same_task": True,
                }
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "worker start outcome is unknown ({})".format(
                    type(error).__name__
                ),
            )
            save_snapshot(self.paths, snapshot)
            return False

        if (
            not isinstance(handle.run_id, str)
            or not handle.run_id
            or handle.run_id in snapshot.handles.values()
        ):
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "provider returned a duplicate or invalid active handle",
            )
            save_snapshot(self.paths, snapshot)
            return False

        current["status"] = self._running_status(role, phase)
        if binding_contract_enabled(contract):
            current["binding_phase"] = "business_work_allowed"
        current["active_task"]["handle_id"] = handle.run_id
        current["start_intent"] = None
        current["quarantine"] = None
        snapshot.handles[node_id] = handle.run_id
        self._record(
            snapshot,
            "start_confirmed",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "role": role,
                "phase": phase,
                "task_id": identity,
                "generation": generation,
                "handle_id": handle.run_id,
            },
            current["active_task"],
        )
        try:
            self._set_binding_status(
                snapshot, node_id, role, self._running_status(role, phase)
            )
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "confirmed task binding cannot be updated ({})".format(
                    type(error).__name__
                ),
            )
            save_snapshot(self.paths, snapshot)
            return False
        save_snapshot(self.paths, snapshot)
        pending = getattr(runner, "is_pending", None)
        if callable(pending) and pending(handle):
            # Keep the active lease, but expose the unresolved bridge action
            # as unknown until a provider-confirmed result is observed.  The
            # handle remains durable so the next tick can poll it; no second
            # writer is scheduled while this side effect is pending.
            current["status"] = "blocked_unknown"
            current["quarantine"] = {
                "run_id": snapshot.run_id,
                "handle_id": handle.run_id,
                "reason": "provider action pending; result not yet confirmed",
            }
            current["reason"] = "provider action pending; result not yet confirmed"
            save_snapshot(self.paths, snapshot)
        return True

    @staticmethod
    def _running_status(role: str, phase: str) -> str:
        if role == "reviewer":
            return "review"
        if phase == "rework":
            return "rework"
        return "running"

    def _binding_for(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        identity: str,
        generation: int,
        status: str,
        runner: Runner,
        contract: Dict[str, Any],
        continuation: bool,
        successor: bool = False,
    ) -> TaskBinding:
        current = snapshot.nodes[node_id]
        previous_binding = None
        if continuation:
            try:
                previous_binding = self._load_task_binding(snapshot, node_id, role)
            except FileNotFoundError:
                previous_binding = None
            if previous_binding is None or previous_binding.task_id != identity:
                raise ValueError("continuation task identity is stale")
        observed_binding = getattr(runner, "task_binding", None)
        if callable(observed_binding):
            binding = observed_binding(
                contract,
                self._worktree_path(current),
                snapshot.run_id,
                status,
            )
            if not isinstance(binding, TaskBinding):
                raise ValueError("runner returned an invalid observed task binding")
            if (
                binding.issue_id != node_id
                or binding.role != role
                or binding.run_id != snapshot.run_id
                or binding.generation != generation
                or (continuation and binding.task_id != identity)
            ):
                raise ValueError("observed task binding does not match runtime scope")
        else:
            binding = TaskBinding(
                provider="runner",
                mode="background",
                issue_id=node_id,
                role=role,
                task_id=identity,
                host=None,
                worktree=str(current["worktree"]),
                branch=str(current["branch"]),
                status_file=str(current.get("status_file", "")),
                handoff_file=str(current.get("handoff_file", "")),
                run_id=snapshot.run_id,
                status=status,
                generation=generation,
                allowlist=list(contract.get("files", [])),
                capability_contract_digest=contract.get("capability_contract_digest"),
                successor_of=contract.get("predecessor_task_id") if successor else None,
            )
        if successor:
            predecessor = contract.get("predecessor_task_id")
            if predecessor and binding.task_id == predecessor:
                raise ValueError("successor task identity reused the predecessor")
            if predecessor and binding.successor_of not in {None, predecessor}:
                raise ValueError("successor task predecessor does not match contract")
            if binding.successor_of is None:
                binding.successor_of = predecessor
        expected_capability_digest = contract.get("capability_contract_digest")
        if expected_capability_digest:
            if binding.capability_contract_digest not in {
                None,
                expected_capability_digest,
            } and not continuation:
                raise ValueError("task binding capability contract digest is stale")
            binding.capability_contract_digest = expected_capability_digest
        expected_allowlist = list(contract.get("files", []))
        if expected_allowlist:
            if binding.allowlist:
                if binding.allowlist != expected_allowlist:
                    if not continuation or not set(binding.allowlist).issubset(
                        set(expected_allowlist)
                    ):
                        raise ValueError("task binding allowlist does not match contract")
                    binding.allowlist = expected_allowlist
            else:
                binding.allowlist = expected_allowlist
        profile_data = contract.get("worker_profile")
        if profile_data is not None:
            profile = profile_data if isinstance(profile_data, WorkerProfile) else WorkerProfile.from_dict(profile_data)
            for field_name, expected in (
                ("route_digest", profile.route_digest),
                ("model", profile.model),
                ("reasoning", profile.reasoning),
            ):
                current_value = getattr(binding, field_name, None)
                if current_value not in (None, "", expected):
                    raise ValueError("task binding {} does not match worker profile".format(field_name))
                setattr(binding, field_name, expected)
        expected_worktree = self._worktree_path(current)
        observed_worktree = Path(str(binding.worktree))
        if not observed_worktree.is_absolute():
            observed_worktree = self.paths.root / observed_worktree
        if (
            observed_worktree.resolve() != expected_worktree.resolve()
            or binding.branch != str(current["branch"])
        ):
            raise ValueError("task binding worktree or branch does not match contract")
        if continuation and previous_binding is not None:
            if (
                binding.cursor is not None
                and binding.cursor != previous_binding.cursor
            ):
                raise ValueError("continuation cursor is stale")
            binding.cursor = previous_binding.cursor
        return binding

    def _set_binding_status(
        self, snapshot: RunSnapshot, node_id: str, role: str, status: str
    ) -> TaskBinding:
        binding = self._load_task_binding(snapshot, node_id, role)
        current = snapshot.nodes[node_id]
        generation_key = (
            "review_generation" if role == "reviewer" else "developer_generation"
        )
        if (
            binding.task_id != current.get(role + "_identity")
            or binding.generation != current.get(generation_key)
        ):
            raise ValueError("task binding identity or generation is stale")
        binding.status = status
        self._save_task_binding(binding)
        snapshot.tasks["{}:{}".format(node_id, role)] = binding.to_dict()
        return binding

    def _registered_active_binding(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        provenance: Dict[str, Any],
    ) -> TaskBinding:
        current = snapshot.nodes[node_id]
        active = current.get("active_task")
        if not isinstance(active, dict):
            raise ValueError("event has no active registered task")
        if any(
            provenance.get(key) != active.get(key)
            for key in ("role", "task_id", "handle_id", "generation")
        ):
            raise ValueError("event provenance is stale or unregistered")
        if snapshot.handles.get(node_id) != active.get("handle_id"):
            raise ValueError("event handle is not the active run handle")
        binding = self._load_task_binding(snapshot, node_id, str(active["role"]))
        if (
            binding.task_id != active.get("task_id")
            or binding.generation != active.get("generation")
        ):
            raise ValueError("event task binding is stale")
        return binding

    def _apply_event(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        handle_id: str,
        event: RunEvent,
        runner: Runner,
    ) -> None:
        self._require_snapshot_authorization(snapshot)
        current = snapshot.nodes[node_id]
        active = current.get("active_task")
        if not isinstance(active, dict) or active.get("handle_id") != handle_id:
            self._mark_blocked_unknown(
                snapshot, node_id, "event arrived from an unregistered handle"
            )
            return
        if not self._event_claims_match(event, node_id, active):
            self._mark_blocked_unknown(
                snapshot, node_id, "event provenance conflicts with the registered task"
            )
            return

        evidence = event.data.get("evidence") or event.data.get("finding")
        if evidence is not None:
            current.setdefault("evidence", []).append(redact_provider_text(evidence))
        role = active["role"]
        if event.event not in {
            "unknown",
            "timeout",
            "state_unknown",
            "visibility_unknown",
        }:
            # A provider-confirmed event consumes any reauthorization retry
            # marker; the active handle now carries the durable continuation.
            current["retryable_action"] = None

        if event.event in {"delivered", "complete"}:
            self._record_runner_event(snapshot, node_id, event, active)
            if role != "developer":
                try:
                    self._set_binding_status(snapshot, node_id, role, "review")
                except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "task binding cannot record review state ({})".format(
                            type(error).__name__
                        ),
                    )
                    return
                current["status"] = "review"
                return
            try:
                self._set_binding_status(snapshot, node_id, role, "delivered")
            except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "task binding cannot record delivery ({})".format(
                        type(error).__name__
                    ),
                )
                return
            current["status"] = "delivered"
            current["active_role"] = None
            current["active_task"] = None
            snapshot.handles.pop(node_id, None)
            reviewer_continuation = False
            reviewer_successor = False
            if current.get("reviewer_started"):
                try:
                    reviewer_binding = self._load_task_binding(snapshot, node_id, "reviewer")
                except FileNotFoundError:
                    reviewer_successor = True
                except (OSError, TypeError, ValueError) as error:
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "reviewer task binding cannot be verified ({})".format(
                            type(error).__name__
                        ),
                    )
                    return
                else:
                    expected_reviewer_identity = current.get("reviewer_identity")
                    if (
                        not expected_reviewer_identity
                        or reviewer_binding.task_id != expected_reviewer_identity
                    ):
                        self._mark_blocked_unknown(
                            snapshot,
                            node_id,
                            "reviewer task binding identity is stale",
                        )
                        return
                    reviewer_continuation = True
            self._start_task(
                snapshot,
                node_id,
                "reviewer",
                "review",
                runner,
                reviewer_continuation,
                reviewer_successor,
            )
        elif event.event == "review_finding":
            if role != "reviewer":
                self._mark_blocked_unknown(
                    snapshot, node_id, "review finding did not come from the reviewer"
                )
                return
            self._record_runner_event(snapshot, node_id, event, active)
            if self._is_implementation_finding(event.data):
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                self._start_task(
                    snapshot, node_id, "developer", "rework", runner, True, False
                )
                return
            if not event.data.get("in_contract", False):
                record = self._snapshot_record(snapshot)
                resolution = resolve_consistency(
                    event.data.get("consistency"),
                    self.plan.decisions,
                    self.nodes[node_id].contract,
                    list(record.allowed_actions),
                    list(record.file_scope),
                    self._consistency_binding(record, self.nodes[node_id]),
                )
                if resolution is not None:
                    if not self._stop_active_for_transition(
                        snapshot, node_id, role, handle_id, runner, "stopped"
                    ):
                        return
                    current.setdefault("contract_overrides", {})[
                        resolution.field
                    ] = resolution.value
                    correction_values = {
                        "field": resolution.field,
                        "value": resolution.value,
                        "source": resolution.source,
                        "action": resolution.action,
                        "files": list(resolution.files),
                        "consistency_binding": dict(
                            resolution.consistency_binding
                        ),
                        "decision": (
                            dict(resolution.decision)
                            if resolution.decision is not None
                            else None
                        ),
                    }
                    correction = {
                        key: correction_values[key]
                        for key in CONSISTENCY_CORRECTION_KEYS
                    }
                    current.setdefault("corrections", []).append(correction)
                    self._record(
                        snapshot,
                        "consistency_corrected",
                        {
                            "run_id": snapshot.run_id,
                            "node_id": node_id,
                            **correction,
                        },
                    )
                    self._start_task(
                        snapshot, node_id, "developer", "rework", runner, True
                    )
                    return
                if not self._stop_active_for_transition(
                    snapshot,
                    node_id,
                    role,
                    handle_id,
                    runner,
                    "blocked_design",
                ):
                    return
                current["status"] = "blocked_design"
                current["reason"] = redact_provider_text(
                    event.data.get("finding", "out-of-contract finding")
                )
                current["old_task_reconciled"] = True
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                self._release_node_lease(snapshot, node_id)
                self._record(
                    snapshot,
                    "blocked_design",
                    {
                        "run_id": snapshot.run_id,
                        "node_id": node_id,
                        "reason": current["reason"],
                        "old_task_reconciled": True,
                    },
                )
                return
            current["active_role"] = None
            current["active_task"] = None
            snapshot.handles.pop(node_id, None)
            self._start_task(snapshot, node_id, "developer", "rework", runner, True)
        elif event.event == "accepted":
            if role != "reviewer":
                self._mark_blocked_unknown(
                    snapshot, node_id, "acceptance did not come from the reviewer"
                )
                return
            try:
                registered = self._load_task_binding(snapshot, node_id, "reviewer")
            except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "reviewer binding cannot be verified ({})".format(
                        type(error).__name__
                    ),
                )
                return
            if (
                registered.task_id != active["task_id"]
                or registered.generation != active["generation"]
            ):
                self._mark_blocked_unknown(
                    snapshot, node_id, "reviewer identity or generation is stale"
                )
                return
            acceptance_event = RunEvent(
                event.event,
                {
                    **event.data,
                    "contract_digest": current["contract_digest"],
                    "authorization_epoch": snapshot.authorization_digest,
                },
            )
            self._record_runner_event(snapshot, node_id, acceptance_event, active)
            try:
                self._set_binding_status(snapshot, node_id, role, "accepted")
            except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "task binding cannot record acceptance ({})".format(
                        type(error).__name__
                    ),
                )
                return
            current["status"] = "accepted"
            current["acceptance"] = {
                "contract_digest": current["contract_digest"],
                "authorization_epoch": snapshot.authorization_digest,
            }
            current["active_role"] = None
            current["active_task"] = None
            current["quarantine"] = None
            snapshot.handles.pop(node_id, None)
            if evidence is None:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "review acceptance has no registered P0-P2 clearance evidence",
                )
                return
            current["review_clearance"] = {"p0": 0, "p1": 0, "p2": 0}
            self._archive_pair(snapshot, node_id)
            self._release_node_lease(snapshot, node_id)
        elif event.event in {"unknown", "timeout", "state_unknown", "visibility_unknown"}:
            self._queue_active_retry(
                snapshot,
                node_id,
                str(redact_provider_text(event.data.get("reason", event.event))),
            )
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                str(redact_provider_text(event.data.get("reason", event.event))),
                quarantine_lease=False,
                retryable_same_task=True,
            )
        elif event.event in {"failed", "stopped", "terminal_failed"}:
            self._record_runner_event(snapshot, node_id, event, active)
            terminal_status = "failed" if event.event != "stopped" else "stopped"
            try:
                self._set_binding_status(snapshot, node_id, role, terminal_status)
            except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "task binding cannot record terminal state ({})".format(
                        type(error).__name__
                    ),
                )
                return
            current["status"] = terminal_status
            current["reason"] = redact_provider_text(
                event.data.get("reason", event.event)
            )
            current["active_role"] = None
            current["active_task"] = None
            current["quarantine"] = None
            snapshot.handles.pop(node_id, None)
            self._release_node_lease(snapshot, node_id)
        else:
            self._mark_blocked_unknown(
                snapshot, node_id, "unrecognized runner event: " + event.event
            )

    def _stop_active_for_transition(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        handle_id: str,
        runner: Runner,
        terminal_status: str,
    ) -> bool:
        try:
            runner.stop(RunHandle(handle_id))
            self._set_binding_status(snapshot, node_id, role, terminal_status)
        except Exception as error:
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "old task stop cannot be proven ({})".format(
                    type(error).__name__
                ),
            )
            return False
        snapshot.handles.pop(node_id, None)
        snapshot.nodes[node_id]["active_role"] = None
        snapshot.nodes[node_id]["active_task"] = None
        return True

    def _archive_pair(self, snapshot: RunSnapshot, node_id: str) -> None:
        current = snapshot.nodes[node_id]
        for role in ("developer", "reviewer"):
            self._set_binding_status(snapshot, node_id, role, "archived")
        current["pair_archived"] = True
        self._record(
            snapshot,
            "pair_archived",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "clearance": current["review_clearance"],
            },
        )

    @staticmethod
    def _event_claims_match(
        event: RunEvent, node_id: str, active: Dict[str, Any]
    ) -> bool:
        claims = {
            "node_id": node_id,
            "role": active.get("role"),
            "task_id": active.get("task_id"),
            "handle_id": active.get("handle_id"),
            "generation": active.get("generation"),
        }
        return all(event.data.get(key) == value for key, value in claims.items())

    @staticmethod
    def _is_implementation_finding(data: Dict[str, Any]) -> bool:
        """Recognize an explicit P0-P2 implementation defect from review data."""
        values = []
        if isinstance(data, dict):
            values.append(data.get("severity"))
            for key in ("finding", "review_finding"):
                value = data.get(key)
                if isinstance(value, dict):
                    values.append(value.get("severity"))
                    nested = value.get("finding")
                    if isinstance(nested, dict):
                        values.append(nested.get("severity"))
        for value in values:
            if isinstance(value, str):
                severity = value.strip().upper()
                if severity in {"P0", "P1", "P2"}:
                    return True
        return False

    def _record_runner_event(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        event: RunEvent,
        active: Dict[str, Any],
    ) -> None:
        data = dict(event.data)
        data.update({"run_id": snapshot.run_id, "node_id": node_id})
        self._record(snapshot, event.event, data, active)

    def _queue_active_retry(
        self, snapshot: RunSnapshot, node_id: str, reason: str
    ) -> None:
        """Record a retry intent without allocating a new task identity.

        A transient provider result is still unknown, but an active handle is
        durable evidence that the same task may be polled again.  Persist the
        logical retry alongside that handle so snapshot recovery can resume
        the original task even when the next poll happens in a new process.
        """
        current = snapshot.nodes[node_id]
        active = current.get("active_task")
        handle_id = snapshot.handles.get(node_id)
        if not isinstance(active, dict) or not isinstance(handle_id, str):
            return
        role = active.get("role")
        task_id = active.get("task_id")
        generation = active.get("generation")
        if role not in {"developer", "reviewer"} or not isinstance(task_id, str):
            return
        phase = "review" if role == "reviewer" else (
            "rework" if current.get("status") == "rework" else "develop"
        )
        previous = current.get("retryable_action")
        attempt = 1
        if isinstance(previous, dict) and previous.get("same_task") is True:
            try:
                attempt = int(previous.get("attempt", 0)) + 1
            except (TypeError, ValueError):
                attempt = 1
        current["retryable_action"] = {
            "role": role,
            "phase": phase,
            "continuation": True,
            "successor": False,
            "successor_candidate": False,
            "same_task": True,
            "task_id": task_id,
            "handle_id": handle_id,
            "generation": generation,
            "attempt": attempt,
            "reason": redact_provider_text(reason),
        }

    def _mark_blocked_unknown(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        reason: str,
        quarantine_lease: bool = True,
        retryable_same_task: bool = False,
    ) -> None:
        current = snapshot.nodes[node_id]
        active = current.get("active_task")
        handle_id = snapshot.handles.get(node_id)
        current["status"] = "blocked_unknown"
        current["reason"] = reason
        current["quarantine"] = {
            "run_id": snapshot.run_id,
            "handle_id": handle_id,
            "reason": reason,
        }
        if isinstance(active, dict) and active.get("role") in {"developer", "reviewer"}:
            try:
                self._set_binding_status(
                    snapshot, node_id, str(active["role"]), "blocked_unknown"
                )
            except (FileNotFoundError, OSError, TypeError, ValueError):
                pass
        if quarantine_lease:
            quarantine_writer_lease(
                self.paths,
                node_id,
                str(current.get("worktree", "")),
                snapshot.run_id,
                reason,
            )
        event_data = {
            "run_id": snapshot.run_id,
            "node_id": node_id,
            "reason": reason,
        }
        if retryable_same_task:
            event_data["retryable_same_task"] = True
        self._record(
            snapshot,
            "blocked_unknown",
            event_data,
            active if isinstance(active, dict) else None,
        )

    def _record(
        self,
        snapshot: RunSnapshot,
        event_name: str,
        data: Dict[str, Any],
        active: Optional[Dict[str, Any]] = None,
    ) -> None:
        provenance = {
            "role": active.get("role", "system") if active else "system",
            "task_id": active.get("task_id") if active else None,
            "handle_id": active.get("handle_id") if active else None,
            "generation": active.get("generation", 0) if active else 0,
            "authorization_digest": snapshot.authorization_digest,
            "node_contract_digest": snapshot.node_contract_digest,
        }
        snapshot.event_sequence = append_event(
            self.paths, RunEvent(event_name, data), provenance
        )

    def _context_estimate(self, snapshot: RunSnapshot, runner: Runner):
        """Obtain an evidence-bound context estimate when the runner exposes one.

        V1 runners do not expose a context limit and therefore retain the
        existing scheduling behavior.  V2 adapters may provide either a
        ``context_budget`` mapping or ``context_limit_tokens`` plus optional
        text fields; absence is intentionally not treated as zero.
        """
        policy = self.context_policy
        supplied = getattr(runner, "context_budget", None)
        if callable(supplied):
            supplied = supplied()
        if isinstance(supplied, dict):
            policy = policy or ContextBudgetPolicy(
                supplied.get("context_limit_tokens", "observed-model-limit"),
                supplied.get("reserve_tokens"),
                supplied.get("warning_ratio", 0.70),
                supplied.get("checkpoint_ratio", 0.80),
                supplied.get("hard_stop_ratio", 0.90),
            )
            fields = dict(getattr(runner, "context_input", {}) or {})
            fields.update(supplied)
        else:
            limit = getattr(runner, "context_limit_tokens", None)
            if limit is None and policy is None:
                return None
            policy = policy or ContextBudgetPolicy(limit)
            fields = getattr(runner, "context_input", {})
            if not isinstance(fields, dict):
                fields = {}
        estimator = ContextBudgetEstimator(policy)
        if "event_summary" not in fields:
            try:
                recent = load_events(self.paths, snapshot.run_id)[-5:]
                fields["event_summary"] = json.dumps(recent, ensure_ascii=False, sort_keys=True)
            except (OSError, ValueError, TypeError):
                fields["event_summary"] = ""
        if "checkpoint" not in fields:
            checkpoint_path = self.paths.root / ".vibe" / "runs" / snapshot.run_id / "monitor_checkpoint.json"
            try:
                fields["checkpoint"] = checkpoint_path.read_text(encoding="utf-8") if checkpoint_path.is_file() else ""
            except OSError:
                fields["checkpoint"] = ""
        return estimator.estimate(
            str(fields.get("system_prompt", "")),
            str(fields.get("current_input", "")),
            str(fields.get("event_summary", "")),
            str(fields.get("checkpoint", "")),
            str(fields.get("expected_output", "")),
            fields.get("tokenizer"),
        )

    def _checkpoint_context(self, snapshot: RunSnapshot, reason: str, exhausted: bool = False, estimate: Any = None) -> None:
        profiles = {}
        for node_id, node in self.nodes.items():
            profile = node.contract.get("worker_profile")
            if isinstance(profile, dict):
                profiles[node_id] = profile
        checkpoint = MonitorCheckpoint(
            run_id=snapshot.run_id,
            plan_revision="{}@{}".format(snapshot.plan_id, snapshot.plan_version),
            state_version=snapshot.schema_version,
            last_event_seq=snapshot.event_sequence,
            next_action="resume",
            stop_conditions=["preserve one run lease", "do not duplicate writer"],
            authorization_digest=snapshot.authorization_digest,
            node_contract_digest=snapshot.node_contract_digest,
            capability_contract_digest=snapshot.capability_contract_digest,
            nodes=snapshot.nodes,
            handles=snapshot.handles,
            worker_profiles=profiles,
            evidence=[{"reason": reason}],
            estimate=(estimate.__dict__ if estimate is not None else None),
        )
        write_checkpoint(self.paths, checkpoint)
        event_name = "monitor_context_exhausted" if exhausted else "monitor_context_checkpoint"
        self._record(
            snapshot,
            event_name,
            {
                "run_id": snapshot.run_id,
                "reason": reason,
                "checkpoint_sha": checkpoint.sha256,
                "last_event_seq": snapshot.event_sequence,
            },
        )
        if exhausted:
            active_nodes = [node_id for node_id, current in snapshot.nodes.items() if current.get("active_task")]
            for node_id in active_nodes:
                snapshot.nodes[node_id]["status"] = "blocked_unknown"
            snapshot.status = "blocked_unknown"

    def _context_allows_dispatch(self, snapshot: RunSnapshot, runner: Runner) -> bool:
        estimate = self._context_estimate(snapshot, runner)
        if estimate is None:
            return True
        if estimate.status == "normal":
            return True
        events = load_events(self.paths, snapshot.run_id)
        if estimate.status == "warning":
            if not any(item.get("event") == "monitor_context_warning" for item in events):
                self._record(snapshot, "monitor_context_warning", {
                    "run_id": snapshot.run_id,
                    "ratio": estimate.ratio,
                    "total_tokens": estimate.total_tokens,
                    "limit_tokens": estimate.limit_tokens,
                    "source": estimate.source,
                })
            return True
        if estimate.status in {"checkpoint", "hard_stop", "blocked_unknown"}:
            self._checkpoint_context(
                snapshot,
                "context budget status: " + estimate.status,
                exhausted=estimate.status in {"hard_stop", "blocked_unknown"},
                estimate=estimate,
            )
            return False
        return True

    def _reconcile_start_intent(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        data: Dict[str, Any],
        provenance: Dict[str, Any],
    ) -> None:
        current = snapshot.nodes[node_id]
        role = data.get("role")
        phase = data.get("phase")
        if role not in {"developer", "reviewer"}:
            raise ValueError("start intent role is invalid")
        if (role == "reviewer" and phase != "review") or (
            role == "developer" and phase not in {"develop", "rework"}
        ):
            raise ValueError("start intent phase is invalid")
        identity = data.get("task_id")
        expected_identity = (
            current.get(role + "_identity")
            or self._identity_from_contract(self.nodes[node_id], role)
            or "{}:{}".format(role, node_id)
        )
        generation_key = (
            "review_generation" if role == "reviewer" else "developer_generation"
        )
        expected_generation = int(current.get(generation_key, 0)) + 1
        if identity != expected_identity or data.get("generation") != expected_generation:
            raise ValueError("start intent identity or generation is stale")
        if (
            provenance.get("role") != role
            or provenance.get("task_id") != identity
            or provenance.get("generation") != expected_generation
            or provenance.get("handle_id") is not None
        ):
            raise ValueError("start intent provenance is invalid")
        binding = self._load_task_binding(snapshot, node_id, role)
        if (
            binding.task_id != identity
            or binding.generation != expected_generation
            or binding.status != "start_pending"
        ):
            raise ValueError("start intent task binding is stale")
        current[role + "_identity"] = identity
        current[generation_key] = expected_generation
        if role == "reviewer":
            current["reviewer_started"] = True
        current["status"] = "start_pending"
        current["active_role"] = role
        current["start_intent"] = {
            key: data[key]
            for key in ("intent_id", "role", "phase", "task_id", "generation")
        }
        current["active_task"] = {
            "role": role,
            "task_id": identity,
            "generation": expected_generation,
            "handle_id": None,
        }

    def _reconcile_start_confirmed(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        data: Dict[str, Any],
        provenance: Dict[str, Any],
    ) -> None:
        current = snapshot.nodes[node_id]
        active = current.get("active_task")
        if not isinstance(active, dict) or active.get("handle_id") is not None:
            raise ValueError("start confirmation has no matching intent")
        expected = {
            "role": data.get("role"),
            "task_id": data.get("task_id"),
            "generation": data.get("generation"),
        }
        if any(active.get(key) != value for key, value in expected.items()):
            raise ValueError("start confirmation identity or generation is stale")
        handle_id = data.get("handle_id")
        if not isinstance(handle_id, str) or not handle_id:
            raise ValueError("start confirmation handle is invalid")
        if (
            provenance.get("role") != expected["role"]
            or provenance.get("task_id") != expected["task_id"]
            or provenance.get("generation") != expected["generation"]
            or provenance.get("handle_id") != handle_id
        ):
            raise ValueError("start confirmation provenance is invalid")
        if any(
            other_node != node_id and other_handle == handle_id
            for other_node, other_handle in snapshot.handles.items()
        ):
            raise ValueError("start confirmation duplicates an active handle")
        binding = self._load_task_binding(snapshot, node_id, str(expected["role"]))
        desired_status = self._running_status(str(expected["role"]), str(data.get("phase")))
        if binding.status not in {"start_pending", desired_status}:
            raise ValueError("start confirmation task binding status is invalid")
        self._set_binding_status(
            snapshot, node_id, str(expected["role"]), desired_status
        )
        active["handle_id"] = handle_id
        current["active_role"] = expected["role"]
        current["start_intent"] = None
        current["status"] = desired_status
        snapshot.handles[node_id] = handle_id

    def _reconcile_unapplied_events(self, snapshot: RunSnapshot) -> None:
        records = load_events(self.paths, snapshot.run_id)
        pending = records[snapshot.event_sequence :]
        for record in pending:
            provenance = record["provenance"]
            if (
                provenance["authorization_digest"] != snapshot.authorization_digest
                or provenance["node_contract_digest"] != snapshot.node_contract_digest
            ):
                raise ValueError("unapplied event lineage is inconsistent")
            data = record["data"]
            if data.get("run_id") != snapshot.run_id:
                raise ValueError("unapplied event run lineage is inconsistent")
            if record["event"] == "authorization_reauthorized":
                self._apply_reauthorization_transition(snapshot, data)
                snapshot.event_sequence = record["sequence"]
                continue
            node_id = data.get("node_id")
            if node_id not in snapshot.nodes:
                raise ValueError("unapplied event references an unknown node")
            current = snapshot.nodes[node_id]
            if record["event"] == "start_intent":
                self._reconcile_start_intent(
                    snapshot, node_id, data, provenance
                )
            elif record["event"] == "start_confirmed":
                self._reconcile_start_confirmed(
                    snapshot, node_id, data, provenance
                )
            elif record["event"] in {"blocked_unknown", "unknown", "timeout"}:
                if provenance["role"] != "system":
                    binding = self._registered_active_binding(
                        snapshot, node_id, provenance
                    )
                    self._set_binding_status(
                        snapshot, node_id, str(provenance["role"]), "blocked_unknown"
                    )
                if (
                    record["event"] in {"unknown", "timeout"}
                    or data.get("retryable_same_task") is True
                ):
                    self._queue_active_retry(
                        snapshot,
                        node_id,
                        str(data.get("reason", record["event"])),
                    )
                current["status"] = "blocked_unknown"
                current["reason"] = data.get("reason", record["event"])
                current["quarantine"] = {
                    "run_id": snapshot.run_id,
                    "handle_id": snapshot.handles.get(node_id),
                    "reason": current["reason"],
                }
            elif record["event"] == "old_task_reconciled":
                if provenance["role"] != "system":
                    raise ValueError("old task reconciliation lacks system provenance")
                role = data.get("role")
                retry = current.get("retryable_action")
                valid = (
                    isinstance(retry, dict)
                    and retry.get("role") == role
                    and retry.get("successor_candidate") is True
                    and data.get("proof") in {"absent", "stopped"}
                )
                predecessor = data.get("predecessor_task_id")
                proof_identity = None
                if valid:
                    proof_identity = self._prove_old_task_stopped_or_absent(
                        snapshot,
                        node_id,
                        retry,
                    )
                    if data.get("proof") == "absent":
                        valid = proof_identity == "" and predecessor in {None, ""}
                    else:
                        valid = (
                            isinstance(predecessor, str)
                            and bool(predecessor)
                            and proof_identity == predecessor
                        )
                if not valid:
                    self._reject_replayed_old_task_reconciliation(
                        snapshot, node_id, data
                    )
                    snapshot.event_sequence = record["sequence"]
                    continue
                current["old_task_reconciled"] = True
                if isinstance(retry, dict):
                    retry = dict(retry)
                    retry["old_task_reconciled"] = True
                    retry["successor"] = True
                    retry["continuation"] = False
                    retry["predecessor_task_id"] = predecessor
                    current["retryable_action"] = retry
            elif record["event"] == "accepted":
                if provenance["role"] != "reviewer":
                    raise ValueError("unapplied acceptance lacks reviewer provenance")
                self._registered_active_binding(snapshot, node_id, provenance)
                if (
                    provenance["task_id"] != current.get("reviewer_identity")
                    or provenance["generation"] != current.get("review_generation")
                    or provenance["handle_id"] != snapshot.handles.get(node_id)
                ):
                    raise ValueError(
                        "unapplied acceptance reviewer identity or generation is stale"
                    )
                try:
                    registered = self._load_task_binding(snapshot, node_id, "reviewer")
                except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                    raise ValueError(
                        "unapplied acceptance reviewer binding is unavailable"
                    ) from error
                if (
                    registered.task_id != provenance["task_id"]
                    or registered.generation != provenance["generation"]
                ):
                    raise ValueError("unapplied acceptance registry generation is stale")
                self._set_binding_status(
                    snapshot, node_id, "reviewer", "accepted"
                )
                current["status"] = "accepted"
                contract_digest = data.get("contract_digest")
                authorization_epoch = data.get("authorization_epoch")
                if (
                    not isinstance(contract_digest, str)
                    or len(contract_digest) != 64
                    or any(character not in "0123456789abcdef" for character in contract_digest)
                    or contract_digest != current.get("contract_digest")
                    or authorization_epoch != snapshot.authorization_digest
                ):
                    raise ValueError("unapplied acceptance contract epoch is stale")
                current["acceptance"] = {
                    "contract_digest": contract_digest,
                    "authorization_epoch": authorization_epoch,
                }
                current["active_role"] = None
                current["active_task"] = None
                current["quarantine"] = None
                snapshot.handles.pop(node_id, None)
                if not data.get("evidence"):
                    current["status"] = "blocked_unknown"
                    current["reason"] = (
                        "review acceptance has no registered P0-P2 clearance evidence"
                    )
                else:
                    current["review_clearance"] = {
                        "p0": 0,
                        "p1": 0,
                        "p2": 0,
                    }
                    for role in ("developer", "reviewer"):
                        self._set_binding_status(
                            snapshot, node_id, role, "archived"
                        )
                    current["pair_archived"] = True
                self._release_node_lease(snapshot, node_id)
            elif record["event"] in {"failed", "stopped", "terminal_failed"}:
                self._registered_active_binding(snapshot, node_id, provenance)
                terminal_status = (
                    "failed" if record["event"] != "stopped" else "stopped"
                )
                self._set_binding_status(
                    snapshot, node_id, str(provenance["role"]), terminal_status
                )
                current["status"] = terminal_status
                current["reason"] = data.get("reason", record["event"])
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                self._release_node_lease(snapshot, node_id)
            elif record["event"] in {"delivered", "complete"}:
                self._registered_active_binding(snapshot, node_id, provenance)
                if provenance["role"] != "developer":
                    current["status"] = "blocked_unknown"
                    current["reason"] = "unapplied reviewer delivery needs reconciliation"
                else:
                    self._set_binding_status(
                        snapshot, node_id, "developer", "delivered"
                    )
                    current["status"] = "delivered"
                    current["active_role"] = None
                    current["active_task"] = None
                    snapshot.handles.pop(node_id, None)
            elif record["event"] == "quarantine_continuation_recovered":
                if provenance["role"] != "system":
                    raise ValueError(
                        "quarantine continuation recovery lacks system provenance"
                    )
                if (
                    data.get("role") != "developer"
                    or data.get("task_id") != current.get("developer_identity")
                    or data.get("generation") != current.get("developer_generation")
                ):
                    raise ValueError("quarantine continuation recovery is stale")
                if not self._recover_quarantined_delivered_developer(
                    snapshot, node_id, record_event=False
                ):
                    raise ValueError(
                        "quarantine continuation recovery cannot be verified"
                    )
            elif record["event"] == "review_finding":
                # The following transition event/start intent is authoritative.
                self._registered_active_binding(snapshot, node_id, provenance)
            elif record["event"] == "consistency_corrected":
                correction = {
                    key: data[key]
                    for key in CONSISTENCY_CORRECTION_KEYS
                }
                current.setdefault("contract_overrides", {})[
                    correction["field"]
                ] = correction["value"]
                if correction not in current.setdefault("corrections", []):
                    current["corrections"].append(correction)
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                current["status"] = "blocked_unknown"
                current["retryable_action"] = {
                    "role": "developer",
                    "phase": "rework",
                    "continuation": True,
                }
            elif record["event"] == "blocked_design":
                current["status"] = "blocked_design"
                current["reason"] = data.get("reason", "design decision required")
                current["old_task_reconciled"] = bool(
                    data.get("old_task_reconciled")
                )
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                self._release_node_lease(snapshot, node_id)
            elif record["event"] == "pair_archived":
                current["pair_archived"] = True
            else:
                current["status"] = "blocked_unknown"
                current["reason"] = "unapplied event needs manual reconciliation"
            snapshot.event_sequence = record["sequence"]

    def _release_node_lease(self, snapshot: RunSnapshot, node_id: str) -> None:
        current = snapshot.nodes[node_id]
        release_writer_lease(
            self.paths,
            node_id,
            str(current.get("worktree", "")),
            snapshot.run_id,
        )

    def _worktree_path(self, current: Dict[str, Any]) -> Path:
        worktree_path = Path(str(current.get("worktree", ".")))
        if not worktree_path.is_absolute():
            worktree_path = self.paths.root / worktree_path
        return worktree_path

    @staticmethod
    def _refresh_run_status(snapshot: RunSnapshot) -> None:
        for current in snapshot.nodes.values():
            current["user_status"] = _user_status(current)
        statuses = [node.get("status") for node in snapshot.nodes.values()]
        if statuses and all(status == "accepted" for status in statuses):
            snapshot.status = "complete"
        elif "blocked_unknown" in statuses:
            snapshot.status = "blocked_unknown"
        elif "blocked_design" in statuses:
            snapshot.status = "blocked_design"
        elif "failed" in statuses or "stopped" in statuses:
            snapshot.status = "failed"
        else:
            snapshot.status = "running"
