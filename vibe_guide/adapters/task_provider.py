"""Platform-neutral task/session bridges.

The public Codex App calls are injected into :class:`CodexAppBridge`; this
package never imports or fabricates the desktop tool implementation.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple


class ProviderUnavailable(RuntimeError):
    """A provider cannot safely create or continue a task."""


class ProviderPending(RuntimeError):
    """A provider returned a pending task handle without a real task id."""


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
    client_thread_id: Optional[str] = None
    visible: bool = False
    limitations: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def thread_id(self):
        return self.task_id if self.provider == "codex-app-visible" else None

    @property
    def host_id(self):
        return self.host if self.provider == "codex-app-visible" else None

    @property
    def pending(self):
        return bool(self.client_thread_id and not self.task_id)

    def to_dict(self):
        result = self.__dict__.copy()
        result["limitations"] = list(self.limitations)
        if self.provider == "codex-app-visible" and self.task_id:
            result["threadId"] = self.task_id
            result["hostId"] = self.host
        return result


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
    source: Optional[str] = None

    def to_dict(self):
        return {
            "visible": self.visible,
            "direct_enter": self.direct_enter,
            "limitations": list(self.limitations),
            "source": self.source,
        }


class CodexAppBridge:
    """Exact structured wrapper around the public Codex App tool schemas."""

    def __init__(
        self,
        *,
        create_thread: Callable[[Mapping[str, Any]], Any],
        navigate_to_codex_page: Callable[[Mapping[str, Any]], Any],
        send_message_to_thread: Callable[[Mapping[str, Any]], Any],
        wait_threads: Callable[[Mapping[str, Any]], Any],
        list_threads: Optional[Callable[[Mapping[str, Any]], Any]] = None,
    ):
        self._create_thread = create_thread
        self._navigate = navigate_to_codex_page
        self._send = send_message_to_thread
        self._wait = wait_threads
        self._list = list_threads

    def create(self, request: Mapping[str, Any]):
        _require_keys(request, ("prompt", "target"), "create_thread")
        if not isinstance(request["prompt"], str) or not isinstance(request["target"], Mapping):
            raise ProviderUnavailable("Codex create_thread request has invalid types")
        return self._create_thread(dict(request))

    def enter_or_locate(self, binding: TaskBinding):
        self._require_real(binding)
        return self._navigate({"threadId": binding.task_id})

    def resume(self, binding: TaskBinding, prompt: str):
        self._require_real(binding)
        request = {"threadId": binding.task_id, "prompt": prompt}
        if binding.host:
            request["hostId"] = binding.host
        return self._send(request)

    def wait(self, binding: TaskBinding, cursor: Optional[str], timeout_ms: int = 120000):
        self._require_real(binding)
        target = {"threadId": binding.task_id}
        if binding.host:
            target["hostId"] = binding.host
        if cursor:
            target["afterCursor"] = cursor
        raw = self._wait({"targets": [target], "timeoutMs": timeout_ms})
        if isinstance(raw, TaskUpdate):
            return raw
        if not isinstance(raw, Mapping):
            raise ProviderUnavailable("Codex wait_threads returned invalid result")
        return TaskUpdate(
            cursor=raw.get("cursor") or raw.get("nextCursor"),
            status=str(raw.get("status", "unknown")),
            payload=dict(raw),
        )

    def visibility(self, binding: TaskBinding):
        if binding.pending:
            return VisibilityResult(False, False, ("任务创建中",), "clientThreadId")
        if not binding.task_id or self._list is None:
            raise ProviderUnavailable("Codex list_threads visibility probe is unavailable")
        raw = self._list({"limit": 100})
        entries = ()
        if isinstance(raw, Mapping):
            entries = tuple(raw.get("pinnedThreads", ())) + tuple(raw.get("threads", ()))
        found = any(
            isinstance(item, Mapping)
            and item.get("threadId") == binding.task_id
            and (not binding.host or item.get("hostId") in (None, binding.host))
            for item in entries
        )
        return VisibilityResult(found, found, () if found else ("任务未出现在列表",), "list_threads")

    def resolve_pending(self, binding: TaskBinding):
        if not binding.pending:
            return binding
        if self._list is None:
            raise ProviderUnavailable("Codex list_threads pending recovery is unavailable")
        raw = self._list({"limit": 100})
        entries = ()
        if isinstance(raw, Mapping):
            entries = tuple(raw.get("pinnedThreads", ())) + tuple(raw.get("threads", ()))
        for item in entries:
            if isinstance(item, Mapping) and item.get("clientThreadId") == binding.client_thread_id and item.get("threadId"):
                return TaskBinding(
                    provider=binding.provider,
                    mode=binding.mode,
                    role=binding.role,
                    issue_id=binding.issue_id,
                    task_id=item.get("threadId"),
                    host=item.get("hostId") or binding.host,
                    worktree=binding.worktree,
                    branch=binding.branch,
                    status_file=binding.status_file,
                    handoff_file=binding.handoff_file,
                    cursor=binding.cursor,
                    visible=True,
                )
        return binding

    @staticmethod
    def _require_real(binding: TaskBinding):
        if binding.pending:
            raise ProviderPending("clientThreadId is pending and is not a threadId")
        if not binding.task_id:
            raise ProviderUnavailable("Codex binding has no threadId")


class VisibleTaskProvider:
    """Provider for user-visible tasks with exact identity and cursor data."""

    mode = "visible"

    def __init__(
        self,
        provider: str,
        bridge: Any = None,
        prompt_factory: Optional[Callable[[str, str, Path], str]] = None,
        target: Optional[Mapping[str, Any]] = None,
    ):
        self.provider = provider
        self.bridge = bridge
        self.prompt_factory = prompt_factory
        self.target = dict(target or {"type": "projectless"})

    def create(self, role: str, issue_id: str, contract_path: Path) -> TaskBinding:
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        contract_path = Path(contract_path)
        prompt = (
            self.prompt_factory(role, issue_id, contract_path)
            if self.prompt_factory
            else "请执行 %s 任务 %s，合同：%s。" % (role, issue_id, contract_path)
        )
        if isinstance(self.bridge, CodexAppBridge):
            raw = self.bridge.create({"prompt": prompt, "target": dict(self.target)})
        else:
            method = getattr(self.bridge, "create", None)
            if not callable(method):
                raise ProviderUnavailable("verified visible bridge create is missing")
            raw = method(role, issue_id, contract_path)
        binding = self._binding(raw, role, issue_id)
        if not binding.task_id and not binding.client_thread_id:
            raise ProviderUnavailable("visible task creation returned no durable task identity")
        return binding

    def enter_or_locate(self, binding: TaskBinding) -> None:
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        if isinstance(self.bridge, CodexAppBridge):
            self.bridge.enter_or_locate(binding)
            return
        self._invoke(binding, "enter_or_locate")

    def resume(self, binding: TaskBinding, contract_path: Path) -> None:
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        contract_path = Path(contract_path)
        if isinstance(self.bridge, CodexAppBridge):
            self.bridge.resume(binding, "请继续处理合同：%s。" % contract_path)
            return
        self._invoke(binding, "resume", contract_path)

    def wait(self, binding: TaskBinding, cursor: Optional[str] = None) -> TaskUpdate:
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        if isinstance(self.bridge, CodexAppBridge):
            return self.bridge.wait(binding, cursor)
        raw = self._invoke(binding, "wait", cursor)
        if isinstance(raw, TaskUpdate):
            return raw
        if isinstance(raw, Mapping):
            return TaskUpdate(raw.get("cursor"), str(raw.get("status", "unknown")), dict(raw))
        raise ProviderUnavailable("visible task wait returned invalid result")

    def visibility(self, binding: TaskBinding):
        if self.bridge is None:
            raise ProviderUnavailable("visible task bridge is not configured")
        if isinstance(self.bridge, CodexAppBridge):
            return self.bridge.visibility(binding)
        raw = self._invoke(binding, "visibility")
        if isinstance(raw, VisibilityResult):
            return raw
        if isinstance(raw, Mapping):
            return VisibilityResult(bool(raw.get("visible")), bool(raw.get("direct_enter")))
        return VisibilityResult(bool(raw), bool(raw))

    def resolve_pending(self, binding: TaskBinding):
        if isinstance(self.bridge, CodexAppBridge):
            return self.bridge.resolve_pending(binding)
        return binding

    def _invoke(self, binding: TaskBinding, method_name: str, *args):
        method = getattr(self.bridge, method_name, None)
        if not callable(method):
            raise ProviderUnavailable("verified visible bridge method missing: %s" % method_name)
        return method(binding, *args)

    def _binding(self, raw: Any, role: str, issue_id: str) -> TaskBinding:
        if isinstance(raw, TaskBinding):
            if raw.provider != self.provider or raw.mode != self.mode:
                raise ProviderUnavailable("visible bridge returned mismatched provider binding")
            return raw
        if not isinstance(raw, Mapping):
            raise ProviderUnavailable("visible task creation returned invalid binding")
        task_id = raw.get("task_id") or raw.get("threadId")
        host = raw.get("host") or raw.get("hostId")
        client_thread_id = raw.get("client_thread_id") or raw.get("clientThreadId")
        return TaskBinding(
            provider=self.provider,
            mode="visible",
            role=role,
            issue_id=issue_id,
            task_id=task_id,
            host=host,
            worktree=raw.get("worktree"),
            branch=raw.get("branch"),
            status_file=raw.get("status_file"),
            handoff_file=raw.get("handoff_file"),
            cursor=raw.get("cursor"),
            client_thread_id=client_thread_id,
            visible=bool(task_id),
            limitations=("任务创建中",) if client_thread_id and not task_id else (),
        )


class BackgroundTaskProvider:
    mode = "background"

    def __init__(self, provider: str, launcher: Optional[Callable[..., Any]] = None):
        self.provider = provider
        self.launcher = launcher

    def create(self, role: str, issue_id: str, contract_path: Path) -> TaskBinding:
        launch = self.launcher
        if not callable(launch) and callable(getattr(launch, "launch", None)):
            launch = launch.launch
        if not callable(launch):
            raise ProviderUnavailable("verified background launcher is not configured")
        raw = launch(role, issue_id, Path(contract_path))
        if isinstance(raw, TaskBinding):
            binding = raw
        elif isinstance(raw, Mapping):
            task_id = raw.get("task_id") or raw.get("run_id") or raw.get("handle")
            if not task_id:
                raise ProviderUnavailable("background launcher returned no durable handle")
            binding = TaskBinding(
                provider=self.provider,
                mode="background",
                role=role,
                issue_id=issue_id,
                task_id=str(task_id),
                host=raw.get("host"),
                worktree=raw.get("worktree"),
                branch=raw.get("branch"),
                status_file=raw.get("status_file"),
                handoff_file=raw.get("handoff_file"),
                cursor=raw.get("cursor"),
                visible=False,
                limitations=("不可见", "不可直接进入", "返工续接受限"),
            )
        else:
            raise ProviderUnavailable("background launcher returned invalid handle")
        if binding.provider != self.provider or binding.mode != self.mode:
            raise ProviderUnavailable("background launcher returned mismatched provider binding")
        if not binding.task_id:
            raise ProviderUnavailable("background launcher returned no durable handle")
        return binding

    def resume(self, binding: TaskBinding, contract_path: Path) -> None:
        raise ProviderUnavailable("background task cannot be directly resumed")

    def enter_or_locate(self, binding: TaskBinding) -> None:
        raise ProviderUnavailable("background task cannot be directly entered")

    def wait(self, binding: TaskBinding, cursor: Optional[str] = None) -> TaskUpdate:
        raise ProviderUnavailable("background task has no visible wait cursor")

    def visibility(self, binding: TaskBinding):
        return VisibilityResult(False, False, binding.limitations, "background")


def _require_keys(value: Mapping[str, Any], keys, operation: str):
    missing = [key for key in keys if key not in value]
    if missing:
        raise ProviderUnavailable("%s request missing: %s" % (operation, ", ".join(missing)))
