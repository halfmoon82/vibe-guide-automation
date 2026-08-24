from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List


@dataclass(frozen=True)
class RunHandle:
    run_id: str


@dataclass(frozen=True)
class RunEvent:
    event: str
    data: Dict[str, Any]


class Runner:
    def start(self, contract: dict, worktree: Path) -> RunHandle:
        raise NotImplementedError

    def poll(self, handle: RunHandle) -> List[RunEvent]:
        raise NotImplementedError

    def stop(self, handle: RunHandle) -> None:
        raise NotImplementedError

