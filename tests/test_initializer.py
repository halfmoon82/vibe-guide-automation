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

