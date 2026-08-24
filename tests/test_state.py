import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.contracts import RunEvent
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import (
    RunSnapshot,
    acquire_writer_lease,
    append_event,
    load_snapshot,
    release_writer_lease,
    save_snapshot,
)


def _append_process_event(root, index, barrier):
    barrier.wait()
    for offset in range(3):
        append_event(
            ProjectPaths(Path(root)),
            RunEvent(
                "worker_event",
                {"run_id": "run-process", "worker": index, "offset": offset},
            ),
        )


def _leave_dead_event_lock(root):
    run_dir = Path(root) / ".vibe" / "runs" / "run-dead-lock"
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / ".events.lock"
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "owner": "dead-owner"}),
        encoding="utf-8",
    )
    os._exit(0)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        self.plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        self.node = DAGNode(
            "n1",
            "n1",
            [],
            [],
            None,
            {"files": ["n1.py"], "worker": "worker-n1", "worktree": ".worktrees/n1"},
            "ready",
        )
        card = build_authorization_card(
            self.plan,
            [self.node],
            AgentCapabilities("fake", True, True, True, True, True, "full"),
        )
        self.record = authorize(card, "AUTHORIZE")

    def tearDown(self):
        self.temporary.cleanup()

    def snapshot(self, status="running", node_status="running"):
        event_path = Path(self.temporary.name) / ".vibe/runs/run-1/events.jsonl"
        if not event_path.exists():
            append_event(
                self.paths,
                RunEvent(
                    "run_started",
                    {
                        "run_id": "run-1",
                        "authorization_digest": self.record.digest,
                        "node_contract_digest": self.record.node_contract_digest,
                        "node_ids": ["n1"],
                    },
                ),
            )
        return RunSnapshot(
            "run-1",
            "plan-1",
            1,
            status,
            {"n1": {"status": node_status}},
            {},
            authorization=self.record.to_dict(),
            authorization_digest=self.record.digest,
            node_contract_digest=self.record.node_contract_digest,
            event_sequence=1,
        )

    def test_events_append_with_sequence_without_rewriting_history(self):
        append_event(self.paths, RunEvent("started", {"run_id": "run-1", "node_id": "n1"}))
        event_path = Path(self.temporary.name) / ".vibe/runs/run-1/events.jsonl"
        first_bytes = event_path.read_bytes()

        append_event(self.paths, RunEvent("accepted", {"run_id": "run-1", "node_id": "n1"}))
        records = [json.loads(line) for line in event_path.read_text().splitlines()]

        self.assertTrue(event_path.read_bytes().startswith(first_bytes))
        self.assertEqual([record["sequence"] for record in records], [1, 2])
        self.assertEqual([record["event"] for record in records], ["started", "accepted"])
        self.assertEqual([record["schema_version"] for record in records], [1, 1])
        self.assertTrue(all(record["provenance"]["role"] == "system" for record in records))
        self.assertEqual([record["run_id"] for record in records], ["run-1", "run-1"])
        self.assertIsNone(records[0]["previous_event_digest"])
        self.assertEqual(records[1]["previous_event_digest"], records[0]["event_digest"])

    def test_atomic_snapshot_ignores_interrupted_temporary_file(self):
        expected = self.snapshot()
        save_snapshot(self.paths, expected)
        state_dir = Path(self.temporary.name) / ".vibe/runs/run-1"
        (state_dir / ".state.json.interrupted").write_text("{not-json")

        self.assertEqual(load_snapshot(self.paths, "run-1"), expected)

    def test_load_falls_back_to_last_valid_snapshot(self):
        first = self.snapshot()
        second = self.snapshot(node_status="pending")
        save_snapshot(self.paths, first)
        save_snapshot(self.paths, second)
        state_path = Path(self.temporary.name) / ".vibe/runs/run-1/state.json"
        state_path.write_text("truncated")

        self.assertEqual(load_snapshot(self.paths, "run-1"), first)

    def test_semantically_invalid_current_snapshot_falls_back(self):
        first = self.snapshot()
        second = self.snapshot(node_status="pending")
        save_snapshot(self.paths, first)
        save_snapshot(self.paths, second)
        state_path = Path(self.temporary.name) / ".vibe/runs/run-1/state.json"
        invalid = second.to_dict()
        invalid["nodes"] = []
        invalid["status"] = "complete"
        state_path.write_text(json.dumps(invalid), encoding="utf-8")

        self.assertEqual(load_snapshot(self.paths, "run-1"), first)

    def test_snapshot_with_tampered_authorization_record_falls_back(self):
        first = self.snapshot()
        second = self.snapshot(node_status="pending")
        save_snapshot(self.paths, first)
        save_snapshot(self.paths, second)
        state_path = Path(self.temporary.name) / ".vibe/runs/run-1/state.json"
        tampered = json.loads(state_path.read_text(encoding="utf-8"))
        tampered["authorization"]["file_scope"].append("outside.py")
        state_path.write_text(json.dumps(tampered), encoding="utf-8")

        self.assertEqual(load_snapshot(self.paths, "run-1"), first)

    def test_event_provenance_hash_chain_rejects_rewrite(self):
        append_event(self.paths, RunEvent("first", {"run_id": "run-chain"}))
        append_event(self.paths, RunEvent("second", {"run_id": "run-chain"}))
        event_path = Path(self.temporary.name) / ".vibe/runs/run-chain/events.jsonl"
        records = [json.loads(line) for line in event_path.read_text().splitlines()]
        records[0]["provenance"]["role"] = "reviewer"
        event_path.write_text(
            "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8"
        )

        with self.assertRaises(ValueError):
            append_event(self.paths, RunEvent("third", {"run_id": "run-chain"}))

    def test_writer_lease_is_exclusive_and_only_owner_can_release(self):
        self.assertTrue(acquire_writer_lease(self.paths, "n1", "worktree-a", "run-1"))
        self.assertFalse(acquire_writer_lease(self.paths, "n1", "worktree-a", "run-2"))
        self.assertFalse(release_writer_lease(self.paths, "n1", "worktree-a", "run-2"))
        self.assertTrue(release_writer_lease(self.paths, "n1", "worktree-a", "run-1"))
        self.assertTrue(acquire_writer_lease(self.paths, "n1", "worktree-a", "run-2"))

    def test_run_registry_rejects_symlink_escape(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        vibe = Path(self.temporary.name) / ".vibe"
        vibe.mkdir()
        (vibe / "runs").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(ValueError):
            append_event(self.paths, RunEvent("started", {"run_id": "run-escape"}))
        self.assertFalse((outside / "run-escape" / "events.jsonl").exists())

    def test_run_registry_rejects_broken_symlink_component(self):
        vibe = Path(self.temporary.name) / ".vibe"
        vibe.mkdir()
        (vibe / "runs").symlink_to(vibe / "missing-run-root", target_is_directory=True)

        with self.assertRaises(ValueError):
            append_event(self.paths, RunEvent("started", {"run_id": "run-broken"}))
        self.assertFalse((vibe / "missing-run-root").exists())

    def test_event_sequence_allocation_is_multi_process_safe(self):
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(16)
        processes = [
            context.Process(
                target=_append_process_event,
                args=(self.temporary.name, index, barrier),
            )
            for index in range(16)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)

        event_path = Path(self.temporary.name) / ".vibe/runs/run-process/events.jsonl"
        records = [json.loads(line) for line in event_path.read_text().splitlines()]
        self.assertEqual([record["sequence"] for record in records], list(range(1, 49)))

    def test_event_lock_recovers_after_holder_process_dies(self):
        context = multiprocessing.get_context("fork")
        process = context.Process(
            target=_leave_dead_event_lock, args=(self.temporary.name,)
        )
        process.start()
        process.join(5)
        self.assertEqual(process.exitcode, 0)
        lock_path = Path(self.temporary.name) / ".vibe/runs/run-dead-lock/.events.lock"
        self.assertTrue(lock_path.exists())

        append_event(
            self.paths,
            RunEvent("recovered", {"run_id": "run-dead-lock"}),
        )

        self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
