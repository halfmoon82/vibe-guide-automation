import json, os, shutil, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
from vibe_guide.skills import SkillSpec, install_skill

class SkillsTests(unittest.TestCase):
    fixture = Path(__file__).parent / 'fixtures' / 'scan-project' / 'skill-source'

    def make_vendor(self, vibe_home, origin='https://github.com/example/demo'):
        vendor = vibe_home / 'vendor' / 'demo'
        vendor.parent.mkdir(parents=True)
        shutil.copytree(self.fixture, vendor)
        subprocess.run(['git', 'init', str(vendor)], check=True, capture_output=True)
        subprocess.run(
            ['git', '-C', str(vendor), 'config', 'user.name', 'Fixture'],
            check=True,
        )
        subprocess.run(
            ['git', '-C', str(vendor), 'config', 'user.email', 'fixture@example.invalid'],
            check=True,
        )
        subprocess.run(
            ['git', '-C', str(vendor), 'remote', 'add', 'origin', origin],
            check=True,
        )
        subprocess.run(['git', '-C', str(vendor), 'add', 'SKILL.md'], check=True)
        subprocess.run(
            ['git', '-C', str(vendor), 'commit', '-m', 'fixture v1'],
            check=True,
            capture_output=True,
        )
        first = subprocess.check_output(
            ['git', '-C', str(vendor), 'rev-parse', 'HEAD'], text=True
        ).strip()
        (vendor / 'SKILL.md').write_text('fixture version two\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(vendor), 'add', 'SKILL.md'], check=True)
        subprocess.run(
            ['git', '-C', str(vendor), 'commit', '-m', 'fixture v2'],
            check=True,
            capture_output=True,
        )
        second = subprocess.check_output(
            ['git', '-C', str(vendor), 'rev-parse', 'HEAD'], text=True
        ).strip()
        return vendor, first, second

    def test_failed_fetch_is_pending(self):
        with tempfile.TemporaryDirectory() as d:
            r=install_skill(SkillSpec('demo','not-a-url','deadbeef'), Path(d), True)
            self.assertEqual(r.status, 'pending'); self.assertFalse(r.installed)

    def test_installs_exact_full_sha_instead_of_mutable_vendor_head(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            vendor, first, second = self.make_vendor(home)

            result = install_skill(
                SkillSpec('demo', 'git@github.com:example/demo.git', first),
                home,
                False,
            )

            self.assertNotEqual(first, second)
            self.assertTrue(result.installed)
            self.assertEqual(result.commit, first)
            target = home / 'skills' / 'demo'
            self.assertFalse(target.is_symlink())
            self.assertEqual(
                (target / 'SKILL.md').read_text(encoding='utf-8'),
                'fixture version one\n',
            )
            record = json.loads(
                (home / 'skills' / 'demo.json').read_text(encoding='utf-8')
            )
            self.assertEqual(record['source'], 'https://github.com/example/demo')
            self.assertEqual(record['sha'], first)
            self.assertEqual(len(record['tree']), 40)
            self.assertEqual(len(record['installed_tree_sha256']), 64)
            self.assertEqual(record['validation'], 'verified')
            self.assertEqual(
                subprocess.check_output(
                    ['git', '-C', str(vendor), 'rev-parse', 'HEAD'], text=True
                ).strip(),
                second,
            )

    def test_rejects_abbreviated_sha_without_publishing(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            _, first, _ = self.make_vendor(home)

            result = install_skill(
                SkillSpec('demo', 'https://github.com/example/demo', first[:12]),
                home,
                False,
            )

            self.assertEqual(result.status, 'pending')
            self.assertFalse((home / 'skills' / 'demo').exists())
            self.assertFalse((home / 'skills' / 'demo.json').exists())

    def test_source_mismatch_does_not_publish_claimed_source(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            _, first, _ = self.make_vendor(
                home, origin='https://github.com/example/other'
            )

            result = install_skill(
                SkillSpec('demo', 'https://github.com/example/demo', first),
                home,
                False,
            )

            self.assertEqual(result.status, 'pending')
            self.assertFalse(result.installed)
            self.assertFalse((home / 'skills' / 'demo').exists())
            self.assertFalse((home / 'skills' / 'demo.json').exists())

    def test_existing_target_collision_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            _, first, _ = self.make_vendor(home)
            target = home / 'skills' / 'demo'
            target.parent.mkdir()
            target.write_text('keep\n', encoding='utf-8')

            result = install_skill(
                SkillSpec('demo', 'https://github.com/example/demo', first),
                home,
                False,
            )

            self.assertEqual(result.status, 'pending')
            self.assertEqual(target.read_text(encoding='utf-8'), 'keep\n')
            self.assertFalse((home / 'skills' / 'demo.json').exists())

    def test_credential_source_is_not_returned_or_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            _, first, _ = self.make_vendor(home)
            credential = 'synthetic' + '-credential'
            source = (
                'https://reader:' + credential + '@github.com/example/demo.git'
            )

            result = install_skill(
                SkillSpec('demo', source, first), home, False
            )

            self.assertEqual(result.status, 'pending')
            self.assertNotIn(credential, repr(result))
            self.assertEqual(result.source, 'https://github.com/example/demo')
            self.assertFalse((home / 'skills' / 'demo').exists())
            self.assertFalse((home / 'skills' / 'demo.json').exists())

    def test_metadata_collision_leaves_no_active_target(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            _, first, _ = self.make_vendor(home)
            record = home / 'skills' / 'demo.json'
            record.mkdir(parents=True)

            result = install_skill(
                SkillSpec('demo', 'https://github.com/example/demo', first),
                home,
                False,
            )

            self.assertEqual(result.status, 'pending')
            self.assertTrue(record.is_dir())
            self.assertFalse((home / 'skills' / 'demo').exists())

    def test_publication_failure_rolls_back_metadata_and_target(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            _, first, _ = self.make_vendor(home)
            target = home / 'skills' / 'demo'
            record = home / 'skills' / 'demo.json'
            real_replace = os.replace

            def fail_target_publish(source, destination):
                if Path(destination).resolve(strict=False) == target.resolve(
                    strict=False
                ):
                    raise OSError('injected publication failure')
                return real_replace(source, destination)

            with mock.patch('os.replace', side_effect=fail_target_publish):
                result = install_skill(
                    SkillSpec('demo', 'https://github.com/example/demo', first),
                    home,
                    False,
                )

            self.assertEqual(result.status, 'pending')
            self.assertFalse(target.exists())
            self.assertFalse(record.exists())
