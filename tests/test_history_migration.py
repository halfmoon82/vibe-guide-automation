import json, tempfile, unittest
from pathlib import Path
from vibe_guide.history_migration import migrate_history, replay_history, historical_run_path
from vibe_guide.paths import ProjectPaths

class HistoryMigrationTests(unittest.TestCase):
    def test_migration_is_read_only_and_separates_namespace(self):
        with tempfile.TemporaryDirectory() as td:
            source=Path(td)/'legacy'; source.mkdir(); (source/'state.json').write_text('{"version":"4.2.0"}')
            (source/'events.jsonl').write_text('')
            dest=Path(td)/'project'; dest.mkdir(); paths=ProjectPaths(dest, Path(td)/'home')
            result=migrate_history(source, paths, 'run-old')
            self.assertEqual(result['status'],'migrated')
            self.assertTrue((source/'state.json').exists())
            self.assertTrue((paths.history_dir/'run-old'/'history_manifest.json').exists())
            self.assertTrue(historical_run_path(paths,'run-old').is_relative_to(paths.history_dir))
            self.assertEqual(replay_history(paths,'run-old')['status'],'historical_incomplete')

    def test_current_namespace_cannot_resume_history(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'p'; root.mkdir(); paths=ProjectPaths(root,Path(td)/'h')
            with self.assertRaises(ValueError): historical_run_path(paths,'../x')

if __name__=='__main__': unittest.main()

class HistoryReworkTests(unittest.TestCase):
    def test_valid_event_chain_replays_as_migrated(self):
        from vibe_guide.state import RunEvent, append_event
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'p'; root.mkdir(); paths=ProjectPaths(root,Path(td)/'h')
            append_event(paths, RunEvent('legacy_event', {'run_id':'old','source':'legacy'}))
            source=root/'.vibe'/'runs'/'old'
            # current event log is already in the legacy run directory
            (source/'state.json').write_text('{"version":"4.2.0"}')
            migrate_history(source, paths, 'replay-ok')
            self.assertEqual(replay_history(paths,'replay-ok')['status'], 'replayed')

    def test_run_dir_version_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'p'; root.mkdir(); paths=ProjectPaths(root,Path(td)/'h')
            source=root/'.vibe'/'runs'/'old'; source.mkdir(parents=True)
            (source/'state.json').write_text('{"version":"4.3.0"}')
            result=migrate_history(source,paths,'detected')
            self.assertEqual(result['source_version'],'4.3.0')

    def _legacy_copy(self, td, mutate=None):
        from vibe_guide.state import RunEvent, append_event
        import shutil
        root=Path(td)/'p'; root.mkdir(); paths=ProjectPaths(root,Path(td)/'h')
        append_event(paths, RunEvent('legacy_event', {'run_id':'old','source':'legacy'}))
        source=root/'.vibe'/'runs'/'old'; (source/'state.json').write_text('{"version":"4.2.0"}')
        if mutate: mutate(source/'events.jsonl')
        migrate_history(source,paths,'case')
        return paths

    def test_legacy_empty_head_is_accepted(self):
        import json, hashlib
        paths=self._legacy_copy(tempfile.mkdtemp())
        path=historical_run_path(paths,'case')/'payload'/'events.jsonl'; rec=json.loads(path.read_text().strip())
        rec.pop('previous_event_digest',None); rec['previous_digest']=''
        payload=dict(rec); payload.pop('event_digest',None)
        rec['event_digest']=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        path.write_text(json.dumps(rec,sort_keys=True,separators=(',',':'))+'\n')
        manifest_path=historical_run_path(paths,'case')/'history_manifest.json'; manifest=json.loads(manifest_path.read_text())
        for entry in manifest['files']:
            if entry['path']=='events.jsonl':
                entry['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest,sort_keys=True,separators=(',',':'))+'\n')
        self.assertEqual(replay_history(paths,'case')['status'],'replayed')


    def test_tampered_payload_and_digest_are_reported(self):
        import json
        for field in ('data','event_digest'):
            with tempfile.TemporaryDirectory() as td:
                paths=self._legacy_copy(td)
                path=historical_run_path(paths,'case')/'payload'/'events.jsonl'; rec=json.loads(path.read_text().strip())
                if field=='data': rec['data']['source']='tampered'
                else: rec['event_digest']='0'*64
                path.write_text(json.dumps(rec,sort_keys=True,separators=(',',':'))+'\n')
                result=replay_history(paths,'case')
                self.assertEqual(result['status'],'historical_incomplete')
                self.assertTrue(result['errors'])
