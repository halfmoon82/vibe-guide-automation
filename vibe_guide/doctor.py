from dataclasses import dataclass
import re
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

    def __post_init__(self):
        if self.proposals is None:
            self.proposals = []


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
    }
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
    return DoctorReport(not issues, issues, facts, status=status, proposals=proposals)
