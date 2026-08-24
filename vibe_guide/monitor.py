"""Authorization-bound, write-ahead monitor for developer/reviewer DAG tasks."""

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

from .authorization import (
    AuthorizationRecord,
    executable_contract_digest,
    is_authorization_valid,
    validate_runtime_contract,
)
from .contracts import RunEvent, RunHandle, Runner
from .models import DAGNode, Plan
from .paths import ProjectPaths
from .planner import resolve_consistency
from .adapters.task_provider import ProviderPending
from .state import (
    CONSISTENCY_CORRECTION_KEYS,
    RunSnapshot,
    acquire_writer_lease,
    append_event,
    load_events,
    load_snapshot,
    quarantine_writer_lease,
    redact_provider_text,
    release_writer_lease,
    save_snapshot,
)
from .task_registry import TaskBinding, load_task_binding, save_task_binding


class Monitor:
    def __init__(self, paths: ProjectPaths, plan: Plan, nodes: List[DAGNode]):
        self.paths = paths
        self.plan = plan
        self.nodes = {node.id: node for node in nodes}

    def start(
        self, record: Optional[AuthorizationRecord], runner: Runner
    ) -> RunSnapshot:
        self._require_record(record)
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
                "contract_overrides": {},
                "corrections": [],
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
            event_sequence=0,
        )
        self._record(
            snapshot,
            "run_started",
            {
                "run_id": run_id,
                "authorization_digest": record.digest,
                "node_contract_digest": record.node_contract_digest,
                "node_ids": sorted(self.nodes),
            },
        )
        save_snapshot(self.paths, snapshot)
        self._schedule_ready(snapshot, runner)
        self._refresh_run_status(snapshot)
        save_snapshot(self.paths, snapshot)
        return snapshot

    def resume(self, run_id: str, runner: Runner) -> RunSnapshot:
        snapshot = load_snapshot(self.paths, run_id)
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
                if node_id not in snapshot.handles:
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "active task handle is missing during resume",
                    )
        self._schedule_ready(snapshot, runner)
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

        self._require_record(record)
        snapshot = load_snapshot(self.paths, run_id)
        self._reconcile_unapplied_events(snapshot)
        if (
            snapshot.plan_id != self.plan.plan_id
            or snapshot.plan_version != self.plan.version
            or set(snapshot.nodes) != set(self.nodes)
        ):
            raise PermissionError("reauthorization must remain on the same plan revision")
        if snapshot.authorization_digest == record.digest:
            self._require_snapshot_authorization(snapshot)
            self._schedule_ready(snapshot, runner)
            self._refresh_run_status(snapshot)
            save_snapshot(self.paths, snapshot)
            return snapshot

        previous = self._snapshot_record(snapshot)
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

        continuation: Dict[str, Any] = {}
        for node_id in sorted(snapshot.nodes):
            for role in ("developer", "reviewer"):
                key = "{}:{}".format(node_id, role)
                try:
                    binding = load_task_binding(
                        self.paths, node_id, role, run_id=snapshot.run_id
                    )
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
            "new_authorization": record.to_dict(),
            "authorization_digest": record.digest,
            "node_contract_digest": record.node_contract_digest,
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
        snapshot = load_snapshot(self.paths, run_id)
        self._require_snapshot_authorization(snapshot)
        self._reconcile_unapplied_events(snapshot)
        for node_id, handle_id in list(snapshot.handles.items()):
            self._require_snapshot_authorization(snapshot)
            try:
                events = runner.poll(RunHandle(handle_id))
            except Exception as error:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "runner poll failed ({})".format(type(error).__name__),
                )
                continue
            for event in events:
                self._apply_event(snapshot, node_id, handle_id, event, runner)
        self._schedule_ready(snapshot, runner)
        self._refresh_run_status(snapshot)
        save_snapshot(self.paths, snapshot)
        return snapshot

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
        ):
            raise ValueError("reauthorization previous lineage is inconsistent")
        self._require_record(replacement)
        if (
            data.get("authorization_digest") != replacement.digest
            or data.get("node_contract_digest")
            != replacement.node_contract_digest
            or data.get("change_reason") != "executable_contract_changed"
        ):
            raise ValueError("reauthorization replacement lineage is inconsistent")
        continuation = data.get("continuation")
        if not isinstance(continuation, dict):
            raise ValueError("reauthorization continuation evidence is invalid")
        for key, evidence in continuation.items():
            if not isinstance(key, str) or ":" not in key or not isinstance(evidence, dict):
                raise ValueError("reauthorization continuation evidence is invalid")
            node_id, role = key.rsplit(":", 1)
            if node_id not in snapshot.nodes or role not in {"developer", "reviewer"}:
                raise ValueError("reauthorization continuation identity is invalid")
            binding = load_task_binding(
                self.paths, node_id, role, run_id=snapshot.run_id
            )
            if (
                set(evidence) != {"task_id", "cursor"}
                or evidence["task_id"] != binding.task_id
                or evidence["cursor"] != binding.cursor
            ):
                raise ValueError("reauthorization continuation cursor is inconsistent")
            snapshot.tasks[key] = binding.to_dict()

        snapshot.authorization = replacement.to_dict()
        snapshot.authorization_digest = replacement.digest
        snapshot.node_contract_digest = replacement.node_contract_digest
        snapshot.handles.clear()
        for current in snapshot.nodes.values():
            current["active_role"] = None
            current["active_task"] = None
            current["start_intent"] = None
            current["quarantine"] = None
            if current.get("status") == "accepted":
                continue
            if int(current.get("developer_generation", 0)) > 0:
                current["status"] = "blocked_unknown"
                current["retryable_action"] = {
                    "role": "developer",
                    "phase": "rework",
                    "continuation": True,
                }
            else:
                current["status"] = "planned"
                current["retryable_action"] = None

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

    def _schedule_ready(self, snapshot: RunSnapshot, runner: Runner) -> None:
        record = self._require_snapshot_authorization(snapshot)
        active_pairs = sum(
            1
            for current in snapshot.nodes.values()
            if not current.get("pair_archived")
            and (
                int(current.get("developer_generation", 0)) > 0
                or current.get("retryable_action") is not None
            )
        )
        for node_id, node in self.nodes.items():
            current = snapshot.nodes[node_id]
            retry = current.get("retryable_action")
            if (
                current.get("status") == "blocked_unknown"
                and isinstance(retry, dict)
                and node_id not in snapshot.handles
            ):
                if self._start_task(
                    snapshot,
                    node_id,
                    str(retry["role"]),
                    str(retry["phase"]),
                    runner,
                    bool(retry.get("continuation")),
                ):
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

    def _start_task(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        phase: str,
        runner: Runner,
        continuation: bool,
    ) -> bool:
        record = self._require_snapshot_authorization(snapshot)
        node = self.nodes[node_id]
        current = snapshot.nodes[node_id]
        identity_key = role + "_identity"
        identity = current.get(identity_key) or "{}:{}".format(role, node_id)
        generation_key = "review_generation" if role == "reviewer" else "developer_generation"
        generation = int(current.get(generation_key, 0)) + 1
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
                "action": phase,
                "run_id": snapshot.run_id,
                "consistency_binding": self._consistency_binding(record, node),
            }
        )
        try:
            contract = validate_runtime_contract(
                contract,
                authorized_actions=record.allowed_actions,
                authorized_files=record.file_scope,
            )
            binding = self._binding_for(
                snapshot,
                node_id,
                role,
                identity,
                generation,
                "start_pending",
                runner,
                contract,
            )
            identity = str(binding.task_id)
            current[identity_key] = identity
            contract["task_id"] = identity
            save_task_binding(self.paths, binding)
        except ProviderPending as error:
            current[generation_key] = generation - 1
            if role == "reviewer" and generation == 1:
                current["reviewer_started"] = False
            current["retryable_action"] = {
                "role": role,
                "phase": phase,
                "continuation": continuation,
            }
            self._mark_blocked_unknown(
                snapshot, node_id, "provider action pending"
            )
            current["reason"] = str(error)
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
            self._mark_blocked_unknown(
                snapshot, node_id, "provider action pending"
            )
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
    ) -> TaskBinding:
        current = snapshot.nodes[node_id]
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
            ):
                raise ValueError("observed task binding does not match runtime scope")
            return binding
        return TaskBinding(
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
        )

    def _set_binding_status(
        self, snapshot: RunSnapshot, node_id: str, role: str, status: str
    ) -> TaskBinding:
        binding = load_task_binding(
            self.paths, node_id, role, run_id=snapshot.run_id
        )
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
        save_task_binding(self.paths, binding)
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
        binding = load_task_binding(
            self.paths,
            node_id,
            str(active["role"]),
            run_id=snapshot.run_id,
        )
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
            self._start_task(
                snapshot,
                node_id,
                "reviewer",
                "review",
                runner,
                bool(current.get("reviewer_started")),
            )
        elif event.event == "review_finding":
            if role != "reviewer":
                self._mark_blocked_unknown(
                    snapshot, node_id, "review finding did not come from the reviewer"
                )
                return
            self._record_runner_event(snapshot, node_id, event, active)
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
                registered = load_task_binding(
                    self.paths, node_id, "reviewer", run_id=snapshot.run_id
                )
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
            self._record_runner_event(snapshot, node_id, event, active)
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
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                str(redact_provider_text(event.data.get("reason", event.event))),
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

    def _mark_blocked_unknown(
        self, snapshot: RunSnapshot, node_id: str, reason: str
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
        quarantine_writer_lease(
            self.paths,
            node_id,
            str(current.get("worktree", "")),
            snapshot.run_id,
            reason,
        )
        self._record(
            snapshot,
            "blocked_unknown",
            {"run_id": snapshot.run_id, "node_id": node_id, "reason": reason},
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
        binding = load_task_binding(
            self.paths, node_id, role, run_id=snapshot.run_id
        )
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
        binding = load_task_binding(
            self.paths,
            node_id,
            str(expected["role"]),
            run_id=snapshot.run_id,
        )
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
                    self._registered_active_binding(
                        snapshot, node_id, provenance
                    )
                current["status"] = "blocked_unknown"
                current["reason"] = data.get("reason", record["event"])
                current["quarantine"] = {
                    "run_id": snapshot.run_id,
                    "handle_id": snapshot.handles.get(node_id),
                    "reason": current["reason"],
                }
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
                    registered = load_task_binding(
                        self.paths,
                        node_id,
                        "reviewer",
                        run_id=snapshot.run_id,
                    )
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
                current["active_role"] = None
                current["active_task"] = None
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
