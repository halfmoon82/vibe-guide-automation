"""ISSUE-92 stage 0: `vibe skill-install` with optional subdir.

Acceptance example: a local fixture repo without a root SKILL.md plus a legal
subdir installs and scans valid; ``subdir=../x`` stays pending with no disk
writes; existing installs without subdir keep passing verbatim; two subdirs of
one repo produce two records.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.scanner import scan_project
from vibe_guide.skills import (
    SkillSpec,
    install_project_skill,
    normalize_skill_subdir,
    repository_vendor_dir,
)

SOURCE = 'https://github.com/example/multi-skill'


def _git(repo, *args):
    subprocess.run(
        ['git', '-C', str(repo), *args], check=True, capture_output=True
    )


def build_repo(root):
    """Create a local git repo: root has no SKILL.md; two skill subdirs do."""
    repo = Path(root) / 'repo'
    repo.mkdir()
    _git(repo, 'init')
    _git(repo, 'config', 'user.name', 'Fixture')
    _git(repo, 'config', 'user.email', 'fixture@example.invalid')
    _git(repo, 'remote', 'add', 'origin', SOURCE)
    (repo / 'docs').mkdir()
    (repo / 'docs' / 'note.md').write_text('not a skill\n', encoding='utf-8')
    for sub in ('skills/alpha', 'skills/beta'):
        (repo / sub).mkdir(parents=True)
        (repo / sub / 'SKILL.md').write_text(
            '# {} skill\n'.format(sub.split('/')[-1]), encoding='utf-8'
        )
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-m', 'fixture')
    sha = subprocess.check_output(
        ['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True
    ).strip()
    return repo, sha


def seed_vendor(repo, vibe_home):
    vendor = repository_vendor_dir(vibe_home, SOURCE)
    vendor.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo, vendor)


class SubdirValidationTests(unittest.TestCase):
    def test_accepts_nested_relative_subdir(self):
        self.assertEqual(normalize_skill_subdir('skills/alpha'), 'skills/alpha')
        self.assertEqual(normalize_skill_subdir(None), '')
        self.assertEqual(normalize_skill_subdir(''), '')

    def test_rejects_traversal_absolute_and_slash_edges(self):
        for bad in ('../x', '/abs', 'a/', '/a', 'a//b', 'a/./b', 'x/../y', 'bad space'):
            with self.assertRaises(ValueError, msg=bad):
                normalize_skill_subdir(bad)


class ProjectInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.vibe_home = self.base / 'vibe-home'
        self.repo, self.sha = build_repo(self.base)
        seed_vendor(self.repo, self.vibe_home)

    def _project_listing(self):
        vibe = self.project / '.vibe'
        if not vibe.exists():
            return []
        return sorted(p.relative_to(vibe).as_posix() for p in vibe.rglob('*'))

    def test_installs_skill_from_subdir_and_registers_record(self):
        result = install_project_skill(
            SkillSpec('alpha', SOURCE, self.sha, 'skills/alpha'),
            self.project,
            self.vibe_home,
        )
        self.assertEqual(result.status, 'installed')
        self.assertTrue(result.installed)
        target = self.project / '.vibe' / 'proposals' / 'skills' / 'alpha'
        self.assertTrue((target / 'SKILL.md').is_file())
        self.assertFalse((target / 'docs').exists(), 'subdir filter drops siblings')
        record = json.loads((self.project / '.vibe' / 'config.json').read_text())
        self.assertEqual(len(record['skills']), 1)
        entry = record['skills'][0]
        self.assertEqual(entry['name'], 'alpha')
        self.assertEqual(entry['commit'], self.sha)
        self.assertEqual(entry['subdir'], 'skills/alpha')
        self.assertEqual(entry['source'], SOURCE)
        report = scan_project(ProjectPaths(self.project))
        self.assertEqual(len(report.skills), 1)
        self.assertTrue(report.skills[0]['valid'])
        self.assertEqual(report.skills[0]['subdir'], 'skills/alpha')

    def test_traversal_subdir_is_pending_without_disk_writes(self):
        result = install_project_skill(
            SkillSpec('evil', SOURCE, self.sha, '../x'),
            self.project,
            self.vibe_home,
        )
        self.assertEqual(result.status, 'pending')
        self.assertFalse(result.installed)
        self.assertEqual(self._project_listing(), [])

    def test_missing_subdir_is_pending_without_disk_writes(self):
        result = install_project_skill(
            SkillSpec('ghost', SOURCE, self.sha, 'skills/nope'),
            self.project,
            self.vibe_home,
        )
        self.assertEqual(result.status, 'pending')
        self.assertFalse((self.project / '.vibe').exists())

    def test_symlinked_proposal_dirs_are_pending_without_outside_writes(self):
        for linked in ('proposals', 'proposals/skills'):
            with self.subTest(linked=linked):
                project = self.base / ('p-' + linked.replace('/', '-'))
                outside = self.base / ('out-' + linked.replace('/', '-'))
                outside.mkdir()
                link = project / '.vibe' / linked
                link.parent.mkdir(parents=True)
                link.symlink_to(outside, target_is_directory=True)
                result = install_project_skill(
                    SkillSpec('alpha', SOURCE, self.sha, 'skills/alpha'),
                    project, self.vibe_home,
                )
                self.assertEqual(result.status, 'pending')
                self.assertEqual(list(outside.rglob('*')), [])
                self.assertFalse((project / '.vibe' / 'config.json').exists())

    def test_scanner_marks_invalid_subdir_record_invalid(self):
        install_project_skill(
            SkillSpec('alpha', SOURCE, self.sha, 'skills/alpha'),
            self.project, self.vibe_home,
        )
        config_path = self.project / '.vibe' / 'config.json'
        record = json.loads(config_path.read_text())
        record['skills'][0]['subdir'] = '../escape'
        config_path.write_text(json.dumps(record), encoding='utf-8')
        report = scan_project(ProjectPaths(self.project))
        self.assertFalse(report.skills[0]['valid'])

    def test_existing_name_is_never_overwritten(self):
        first = install_project_skill(
            SkillSpec('alpha', SOURCE, self.sha, 'skills/alpha'),
            self.project, self.vibe_home,
        )
        self.assertTrue(first.installed)
        again = install_project_skill(
            SkillSpec('alpha', SOURCE, self.sha, 'skills/beta'),
            self.project, self.vibe_home,
        )
        self.assertEqual(again.status, 'pending')
        record = json.loads((self.project / '.vibe' / 'config.json').read_text())
        self.assertEqual(len(record['skills']), 1)

    def test_two_subdirs_of_one_repo_produce_two_records(self):
        a = install_project_skill(
            SkillSpec('alpha', SOURCE, self.sha, 'skills/alpha'),
            self.project, self.vibe_home,
        )
        b = install_project_skill(
            SkillSpec('beta', SOURCE, self.sha, 'skills/beta'),
            self.project, self.vibe_home,
        )
        self.assertTrue(a.installed and b.installed)
        record = json.loads((self.project / '.vibe' / 'config.json').read_text())
        self.assertEqual({r['name'] for r in record['skills']}, {'alpha', 'beta'})
        self.assertEqual(
            {r['subdir'] for r in record['skills']},
            {'skills/alpha', 'skills/beta'},
        )


class SkillInstallCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.vibe_home = self.base / 'vibe-home'
        self.repo, self.sha = build_repo(self.base)
        seed_vendor(self.repo, self.vibe_home)
        self._vibe_home = patch.dict(os.environ, {'VIBE_HOME': str(self.vibe_home)})
        self._vibe_home.start()
        self.addCleanup(self._vibe_home.stop)

    def cli(self, argv):
        return run_cli(argv + ['--json'], self.project)

    def test_requires_confirm(self):
        result = self.cli([
            'skill-install', '--source', SOURCE, '--sha', self.sha,
            '--name', 'alpha', '--subdir', 'skills/alpha',
        ])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.payload.get('reason'), 'confirmation required')
        self.assertFalse((self.project / '.vibe').exists())

    def test_installs_with_confirm(self):
        result = self.cli([
            'skill-install', '--source', SOURCE, '--sha', self.sha,
            '--name', 'alpha', '--subdir', 'skills/alpha', '--confirm',
        ])
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertEqual(result.payload['status'], 'installed')
        self.assertTrue(
            (self.project / '.vibe' / 'proposals' / 'skills' / 'alpha' / 'SKILL.md').is_file()
        )
        report = scan_project(ProjectPaths(self.project))
        self.assertTrue(report.skills[0]['valid'])

    def test_invalid_subdir_blocks_before_any_write(self):
        result = self.cli([
            'skill-install', '--source', SOURCE, '--sha', self.sha,
            '--name', 'evil', '--subdir', '../x', '--confirm',
        ])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.payload['status'], 'pending')
        self.assertFalse((self.project / '.vibe').exists())


if __name__ == '__main__':
    unittest.main()
