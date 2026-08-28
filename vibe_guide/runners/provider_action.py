"""Durable provider-neutral runner backed by desktop action requests/results."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

from ..adapters.task_provider import ProviderActionStore, ProviderPending
from ..contracts import RunEvent, RunHandle, Runner
from ..paths import ProjectPaths
from ..task_registry import (
    TaskBinding,
    load_task_binding,
    save_task_binding,
)


class ProviderActionRunner(Runner):
    """Expose a public CLI runner while the App owns native tool execution."""

    def __init__(
        self,
        paths: ProjectPaths,
        adapter_id: str,
        provider: str,
    ):
        self.paths = paths
        self.adapter_id = adapter_id
        self.provider = provider
        self.store = ProviderActionStore(paths)

    def _native_tool(self, operation: str) -> str:
        if self.provider == "codex-app-visible":
            return {
                "create": "codex_app__create_thread",
                "locate": "codex_app__navigate_to_codex_page",
                "visibility": "codex_app__wait_threads",
                "resume": "codex_app__send_message_to_thread",
                "wait": "codex_app__wait_threads",
            }[operation]
        return self.provider + "." + operation

    @staticmethod
    def _consistency_instruction(contract: Dict[str, Any]) -> str:
        return "一致性纠偏证据必须原样绑定：{}".format(
            json.dumps(
                contract.get("consistency_binding", {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def _action(
        self,
        contract: Dict[str, Any],
        run_id: str,
        operation: str,
        request: Dict[str, Any],
        sequence: int = 0,
    ) -> Dict[str, Any]:
        return self.store.request(
            operation=operation,
            provider=self.provider,
            run_id=run_id,
            issue_id=str(contract["node_id"]),
            role=str(contract["role"]),
            generation=int(contract["generation"]),
            native_tool=self._native_tool(operation),
            request=request,
            sequence=sequence,
        )

    def _require_result(
        self,
        contract: Dict[str, Any],
        run_id: str,
        operation: str,
        request: Dict[str, Any],
    ) -> Dict[str, Any]:
        action = self._action(contract, run_id, operation, request)
        result = self.store.result(action["action_id"])
        if result is None:
            raise ProviderPending(
                "provider %s action is pending" % operation
            )
        return result

    def task_binding(
        self,
        contract: Dict[str, Any],
        worktree: Path,
        run_id: str,
        status: str,
    ) -> TaskBinding:
        node_id = str(contract["node_id"])
        role = str(contract["role"])
        generation = int(contract["generation"])
        try:
            existing = load_task_binding(
                self.paths, node_id, role, run_id=run_id
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            existing.status = status
            existing.generation = generation
            return existing

        project_id = contract.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("visible provider contract requires project_id")
        prompt = "请执行 {} 任务，Issue {}。{}".format(
            role,
            node_id,
            self._consistency_instruction(contract),
        )
        create_request = {
            "prompt": prompt,
            "target": {
                "type": "project",
                "projectId": project_id,
                "environment": {"type": "local"},
            },
        }
        created = self._require_result(
            contract, run_id, "create", create_request
        )
        binding_data = created.get("binding")
        if not isinstance(binding_data, dict):
            raise ValueError("provider create result has no verified binding")
        task_id = binding_data.get("threadId") or binding_data.get("task_id")
        host = binding_data.get("hostId") or binding_data.get("host")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("provider create result has no task identity")
        if not isinstance(host, str) or not host:
            raise ValueError("provider create result has no host identity")

        located = self._require_result(
            contract,
            run_id,
            "locate",
            {"threadId": task_id},
        )
        if located.get("located") is not True:
            raise ValueError("provider task cannot be located")
        visible = self._require_result(
            contract,
            run_id,
            "visibility",
            {
                "targets": [{"threadId": task_id, "hostId": host}],
                "timeoutMs": 0,
            },
        )
        if visible.get("visible") is not True or visible.get("direct_enter") is not True:
            raise ValueError("provider task visibility is not verified")
        return TaskBinding(
            provider=self.provider,
            mode="visible",
            issue_id=node_id,
            role=role,
            task_id=task_id,
            host=host,
            worktree=str(contract.get("worktree") or worktree),
            branch=str(contract.get("branch", "")),
            status_file=str(contract.get("status_file", "")),
            handoff_file=str(contract.get("handoff_file", "")),
            threadId=task_id if self.provider == "codex-app-visible" else None,
            hostId=host if self.provider == "codex-app-visible" else None,
            run_id=run_id,
            status=status,
            visible=True,
            generation=generation,
        )

    @staticmethod
    def _handle_id(contract: Dict[str, Any], run_id: str) -> str:
        basis = "\0".join(
            (
                run_id,
                str(contract["node_id"]),
                str(contract["role"]),
                str(contract["generation"]),
            )
        )
        return "bridge-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]

    def _handle_path(self, handle_id: str) -> Path:
        directory = self.store._directory("handles")
        return directory / (handle_id + ".json")

    def start(self, contract: dict, worktree: Path) -> RunHandle:
        run_id = str(contract.get("run_id", ""))
        if not run_id:
            raise ValueError("provider runner requires run_id")
        handle = RunHandle(self._handle_id(contract, run_id))
        binding = load_task_binding(
            self.paths,
            str(contract["node_id"]),
            str(contract["role"]),
            run_id=run_id,
        )
        metadata = {
            "schema_version": 1,
            "handle_id": handle.run_id,
            "run_id": run_id,
            "node_id": str(contract["node_id"]),
            "role": str(contract["role"]),
            "task_id": binding.task_id,
            "host": binding.host,
            "generation": int(contract["generation"]),
            "pending_action": None,
            "pending_operation": None,
            "wait_sequence": 0,
            "cursor": binding.cursor,
            "terminal_confirmed": False,
        }
        if contract.get("continuation"):
            request = {
                "threadId": binding.task_id,
                "hostId": binding.host,
                "prompt": "请继续处理 Issue {}。{}".format(
                    contract["node_id"], self._consistency_instruction(contract)
                ),
            }
            action = self._action(contract, run_id, "resume", request)
            metadata["pending_action"] = action["action_id"]
            metadata["pending_operation"] = "resume"
        self.store._atomic(self._handle_path(handle.run_id), metadata)
        return handle

    def is_pending(self, handle: RunHandle) -> bool:
        metadata = self.store._read(self._handle_path(handle.run_id))
        action_id = metadata.get("pending_action")
        return bool(action_id and self.store.result(action_id) is None)

    def poll(self, handle: RunHandle):
        metadata = self.store._read(self._handle_path(handle.run_id))
        claims = {
            "node_id": metadata["node_id"],
            "role": metadata["role"],
            "task_id": metadata["task_id"],
            "handle_id": handle.run_id,
            "generation": metadata["generation"],
        }
        pending_action = metadata.get("pending_action")
        if pending_action:
            result = self.store.result(pending_action)
            if result is None:
                operation = str(metadata.get("pending_operation") or "action")
                return [
                    RunEvent(
                        "visibility_unknown",
                        {**claims, "reason": "provider %s is pending" % operation},
                    )
                ]
            operation = metadata.get("pending_operation")
            if operation == "wait":
                return self._wait_result(handle, metadata, claims, result)
            if operation != "resume" or result.get("resumed") is not True:
                return [
                    RunEvent(
                        "visibility_unknown",
                        {**claims, "reason": "provider resume is unverified"},
                    )
                ]
            metadata["pending_action"] = None
            metadata["pending_operation"] = None
            self.store._atomic(self._handle_path(handle.run_id), metadata)

        contract = {
            "node_id": metadata["node_id"],
            "role": metadata["role"],
            "generation": metadata["generation"],
        }
        wait_request = {
            "targets": [
                {
                    "threadId": metadata["task_id"],
                    "hostId": metadata["host"],
                }
            ],
            "timeoutMs": 0,
        }
        cursor = metadata.get("cursor")
        if isinstance(cursor, str) and cursor:
            wait_request["targets"][0]["afterCursor"] = cursor
        sequence = int(metadata.get("wait_sequence", 0)) + 1
        action = self._action(
            contract,
            metadata["run_id"],
            "wait",
            wait_request,
            sequence=sequence,
        )
        metadata["wait_sequence"] = sequence
        metadata["pending_action"] = action["action_id"]
        metadata["pending_operation"] = "wait"
        self.store._atomic(self._handle_path(handle.run_id), metadata)
        result = self.store.result(action["action_id"])
        if result is None:
            return [
                RunEvent(
                    "visibility_unknown",
                    {**claims, "reason": "provider wait is pending"},
                )
            ]
        return self._wait_result(handle, metadata, claims, result)

    def _wait_result(
        self,
        handle: RunHandle,
        metadata: Dict[str, Any],
        claims: Dict[str, Any],
        result: Dict[str, Any],
    ):
        status = result.get("status")
        cursor = result.get("cursor")
        if cursor is not None and (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > 4096
            or "\x00" in cursor
        ):
            return [
                RunEvent(
                    "visibility_unknown",
                    {**claims, "reason": "provider cursor is invalid"},
                )
            ]
        if cursor is not None:
            binding = load_task_binding(
                self.paths,
                metadata["node_id"],
                metadata["role"],
                run_id=metadata["run_id"],
            )
            binding.cursor = cursor
            save_task_binding(self.paths, binding)
            metadata["cursor"] = cursor
        metadata["pending_action"] = None
        metadata["pending_operation"] = None
        self.store._atomic(self._handle_path(handle.run_id), metadata)
        if status in {"timeout", "unknown", "error"}:
            return [RunEvent("visibility_unknown", {**claims, "reason": str(status)})]
        event_name = result.get("event")
        if event_name not in {
            "complete",
            "delivered",
            "accepted",
            "review_finding",
            "failed",
            "stopped",
        }:
            return [
                RunEvent(
                    "visibility_unknown",
                    {**claims, "reason": "provider event is unsupported"},
                )
            ]
        if status not in {"complete", "completed", "failed", "stopped"}:
            return [
                RunEvent(
                    "visibility_unknown",
                    {**claims, "reason": "provider terminal state is unverified"},
                )
            ]
        metadata["terminal_confirmed"] = True
        self.store._atomic(self._handle_path(handle.run_id), metadata)
        data = dict(claims)
        for key in ("evidence", "finding", "in_contract", "consistency"):
            if key in result:
                data[key] = result[key]
        return [RunEvent(str(event_name), data)]

    def stop(self, handle: RunHandle) -> None:
        metadata = self.store._read(self._handle_path(handle.run_id))
        if metadata.get("terminal_confirmed") is True:
            return
        pending_action = metadata.get("pending_action")
        if not isinstance(pending_action, str) or not pending_action:
            raise ProviderPending(
                "provider stop requires a pending terminal wait reconciliation"
            )
        if metadata.get("pending_operation") != "wait":
            raise ProviderPending(
                "provider stop cannot reconcile a non-terminal provider action"
            )
        result = self.store.result(pending_action)
        if result is None:
            raise ProviderPending("provider terminal wait is still pending")
        if result.get("status") not in {"stopped", "complete", "completed"} or result.get(
            "event"
        ) != "stopped":
            raise ProviderPending("provider terminal wait did not prove a stop")
        cursor = result.get("cursor")
        if cursor is not None and (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > 4096
            or "\x00" in cursor
        ):
            raise ProviderPending("provider terminal wait cursor is invalid")
        claims = {
            "node_id": metadata["node_id"],
            "role": metadata["role"],
            "task_id": metadata["task_id"],
            "handle_id": handle.run_id,
            "generation": metadata["generation"],
        }
        events = self._wait_result(handle, metadata, claims, result)
        if len(events) != 1 or events[0].event != "stopped":
            raise ProviderPending("provider terminal wait did not prove a stop")
