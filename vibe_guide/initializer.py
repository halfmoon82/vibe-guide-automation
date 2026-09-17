from dataclasses import dataclass
from pathlib import Path
import os
import json, tempfile

from .scanner import (
    CAPABILITY_RULES,
    CAPABILITY_RULE_MARKER,
    PRD_GUIDE_MARKER,
    PRD_GUIDE_RULES,
    build_agentsmd_patch,
    missing_agentsmd_blocks,
    scan_project,
)
from .protocols import PRD_GUIDE_NAME, PRD_GUIDE_PROPOSAL_RELATIVE, load_protocol
from .capability_contract import build_contract, contract_path, load_contract, save_contract
from .migration import migrate_v2_to_v310, migrate_v2_to_v42, _backup, _payload_is_complete
from .workflow_gate import V42_STATE

# Newer rule blocks land here when a reviewed proposal.md already exists; both
# the writer (init) and the reader (apply-agentsmd) name it from here.
PENDING_UPDATE_NAME = "proposal.pending-update.md"


@dataclass
class InitResult:
    changed: bool
    paths: list


def migrate_project(source, destination):
    """Initializer-facing entry point for explicit, backup-first upgrades."""
    return migrate_v2_to_v42(source, destination)


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
        root / '.vibe' / 'proposals' / 'skills' / 'prd-guide',
    )
    files = (
        root / '.vibe' / 'config.json',
        root / '.vibe' / 'state.json',
        root / '.vibe' / 'session-contract.json',
        root / '.vibe' / 'proposals' / 'agentsmd' / 'proposal.md',
        root / '.vibe' / 'proposals' / 'skills' / 'proposal.md',
        root / PRD_GUIDE_PROPOSAL_RELATIVE,
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
    if data == {}:
        _atomic_write(path, {"workflow_version": 2})
        return True
    if data == V42_STATE:
        return False
    if data.get('workflow_version') not in (2, 3, 3.1, 3.10):
        raise ValueError('state.json legacy version is unknown')
    # Preserve the complete legacy snapshot and a verified backup before the
    # new state becomes visible. Unknown legacy fields are never discarded.
    evidence = path.parent / 'migration-evidence.json'
    import hashlib
    source_bytes = json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8')
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if evidence.exists():
        try:
            recorded = json.loads(evidence.read_text(encoding='utf-8'))
            backup = Path(recorded['backup_path'])
            manifest = recorded['backup_manifest']
            valid = (
                isinstance(recorded, dict)
                and recorded.get('target_version') == '4.2.0'
                and recorded.get('source_state') == data
                and recorded.get('source_sha256') == source_sha256
                and isinstance(manifest, dict)
                and json.loads((backup / 'manifest.json').read_text(encoding='utf-8')) == manifest
                and _payload_is_complete(backup / 'payload', manifest.get('files'))
            )
            if not valid:
                raise ValueError('legacy migration evidence is unverifiable')
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise ValueError('legacy migration evidence is unverifiable') from error
    else:
        project_root = path.parent.parent
        backup, manifest = _backup(project_root, project_root.parent)
        if not _payload_is_complete(backup / 'payload', manifest['files']):
            raise ValueError('legacy backup failed integrity check')
        _atomic_write(evidence, {
            'source': str(project_root),
            'destination': str(project_root),
            'source_state': data,
            'source_sha256': source_sha256,
            'target_state': V42_STATE,
            'target_version': '4.2.0',
            'backup_path': str(backup),
            'backup_manifest': manifest,
        })
    data = dict(V42_STATE)
    _atomic_write(path, data)
    return True


def _atomic_write(path, data):
    descriptor, temporary_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=str(path.parent))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


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
            _write_new(path, (json.dumps(V42_STATE, ensure_ascii=False, sort_keys=True) + '\n') if relative == '.vibe/state.json' else '{}\n')
            created.append(relative)
    capability_target = contract_path(paths)
    if capability_target.exists():
        load_contract(paths)
    else:
        runtime_status = 'verified_available' if report.python_version and report.git_version else 'probe_failed'
        facts = {
            'runtime.exec': {'status': runtime_status, 'scope': 'init', 'route': 'runtime.exec' if runtime_status == 'verified_available' else '', 'evidence_ref': 'init:scan:runtime'},
            'task.terminal': {'status': 'unknown', 'scope': 'task', 'route': '', 'evidence_ref': 'init:unobserved:task.terminal'},
            'task.browser.control': {'status': 'unknown', 'scope': 'task', 'route': '', 'evidence_ref': 'init:unobserved:task.browser.control'},
            'task.visible_session': {'status': 'unknown', 'scope': 'task', 'route': '', 'evidence_ref': 'init:unobserved:task.visible_session'},
        }
        save_contract(paths, build_contract(Path(paths.root), facts=facts))
        created.append('.vibe/session-contract.json')
    proposal = build_agentsmd_patch(report.agentsmd_content, report)
    proposal_path = root / '.vibe/proposals/agentsmd/proposal.md'
    if proposal.proposed:
        existing_proposal = None
        if proposal_path.is_file() and not proposal_path.is_symlink():
            try:
                existing_proposal = proposal_path.read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError):
                existing_proposal = None
        if existing_proposal is None:
            _write_new(proposal_path, proposal.content)
            created.append(str(proposal_path.relative_to(root)))
        else:
            # An existing proposal is awaiting human review, and a reviewer may
            # have deliberately deleted a section they do not want.  Rewriting
            # it in place would restore that section and discard their notes, so
            # a proposal that predates a newer rule block goes to a side file
            # and the reviewed bytes are left alone.
            pending_blocks = [
                block for block in missing_agentsmd_blocks(report.agentsmd_content)
                if block.strip() not in existing_proposal
            ]
            if pending_blocks:
                update_path = proposal_path.with_name(PENDING_UPDATE_NAME)
                update = (
                    '# 提案增量（本次未合入 proposal.md）\n\n'
                    '现有 proposal.md 正在等待人工评审，可能已被有意修改，因此不改写它。\n'
                    '以下小节是当前版本新增、proposal.md 里还没有的内容。\n'
                    'vibe apply-agentsmd --confirm 会连同本文件一起合入 AGENTS.md；\n'
                    '不想采纳某一节就在这里删掉它。\n\n'
                    + '\n'.join(pending_blocks)
                )
                if not update_path.exists():
                    _write_new(update_path, update)
                    created.append(str(update_path.relative_to(root)))
                elif update_path.is_file() and not update_path.is_symlink():
                    if update_path.read_text(encoding='utf-8') != update:
                        _atomic_write(update_path, update)
                        created.append(str(update_path.relative_to(root)))
    skill_proposal = root / '.vibe/proposals/skills/proposal.md'
    if not skill_proposal.exists() and not any(item.get('valid') and item.get('name') == 'architecture-skill-pack' for item in report.skills):
        _write_new(
            skill_proposal,
            '# Skill proposal\n\n'
            '- name: architecture-skill-pack\n'
            '- source: https://github.com/lov-team/architecture-skill-pack\n'
            '- source_status: remote\n',
        )
        created.append(str(skill_proposal.relative_to(root)))
    # The PRD-guide protocol is vibe's own, shipped with the package; it is
    # proposed into the project so the host agent finds it, and never
    # rewritten once present so user edits survive re-initialization.
    prd_guide = root / PRD_GUIDE_PROPOSAL_RELATIVE
    if not prd_guide.exists():
        prd_guide.parent.mkdir(parents=True, exist_ok=True)
        _write_new(prd_guide, load_protocol(PRD_GUIDE_NAME))
        created.append(PRD_GUIDE_PROPOSAL_RELATIVE)
    return InitResult(bool(created), created)


def _read_proposal(path):
    """Return a proposal file's text, or None when there is nothing to read.

    Both the reviewed proposal and the increment beside it are files a human
    may have edited, so each gets the same bounds: a regular file, within the
    size limit, valid UTF-8.
    """
    if path.is_symlink() or not path.is_file():
        return None
    raw = path.read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError("%s exceeds the size bound" % path.name)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("%s is not valid UTF-8" % path.name) from error


def _proposal_sections(proposal):
    """Split a reviewed proposal into its `## ` sections, heading included.

    Sections are the unit of application: each one is appended or skipped on
    its own so a project that already carries an earlier section can still
    receive a later one.  Any preamble before the first heading is dropped,
    since it only titles the proposal document.

    A `## ` inside a fenced block is example text, not a boundary.  This
    protocol's own rules are written with fenced examples, so splitting on one
    would tear the fence apart and strand its closing line -- and everything
    after it -- in a section whose heading AGENTS.md may already carry.
    """
    sections = []
    current = []
    fenced = False
    for line in proposal.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if line.startswith("## ") and not fenced:
            if current:
                sections.append("".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        sections.append("".join(current))
    return [section for section in (item.strip("\n") for item in sections) if section.strip()]


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
    proposal = _read_proposal(proposal_path)
    if proposal is None:
        raise ValueError("AGENTS.md proposal is missing or not a regular file")
    if not proposal.strip() or not (
        CAPABILITY_RULE_MARKER in proposal or PRD_GUIDE_MARKER in proposal
    ):
        raise ValueError("AGENTS.md proposal does not contain capability rules")
    # A project that reviewed an earlier proposal keeps those bytes, so newer
    # rule blocks were written beside it rather than into it.  Applying only
    # proposal.md would leave that increment on disk forever: the rule would
    # never reach AGENTS.md and nothing would say so.  Section-level dedup
    # below makes reading both safe.
    increment = _read_proposal(proposal_path.with_name(PENDING_UPDATE_NAME))

    target = root / "AGENTS.md"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("AGENTS.md must be a regular file")
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    # Append only the sections this AGENTS.md still lacks, judging each by its
    # own heading.  Keying the whole append on one marker would strand every
    # later section on any project that already applied an earlier one.
    sections = _proposal_sections(proposal)
    if increment:
        seen = {section.splitlines()[0].strip() for section in sections}
        sections += [
            section for section in _proposal_sections(increment)
            if section.splitlines()[0].strip() not in seen
        ]
    pending = [
        section for section in sections
        if section.splitlines()[0].strip() not in existing
    ]
    if not pending:
        return InitResult(False, [])
    proposal = "\n\n".join(pending) + "\n"

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
