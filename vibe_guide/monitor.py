"""Small, recoverable monitor state machine for DAG developer/reviewer tasks."""

from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

from .authorization import AuthorizationRecord, is_authorization_valid
from .contracts import RunEvent, RunHandle, Runner
from .models import DAGNode, Plan
from .paths import ProjectPaths
from .state import (
    RunSnapshot,
    acquire_writer_lease,
    append_event,
    load_snapshot,
    release_writer_lease,
    save_snapshot,
)
from .task_registry import TaskBinding, save_task_binding


class Monitor:
    def __init__(self, paths: ProjectPaths, plan: Plan, nodes: List[DAGNode]):
        self.paths = paths
        self.plan = plan
        self.nodes = {node.id: node for node in nodes}

    def start(self, record: Optional[AuthorizationRecord], runner: Runner) -> RunSnapshot:
        if record is None or not is_authorization_valid(record, self.plan):
            raise PermissionError("a valid plan-bound authorization is required")
        if record.node_ids != tuple(sorted(self.nodes)):
            raise PermissionError("authorization node scope does not match monitor scope")

        run_id = "run-" + uuid.uuid4().hex
        node_state: Dict[str, Dict[str, Any]] = {}
        for node_id, node in self.nodes.items():
            completed = node.status in ("complete", "accepted")
            node_state[node_id] = {
                "status": "accepted" if completed else "pending",
                "worker": node.contract.get("worker"),
                "worktree": node.contract.get("worktree", ".worktrees/" + node_id),
                "branch": node.contract.get("branch", ""),
                "status_file": node.contract.get("status_file", ""),
                "handoff_file": node.contract.get("handoff_file", ""),
                "evidence": [],
                "active_role": None,
                "developer_identity": self._identity_from_contract(node, "developer"),
                "reviewer_identity": self._identity_from_contract(node, "reviewer"),
                "reviewer_started": False,
            }
        snapshot = RunSnapshot(
            run_id,
            self.plan.plan_id,
            self.plan.version,
            "running",
            node_state,
            {},
            {},
        )
        append_event(self.paths, RunEvent("run_started", {"run_id": run_id}))
        self._schedule_ready(snapshot, runner)
        self._refresh_run_status(snapshot)
        save_snapshot(self.paths, snapshot)
        return snapshot

    def resume(self, run_id: str, runner: Runner) -> RunSnapshot:
        snapshot = load_snapshot(self.paths, run_id)
        if snapshot.plan_id != self.plan.plan_id or snapshot.plan_version != self.plan.version:
            raise PermissionError("snapshot plan no longer matches monitor plan")
        # A missing execution handle after an interruption is not evidence of
        # completion.  Keep the run fail-closed until a runner can establish
        # what happened.
        for node_id, current in snapshot.nodes.items():
            if current.get("status") in {"running", "review", "rework"} and node_id not in snapshot.handles:
                current["status"] = "blocked_unknown"
                current["reason"] = "active task handle is missing during resume"
                append_event(
                    self.paths,
                    RunEvent(
                        "blocked_unknown",
                        {"run_id": run_id, "node_id": node_id, "reason": current["reason"]},
                    ),
                )
                self._release_node_lease(snapshot, node_id)
        self._schedule_ready(snapshot, runner)
        self._refresh_run_status(snapshot)
        save_snapshot(self.paths, snapshot)
        return snapshot

    def tick(self, run_id: str, runner: Runner) -> RunSnapshot:
        snapshot = load_snapshot(self.paths, run_id)
        for node_id, handle_id in list(snapshot.handles.items()):
            try:
                events = runner.poll(RunHandle(handle_id))
            except Exception as error:
                self._mark_blocked_unknown(
                    snapshot, node_id, "runner poll failed: " + str(error)
                )
                continue
            for event in events:
                self._apply_event(snapshot, node_id, event, runner)
        self._schedule_ready(snapshot, runner)
        self._refresh_run_status(snapshot)
        save_snapshot(self.paths, snapshot)
        return snapshot

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
        for node_id, node in self.nodes.items():
            current = snapshot.nodes[node_id]
            if current.get("status") != "pending":
                continue
            if not all(
                snapshot.nodes[dependency].get("status") == "accepted"
                for dependency in node.depends_on
            ):
                continue
            worktree = str(current["worktree"])
            if not acquire_writer_lease(self.paths, node_id, worktree, snapshot.run_id):
                # Another active run owns this node.  Do not create a second
                # developer task while ownership is unresolved.
                self._mark_blocked_unknown(
                    snapshot, node_id, "writer lease is already owned by another run"
                )
                continue
            if not self._start_developer(snapshot, node_id, runner, "develop"):
                self._release_node_lease(snapshot, node_id)

    def _start_developer(
        self, snapshot: RunSnapshot, node_id: str, runner: Runner, phase: str
    ) -> bool:
        node = self.nodes[node_id]
        current = snapshot.nodes[node_id]
        identity = current.get("developer_identity")
        if not identity:
            identity = str(node.contract.get("task_id") or "developer:" + node_id)
            current["developer_identity"] = identity
        contract = dict(node.contract)
        contract.update(
            {
                "node_id": node_id,
                "role": "developer",
                "worker": current.get("worker"),
                "phase": phase,
                "task_id": identity,
                "continuation": phase == "rework",
            }
        )
        if not self._persist_binding(snapshot, node_id, "developer", contract, identity):
            self._mark_blocked_unknown(snapshot, node_id, "duplicate or invalid developer task binding")
            return False
        try:
            handle = runner.start(contract, self._worktree_path(current))
        except Exception as error:
            self._mark_blocked_unknown(snapshot, node_id, "worker start failed: " + str(error))
            return False
        current["status"] = "running" if phase == "develop" else "rework"
        current["active_role"] = "developer"
        snapshot.handles[node_id] = handle.run_id
        append_event(
            self.paths,
            RunEvent(
                "node_started",
                {
                    "run_id": snapshot.run_id,
                    "node_id": node_id,
                    "role": "developer",
                    "worker": current.get("worker"),
                    "phase": phase,
                    "task_id": identity,
                },
            ),
        )
        return True

    def _start_reviewer(
        self, snapshot: RunSnapshot, node_id: str, runner: Runner, continuation: bool
    ) -> bool:
        node = self.nodes[node_id]
        current = snapshot.nodes[node_id]
        identity = current.get("reviewer_identity")
        if not identity:
            identity = self._identity_from_contract(node, "reviewer") or "reviewer:" + node_id
            current["reviewer_identity"] = identity
        contract = dict(node.contract)
        contract.update(
            {
                "node_id": node_id,
                "role": "reviewer",
                "worker": current.get("reviewer_worker") or node.contract.get("reviewer_worker"),
                "phase": "review",
                "task_id": identity,
                "continuation": continuation,
            }
        )
        if not self._persist_binding(snapshot, node_id, "reviewer", contract, identity):
            self._mark_blocked_unknown(snapshot, node_id, "duplicate or invalid reviewer task binding")
            return False
        try:
            handle = runner.start(contract, self._worktree_path(current))
        except Exception as error:
            self._mark_blocked_unknown(snapshot, node_id, "reviewer start failed: " + str(error))
            return False
        current["status"] = "review"
        current["active_role"] = "reviewer"
        current["reviewer_started"] = True
        snapshot.handles[node_id] = handle.run_id
        append_event(
            self.paths,
            RunEvent(
                "reviewer_started",
                {
                    "run_id": snapshot.run_id,
                    "node_id": node_id,
                    "role": "reviewer",
                    "phase": "review",
                    "task_id": identity,
                    "continuation": continuation,
                },
            ),
        )
        return True

    def _persist_binding(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        contract: Dict[str, Any],
        identity: str,
    ) -> bool:
        mode = str(contract.get("mode", "background"))
        host = contract.get("host") or contract.get("hostId")
        if mode == "visible" and not host:
            return False
        try:
            binding = TaskBinding(
                provider=str(contract.get("provider", "runner")),
                mode=mode,
                issue_id=node_id,
                role=role,
                task_id=identity,
                host=str(host) if host else None,
                worktree=str(snapshot.nodes[node_id].get("worktree", "")),
                branch=str(snapshot.nodes[node_id].get("branch", "")),
                status_file=str(snapshot.nodes[node_id].get("status_file", "")),
                handoff_file=str(snapshot.nodes[node_id].get("handoff_file", "")),
                cursor=contract.get("cursor"),
                token=contract.get("token"),
                threadId=contract.get("threadId") or (identity if contract.get("provider") == "codex" else None),
                hostId=contract.get("hostId") or (str(host) if contract.get("provider") == "codex" and host else None),
                run_id=snapshot.run_id,
            )
            save_task_binding(self.paths, binding)
        except (ValueError, OSError, TypeError):
            return False
        snapshot.tasks["{}:{}".format(node_id, role)] = binding.to_dict()
        return True

    def _apply_event(
        self, snapshot: RunSnapshot, node_id: str, event: RunEvent, runner: Runner
    ) -> None:
        if node_id not in snapshot.nodes:
            return
        current = snapshot.nodes[node_id]
        event_data = dict(event.data)
        evidence = event_data.get("evidence") or event_data.get("finding")
        if evidence is not None:
            current.setdefault("evidence", []).append(evidence)

        active_role = current.get("active_role")
        if event.event == "delivered":
            if active_role == "reviewer":
                current["status"] = "review"
            elif active_role == "developer":
                current["status"] = "review"
                if not self._start_reviewer(
                    snapshot,
                    node_id,
                    runner,
                    continuation=bool(current.get("reviewer_started", False)),
                ):
                    return
            else:
                self._mark_blocked_unknown(snapshot, node_id, "delivery from an unknown task")
        elif event.event == "review_finding":
            if active_role != "reviewer":
                self._mark_blocked_unknown(snapshot, node_id, "review finding from an unknown task")
            elif not event_data.get("in_contract", False):
                current["status"] = "blocked_design"
                current["reason"] = event_data.get("finding", "out-of-contract finding")
                current["active_role"] = None
                snapshot.handles.pop(node_id, None)
                self._release_node_lease(snapshot, node_id)
            else:
                snapshot.handles.pop(node_id, None)
                if not self._start_developer(snapshot, node_id, runner, "rework"):
                    return
        elif event.event == "accepted":
            current["status"] = "accepted"
            current["active_role"] = None
            snapshot.handles.pop(node_id, None)
            self._release_node_lease(snapshot, node_id)
        elif event.event in ("unknown", "timeout", "state_unknown", "visibility_unknown"):
            self._mark_blocked_unknown(
                snapshot, node_id, event_data.get("reason", event.event)
            )
        elif event.event == "failed":
            current["status"] = "failed"
            current["reason"] = event_data.get("reason", "runner failed")
            current["active_role"] = None
            snapshot.handles.pop(node_id, None)
            self._release_node_lease(snapshot, node_id)
        else:
            self._mark_blocked_unknown(snapshot, node_id, "unrecognized runner event: " + event.event)

        logged = dict(event_data)
        logged.update({"run_id": snapshot.run_id, "node_id": node_id})
        append_event(self.paths, RunEvent(event.event, logged))

    def _mark_blocked_unknown(self, snapshot: RunSnapshot, node_id: str, reason: str) -> None:
        current = snapshot.nodes[node_id]
        current["status"] = "blocked_unknown"
        current["reason"] = reason
        current["active_role"] = None
        snapshot.handles.pop(node_id, None)
        self._release_node_lease(snapshot, node_id)
        append_event(
            self.paths,
            RunEvent(
                "blocked_unknown",
                {"run_id": snapshot.run_id, "node_id": node_id, "reason": reason},
            ),
        )

    def _release_node_lease(self, snapshot: RunSnapshot, node_id: str) -> None:
        current = snapshot.nodes.get(node_id)
        if current is not None:
            release_writer_lease(
                self.paths, node_id, str(current.get("worktree", "")), snapshot.run_id
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
        elif "failed" in statuses:
            snapshot.status = "failed"
        else:
            snapshot.status = "running"
