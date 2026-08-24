"""Small runner interfaces shared by later implementation nodes."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from .models import _identifier, _json_safe


@dataclass(frozen=True)
class RunHandle:
    run_id: str

    def __post_init__(self):
        _identifier(self.run_id, "run id")

    def to_dict(self) -> Dict[str, Any]:
        return {"run_id": self.run_id}


@dataclass(frozen=True)
class RunEvent:
    event: str
    data: Dict[str, Any]

    def __post_init__(self):
        if not isinstance(self.event, str) or not self.event:
            raise ValueError("event must be a non-empty string")
        if not isinstance(self.data, dict):
            raise TypeError("event data must be a dictionary")
        object.__setattr__(self, "data", _json_safe(self.data))

    def to_dict(self) -> Dict[str, Any]:
        return {"event": self.event, "data": _json_safe(self.data)}


class Runner:
    def start(self, contract: dict, worktree: Path) -> RunHandle:
        raise NotImplementedError

    def poll(self, handle: RunHandle) -> List[RunEvent]:
        raise NotImplementedError

    def stop(self, handle: RunHandle) -> None:
        raise NotImplementedError
