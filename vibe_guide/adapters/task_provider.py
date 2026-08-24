"""Platform-neutral task/session bridge contracts."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple


class ProviderUnavailable(RuntimeError):
    """Raised when a visible bridge has not been verified or supplied."""


@dataclass(frozen=True)
class TaskBinding:
    provider: str
    mode: str
    role: str
    issue_id: str
    task_id: Optional[str] = None
    host: Optional[str] = None
    worktree: Optional[str] = None
    branch: Optional[str] = None
    status_file: Optional[str] = None
    handoff_file: Optional[str] = None
    cursor: Optional[str] = None
    visible: bool = False
    limitations: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self):
        result = self.__dict__.copy()
        result["limitations"] = list(self.limitations)
        if self.provider == "codex-thread":
            result["threadId"] = self.task_id
            result["hostId"] = self.host
        return result

    @property
    def thread_id(self):
        return self.task_id if self.provider == "codex-thread" else None

    @property
    def host_id(self):
        return self.host if self.provider == "codex-thread" else None


@dataclass(frozen=True)
class TaskUpdate:
    cursor: Optional[str]
    status: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def token(self):
        return self.cursor


@dataclass(frozen=True)
class VisibilityResult:
    visible: bool
    direct_enter: bool
    limitations: Tuple[str, ...] = ()

    def to_dict(self):
        return {
            "visible": self.visible,
            "direct_enter": self.direct_enter,
            "limitations": list(self.limitations),
        }


def _invoke(bridge: Any, name: str, *args, aliases=()):
    method = getattr(bridge, name, None)
    if method is None and isinstance(bridge, Mapping):
        method = bridge.get(name)
    if method is None:
        for alias in aliases:
            method = getattr(bridge, alias, None)
            if method is None and isinstance(bridge, Mapping):
                method = bridge.get(alias)
            if method is not None:
                break
    if method is None or not callable(method):
        raise ProviderUnavailable("verified bridge method missing: %s" % name)
    return method(*args)


class VisibleTaskProvider:
    """A bridge for a user-visible, directly enterable task.

    ``bridge`` is an injected, externally verified implementation.  Without
    one, calls fail closed instead of pretending a task was created.
    """

    mode = "visible"

    def __init__(self, provider: str, bridge: Any = None):
        self.provider = provider
        self.bridge = bridge

    def create(self, role: str, issue_id: str, contract_path: Path) -> TaskBinding:
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        aliases = ("create_thread",) if self.provider == "codex-thread" else ()
        raw = _invoke(
            self.bridge,
            "create",
            role,
            issue_id,
            Path(contract_path),
            aliases=aliases,
        )
        binding = self._binding(raw, role, issue_id)
        if not binding.task_id or not binding.host:
            raise ProviderUnavailable("visible task creation returned no task id/host")
        return binding

    def enter_or_locate(self, binding: TaskBinding) -> None:
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        aliases = ("enter_thread", "locate_thread") if self.provider == "codex-thread" else ()
        _invoke(self.bridge, "enter_or_locate", binding, aliases=aliases)

    def resume(self, binding: TaskBinding, contract_path: Path) -> None:
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        aliases = ("resume_thread",) if self.provider == "codex-thread" else ()
        _invoke(self.bridge, "resume", binding, Path(contract_path), aliases=aliases)

    def wait(self, binding: TaskBinding, cursor: Optional[str] = None) -> TaskUpdate:
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        aliases = ("wait_thread",) if self.provider == "codex-thread" else ()
        raw = _invoke(self.bridge, "wait", binding, cursor, aliases=aliases)
        if isinstance(raw, TaskUpdate):
            return raw
        if isinstance(raw, Mapping):
            return TaskUpdate(
                cursor=raw.get("cursor"),
                status=str(raw.get("status", "unknown")),
                payload=dict(raw.get("payload", {})),
            )
        raise ProviderUnavailable("visible task wait returned an invalid update")

    def visibility(self, binding: TaskBinding):
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        aliases = ("thread_visibility",) if self.provider == "codex-thread" else ()
        raw = _invoke(self.bridge, "visibility", binding, aliases=aliases)
        if isinstance(raw, VisibilityResult):
            return raw
        if isinstance(raw, Mapping):
            return VisibilityResult(
                visible=bool(raw.get("visible", False)),
                direct_enter=bool(raw.get("direct_enter", False)),
                limitations=tuple(raw.get("limitations", ())),
            )
        return VisibilityResult(visible=bool(raw), direct_enter=bool(raw))

    def _binding(self, raw: Any, role: str, issue_id: str) -> TaskBinding:
        if isinstance(raw, TaskBinding):
            return raw
        if not isinstance(raw, Mapping):
            raise ProviderUnavailable("visible task creation returned invalid binding")
        return TaskBinding(
            provider=str(raw.get("provider", self.provider)) or self.provider,
            mode="visible",
            role=role,
            issue_id=issue_id,
            task_id=raw.get("task_id") or raw.get("threadId"),
            host=raw.get("host") or raw.get("hostId"),
            worktree=raw.get("worktree"),
            branch=raw.get("branch"),
            status_file=raw.get("status_file"),
            handoff_file=raw.get("handoff_file"),
            cursor=raw.get("cursor"),
            visible=True,
        )


class BackgroundTaskProvider:
    mode = "background"

    def __init__(self, provider: str):
        self.provider = provider

    def create(self, role: str, issue_id: str, contract_path: Path) -> TaskBinding:
        return TaskBinding(
            provider=self.provider,
            mode="background",
            role=role,
            issue_id=issue_id,
            visible=False,
            limitations=(
                "不可见",
                "不可直接进入",
                "返工续接受限",
            ),
        )

    def resume(self, binding: TaskBinding, contract_path: Path) -> None:
        raise ProviderUnavailable("background task cannot be directly resumed")

    def enter_or_locate(self, binding: TaskBinding) -> None:
        raise ProviderUnavailable("background task cannot be directly entered")

    def wait(self, binding: TaskBinding, cursor: Optional[str] = None) -> TaskUpdate:
        raise ProviderUnavailable("background task has no visible wait cursor")

    def visibility(self, binding: TaskBinding):
        return VisibilityResult(
            visible=False,
            direct_enter=False,
            limitations=binding.limitations,
        )
