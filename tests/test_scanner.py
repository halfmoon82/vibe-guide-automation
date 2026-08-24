import tempfile, unittest
from pathlib import Path
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
