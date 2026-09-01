from dataclasses import dataclass
from pathlib import Path
import os
import json, tempfile

from .scanner import build_agentsmd_patch, scan_project
from .capability_contract import build_contract, contract_path, load_contract, save_contract


@dataclass
class InitResult:
    changed: bool
    paths: list


def _is_within(root, path):
    try:
        return os.path.commonpath(
            (str(root), str(path.resolve(strict=False)))
        ) == str(root)
    except (OSError, ValueError):
        return False


def _validate_initialization_paths(root):
    root = root.resolve()
    if not root.is_dir():
        raise ValueError('project root must be a directory')
    directories = (
        root / '.vibe',
        root / '.vibe' / 'knowledge',
        root / '.vibe' / 'proposals',
        root / '.vibe' / 'proposals' / 'agentsmd',
        root / '.vibe' / 'proposals' / 'skills',
    )
    files = (
        root / '.vibe' / 'config.json',
        root / '.vibe' / 'state.json',
        root / '.vibe' / 'session-contract.json',
        root / '.vibe' / 'proposals' / 'agentsmd' / 'proposal.md',
    )
    for path in directories:
        if not _is_within(root, path):
            raise ValueError('initialization path escapes project root')
        if path.is_symlink():
            raise ValueError('initialization directory must not be a symlink')
        if path.exists() and not path.is_dir():
            raise ValueError('initialization directory is not a directory')
    for path in files:
        if not _is_within(root, path):
            raise ValueError('initialization file escapes project root')
        if path.is_symlink():
            raise ValueError('initialization file must not be a symlink')
        if path.exists() and not path.is_file():
            raise ValueError('initialization file is not a regular file')


def _write_new(path, content):
    with path.open('x', encoding='utf-8') as handle:
        handle.write(content)

def _migrate_state(path):
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ValueError('state.json is invalid') from error
    if not isinstance(data, dict):
        raise ValueError('state.json must be an object')
    if (
        data.get('workflow_version') == 2
        and data.get('session_gate') == 's0_required'
        and data.get('capability_contract_required') is True
    ):
        return False
    data.setdefault('workflow_version', 2)
    data.setdefault('session_gate', 's0_required')
    data.setdefault('capability_contract_required', True)
    descriptor, temporary_name = tempfile.mkstemp(prefix='.state.', dir=str(path.parent))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name): os.unlink(temporary_name)
    return True


def init_project(paths, confirm):
    if not confirm:
        return InitResult(False, [])
    root = Path(paths.root).resolve()
    _validate_initialization_paths(root)
    _migrate_state(root / '.vibe' / 'state.json')
    report = scan_project(paths)
    created = []
    for relative in (
        '.vibe',
        '.vibe/knowledge',
        '.vibe/proposals',
        '.vibe/proposals/agentsmd',
        '.vibe/proposals/skills',
    ):
        path = root / relative
        if not path.exists():
            path.mkdir()
            if relative in ('.vibe/knowledge', '.vibe/proposals/agentsmd', '.vibe/proposals/skills'):
                created.append(relative)
    for relative in ('.vibe/config.json', '.vibe/state.json'):
        path = root / relative
        if not path.exists():
            content = '{}\n'
            if relative == '.vibe/state.json':
                content = '{"workflow_version": 2, "session_gate": "s0_required", "capability_contract_required": true}\n'
            _write_new(path, content)
            created.append(relative)
    capability_target = contract_path(paths)
    if capability_target.exists():
        load_contract(paths)
    else:
        runtime_status = (
            'verified_available'
            if report.python_version and report.git_version
            else 'probe_failed'
        )
        facts = {
            'runtime.exec': {
                'status': runtime_status,
                'scope': 'init',
                'route': 'runtime.exec' if runtime_status == 'verified_available' else '',
                'evidence_ref': 'init:scan:runtime',
            },
            'task.terminal': {
                'status': 'unknown',
                'scope': 'task',
                'route': '',
                'evidence_ref': 'init:unobserved:task.terminal',
            },
            'task.browser.control': {
                'status': 'unknown',
                'scope': 'task',
                'route': '',
                'evidence_ref': 'init:unobserved:task.browser.control',
            },
            'task.visible_session': {
                'status': 'unknown',
                'scope': 'task',
                'route': '',
                'evidence_ref': 'init:unobserved:task.visible_session',
            },
        }
        save_contract(paths, build_contract(Path(paths.root), facts=facts))
        created.append('.vibe/session-contract.json')
    proposal = build_agentsmd_patch(report.agentsmd_content, report)
    proposal_path = root / '.vibe/proposals/agentsmd/proposal.md'
    if proposal.proposed and not proposal_path.exists():
        _write_new(proposal_path, proposal.content)
        created.append(str(proposal_path.relative_to(root)))
    skill_proposal = root / '.vibe/proposals/skills/proposal.md'
    if not skill_proposal.exists() and not any(item.get('valid') and item.get('name') == 'architecture-skill-pack' for item in report.skills):
        _write_new(skill_proposal, '# Skill proposal\n\n- architecture-skill-pack\n')
        created.append(str(skill_proposal.relative_to(root)))
    return InitResult(bool(created), created)


def apply_agentsmd_proposal(paths, confirm):
    """Apply the generated capability rules only after explicit confirmation.

    Initialization remains proposal-only.  This separate operation preserves
    the existing AGENTS.md bytes and appends the reviewed proposal exactly
    once, so the rules become part of the supervisor's project context without
    silently overwriting user instructions.
    """
    if not confirm:
        return InitResult(False, [])
    root = Path(paths.root).resolve()
    if not root.is_dir():
        raise ValueError("project root must be a directory")
    proposal_path = root / ".vibe" / "proposals" / "agentsmd" / "proposal.md"
    if proposal_path.is_symlink() or not proposal_path.is_file():
        raise ValueError("AGENTS.md proposal is missing or not a regular file")
    proposal_raw = proposal_path.read_bytes()
    if len(proposal_raw) > 64 * 1024:
        raise ValueError("AGENTS.md proposal exceeds the size bound")
    try:
        proposal = proposal_raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("AGENTS.md proposal is not valid UTF-8") from error
    if not proposal.strip() or "## Capability and Tool Truth" not in proposal:
        raise ValueError("AGENTS.md proposal does not contain capability rules")

    target = root / "AGENTS.md"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("AGENTS.md must be a regular file")
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if "## Capability and Tool Truth" in existing:
        return InitResult(False, [])

    if existing:
        separator = "" if existing.endswith("\n") else "\n"
        content = existing + separator + "\n" + proposal.lstrip("\n")
    else:
        content = proposal
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".AGENTS.md.", dir=str(root)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(target))
    finally:
        if temporary.exists():
            temporary.unlink()
    return InitResult(True, ["AGENTS.md"])
