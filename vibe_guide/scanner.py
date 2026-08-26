from dataclasses import dataclass, field
from pathlib import Path
import json
import re
import shutil
import subprocess
from typing import Dict, List, Optional

from .paths import ProjectPaths
from .skills import normalize_github_source, sanitize_git_url_for_display


_AGENT_COMMANDS = (
    'codex',
    'claude',
    'cursor',
    'grok',
    'workbuddy',
    'kimi',
    'deepseek',
)
_CONFIG_LIMIT = 64 * 1024
_SKILL_LIMIT = 64
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


CAPABILITY_RULE_MARKER = "Capability and Tool Truth"
CAPABILITY_RULES = """## Capability and Tool Truth

- 不得根据记忆、README、工具未被提及或一次失败判断能力不存在。
- “当前会话未暴露”不等于“平台不具备该能力”。
- 超时、空响应和格式异常统一保持 UNKNOWN；`unknown_timeout` 不得转成 UNAVAILABLE。
- 监工和 worker 的自然语言自报不是能力证据。
- 能力判断必须引用 session contract 的 evidence_ref。
- 没有证据时请求 refresh 或报告 UNKNOWN，不得直接终止。
- 只有 runtime/provider 的结构化结果才能进入能力阻断状态。
"""


@dataclass
class ScanReport:
    root: str
    python_version: str
    git_version: str
    git_root: Optional[str]
    git_remote: Optional[str]
    agentsmd_exists: bool
    agentsmd_content: Optional[str]
    knowledge_exists: bool
    vibe_exists: bool
    skills: List[dict]
    agent_commands: Dict[str, bool] = field(default_factory=dict)
    skill_records_error: Optional[str] = None


@dataclass
class PatchProposal:
    proposed: bool
    content: str


def _run(*args):
    try:
        completed = subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    if completed.returncode != 0:
        return ''
    return completed.stdout.strip()


def _configured_skills(vibe):
    config = vibe / 'config.json'
    if not config.exists():
        return [], None
    if config.is_symlink() or not config.is_file():
        return [], 'config is not a regular file'
    try:
        with config.open('rb') as handle:
            raw = handle.read(_CONFIG_LIMIT + 1)
    except OSError:
        return [], 'config unreadable'
    if len(raw) > _CONFIG_LIMIT:
        return [], 'config too large'
    try:
        document = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [], 'config invalid'
    records = document.get('skills', []) if isinstance(document, dict) else None
    if not isinstance(records, list):
        return [], 'skills must be a list'
    if len(records) > _SKILL_LIMIT:
        return [], 'too many skill records'

    result = []
    for record in records:
        if not isinstance(record, dict):
            result.append(
                {
                    'name': '',
                    'source': '<invalid-source>',
                    'commit': '',
                    'valid': False,
                }
            )
            continue
        name = record.get('name') if isinstance(record.get('name'), str) else ''
        source = (
            record.get('source') if isinstance(record.get('source'), str) else ''
        )
        commit = (
            record.get('commit') if isinstance(record.get('commit'), str) else ''
        )
        try:
            canonical_source = normalize_github_source(source)
            source_valid = True
        except ValueError:
            canonical_source = sanitize_git_url_for_display(source)
            source_valid = False
        valid = bool(
            _SKILL_NAME.fullmatch(name)
            and _FULL_SHA.fullmatch(commit)
            and source_valid
        )
        result.append(
            {
                'name': name[:128],
                'source': canonical_source,
                'commit': commit.lower() if _FULL_SHA.fullmatch(commit) else '',
                'valid': valid,
            }
        )
    return result, None


def scan_project(paths):
    root = paths.root
    git_root = _run('git', '-C', str(root), 'rev-parse', '--show-toplevel') or None
    remote = (
        _run('git', '-C', str(root), 'config', '--get', 'remote.origin.url')
        or None
    )
    python_version = _run('python3', '--version')
    git_version = _run('git', '--version')
    agentsmd = root / 'AGENTS.md'
    vibe = root / '.vibe'
    agentsmd_exists = agentsmd.is_file() and not agentsmd.is_symlink()
    if not vibe.exists() or vibe.is_symlink() or not vibe.is_dir():
        skills, skill_records_error = [], 'invalid .vibe directory'
    else:
        skills, skill_records_error = _configured_skills(vibe)
    commands = {
        command: shutil.which(command) is not None for command in _AGENT_COMMANDS
    }
    return ScanReport(
        root=str(root),
        python_version=python_version,
        git_version=git_version,
        git_root=git_root,
        git_remote=sanitize_git_url_for_display(remote) if remote else None,
        agentsmd_exists=agentsmd_exists,
        agentsmd_content=(
            agentsmd.read_text(encoding='utf-8') if agentsmd_exists else None
        ),
        knowledge_exists=(
            (vibe / 'knowledge').is_dir()
            and not (vibe / 'knowledge').is_symlink()
        ),
        vibe_exists=vibe.is_dir() and not vibe.is_symlink(),
        skills=skills,
        agent_commands=commands,
        skill_records_error=skill_records_error,
    )


def build_agentsmd_patch(existing, report):
    if (
        existing is not None
        and 'Vibe Guide' in existing
        and 'project' in existing.lower()
        and CAPABILITY_RULE_MARKER in existing
        and 'evidence_ref' in existing
        and 'unknown_timeout' in existing
    ):
        return PatchProposal(False, '')
    if existing is None:
        content = '# Vibe Guide\n\nProject guidance is maintained through the Vibe Guide.\n\n' + CAPABILITY_RULES
    else:
        content = (
            '# Vibe Guide capability contract proposal\n\n'
            + CAPABILITY_RULES
        )
    return PatchProposal(
        True,
        content,
    )
