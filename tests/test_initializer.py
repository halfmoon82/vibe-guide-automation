import tempfile, unittest
from pathlib import Path
from vibe_guide.paths import ProjectPaths
from vibe_guide.initializer import init_project

class InitializerTests(unittest.TestCase):
    def test_no_confirm_does_not_write_and_confirm_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            p=ProjectPaths.from_cwd(Path(d)); self.assertFalse(init_project(p, False).changed)
            first=init_project(p, True); second=init_project(p, True)
            self.assertTrue(first.changed); self.assertFalse(second.changed)
            self.assertTrue((p.root/'.vibe/knowledge').is_dir())
            self.assertFalse((p.root/'AGENTS.md').exists())

    def test_symlinked_vibe_is_rejected_without_outside_write(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            project = base / 'project'
            outside = base / 'outside'
            project.mkdir()
            outside.mkdir()
            (project / '.vibe').symlink_to(outside, target_is_directory=True)

            with self.assertRaises(ValueError):
                init_project(ProjectPaths.from_cwd(project), True)

            self.assertEqual(list(outside.iterdir()), [])

    def test_nested_symlink_is_rejected_before_any_initialization_write(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            project = base / 'project'
            outside = base / 'outside'
            (project / '.vibe').mkdir(parents=True)
            outside.mkdir()
            (project / '.vibe' / 'knowledge').symlink_to(
                outside, target_is_directory=True
            )

            with self.assertRaises(ValueError):
                init_project(ProjectPaths.from_cwd(project), True)

            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((project / '.vibe' / 'config.json').exists())
            self.assertFalse((project / '.vibe' / 'state.json').exists())
            self.assertFalse((project / '.vibe' / 'proposals').exists())

    def test_non_directory_vibe_is_rejected_without_replacement(self):
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            marker = project / '.vibe'
            marker.write_text('keep\n', encoding='utf-8')

            try:
                init_project(ProjectPaths.from_cwd(project), True)
            except Exception as exc:
                self.assertIsInstance(exc, ValueError)
            else:
                self.fail('non-directory .vibe must be rejected')

            self.assertEqual(marker.read_text(encoding='utf-8'), 'keep\n')
