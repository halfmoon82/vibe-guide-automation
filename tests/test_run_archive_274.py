"""Run archiving (issue #274): terminal runs move to .vibe/runs-archive/.

Covers the acceptance example: a completed run's directory, mailbox requests,
writer leases and task bindings leave .vibe/runs/ together; pending() stops
reporting the dead run's requests; the node is free for a same-name
redispatch; a second archive is idempotent; and a run_archived event is
recorded in the moved event log.
"""
import json
import unittest
from unittest import mock

from tests import support_v45_authorize as support
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.cli import _load_plan, run_cli
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import (
    acquire_writer_lease,
    append_event,
    archive_run,
    archive_run_leases,
    load_events,
    run_dir,
    runs_archive_dir,
    unarchive_run,
)
from vibe_guide.contracts import RunEvent
from vibe_guide.runners.provider_action import archive_run_provider_actions
from vibe_guide.task_registry import TaskBinding, save_task_binding


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _request(action_id, run_id, operation="create"):
    return {
        "schema_version": 1,
        "action_id": action_id,
        "request_digest": "d" * 64,
        "operation": operation,
        "provider": "claude-code",
        "run_id": run_id,
        "issue_id": "probe-node-a",
        "role": "developer",
        "generation": 0,
        "request": {"authorization_digest": "a" * 64},
    }


class RunArchiveFixture(unittest.TestCase):
    """A project with one fabricated finished run plus mailbox debris."""

    def setUp(self):
        self.root = support.publish_complex_probe(self)
        self.paths = ProjectPaths(self.root)
        _, self.plan, self.nodes, _ = _load_plan(self.paths, "probe-plan")
        self.monitor = Monitor(self.paths, self.plan, self.nodes)
        self.run_id = "run-deadbeef274"
        self.other_run = "run-liveson0001"
        self._fabricate_run(self.run_id, "complete")
        self._fabricate_mailbox(self.run_id)
        self._fabricate_mailbox(self.other_run)

    def _fabricate_run(self, run_id, status):
        directory = self.paths.vibe / "runs" / run_id
        directory.mkdir(parents=True)
        _write(directory / "state.json", {"status": status, "run_id": run_id})
        append_event(
            self.paths,
            RunEvent("run_started", {"run_id": run_id, "status": "running"}),
        )

    def _fabricate_mailbox(self, run_id):
        store = ProviderActionStore(self.paths)
        aid = "action-{}-1".format(run_id[-6:])
        _write(store.root / "requests" / (aid + ".json"), _request(aid, run_id))
        # A second, still-unanswered request is what keeps the run visible in
        # pending(); the answered one above exercises result relocation.
        pending_aid = "action-{}-2".format(run_id[-6:])
        _write(store.root / "requests" / (pending_aid + ".json"),
               _request(pending_aid, run_id, operation="wait"))
        _write(
            store.root / "results" / (aid + ".json"),
            {"schema_version": 1, "action_id": aid,
             "request_digest": "d" * 64, "payload": {"status": "ok"}},
        )
        _write(
            store.root / "deliveries" / ("{}-probe-node-a-developer-g0.json".format(run_id)),
            {"run_id": run_id, "issue_id": "probe-node-a", "role": "developer",
             "generation": 0},
        )
        prompt = store.root / "prompts" / ("{}-probe-node-a-developer-g0.md".format(run_id))
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text("# prompt for {}\n".format(run_id), encoding="utf-8")
        _write(
            store.root / "claude-bg-sessions" / ("claude-bg-probe-node-a-{}.json".format(run_id[-6:])),
            {"contract": {}, "worktree": ".worktrees/x",
             "claims": {"run_id": run_id, "node_id": "probe-node-a",
                        "role": "developer", "generation": 0}},
        )
        _write(
            store.root / "handles" / ("handle-{}.json".format(run_id[-6:])),
            {"run_id": run_id, "node_id": "probe-node-a"},
        )

    def _fabricate_lease(self, run_id, node_id="probe-node-a", worktree=".worktrees/x"):
        lease_dir = self.paths.vibe / "leases"
        lease_dir.mkdir(parents=True, exist_ok=True)
        _write(
            lease_dir / (run_id[-8:] + ".json"),
            {"schema_version": 1, "node_id": node_id, "worktree": worktree,
             "run_id": run_id, "lease_id": "l" * 64, "status": "active"},
        )

    def pending_ids(self):
        return [item["run_id"] for item in ProviderActionStore(self.paths).pending()]


class RunArchiveTests(RunArchiveFixture):
    def test_completed_run_directory_moves_and_stays_readable(self):
        manifest = self.monitor.archive_run(self.run_id)
        self.assertFalse((self.paths.vibe / "runs" / self.run_id).exists())
        archive = runs_archive_dir(self.paths, self.run_id)
        self.assertTrue(archive.is_dir())
        self.assertTrue((archive / "state.json").is_file())
        # run_dir redirects so late supervisor writes land in the archive
        # instead of recreating a stub under .vibe/runs/.
        self.assertEqual(run_dir(self.paths, self.run_id), archive)
        self.assertIn(".", manifest["moved"])

    def test_dead_run_requests_leave_pending(self):
        self.assertIn(self.run_id, self.pending_ids())
        self.monitor.archive_run(self.run_id)
        pending = self.pending_ids()
        self.assertNotIn(self.run_id, pending)
        self.assertIn(self.other_run, pending)
        archive = runs_archive_dir(self.paths, self.run_id)
        for name in ("requests", "results", "deliveries", "prompts",
                     "handles", "claude-bg-sessions"):
            self.assertTrue(
                any((archive / "provider-actions" / name).glob("*")),
                name,
            )
        # The surviving run's mailbox is untouched.
        store = ProviderActionStore(self.paths)
        self.assertTrue(list((store.root / "requests").glob("*.json")))

    def test_lease_and_binding_stop_blocking_same_node(self):
        self._fabricate_lease(self.run_id)
        binding = TaskBinding(
            provider="claude-code", mode="visible", issue_id="probe-node-a",
            role="developer", task_id="dead-task", host="local",
            run_id=self.run_id, worktree=".worktrees/x", branch="b",
            generation=0, status="archived",
        )
        save_task_binding(self.paths, binding)
        self.monitor.archive_run(self.run_id)
        # tasks.json moved with the run directory; the lease file moved into
        # the archive, so the same node/worktree acquires a fresh lease.
        self.assertFalse((self.paths.vibe / "leases" / (self.run_id[-8:] + ".json")).exists())
        archive = runs_archive_dir(self.paths, self.run_id)
        self.assertTrue((archive / "tasks.json").is_file())
        self.assertTrue((archive / "leases" / (self.run_id[-8:] + ".json")).is_file())
        self.assertTrue(
            acquire_writer_lease(self.paths, "probe-node-a", ".worktrees/x", "run-newlease001")
        )

    def test_second_archive_is_idempotent(self):
        first = self.monitor.archive_run(self.run_id)
        second = self.monitor.archive_run(self.run_id)
        self.assertTrue(second["archived"])
        archive = runs_archive_dir(self.paths, self.run_id)
        self.assertTrue((archive / "state.json").is_file())
        self.assertNotIn(self.run_id, self.pending_ids())

    def test_archive_event_is_recorded_in_moved_log(self):
        self.monitor.archive_run(self.run_id)
        records = load_events(self.paths, self.run_id)
        self.assertEqual(records[-1]["event"], "run_archived")
        self.assertEqual(records[-1]["data"]["run_id"], self.run_id)
        # The log moved with the directory: no events.jsonl is recreated.
        self.assertFalse((self.paths.vibe / "runs" / self.run_id).exists())

    def test_live_run_requires_force(self):
        live = "run-alive000274"
        self._fabricate_run(live, "running")
        with self.assertRaises(ValueError):
            self.monitor.archive_run(live)
        self.monitor.archive_run(live, force=True)
        self.assertTrue(runs_archive_dir(self.paths, live).is_dir())

    def test_missing_state_run_archives_only_with_force(self):
        broken = "run-broken00274"
        (self.paths.vibe / "runs" / broken).mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.monitor.archive_run(broken)
        manifest = self.monitor.archive_run(broken, force=True)
        self.assertTrue(manifest["archived"])

    def test_existing_archive_is_not_overwritten(self):
        self.monitor.archive_run(self.run_id)
        archive = runs_archive_dir(self.paths, self.run_id)
        marker = archive / "kept.txt"
        marker.write_text("do not touch", encoding="utf-8")
        self.monitor.archive_run(self.run_id)
        self.assertEqual(marker.read_text(encoding="utf-8"), "do not touch")

    def test_recreated_stub_merges_without_overwriting(self):
        self.monitor.archive_run(self.run_id)
        stub = self.paths.vibe / "runs" / self.run_id
        (stub / "tool-output").mkdir(parents=True)
        (stub / "tool-output" / "late.txt").write_text("late", encoding="utf-8")
        (stub / "state.json").write_text('{"status": "complete"}', encoding="utf-8")
        self.monitor.archive_run(self.run_id, force=True)
        self.assertFalse(stub.exists())
        archive = runs_archive_dir(self.paths, self.run_id)
        self.assertEqual(
            (archive / "tool-output" / "late.txt").read_text(encoding="utf-8"), "late"
        )
        # The archived state.json was not overwritten by the stub's version.
        self.assertIn('"status": "complete"', (archive / "state.json").read_text())

    def test_failed_archive_records_reason_not_false_success(self):
        # A mid-move failure must leave run_archive_failed evidence with its
        # reason class and must not leave a false run_archived record; the
        # run directory stays in place untouched (contract error behavior).
        with mock.patch(
            "vibe_guide.monitor._archive_run_artifacts",
            side_effect=ValueError("archive lease destination already exists: x.json"),
        ):
            with self.assertRaises(ValueError):
                self.monitor.archive_run(self.run_id)
        primary = self.paths.vibe / "runs" / self.run_id
        self.assertTrue(primary.is_dir())
        self.assertTrue((primary / "state.json").is_file())
        records = load_events(self.paths, self.run_id)
        self.assertEqual(records[-1]["event"], "run_archive_failed")
        self.assertEqual(records[-1]["data"]["reason_class"], "ValueError")
        self.assertFalse(
            any(record["event"] == "run_archived" for record in records)
        )

    def test_failed_archive_records_partial_move_in_archive(self):
        # When the run directory already moved before the failure, the
        # failure event still lands on disk -- inside the archive leg via
        # run_dir's redirect -- so the partial state stays auditable.
        def _move_then_fail(paths, run_id):
            import os
            destination = runs_archive_dir(paths, run_id)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.rename(str(paths.vibe / "runs" / run_id), str(destination))
            raise OSError("simulated mid-move failure")

        with mock.patch(
            "vibe_guide.monitor._archive_run_artifacts",
            side_effect=_move_then_fail,
        ):
            with self.assertRaises(OSError):
                self.monitor.archive_run(self.run_id)
        records = [
            json.loads(line)
            for line in (runs_archive_dir(self.paths, self.run_id)
                         / "events.jsonl").read_text().splitlines()
        ]
        self.assertEqual(records[-1]["event"], "run_archive_failed")
        self.assertEqual(records[-1]["data"]["reason_class"], "OSError")

    def test_sweep_failure_dedupes_failure_events(self):
        with mock.patch(
            "vibe_guide.monitor._archive_run_artifacts",
            side_effect=TypeError("boom"),
        ):
            self.monitor._archive_finished_runs()
            self.monitor._archive_finished_runs()
        records = load_events(self.paths, self.run_id)
        failures = [r for r in records if r["event"] == "run_archive_failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["data"]["reason_class"], "TypeError")
        # A later successful archive still gets its run_archived record.
        self.monitor.archive_run(self.run_id)
        records = load_events(self.paths, self.run_id)
        self.assertEqual(records[-1]["event"], "run_archived")

    def test_reconcile_package_routes_to_archive(self):
        payload = {"kind": "run_archive", "run_id": self.run_id}
        with self.assertRaises(ValueError):
            # The fabricated run has no loadable RunSnapshot, but the archive
            # itself already completed before the snapshot load is attempted.
            self.monitor.reconcile_evidence(self.run_id, payload)
        self.assertTrue(runs_archive_dir(self.paths, self.run_id).is_dir())
        self.assertNotIn(self.run_id, self.pending_ids())

    def test_monitor_entry_sweep_archives_only_complete_runs(self):
        self._fabricate_run("run-running274", "running")
        self._fabricate_run("run-failed0274", "failed")
        self._fabricate_run("run-blocked274", "blocked_unknown")
        self.monitor._archive_finished_runs()
        # Only the completed run moves; failed/blocked runs may still hold a
        # quarantined writer lease that must keep blocking a redispatch, so
        # they archive only through the manual run_archive package.
        self.assertTrue(runs_archive_dir(self.paths, self.run_id).is_dir())
        for survivor in ("run-running274", "run-failed0274", "run-blocked274"):
            self.assertTrue(
                (self.paths.vibe / "runs" / survivor).is_dir(), survivor
            )

    def test_reopened_run_restores_under_runs(self):
        self.monitor.archive_run(self.run_id)
        self.assertTrue(unarchive_run(self.paths, self.run_id))
        primary = self.paths.vibe / "runs" / self.run_id
        self.assertTrue(primary.is_dir())
        self.assertTrue((primary / "state.json").is_file())
        # Mailbox requests ride back so a reopened run regains its pending
        # wait evidence instead of being redispatched blind.
        self.assertIn(self.run_id, self.pending_ids())
        archive = runs_archive_dir(self.paths, self.run_id)
        self.assertFalse(archive.exists())

    def test_unarchive_restores_leases(self):
        self._fabricate_lease(self.run_id)
        self.monitor.archive_run(self.run_id)
        self.assertTrue(unarchive_run(self.paths, self.run_id))
        self.assertTrue(
            (self.paths.vibe / "leases" / (self.run_id[-8:] + ".json")).is_file()
        )

    def test_run_dir_redirect_keeps_tool_output_in_archive(self):
        self.monitor.archive_run(self.run_id)
        # bounded_supervisor_output calls run_dir(create=True) after monitor
        # returns; it must land inside the archive, not resurrect a stub.
        directory = run_dir(self.paths, self.run_id, create=True) / "tool-output"
        directory.mkdir(parents=True)
        (directory / "x.txt").write_text("x", encoding="utf-8")
        self.assertFalse((self.paths.vibe / "runs" / self.run_id).exists())
        self.assertTrue((runs_archive_dir(self.paths, self.run_id) / "tool-output" / "x.txt").is_file())

    def test_cli_reconcile_run_archive(self):
        (self.paths.vibe / "archive-pkg.json").write_text(json.dumps(
            {"kind": "run_archive", "run_id": self.run_id}))
        result = run_cli(
            ["reconcile", "--plan", "probe-plan", "--run-id", self.run_id,
             "--evidence", ".vibe/archive-pkg.json", "--json"],
            self.root,
        )
        # No loadable snapshot exists for the fabricated run, so the command
        # reports blocked_unknown while the archive itself is complete.
        self.assertEqual(result.payload.get("status"), "blocked_unknown", result.payload)
        self.assertTrue(runs_archive_dir(self.paths, self.run_id).is_dir())
        self.assertNotIn(self.run_id, self.pending_ids())


if __name__ == "__main__":
    unittest.main()
