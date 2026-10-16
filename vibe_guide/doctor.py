from dataclasses import dataclass
from pathlib import Path
import re
from . import host_rules
from .diagnostics import diagnose_skill, build_skill_reference_proposal, build_agentsmd_proposal, check_agents_contract
from .scanner import WORKBUDDY_ORIGIN, legacy_entry_rule_lines


_PYTHON_VERSION = re.compile(r"Python\s+(\d+)\.(\d+)")
_REQUIRED_SKILL = 'architecture-skill-pack'


@dataclass
class DoctorReport:
    ok: bool
    issues: list
    facts: dict
    status: str = 'ready'
    proposals: list = None
    # Advisory findings that must not change readiness, e.g. user-level
    # files outside the project that a test or CI machine cannot control.
    warnings: list = None

    def __post_init__(self):
        if self.proposals is None:
            self.proposals = []
        if self.warnings is None:
            self.warnings = []


def _host_rules_facts(report):
    """Host-rule exception facts; doctor must never fail on them."""
    try:
        return host_rules.status(Path(report.root), Path.home())
    except Exception as error:  # noqa: BLE001 - diagnostics stay best-effort
        return {'decision': 'unknown', 'files': {}, 'reason': error.__class__.__name__}


def doctor(report):
    issues = []
    match = _PYTHON_VERSION.search(report.python_version or '')
    python_available = match is not None
    python_supported = bool(
        match and (int(match.group(1)), int(match.group(2))) >= (3, 9)
    )
    git_available = bool(report.git_version)
    # Older / hand-built ScanReport objects predate the field, so fall back to
    # the historical name rather than raising AttributeError.
    rules_file = getattr(report, 'rules_file', None)
    # Direct attributes, no fallback: `scanner.ScanReport` is the only thing
    # that builds a report (it is the one construction site in the package),
    # so a default here would be defensive code with nothing to defend.
    claude_host = bool(report.claude_code_host)
    claude_loaded = bool(report.claude_rules_loaded)
    valid_skills = [skill for skill in report.skills if skill.get('valid')]
    # Configured means pinned to a github source and commit (AGENTS.md §7);
    # an unpinned host copy is listed under host_provided, never counted.
    configured_names = sorted(
        skill.get('name', '')
        for skill in valid_skills
        if skill.get('name') and skill.get('origin') != WORKBUDDY_ORIGIN
    )
    required_skill = _REQUIRED_SKILL in configured_names
    host_provided = sorted(
        skill.get('name', '')
        for skill in report.skills
        if isinstance(skill, dict)
        and skill.get('valid')
        and skill.get('origin') == WORKBUDDY_ORIGIN
        and skill.get('name')
    )
    available_agents = sorted(
        command
        for command, available in report.agent_commands.items()
        if available
    )

    if not python_available:
        issues.append('python3 unavailable')
    elif not python_supported:
        issues.append('python3 below 3.9')
    if not git_available:
        issues.append('git unavailable')
    if not report.agentsmd_exists:
        issues.append('missing ' + (rules_file or 'AGENTS.md'))
    # Outside its own marker blocks doctor read nothing, so an older
    # "every task runs vibe first" rule sat beside New Session Entry and the
    # check stayed green (#134).
    legacy_entry_lines = legacy_entry_rule_lines(report.agentsmd_content)
    for number in legacy_entry_lines:
        issues.append(
            (rules_file or 'AGENTS.md') + ' line ' + str(number)
            + ': legacy entry rule conflicts with New Session Entry'
        )
    # Claude Code loads CLAUDE.md and never AGENTS.md, so rules written to the
    # shared file reach that host only through an import line (#126).  Gated on
    # a rules file existing: with none, "missing AGENTS.md" already says it,
    # and "not loaded" would describe a file that is not there.
    if claude_host and rules_file and not claude_loaded:
        issues.append('rules file not loaded by host: Claude Code')
    if not report.knowledge_exists:
        issues.append('missing .vibe/knowledge')
    if report.skill_records_error:
        issues.append('configured Skill records invalid')
    if len(valid_skills) != len(report.skills):
        issues.append('configured Skill record failed validation')
    if not required_skill:
        issues.append('required Skill not configured')
    if not available_agents:
        issues.append('no candidate Agent command found')

    facts = {
        'python': {
            'available': python_available,
            'supported': python_supported,
            'version': report.python_version,
        },
        'git': {'available': git_available, 'version': report.git_version},
        'rules': {
            'present': report.agentsmd_exists,
            'file': rules_file,
            'legacy_entry_lines': legacy_entry_lines,
        },
        # Reported as a pair: a host the project carries but the rules do not
        # reach is the #126 failure, and either half alone reads as fine.
        'claude_code': {
            'host': claude_host,
            'rules_loaded': claude_loaded,
        },
        'knowledge': {'present': report.knowledge_exists},
        'skills': {
            'configured': configured_names,
            'host_provided': host_provided,
            'required_configured': required_skill,
            'records_valid': (
                report.skill_records_error is None
                and len(valid_skills) == len(report.skills)
            ),
        },
        'agents': {'available': available_agents},
        'host_rules': _host_rules_facts(report),
    }
    warnings = []
    for label, state in sorted(facts['host_rules'].get('files', {}).items()):
        # An unanswered decision stays a fact only; a hand-edited or
        # outdated block means the exception no longer says what vibe wrote.
        # These live in the user's home, so they warn without touching
        # project readiness.
        if state == 'modified':
            warnings.append(label + ': vibe 监工会话例外标记块被人工改过')
        elif state == 'stale':
            warnings.append(label + ': vibe 监工会话例外标记块不是当前版本')
    malformed = bool(report.skill_records_error or len(valid_skills) != len(report.skills))
    status = 'blocked' if malformed else ('attention' if issues else 'ready')
    proposals = []
    skill_diag = diagnose_skill(_REQUIRED_SKILL, report, {"global_skills": []})
    if skill_diag.status == 'attention':
        proposals.append('.vibe/proposals/skills/proposal.md')
    if report.agentsmd_content is not None:
        contract = check_agents_contract(report.agentsmd_content, ['Vibe Guide', 'Project guidance'])
        if not contract.ok:
            proposals.append('.vibe/proposals/agentsmd/proposal.md')
    return DoctorReport(not issues, issues, facts, status=status, proposals=proposals, warnings=warnings)
