"""Issue #134: an older hand-written entry rule ("every task runs vibe first")
left beside the New Session Entry block gives the host two contradictory
entry rules, and doctor used to stay green because it only looked at its own
marker blocks."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide.cli import run_cli
from vibe_guide.doctor import doctor
from vibe_guide.initializer import apply_agentsmd_proposal
from vibe_guide.paths import ProjectPaths
from vibe_guide.scanner import (
    PRD_GUIDE_RULES,
    VIBE_ENTRY_RULES,
    legacy_entry_rule_lines,
    scan_project,
)


# Lines quoted from the two projects named in the issue, plus neighbours from
# the same files that mention vibe commands legitimately.
PII_LEGACY = (
    '- 任何开发任务（改代码、建计划、派发 worker）开始前必须先跑 '
    '`vibe doctor --json`：`issues` 非空或 `ok != true` 时先修复再开工，不得绕过。'
)
SXM_LEGACY = '- 新任务先跑 `vibe plan --request "<需求>"` 定级，再决定是否进入复杂流程。'
LEGIT_LINES = (
    '- 目标链路：`vibe plan` → 授权卡 `AUTHORIZE` → `vibe monitor` 派发 → '
    '`vibe resume/status` 轮询回收。',
    '- 派发前必须核对活跃桥 `adapter_id` 与当前宿主一致；不一致时切换后重跑 '
    '`vibe doctor --json` 确认 `provider_bridge.detected == true`。',
    # "every" without "before starting" is a routine, not an entry gate.
    '- 每次发版后用 `vibe doctor --json` 复核环境。',
    # "before" scoped to one occasion, not to every task.
    '- 发版前先跑 `vibe doctor --json` 确认环境。',
    # An every-task gate that is not a vibe command.
    '- 任何任务开始前先读 README 和 docs/。',
)


def _agents(*parts):
    return '\n'.join(parts) + '\n'


def _line_of(content, needle):
    return content.splitlines().index(needle) + 1


class LegacyEntryRuleLinesTests(unittest.TestCase):
    def test_flags_both_issue_examples_and_nothing_else(self):
        content = _agents(
            '# Project',
            '',
            '```bash',
            'vibe doctor --json             # 每个任务开始前先跑预检',
            '```',
            '',
            '## 11. VibeGuide 强制工作流（vibe-guide 4.1.0）',
            '',
            PII_LEGACY,
            *LEGIT_LINES,
            SXM_LEGACY,
            '',
            VIBE_ENTRY_RULES,
            PRD_GUIDE_RULES,
        )
        self.assertEqual(
            legacy_entry_rule_lines(content),
            [_line_of(content, PII_LEGACY), _line_of(content, SXM_LEGACY)],
        )

    def test_english_per_task_rule_is_flagged(self):
        legacy = '- Before every task, run `vibe doctor --json` first.'
        content = _agents('# Rules', legacy, '', VIBE_ENTRY_RULES)
        self.assertEqual(legacy_entry_rule_lines(content), [2])

    def test_no_conflict_without_new_session_entry(self):
        # With no New Session Entry block the old rule is the only entry rule;
        # there is nothing for it to contradict.
        self.assertEqual(
            legacy_entry_rule_lines(_agents('# Rules', PII_LEGACY, SXM_LEGACY)),
            [],
        )

    def test_lines_inside_an_older_entry_block_are_not_flagged(self):
        # An outdated New Session Entry block is reported by its own sentinel
        # check; its wording is vibe's, not a second rule.
        content = _agents(
            '## New Session Entry',
            '',
            '- 每个开发任务先跑 `vibe plan --request` 自评后再动手。',
            '',
            '## Other',
            SXM_LEGACY,
        )
        self.assertEqual(legacy_entry_rule_lines(content), [6])

    def test_none_and_empty(self):
        self.assertEqual(legacy_entry_rule_lines(None), [])
        self.assertEqual(legacy_entry_rule_lines(''), [])


def _project(root, agents_text):
    (root / 'AGENTS.md').write_text(agents_text, encoding='utf-8')
    (root / '.vibe' / 'knowledge').mkdir(parents=True)
    (root / '.vibe' / 'config.json').write_text(
        json.dumps({'skills': [{
            'name': 'architecture-skill-pack',
            'source': 'https://github.com/lov-team/architecture-skill-pack',
            'commit': 'b' * 40,
        }]}),
        encoding='utf-8',
    )


def _doctor(root):
    with mock.patch(
        'vibe_guide.scanner.shutil.which',
        side_effect=lambda command: '/usr/bin/' + command if command == 'codex' else None,
    ):
        return doctor(scan_project(ProjectPaths.from_cwd(root)))


class DoctorLegacyEntryRuleTests(unittest.TestCase):
    def test_doctor_reports_conflicting_line_numbers(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            content = _agents('# Rules', PII_LEGACY, '', VIBE_ENTRY_RULES)
            _project(root, content)
            result = _doctor(root)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 'attention')
        self.assertIn(
            'AGENTS.md line 2: legacy entry rule conflicts with New Session Entry',
            result.issues,
        )
        self.assertEqual(result.facts['rules']['legacy_entry_lines'], [2])

    def test_doctor_stays_ready_with_only_legitimate_mentions(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _project(root, _agents('# Rules', *LEGIT_LINES, '', VIBE_ENTRY_RULES))
            result = _doctor(root)
        self.assertTrue(result.ok, result.issues)
        self.assertEqual(result.facts['rules']['legacy_entry_lines'], [])


class ApplyAgentsmdLegacyEntryRuleTests(unittest.TestCase):
    def test_apply_notes_the_conflict_it_creates_without_touching_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            before = _agents('# Rules', '', SXM_LEGACY)
            (root / 'AGENTS.md').write_text(before, encoding='utf-8')
            proposal = root / '.vibe' / 'proposals' / 'agentsmd' / 'proposal.md'
            proposal.parent.mkdir(parents=True)
            proposal.write_text(
                '# Vibe Guide capability contract proposal\n\n' + VIBE_ENTRY_RULES,
                encoding='utf-8',
            )
            result = apply_agentsmd_proposal(ProjectPaths.from_cwd(root), True)
            after = (root / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertTrue(result.changed)
        self.assertTrue(after.startswith(before))
        self.assertEqual(len(result.notes), 1)
        self.assertIn('AGENTS.md 第 3 行', result.notes[0])
        self.assertIn('New Session Entry', result.notes[0])

    def test_apply_without_conflict_has_no_note(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'AGENTS.md').write_text(_agents('# Rules', *LEGIT_LINES), encoding='utf-8')
            proposal = root / '.vibe' / 'proposals' / 'agentsmd' / 'proposal.md'
            proposal.parent.mkdir(parents=True)
            proposal.write_text(
                '# Vibe Guide capability contract proposal\n\n' + VIBE_ENTRY_RULES,
                encoding='utf-8',
            )
            result = apply_agentsmd_proposal(ProjectPaths.from_cwd(root), True)
        self.assertTrue(result.changed)
        self.assertEqual(result.notes, [])

    def test_cli_reports_the_note_in_json_and_text(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'AGENTS.md').write_text(_agents('# Rules', SXM_LEGACY), encoding='utf-8')
            proposal = root / '.vibe' / 'proposals' / 'agentsmd' / 'proposal.md'
            proposal.parent.mkdir(parents=True)
            proposal.write_text(
                '# Vibe Guide capability contract proposal\n\n' + VIBE_ENTRY_RULES,
                encoding='utf-8',
            )
            as_json = run_cli(['apply-agentsmd', '--confirm', '--json'], root)
            (root / 'AGENTS.md').write_text(_agents('# Rules', SXM_LEGACY), encoding='utf-8')
            as_text = run_cli(['apply-agentsmd', '--confirm'], root)
        payload = as_json.payload
        self.assertEqual(len(payload['notes']), 1)
        self.assertIn('第 2 行', payload['notes'][0])
        self.assertIn('请注意：AGENTS.md 第 2 行', as_text.text)


if __name__ == '__main__':
    unittest.main()
