import json, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
from vibe_guide.doctor import doctor
from vibe_guide.paths import ProjectPaths
from vibe_guide.scanner import scan_project, build_agentsmd_patch

class ScannerTests(unittest.TestCase):
    def test_scan_facts_and_missing_rules_proposal(self):
        with tempfile.TemporaryDirectory() as d:
            p = ProjectPaths.from_cwd(Path(d)); r = scan_project(p)
            self.assertFalse(r.agentsmd_exists); self.assertFalse(r.knowledge_exists)
            self.assertTrue(build_agentsmd_patch(None, r).proposed)
    def test_existing_agents_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); (p/'AGENTS.md').write_text('keep', encoding='utf-8')
            self.assertEqual(scan_project(ProjectPaths.from_cwd(p)).agentsmd_content, 'keep')

    def test_scan_redacts_remote_userinfo_and_reports_agent_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            credential = 'synthetic' + '-credential'
            subprocess.run(['git', 'init', str(root)], check=True, capture_output=True)
            subprocess.run(
                [
                    'git', '-C', str(root), 'remote', 'add', 'origin',
                    'https://reader:' + credential + '@github.com/example/demo.git',
                ],
                check=True,
            )

            with mock.patch(
                'vibe_guide.scanner.shutil.which',
                side_effect=lambda command: '/usr/bin/' + command
                if command == 'codex' else None,
            ):
                report = scan_project(ProjectPaths.from_cwd(root))

            self.assertEqual(
                report.git_remote, 'https://github.com/example/demo'
            )
            self.assertNotIn(credential, repr(report))
            commands = getattr(report, 'agent_commands', {})
            self.assertTrue(commands.get('codex'))
            self.assertFalse(commands.get('claude'))

    def test_scan_discovers_bounded_configured_skill_records(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / '.vibe').mkdir()
            config = {
                'skills': [
                    {
                        'name': 'architecture-skill-pack',
                        'source': (
                            'git@github.com:lov-team/architecture-skill-pack.git'
                        ),
                        'commit': 'a' * 40,
                    }
                ]
            }
            (root / '.vibe' / 'config.json').write_text(
                json.dumps(config), encoding='utf-8'
            )

            report = scan_project(ProjectPaths.from_cwd(root))

            self.assertEqual(len(report.skills), 1)
            self.assertEqual(
                report.skills[0]['source'],
                'https://github.com/lov-team/architecture-skill-pack',
            )
            self.assertTrue(report.skills[0]['valid'])
            self.assertIsNone(getattr(report, 'skill_records_error', 'missing'))

    def test_scan_bounds_configured_skill_record_input(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / '.vibe').mkdir()
            (root / '.vibe' / 'config.json').write_text(
                ' ' * (64 * 1024 + 1), encoding='utf-8'
            )

            report = scan_project(ProjectPaths.from_cwd(root))

            self.assertEqual(report.skills, [])
            self.assertEqual(
                getattr(report, 'skill_records_error', None), 'config too large'
            )

    def test_doctor_reports_observable_facts_without_authority_inference(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'AGENTS.md').write_text('rules\n', encoding='utf-8')
            (root / '.vibe' / 'knowledge').mkdir(parents=True)
            (root / '.vibe' / 'config.json').write_text(
                json.dumps(
                    {
                        'skills': [
                            {
                                'name': 'architecture-skill-pack',
                                'source': (
                                    'https://github.com/lov-team/'
                                    'architecture-skill-pack'
                                ),
                                'commit': 'b' * 40,
                            }
                        ]
                    }
                ),
                encoding='utf-8',
            )
            with mock.patch(
                'vibe_guide.scanner.shutil.which',
                side_effect=lambda command: '/usr/bin/' + command
                if command == 'codex' else None,
            ):
                result = doctor(scan_project(ProjectPaths.from_cwd(root)))

            self.assertTrue(hasattr(result, 'facts'))
            self.assertTrue(result.ok, result.issues)
            self.assertTrue(result.facts['python']['available'])
            self.assertTrue(result.facts['git']['available'])
            self.assertTrue(result.facts['rules']['present'])
            self.assertTrue(result.facts['skills']['required_configured'])
            self.assertEqual(result.facts['agents']['available'], ['codex'])
            serialized = json.dumps(result.facts, sort_keys=True).lower()
            for unsupported in ('login', 'approval', 'merge', 'deploy', 'authority'):
                self.assertNotIn(unsupported, serialized)
