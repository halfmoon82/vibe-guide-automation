from dataclasses import dataclass
from pathlib import Path
import os

from .scanner import build_agentsmd_patch, scan_project


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
    )
    files = (
        root / '.vibe' / 'config.json',
        root / '.vibe' / 'state.json',
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


def init_project(paths, confirm):
    if not confirm:
        return InitResult(False, [])
    root = Path(paths.root).resolve()
    _validate_initialization_paths(root)
    report = scan_project(paths)
    created = []
    for relative in (
        '.vibe',
        '.vibe/knowledge',
        '.vibe/proposals',
        '.vibe/proposals/agentsmd',
    ):
        path = root / relative
        if not path.exists():
            path.mkdir()
            if relative in ('.vibe/knowledge', '.vibe/proposals/agentsmd'):
                created.append(relative)
    for relative in ('.vibe/config.json', '.vibe/state.json'):
        path = root / relative
        if not path.exists():
            _write_new(path, '{}\n')
            created.append(relative)
    proposal = build_agentsmd_patch(report.agentsmd_content, report)
    proposal_path = root / '.vibe/proposals/agentsmd/proposal.md'
    if proposal.proposed and not proposal_path.exists():
        _write_new(proposal_path, proposal.content)
        created.append(str(proposal_path.relative_to(root)))
    return InitResult(bool(created), created)
