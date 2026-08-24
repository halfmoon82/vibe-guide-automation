import tempfile, unittest
from pathlib import Path
from vibe_guide.skills import SkillSpec, install_skill

class SkillsTests(unittest.TestCase):
    def test_failed_fetch_is_pending(self):
        with tempfile.TemporaryDirectory() as d:
            r=install_skill(SkillSpec('demo','not-a-url','deadbeef'), Path(d), True)
            self.assertEqual(r.status, 'pending'); self.assertFalse(r.installed)

