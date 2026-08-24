from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..contracts import RunEvent, RunHandle, Runner


class FakeRunner(Runner):
    def __init__(self, events: Optional[Dict[Any, List[Any]]] = None):
        self.events = {node_id: list(queue) for node_id, queue in (events or {}).items()}
        self.start_calls = []
        self.stop_calls = []
        self._nodes_by_handle = {}
        self._roles_by_handle = {}
        self._claims_by_handle = {}

    def start(self, contract: dict, worktree: Path) -> RunHandle:
        call = dict(contract)
        call["worktree"] = str(worktree)
        self.start_calls.append(call)
        handle = RunHandle("fake-{}-{}".format(contract["node_id"], len(self.start_calls)))
        self._nodes_by_handle[handle.run_id] = contract["node_id"]
        self._roles_by_handle[handle.run_id] = contract.get("role", "developer")
        self._claims_by_handle[handle.run_id] = {
            "node_id": contract["node_id"],
            "role": contract.get("role", "developer"),
            "task_id": contract.get("task_id"),
            "handle_id": handle.run_id,
            "generation": contract.get("generation"),
        }
        return handle

    def poll(self, handle: RunHandle) -> List[RunEvent]:
        node_id = self._nodes_by_handle.get(handle.run_id)
        role = self._roles_by_handle.get(handle.run_id, "developer")
        queue = self.events.get((node_id, role), self.events.get(node_id, []))
        # A provider fixture may key an event queue by the exact execution
        # handle.  Keep the node-keyed form for backwards compatibility.
        queue = self.events.get(handle.run_id, queue)
        if not queue:
            return []
        item = queue.pop(0)
        if isinstance(item, RunEvent):
            event = item.event
            data = dict(item.data)
        else:
            event, data = item
            data = dict(data)
        for key, value in self._claims_by_handle.get(handle.run_id, {}).items():
            data.setdefault(key, value)
        return [RunEvent(event, data)]

    def stop(self, handle: RunHandle) -> None:
        self.stop_calls.append(handle.run_id)
