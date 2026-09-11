import json
import tempfile
import unittest
from pathlib import Path
from vibe_guide.installation import inspect_compatibility, migration_preview, migrate_state
from vibe_guide.cli import run_cli

class V44InstallCompatibilityTests(unittest.TestCase):
    def test_mixed_versions_preview_is_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'.vibe').mkdir()
            state={'workflow_version':2,'schema_version':1,'binding':{'provider':'codex'}}
            (root/'.vibe'/'state.json').write_text(json.dumps(state))
            before=(root/'.vibe'/'state.json').read_bytes()
            report=inspect_compatibility(root)
            self.assertEqual(report['status'],'mixed'); self.assertTrue(migration_preview(root)['read_only'])
            self.assertEqual(before,(root/'.vibe'/'state.json').read_bytes())

    def test_explicit_migration_preserves_source_and_records_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'.vibe').mkdir(); source=root/'.vibe'/'state.json'; source.write_text('{"workflow_version": 2}')
            result=migrate_state(root)
            self.assertEqual(result['status'],'complete'); self.assertTrue(source.exists())
            self.assertTrue((root/'.vibe'/'migration_evidence.json').exists())
            self.assertTrue(Path(result['target']).exists())

    def test_cli_migrate_state_is_explicit_entrypoint(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'.vibe').mkdir(); (root/'.vibe'/'state.json').write_text('{"workflow_version": 2}')
            result=run_cli(["migrate-state", "--json"], root)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.payload["status"], "complete")
            self.assertIn("rollback_evidence", result.payload)

    def test_history_manifest_and_rollback_evidence_preserve_runs(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); vibe=root/'.vibe'; (vibe/'runs'/'old').mkdir(parents=True)
            source=vibe/'state.json'; source.write_text('{"workflow_version": 2, "schema_version": 1}')
            (vibe/'runs'/'old'/'events.jsonl').write_text('event')
            result=migrate_state(root)
            self.assertTrue((vibe/'history_manifest.json').exists())
            self.assertTrue((vibe/'rollback_evidence.json').exists())
            self.assertTrue((vibe/'runs'/'old'/'events.jsonl').exists())
            self.assertTrue(Path(result['target']).exists())

if __name__=='__main__': unittest.main()
