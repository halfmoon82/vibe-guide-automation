"""Which host platform the calling session runs in, from session-id variables only.

Identity evidence is a session/thread id the host itself sets for every
process its session spawns:

* WorkBuddy: ``CODEBUDDY_SESSION_ID``
* Codex: ``CODEX_THREAD_ID`` or ``CODEX_SESSION_ID``
* Claude Code: ``CLAUDE_CODE_SESSION_ID`` together with ``CLAUDECODE``

Configuration-directory overrides (``WORKBUDDY_CONFIG_DIR``,
``CODEBUDDY_CONFIG_DIR``, ``CODEX_HOME`` ...) say where a host keeps its files,
not who is calling, and are never read here.

Host variables are inherited: a Claude CLI started inside a WorkBuddy or Codex
session carries both sets.  The more specific hosts (WorkBuddy, Codex) are
therefore read first and Claude Code last.  WorkBuddy and Codex together is a
contradiction nobody can settle from the environment, so it stays unknown;
nothing here guesses.

The environment is read once, at the command-line entry (``cli.main``), and
handed down explicitly.  Programmatic ``run_cli`` callers that pass no
environment get no detection, so their results never depend on which host ran
them.
"""
from dataclasses import dataclass, field
import re
from typing import Mapping, Optional, Tuple

from .platform_ledger import platform_for_provider

UNKNOWN = "unknown"

WORKBUDDY_SESSION_ENV = ("CODEBUDDY_SESSION_ID",)
CODEX_SESSION_ENV = ("CODEX_THREAD_ID", "CODEX_SESSION_ID")
CLAUDE_SESSION_ENV = ("CLAUDE_CODE_SESSION_ID",)
CLAUDE_MARKER_ENV = "CLAUDECODE"

#: ``AI_AGENT`` is how a host names itself and its version to child tools,
#: e.g. ``claude-code_2-1-316_agent``.  It is read for the version only, and
#: only when it names the platform the session ids already identified.
AGENT_ENV = "AI_AGENT"
_AGENT_PATTERN = re.compile(r"^(?P<name>[a-z][a-z0-9-]*)_(?P<version>[0-9][0-9-]*[0-9]|[0-9])(?:_|$)")


class HostMismatchError(ValueError):
    """An explicit ``--adapter`` disagrees with the platform the environment names."""


class HostUnknownError(ValueError):
    """The environment carries host evidence that contradicts itself or is partial."""


@dataclass(frozen=True)
class HostDetection:
    platform: Optional[str]
    session_id: Optional[str] = None
    host_version: Optional[str] = None
    #: The variable names that decided it (or that contradicted each other).
    evidence: Tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""

    @property
    def known(self) -> bool:
        return self.platform is not None

    def describe(self) -> str:
        if self.platform is None:
            return "未知（{}）".format(self.reason) if self.reason else "未知"
        return "{}（{}）".format(self.platform, "、".join(self.evidence))

    def to_dict(self) -> dict:
        return {
            "platform": self.platform or UNKNOWN,
            "session_id": self.session_id,
            "host_version": self.host_version,
            "evidence": list(self.evidence),
            "reason": self.reason,
        }


def _first(environ: Mapping[str, str], names: Tuple[str, ...]) -> Tuple[Optional[str], Optional[str]]:
    for name in names:
        value = str(environ.get(name) or "").strip()
        if value:
            return name, value
    return None, None


def _agent_version(environ: Mapping[str, str], platform: str) -> Optional[str]:
    match = _AGENT_PATTERN.match(str(environ.get(AGENT_ENV) or "").strip())
    if not match or platform_for_provider(match.group("name")) != platform:
        return None
    return match.group("version").replace("-", ".")


def detect_host(environ: Optional[Mapping[str, str]]) -> HostDetection:
    """The calling platform, or an unknown detection saying why."""
    if environ is None:
        return HostDetection(None, reason="未提供宿主环境")
    wb_name, wb_id = _first(environ, WORKBUDDY_SESSION_ENV)
    cx_name, cx_id = _first(environ, CODEX_SESSION_ENV)
    if wb_id and cx_id:
        return HostDetection(
            None, evidence=(wb_name, cx_name),
            reason="{} 与 {} 同时存在，互相矛盾".format(wb_name, cx_name),
        )
    if wb_id:
        return HostDetection("workbuddy", wb_id, _agent_version(environ, "workbuddy"), (wb_name,))
    if cx_id:
        return HostDetection("codex", cx_id, _agent_version(environ, "codex"), (cx_name,))
    cc_name, cc_id = _first(environ, CLAUDE_SESSION_ENV)
    marker = str(environ.get(CLAUDE_MARKER_ENV) or "").strip()
    if cc_id and marker:
        return HostDetection(
            "claude-code", cc_id, _agent_version(environ, "claude-code"), (cc_name, CLAUDE_MARKER_ENV),
        )
    if cc_id or marker:
        present = cc_name if cc_id else CLAUDE_MARKER_ENV
        return HostDetection(None, evidence=(present,), reason="只有 {}，Claude Code 需要会话编号与 CLAUDECODE 同时存在".format(present))
    return HostDetection(None, reason="没有会话编号类环境变量")


def resolve_adapter(explicit: Optional[str], detection: HostDetection, flag: str = "--adapter") -> Optional[str]:
    """The adapter a command acts for: the explicit one, else the detected one.

    An explicit value that names a different platform than a *known* detection
    is refused with the detection in the message.  Host evidence that
    contradicts itself, or is only half there, stays unknown and needs the
    flag (``HostUnknownError``).  ``None`` means the environment carries no
    host evidence at all; the caller keeps its own fallback.
    """
    value = str(explicit or "").strip()
    if value:
        named = platform_for_provider(value) or value
        if detection.known and named != detection.platform:
            raise HostMismatchError(
                "{} {} 与当前环境不一致：环境识别为 {}；请在 {} 会话里执行，或去掉 {}".format(
                    flag, value, detection.describe(), value, flag,
                )
            )
        return value
    if detection.platform is None and detection.evidence:
        raise HostUnknownError(adapter_required_message(detection, flag))
    return detection.platform


def adapter_required_message(detection: HostDetection, flag: str = "--adapter") -> str:
    return "无法从环境识别当前平台：{}；请用 {} 指明本会话所属平台".format(detection.describe(), flag)
