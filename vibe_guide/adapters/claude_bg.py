"""Claude Code background-session launcher (degraded, invisible provider).

Drives ``claude --bg`` sessions through an injectable command executor so unit
tests never start a real ``claude``.  Credentials (an ``apiKeyHelper`` settings
file path) and the default model come only from the user-level
``$VIBE_HOME/config.json``; vibe records the path, never reads or stores keys.
Anything it cannot verify is reported as ``ClaudeBgUnknown`` -- neither success
nor permanent unavailability.
"""

from dataclasses import dataclass
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ..authorization import (
    REMOTE_GIT_BAN_TEXT,
    REMOTE_GIT_DISALLOWED_TOOLS,
    REMOTE_GIT_LAUNCH_ENV,
)
from ..paths import ProjectPaths
from .task_provider import BackgroundTaskRouting, ProviderUnavailable, VisibilityResult

CLAUDE_BG_PROVIDER = "claude-code-background"

#: Worktree preflight vocabulary (#235).  The codes are fixed enums a caller
#: may persist as-is; the events are what the supervisor's event stream
#: records when a dispatch had to build a node worktree or was blocked by one.
WORKTREE_MISSING = "worktree_missing"
WORKTREE_MISMATCH = "worktree_mismatch"
WORKTREE_CREATE_FAILED = "worktree_create_failed"
#: Accepted upstream branches of a downstream node do not merge cleanly.
WORKTREE_MERGE_CONFLICT = "worktree_merge_conflict"
WORKTREE_REASON_CODES = (
    WORKTREE_MISSING, WORKTREE_MISMATCH, WORKTREE_CREATE_FAILED, WORKTREE_MERGE_CONFLICT,
)
WORKTREE_CREATED_EVENT = "worktree_created"
WORKTREE_BLOCKED_EVENT = "worktree_preflight_blocked"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_BACKGROUNDED = re.compile(r"backgrounded\s*[·•.]\s*([0-9a-f]{8})\b")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_STATES = {"working", "done", "failed", "blocked", "stopped", "idle"}
_DEFAULT_TIMEOUT = 60.0


class ClaudeBgUnknown(ProviderUnavailable):
    """The background state is unknown; carries the reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


WORKSPACE_NOT_TRUSTED = "workspace_not_trusted"
_NOT_TRUSTED = re.compile(r"workspace not trusted", re.IGNORECASE)


class ClaudeBgWorkspaceUntrusted(ClaudeBgUnknown):
    """``claude --bg`` refused to start because the directory was never trusted.

    Retrying cannot help: someone has to run ``claude`` there once and accept
    the prompt.  ``reason_code`` and the directory are program-authored, so a
    caller can persist them verbatim while the CLI's own text stays redacted.
    """

    def __init__(self, reason_code: str, reason: str, worktree: str, branch: str = ""):
        super().__init__(reason)
        self.reason_code = reason_code
        self.worktree = worktree
        self.branch = branch
        self.command = "cd %s && claude" % worktree
        self.observation = {"reason_code": reason_code, "worktree": worktree, "branch": branch}


class ClaudeBgWorktreeBlocked(ClaudeBgUnknown):
    """A worktree precondition this launcher will not repair on its own.

    ``reason_code`` is a fixed enum, never provider text, so a caller can
    persist it verbatim (#235): "the directory is not there" is a fact about
    this machine, and hiding it behind a redacted ``reason`` is what left the
    node waiting for an action nobody knew it needed.
    """

    def __init__(
        self,
        reason_code: str,
        reason: str,
        worktree: str,
        branch: str,
        command: Optional[str] = None,
    ):
        super().__init__(reason)
        self.reason_code = reason_code
        self.worktree = worktree
        self.branch = branch
        self.command = command
        # A structured observation without any provider-text key, so failure
        # classification still fails closed instead of parsing free text.
        self.observation = {
            "reason_code": reason_code,
            "worktree": worktree,
            "branch": branch,
            "worktree_command": command,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "worktree": self.worktree,
            "branch": self.branch,
            "worktree_command": self.command,
        }


class ClaudeBgConfigRefused(ValueError):
    """The project config tried to set a user-level-only background setting."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandExecutor = Callable[[Sequence[str], Path, float], CommandResult]

# Set by the Claude Code desktop host for its own sessions; inherited by a
# background session it makes the session ignore --settings apiKeyHelper and
# come up "Not logged in" (verified live 2026-10-14, the only variable needed).
_HOST_ONLY_ENV = ("CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",)

# A worker that asks the user parks in `blocked` until a human answers; the
# supervisor decides engineering questions itself.  Variadic, so it must come
# before `--` or the prompt is swallowed as a tool name.  Launch only: a
# session keeps its saved options, and passing any option to `--resume` starts
# a copy instead of continuing it (verified live).
_NO_USER_QUESTIONS = ("--disallowedTools", "AskUserQuestion")

# Waits before each resume attempt after `claude stop` (seconds; live: 0s
# copies, 5s continued the same session).
_RESUME_SETTLE_SECONDS = (5, 10, 20)


def subprocess_executor(
    argv: Sequence[str], cwd: Path, timeout: float, extra_env: Optional[Mapping[str, str]] = None
) -> CommandResult:
    env = {key: value for key, value in os.environ.items() if key not in _HOST_ONLY_ENV}
    env.update(extra_env or {})
    try:
        done = subprocess.run(
            list(argv),
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise ClaudeBgUnknown("claude command not found")
    except subprocess.TimeoutExpired:
        raise ClaudeBgUnknown("%s timed out after %ss" % (argv[0], timeout))
    except OSError as error:
        raise ClaudeBgUnknown("cannot run %s: %s" % (argv[0], error))
    return CommandResult(done.returncode, done.stdout or "", done.stderr or "")


def _accepts_env(executor) -> bool:
    """Whether an injected executor takes the optional fourth ``env`` argument."""
    try:
        params = list(inspect.signature(executor).parameters.values())
    except (TypeError, ValueError):
        return False
    if any(p.kind is p.VAR_POSITIONAL for p in params):
        return True
    return len([p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]) >= 4


@dataclass(frozen=True)
class ClaudeBgConfig:
    settings_path: str
    model: str = "sonnet"
    claude_command: str = "claude"
    git_user_name: Optional[str] = None
    git_user_email: Optional[str] = None
    timeout: float = _DEFAULT_TIMEOUT


def _read_json(path: Path) -> Dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_claude_bg_config(paths: ProjectPaths) -> ClaudeBgConfig:
    """Read ``claude_bg`` settings from the user-level config only."""
    try:
        project_dir = paths.vibe_dir
        project_path = project_dir / "config.json"
        if project_path.is_symlink():
            raise ClaudeBgUnknown("cannot verify project config: config.json is a symlink")
        paths.resolve_vibe_path("config.json")
    except ValueError as error:
        raise ClaudeBgUnknown("cannot verify project config: %s" % error)
    project = {}
    if project_path.exists():
        try:
            project = json.loads(project_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ClaudeBgUnknown("cannot verify project config: %s" % type(error).__name__)
        if not isinstance(project, dict):
            raise ClaudeBgUnknown("cannot verify project config: not a JSON object")
    if "claude_bg" in project:
        section = project["claude_bg"]
        if not isinstance(section, dict):
            raise ClaudeBgConfigRefused(
                "project config claude_bg must be an object with only 'enabled'; "
                "credentials, command and model can only be written in the "
                "user-level $VIBE_HOME/config.json"
            )
        extra = sorted(set(section) - {"enabled"})
        if extra:
            raise ClaudeBgConfigRefused(
                "project config must not set claude_bg.%s; credentials, command and "
                "model can only be written in the user-level $VIBE_HOME/config.json"
                % ", ".join(extra)
            )
    try:
        user = _read_json(paths.resolve_vibe_home_path("config.json"))
    except ValueError:
        raise ClaudeBgUnknown("user-level config escapes VIBE_HOME")
    bg = user.get("claude_bg")
    bg = bg if isinstance(bg, dict) else {}
    settings_path = bg.get("settings_path")
    if not isinstance(settings_path, str) or not settings_path.strip():
        raise ClaudeBgUnknown(
            "background credentials not configured: set claude_bg.settings_path "
            "in the user-level $VIBE_HOME/config.json"
        )
    if not Path(settings_path).is_absolute():
        raise ClaudeBgUnknown("claude_bg.settings_path must be an absolute path")

    def text(name: str, default: Optional[str]) -> Optional[str]:
        value = bg.get(name)
        return value if isinstance(value, str) and value.strip() else default

    timeout = bg.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        timeout = _DEFAULT_TIMEOUT
    return ClaudeBgConfig(
        settings_path=settings_path,
        model=text("model", "sonnet"),
        claude_command=text("claude_command", "claude"),
        git_user_name=text("git_user_name", None),
        git_user_email=text("git_user_email", None),
        timeout=float(timeout),
    )


def list_agent_sessions(
    config: ClaudeBgConfig, cwd: Path, executor: CommandExecutor = subprocess_executor
) -> List[Mapping[str, Any]]:
    """Every session ``claude agents --json --all`` reports, stopped ones included."""
    result = executor([config.claude_command, "agents", "--json", "--all"], cwd, config.timeout)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-200:]
        raise ClaudeBgUnknown("claude agents failed (exit %s): %s" % (result.returncode, tail))
    try:
        data = json.loads(_ANSI.sub("", result.stdout or ""))
    except json.JSONDecodeError:
        raise ClaudeBgUnknown("claude agents output is not valid JSON")
    if isinstance(data, dict):
        data = data.get("agents", data.get("sessions"))
    if not isinstance(data, list):
        raise ClaudeBgUnknown("claude agents output has no session list")
    return [item for item in data if isinstance(item, dict)]


def _default_transcript_locator(task_id: str) -> Optional[Path]:
    root = Path.home() / ".claude" / "projects"
    if not _UUID.fullmatch(task_id) or not root.is_dir():
        return None
    for candidate in root.glob("*/%s.jsonl" % task_id):
        return candidate
    return None


class ClaudeBgLauncher:
    def __init__(
        self,
        routing: BackgroundTaskRouting,
        config: ClaudeBgConfig,
        executor: CommandExecutor = subprocess_executor,
        prompt_builder: Optional[Callable[[str, str, Path], str]] = None,
        model: Optional[str] = None,
        transcript_locator: Callable[[str], Optional[Path]] = _default_transcript_locator,
        sleeper: Callable[[float], None] = time.sleep,
        upstream_branches: Optional[Sequence[str]] = None,
        remote_git_deny: bool = False,
        base_sha: Optional[str] = None,
    ):
        #: True only when the authorization card says ``remote_git_actions:
        #: deny``: ``launch`` then appends the ban to the prompt and adds
        #: ``--disallowedTools`` plus ``GIT_ALLOW_PROTOCOL``.  False adds nothing.
        self.remote_git_deny = bool(remote_git_deny)
        self.routing = routing
        self.sleeper = sleeper
        self.config = config
        self.executor = executor
        self.prompt_builder = prompt_builder
        self.model = model or config.model
        self.transcript_locator = transcript_locator
        #: Accepted upstream node branches, in topological order, merged into
        #: the baseline of a worktree this launcher builds.
        self.upstream_branches = [str(item) for item in (upstream_branches or ())]
        #: Optional supervisor-owned baseline (visible dispatch).  Background
        #: callers keep their historical repository-HEAD baseline.
        self.base_sha = base_sha
        #: What ``launch`` had to do about the node worktree (#235), read back
        #: by the CLI after the dispatch returns: ``{"event": ...,
        #: "worktree": ..., "branch": ..., "worktree_base": ..., ...}``.
        #: ``None`` until a launch ran, so a caller never mistakes "no launch
        #: yet" for "the worktree was already there".
        self.worktree_preflight: Optional[Dict[str, Any]] = None

    # -- helpers -----------------------------------------------------------
    def _run(self, argv: List[str], cwd: Path, env: Optional[Mapping[str, str]] = None) -> CommandResult:
        if env and _accepts_env(self.executor):
            return self.executor(argv, cwd, self.config.timeout, dict(env))
        return self.executor(argv, cwd, self.config.timeout)

    def _claude(self, *args: str) -> List[str]:
        return [self.config.claude_command] + list(args)

    def _worktree(self) -> Path:
        path = Path(self.routing.worktree)
        if not path.is_dir():
            raise ClaudeBgUnknown("worktree is not an existing directory: %s" % path)
        return path

    # -- worktree preflight (#235) ----------------------------------------
    def _blocked(
        self, reason_code: str, reason: str, command: Optional[str] = None,
        conflict_files: Optional[List[str]] = None,
    ) -> ClaudeBgWorktreeBlocked:
        self.worktree_preflight = {
            "event": WORKTREE_BLOCKED_EVENT,
            "reason_code": reason_code,
            "worktree": self.routing.worktree,
            "branch": self.routing.branch,
            "worktree_command": command,
        }
        if conflict_files:
            self.worktree_preflight["conflict_files"] = list(conflict_files)
        return ClaudeBgWorktreeBlocked(
            reason_code, reason, self.routing.worktree, self.routing.branch, command
        )

    def _prepare_worktree(self) -> Path:
        """The dispatch worktree, built only when nothing contradicts it.

        ``launch`` is the only caller: every other operation must observe the
        worktree as it is.  A directory that is already there is never
        switched, reset or overwritten -- a contradiction is reported, not
        repaired, because the other side of that directory may be live work.
        """
        path = Path(self.routing.worktree)
        if path.is_dir():
            return self._verify_worktree(path)
        return self._create_worktree(path)

    def _verify_worktree(self, path: Path) -> Path:
        if not (path / ".git").exists():
            # An ordinary directory has no verifiable branch, HEAD or writer
            # identity.  It is never a degraded dispatch target.
            repo = self._repository_root(path)
            manual = self._manual_command(
                path, self.routing.branch, None, "普通目录不是 git worktree"
            )
            if repo is not None:
                try:
                    base = self._read_base(repo)
                    if self._branch_exists(repo, self.routing.branch):
                        manual = "git worktree add %s %s" % (path, self.routing.branch)
                    else:
                        manual = self._manual_command(path, self.routing.branch, base)
                except ClaudeBgUnknown:
                    # The block below remains fail-closed; the command says
                    # why no trustworthy baseline could be filled in.
                    pass
            raise self._blocked(
                WORKTREE_MISSING,
                "worktree %s is an ordinary directory without .git; refusing to dispatch into it"
                % path,
                manual,
            )
        try:
            branch = self._git_branch(path)
            head = self._git_text(["rev-parse", "HEAD"], path)
            dirty = self._git_text(
                ["status", "--porcelain", "--untracked-files=all"], path
            )
        except ClaudeBgUnknown as error:
            raise self._blocked(
                WORKTREE_MISMATCH,
                "cannot read the git state of worktree %s (%s); refusing to dispatch into it"
                % (path, error.reason),
                "git -C %s rev-parse --abbrev-ref HEAD" % path,
            )
        if self.base_sha and dirty:
            # Only the visible initial binding contract requires a clean
            # worktree.  Background continuation/rework deliberately starts
            # a fresh session beside uncommitted work in the same worktree.
            raise self._blocked(
                WORKTREE_MISMATCH,
                "worktree %s has uncommitted changes; refusing to dispatch into it" % path,
                "git -C %s status --porcelain --untracked-files=all" % path,
            )
        if self.base_sha and head.casefold() != str(self.base_sha).casefold():
            raise self._blocked(
                WORKTREE_MISMATCH,
                "worktree %s HEAD %s is not the contract base %s" % (
                    path, head, self.base_sha
                ),
                "git -C %s rev-parse HEAD" % path,
            )
        if branch != self.routing.branch:
            raise self._blocked(
                WORKTREE_MISMATCH,
                "worktree %s is on %s, not the contract branch %s; vibe does not switch or "
                "overwrite an existing worktree"
                % (path, branch or "a detached HEAD", self.routing.branch),
                "git -C %s rev-parse --abbrev-ref HEAD" % path,
            )
        self.worktree_preflight = {
            "event": None,
            "worktree": str(path),
            "branch": branch,
            "worktree_base": head,
        }
        return path

    def _manual_command(
        self, path: Path, branch: str, base: Optional[str], why: str = ""
    ) -> str:
        """The command a supervisor has to run for this node (#235).

        A literal ``<base>`` placeholder used to stand in for the run
        baseline: the string looked paste-able but failed with a git error,
        and the supervisor had to guess which commit vibe meant.  With the
        baseline read the command names the real commit; without one it says
        the baseline is unavailable -- and why -- instead of faking a command.
        """
        if base:
            return "git worktree add %s -b %s %s" % (path, branch, base)
        return "base 不可用（%s）：无可直接执行的 git worktree add 命令" % (
            why or "尚未读取仓库基线",
        )

    def _create_worktree(self, path: Path) -> Path:
        repo = self._repository_root(path)
        branch = self.routing.branch
        # No baseline has been read yet here, so the command must not pretend
        # one exists.
        manual = self._manual_command(
            path, branch, None, "分支名无法传给 git，且基线尚未读取"
        )
        if branch.startswith("-"):
            raise self._blocked(
                WORKTREE_MISSING,
                "worktree %s does not exist and branch %r cannot be passed to git" % (path, branch),
                manual,
            )
        if repo is None:
            raise self._blocked(
                WORKTREE_MISSING,
                "worktree %s does not exist and no git repository contains it; create it "
                "inside a repository before dispatch" % path,
                self._manual_command(path, branch, None, "%s 不在任何 git 仓库内" % path),
            )
        try:
            base = self._read_base(repo)
        except ClaudeBgUnknown as error:
            raise self._blocked(
                WORKTREE_CREATE_FAILED,
                "cannot read the run baseline from %s (%s); worktree %s was not created"
                % (repo, error.reason, path),
                self._manual_command(path, branch, None, "无法从 %s 读取基线" % repo),
            )
        # The baseline is known from here on, so every command below names the
        # real commit instead of a placeholder.
        manual = self._manual_command(path, branch, base)
        if self._branch_exists(repo, branch):
            raise self._blocked(
                WORKTREE_MISSING,
                "worktree %s does not exist and branch %s already exists; vibe does not "
                "reuse or overwrite an existing branch: git worktree add %s %s"
                % (path, branch, path, branch),
                "git worktree add %s %s" % (path, branch),
            )
        if self.upstream_branches:
            base = self._compose_upstream_base(repo, path, branch, base)
            manual = self._manual_command(path, branch, base)
        result = self._run(["git", "worktree", "add", str(path), "-b", branch, base], repo)
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()[-200:]
            raise self._blocked(
                WORKTREE_CREATE_FAILED,
                "git worktree add for %s failed (exit %s): %s" % (path, result.returncode, tail),
                manual,
            )
        try:
            head = self._git_text(["rev-parse", "HEAD"], path)
            created_branch = self._git_branch(path)
        except ClaudeBgUnknown as error:
            raise self._blocked(
                WORKTREE_MISMATCH,
                "worktree %s was created but its git state is unreadable (%s)"
                % (path, error.reason),
                manual,
            )
        if head.lower() != base.lower() or created_branch != branch:
            raise self._blocked(
                WORKTREE_MISMATCH,
                "worktree %s was created at %s on %s, not %s on %s"
                % (path, head, created_branch or "a detached HEAD", base, branch),
                manual,
            )
        self.worktree_preflight = {
            "event": WORKTREE_CREATED_EVENT,
            "worktree": str(path),
            "branch": branch,
            "worktree_base": base,
        }
        return path

    def _read_base(self, repo: Path) -> str:
        """Read the authorized baseline, or the repository HEAD for legacy callers."""
        if self.base_sha:
            return self._git_text(
                ["rev-parse", "--verify", self.base_sha + "^{commit}"], repo
            )
        return self._git_text(["rev-parse", "HEAD"], repo)

    def _compose_upstream_base(self, repo: Path, path: Path, branch: str, base: str) -> str:
        """Main plus every accepted upstream branch; nothing is built on failure."""
        from ..worktree_base import (
            UpstreamBaseUnavailable, UpstreamMergeConflict, compose_base,
        )

        try:
            return compose_base(lambda args: self._run(["git"] + args, repo), base,
                                self.upstream_branches)
        except UpstreamMergeConflict as conflict:
            files = "、".join(conflict.files) or "（git 未列出文件）"
            raise self._blocked(
                WORKTREE_MERGE_CONFLICT,
                "accepted upstream branch %s does not merge cleanly into the baseline of %s; "
                "conflicting files: %s; worktree %s was not created"
                % (conflict.branch, branch, ", ".join(conflict.files), path),
                "上游分支 %s 与已合入的基线冲突（冲突文件：%s）：先在上游分支解决冲突或调整依赖，"
                "再重新派发本节点" % (conflict.branch, files),
                conflict_files=conflict.files,
            )
        except UpstreamBaseUnavailable as error:
            raise self._blocked(
                WORKTREE_CREATE_FAILED,
                "cannot compose the upstream baseline for %s (%s); worktree %s was not created"
                % (branch, error, path),
                self._manual_command(path, branch, None, str(error)),
            )

    def _repository_root(self, path: Path) -> Optional[Path]:
        """The nearest ancestor that holds a repository, or None."""
        try:
            current = Path(path).resolve()
        except OSError:
            return None
        for candidate in [current, *current.parents]:
            if (candidate / ".git").exists():
                return candidate
        return None

    def _git_text(self, args: List[str], cwd: Path) -> str:
        result = self._run(["git"] + list(args), cwd)
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()[-200:]
            raise ClaudeBgUnknown("git %s failed (exit %s): %s" % (" ".join(args), result.returncode, tail))
        return _ANSI.sub("", result.stdout or "").strip()

    def _git_branch(self, cwd: Path) -> str:
        """The checked-out branch; "" when HEAD is detached (a real answer)."""
        result = self._run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd)
        if result.returncode not in (0, 1):
            tail = (result.stderr or result.stdout or "").strip()[-200:]
            raise ClaudeBgUnknown(
                "git symbolic-ref failed (exit %s): %s" % (result.returncode, tail)
            )
        return _ANSI.sub("", result.stdout or "").strip()

    def _branch_exists(self, repo: Path, branch: str) -> bool:
        for ref in ("refs/heads/" + branch,):
            result = self._run(["git", "show-ref", "--verify", "--quiet", ref], repo)
            if result.returncode == 0:
                return True
            if result.returncode not in (1, 128):
                # Not "absent": the launcher cannot tell, so it must not create.
                tail = (result.stderr or result.stdout or "").strip()[-200:]
                raise self._blocked(
                    WORKTREE_CREATE_FAILED,
                    "cannot verify whether branch %s exists (exit %s): %s"
                    % (branch, result.returncode, tail),
                )
        return False

    def _require_ok(self, result: CommandResult, what: str) -> str:
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()[-200:]
            raise ClaudeBgUnknown("%s failed (exit %s): %s" % (what, result.returncode, tail))
        return _ANSI.sub("", result.stdout or "")

    def _entries(self) -> List[Mapping[str, Any]]:
        return list_agent_sessions(self.config, self._cwd_for_query(), self.executor)

    def _cwd_for_query(self) -> Path:
        return self._worktree()

    def _find(self, task_id: str) -> Mapping[str, Any]:
        for item in self._entries():
            if item.get("sessionId") == task_id:
                return item
        raise ClaudeBgUnknown("session %s not found in claude agents" % task_id)

    @staticmethod
    def _short(task_id: str) -> str:
        if not isinstance(task_id, str) or not _UUID.fullmatch(task_id):
            raise ClaudeBgUnknown("full lowercase session UUID required, got %r" % (task_id,))
        return task_id[:8]

    def _same_dir(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            return Path(value).resolve() == Path(self.routing.worktree).resolve()
        except OSError:
            return False

    # -- operations --------------------------------------------------------
    def launch(self, role: str, issue_id: str, contract_path: Path) -> Dict[str, Any]:
        # #235: a dispatch whose node worktree was never built used to die here
        # as an unreadable "unknown"; build it when nothing contradicts it, and
        # report a readable reason when something does.
        worktree = self._prepare_worktree()
        if self.prompt_builder:
            prompt = self.prompt_builder(role, issue_id, Path(contract_path))
        else:
            try:
                prompt = Path(contract_path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise ClaudeBgUnknown("cannot read contract: %s" % error)
        for key, value in (
            ("user.name", self.config.git_user_name),
            ("user.email", self.config.git_user_email),
        ):
            if value:
                # Plain `git config` in a linked worktree writes the shared
                # .git/config; --worktree refuses unless worktreeConfig is on.
                self._require_ok(self._run(["git", "config", "--worktree", key, value], worktree), "git config")
        name = "%s-%s" % (issue_id, role)
        started_ms = int(time.time() * 1000)
        # One --disallowedTools argument: background workers never get
        # AskUserQuestion (#328); under remote-git deny the remote git tools
        # are added to the same list rather than passing the option twice.
        denied_tools = [_NO_USER_QUESTIONS[1]]
        if self.remote_git_deny:
            if REMOTE_GIT_BAN_TEXT not in prompt:
                prompt = prompt + "\n" + REMOTE_GIT_BAN_TEXT
            denied_tools += list(REMOTE_GIT_DISALLOWED_TOOLS)
        deny_flags: List[str] = [_NO_USER_QUESTIONS[0]] + denied_tools
        try:
            started = self._run(
                self._claude(
                    "--bg",
                    "-n",
                    name,
                    "--model",
                    self.model,
                    "--settings",
                    self.config.settings_path,
                    "--permission-mode",
                    "bypassPermissions",
                    *deny_flags,
                    # A prompt starting with "-" is otherwise read as an option (live).
                    "--",
                    prompt,
                ),
                worktree,
                REMOTE_GIT_LAUNCH_ENV if self.remote_git_deny else None,
            )
            if started.returncode != 0 and _NOT_TRUSTED.search(
                _ANSI.sub("", (started.stderr or "") + "\n" + (started.stdout or ""))
            ):
                raise ClaudeBgWorkspaceUntrusted(
                    WORKSPACE_NOT_TRUSTED,
                    "claude refused to start in %s: the workspace was never trusted; run "
                    "`claude` there once and accept the trust prompt, then retry" % worktree,
                    str(worktree), self.routing.branch,
                )
            out = self._require_ok(started, "claude --bg")
            match = _BACKGROUNDED.search(out)
            if not match:
                raise ClaudeBgUnknown("cannot parse 'backgrounded' short id from claude output")
        except ClaudeBgWorkspaceUntrusted:
            raise
        except ClaudeBgUnknown as error:
            raise ClaudeBgUnknown("%s; %s" % (error.reason, self._stop_strays(name, started_ms)))
        short_id = match.group(1)
        try:
            return self._bind(short_id, role, issue_id)
        except ClaudeBgUnknown as error:
            try:
                self._run(self._claude("stop", short_id), worktree)
            except ClaudeBgUnknown:
                pass
            raise ClaudeBgUnknown(
                "session %s started but could not be confirmed (%s); stop attempted"
                % (short_id, error.reason)
            )

    def _stop_strays(self, name: str, started_ms: int) -> str:
        """Stop sessions this failed launch may have started (live: a client
        timeout still leaves a working bypassPermissions session)."""
        try:
            entries = self._entries()
        except ClaudeBgUnknown as error:
            return "stray check unknown (%s)" % error.reason
        stopped = []
        unreadable = False
        for item in entries:
            started = item.get("startedAt")
            session_id = item.get("sessionId")
            if (
                item.get("name") != name
                or not self._same_dir(item.get("cwd"))
                or item.get("state") == "stopped"
                or not isinstance(session_id, str)
                or not _UUID.fullmatch(session_id)
            ):
                continue
            # startedAt is epoch milliseconds (live: 1789700548837).
            if not isinstance(started, (int, float)) or isinstance(started, bool):
                unreadable = True
                continue
            if started < started_ms - 5000:
                continue
            short = session_id[:8]
            try:
                result = self._run(self._claude("stop", short), self._worktree())
                stopped.append(short if result.returncode == 0 else short + "(stop failed)")
            except ClaudeBgUnknown:
                stopped.append(short + "(stop unknown)")
        if unreadable:
            return "stray check unknown (session start time unreadable); stopped: %s" % (
                ", ".join(stopped) or "none")
        return "stray sessions stopped: %s" % (", ".join(stopped) or "none")

    def _bind(self, short_id: str, role: str, issue_id: str) -> Dict[str, Any]:
        found = [
            item
            for item in self._entries()
            if isinstance(item.get("sessionId"), str) and item["sessionId"][:8] == short_id
        ]
        if not found:
            raise ClaudeBgUnknown("session %s not found in claude agents" % short_id)
        if len(found) > 1:
            raise ClaudeBgUnknown("short id %s matches several sessions" % short_id)
        entry = found[0]
        session_id = entry["sessionId"]
        if not _UUID.fullmatch(session_id):
            raise ClaudeBgUnknown("claude agents returned a malformed session id")
        if not self._same_dir(entry.get("cwd")):
            raise ClaudeBgUnknown("session %s cwd missing or outside the authorized worktree" % short_id)
        return {
            "task_id": session_id,
            "short_id": short_id,
            "provider": CLAUDE_BG_PROVIDER,
            "mode": "background",
            "role": role,
            "issue_id": issue_id,
            "host": self.routing.host,
            "worktree": self.routing.worktree,
            "branch": self.routing.branch,
            "status_file": self.routing.status_file,
            "handoff_file": self.routing.handoff_file,
        }

    def status(self, task_id: str) -> str:
        self._short(task_id)
        state = self._find(task_id).get("state")
        if state not in _STATES:
            raise ClaudeBgUnknown("unknown session state: %r" % (state,))
        return state

    def final_reply(self, task_id: str) -> Optional[str]:
        self._short(task_id)
        path = self.transcript_locator(task_id)
        if path is None:
            return None
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as error:
            raise ClaudeBgUnknown("cannot read transcript: %s" % error)
        reply = None
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict) or item.get("type") != "assistant":
                continue
            message = item.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                parts = [content]
            elif isinstance(content, list):
                parts = [
                    block.get("text")
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ]
            else:
                parts = []
            if parts:
                reply = "\n".join(parts)
        return reply

    def stop(self, task_id: str) -> None:
        short = self._short(task_id)
        result = self._run(self._claude("stop", short), self._worktree())
        if result.returncode != 0:
            # `claude stop` can fail for a session that already exited after
            # delivering.  Its listed end state proves it is not running --
            # the fallback `resume` uses, but stricter: `resume` only refuses
            # `working`, while a stop must prove nothing will write again, so
            # `blocked` (alive, waiting) and an unknown state stay failures.
            try:
                state = self.status(task_id)
            except ClaudeBgUnknown:
                state = None
            if state in {"done", "stopped", "failed"}:
                return
        self._require_ok(result, "claude stop")

    def resume(self, task_id: str, message: str) -> None:
        # A short id would copy the session instead of continuing it.
        short = self._short(task_id)
        worktree = self._worktree()
        stopped = self._run(self._claude("stop", short), worktree)
        if stopped.returncode != 0:
            # Only resume when the session is verifiably not running.
            state = self.status(task_id)
            if state == "working":
                raise ClaudeBgUnknown("session %s is still working and could not be stopped" % short)
        for attempt, delay in enumerate(_RESUME_SETTLE_SECONDS):
            # `claude stop` returns before the session stops counting as
            # running; resuming inside that window starts a copy (live).
            self.sleeper(delay)
            deny = self.remote_git_deny
            result = self._run(
                self._claude(
                    "--bg", "--resume", task_id,
                    *(["--disallowedTools"] + list(REMOTE_GIT_DISALLOWED_TOOLS) if deny else []),
                    "--", message,
                ),
                worktree,
                REMOTE_GIT_LAUNCH_ENV if deny else None,
            )
            combined = _ANSI.sub("", (result.stdout or "") + "\n" + (result.stderr or ""))
            if result.returncode != 0:
                tail = combined.strip()[-200:]
                raise ClaudeBgUnknown("claude --bg --resume failed (exit %s): %s" % (result.returncode, tail))
            match = _BACKGROUNDED.search(combined)
            if "started a copy" not in combined and match and match.group(1) == short:
                return
            copy = match.group(1) if match else None
            cleaned = True
            if copy and copy != short:
                for op in ("stop", "rm"):
                    try:
                        cleaned = self._run(self._claude(op, copy), worktree).returncode == 0 and cleaned
                    except ClaudeBgUnknown:
                        cleaned = False
            if "is already running" in combined and attempt + 1 < len(_RESUME_SETTLE_SECONDS):
                continue
            if "is already running" in combined:
                raise ClaudeBgUnknown(
                    "session %s still running after stop; resume gave up after %d attempts (copy %s %s)"
                    % (short, len(_RESUME_SETTLE_SECONDS), copy or "unknown",
                       "removed" if cleaned else "cleanup failed")
                )
            raise ClaudeBgUnknown(
                "resume of %s did not continue the same session (copy short id: %s); copy stop attempted"
                % (short, copy or "unknown")
            )

    def remove(self, task_id: str) -> None:
        short = self._short(task_id)
        self._require_ok(self._run(self._claude("rm", short), self._worktree()), "claude rm")

    def visibility(self, task_id: Optional[str] = None) -> VisibilityResult:
        return VisibilityResult(
            False,
            False,
            ("桌面不可见", "只能终端 attach 进入", "返工可续接同一会话"),
            CLAUDE_BG_PROVIDER,
        )

    def locate(self, task_id: str) -> Dict[str, Any]:
        return {"command": "claude attach %s" % self._short(task_id), "terminal_only": True}
