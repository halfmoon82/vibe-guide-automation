"""vibe run-restore: bring an archived run back, safely and exactly once."""

import json
import unittest
from unittest import mock

from tests.test_run_archive_274 import RunArchiveFixture
from vibe_guide import daemon as dm
from vibe_guide.cli import run_cli
from vibe_guide.state import load_events, runs_archive_dir


class RunRestoreTests(RunArchiveFixture):
    def archive(self):
        self.monitor.archive_run(self.run_id)
        self.assertTrue(runs_archive_dir(self.paths, self.run_id).is_dir())

    def events(self):
        return [e["event"] for e in load_events(self.paths, self.run_id)]

    def restore(self):
        starts = []

        def starter(paths, run_id, plan_id, **_k):
            starts.append((run_id, plan_id))
            return {"status": "started"}
        with mock.patch.object(dm, "start_daemon", starter):
            result = dm.restore_run(self.paths, self.run_id)
        return result, starts

    def test_restore_twice_second_call_has_no_side_effect(self):
        self.archive()
        first, starts = self.restore()
        self.assertEqual(first["status"], "restored")
        self.assertTrue(first["changed"])
        self.assertTrue((self.paths.vibe / "runs" / self.run_id / "state.json").is_file())
        self.assertFalse(runs_archive_dir(self.paths, self.run_id).exists())
        self.assertEqual(self.events().count("run_unarchived"), 1)
        before = (self.paths.vibe / "runs" / self.run_id / "events.jsonl").read_bytes()
        backups = sorted((self.paths.vibe / "runs-restore-backups").iterdir())
        second, starts_again = self.restore()
        self.assertEqual(second["status"], "already_live")
        self.assertFalse(second["changed"])
        self.assertEqual(starts_again, [])
        self.assertEqual(self.events().count("run_unarchived"), 1)
        self.assertEqual((self.paths.vibe / "runs" / self.run_id / "events.jsonl").read_bytes(), before)
        self.assertEqual(sorted((self.paths.vibe / "runs-restore-backups").iterdir()), backups)

    def test_backup_is_taken_before_anything_moves_and_matches_the_archive(self):
        self.archive()
        archive = runs_archive_dir(self.paths, self.run_id)
        expected = {p.relative_to(archive).as_posix(): p.read_bytes() for p in archive.rglob("*") if p.is_file()}
        seen = {}
        original = dm.unarchive_run

        def spy(paths, run_id):
            backup = next((paths.vibe / "runs-restore-backups").iterdir())
            seen.update({p.relative_to(backup).as_posix(): p.read_bytes() for p in backup.rglob("*") if p.is_file()})
            self.assertTrue(archive.is_dir())  # not moved yet
            return original(paths, run_id)
        with mock.patch.object(dm, "unarchive_run", spy):
            self.restore()
        self.assertEqual(seen, expected)

    def test_the_daemon_is_started_again_with_the_runs_plan(self):
        self.archive()
        with mock.patch.object(dm, "load_snapshot",
                               return_value=mock.Mock(plan_id="probe-plan", authorization_digest="a", node_contract_digest="n")):
            result, starts = self.restore()
        self.assertEqual(starts, [(self.run_id, "probe-plan")])
        self.assertEqual(result["daemon"]["status"], "started")

    def test_archive_after_restore_records_a_new_run_archived_event(self):
        self.archive()
        self.restore()
        self.monitor.archive_run(self.run_id)
        self.assertEqual(self.events().count("run_archived"), 2)

    def test_unknown_run_is_reported_not_invented(self):
        result = dm.restore_run(self.paths, "run-doesnotexist1")
        self.assertEqual(result["status"], "not_found")
        self.assertFalse(result["changed"])

    def test_cli_command_restores_an_archived_run(self):
        self.archive()
        with mock.patch.object(dm, "start_daemon", return_value={"status": "started"}):
            result = run_cli(["run-restore", "--run-id", self.run_id, "--json"], self.root)
        self.assertEqual(result.payload["status"], "restored", result.payload)
        again = run_cli(["run-restore", "--run-id", self.run_id, "--json"], self.root)
        self.assertEqual(again.payload["status"], "already_live", again.payload)
        self.assertEqual(self.events().count("run_unarchived"), 1)


if __name__ == "__main__":
    unittest.main()
