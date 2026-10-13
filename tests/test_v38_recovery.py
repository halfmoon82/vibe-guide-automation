import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
import json
from unittest.mock import patch

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.contracts import RunEvent
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide import state as state_module
from vibe_guide.manifest import RunManifest, save_run_manifest
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import RunSnapshot, append_event, load_snapshot, save_snapshot


def _state_snapshot(paths, record, node_status):
    event_path = paths.root / ".vibe/runs/run-recovery/events.jsonl"
    if not event_path.exists():
        append_event(
            paths,
            RunEvent(
                "run_started",
                {
                    "run_id": "run-recovery",
                    "authorization_digest": record.digest,
                    "node_contract_digest": record.node_contract_digest,
                    "node_ids": ["n1"],
                },
            ),
        )
    return RunSnapshot(
        "run-recovery",
        "plan-1",
        1,
        "running",
        {"n1": {"status": node_status}},
        {},
        authorization=record.to_dict(),
        authorization_digest=record.digest,
        node_contract_digest=record.node_contract_digest,
        event_sequence=1,
    )


class V38RecoveryTests(unittest.TestCase):
    def test_corrupt_current_state_is_restored_from_valid_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
            node = DAGNode(
                "n1", "n1", [], [], None,
                {"files": ["n1.py"], "worker": "worker-n1", "worktree": ".worktrees/n1"},
                "ready",
            )
            card = build_authorization_card(
                plan, [node], AgentCapabilities("fake", True, True, True, True, True, "full")
            )
            record = authorize(card, "AUTHORIZE")
            first = _state_snapshot(paths, record, "planned")
            save_snapshot(paths, first)
            # The second write creates a valid state.previous.json containing
            # the first snapshot, which is the recovery candidate.
            second = _state_snapshot(paths, record, "running")
            save_snapshot(paths, second)
            state_path = paths.root / ".vibe/runs/run-recovery/state.json"
            previous_path = state_path.with_name("state.previous.json")
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            state_path.write_text("{truncated", encoding="utf-8")

            recovered = load_snapshot(paths, "run-recovery")

            self.assertEqual(recovered.nodes["n1"]["status"], "planned")
            self.assertNotEqual(state_path.read_bytes(), b"{truncated")
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), previous)

    def test_recovery_preserves_newer_current_snapshot_written_before_recovery_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
            node = DAGNode(
                "n1", "n1", [], [], None,
                {"files": ["n1.py"], "worker": "worker-n1", "worktree": ".worktrees/n1"},
                "ready",
            )
            card = build_authorization_card(
                plan, [node], AgentCapabilities("fake", True, True, True, True, True, "full")
            )
            record = authorize(card, "AUTHORIZE")
            older = _state_snapshot(paths, record, "planned")
            newer = _state_snapshot(paths, record, "running")
            save_snapshot(paths, older)
            save_snapshot(paths, newer)
            state_path = paths.root / ".vibe/runs/run-recovery/state.json"
            state_path.write_text("{truncated", encoding="utf-8")
            original_lock = state_module.interprocess_lock
            once = [False]

            @contextmanager
            def write_before_recovery_lock(lock_path, *args, **kwargs):
                if not once[0]:
                    once[0] = True
                    with patch.object(state_module, "interprocess_lock", original_lock):
                        save_snapshot(paths, newer)
                with original_lock(lock_path, *args, **kwargs):
                    yield

            with patch.object(state_module, "interprocess_lock", write_before_recovery_lock):
                recovered = load_snapshot(paths, "run-recovery")

            self.assertEqual(recovered.nodes["n1"]["status"], "running")
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["nodes"]["n1"]["status"],
                "running",
            )

    def test_manifest_rejects_foreign_epoch_and_invalid_sha(self):
        with self.assertRaises(ValueError):
            RunManifest.from_mapping({
                "plan_id": "plan", "plan_revision": 1, "run_id": "run",
                "base_sha": "not-a-sha", "target_branch": "codex/v38",
                "execution_epoch": 0, "authorization_digest": "",
                "evidence_ref": "events.jsonl",
            })

    def test_save_does_not_follow_manifest_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            run_dir = Path(directory) / ".vibe" / "runs" / "run"
            run_dir.mkdir(parents=True)
            target = Path(directory) / "outside.json"
            target.write_text("sentinel", encoding="utf-8")
            (run_dir / "run-manifest.json").symlink_to(target)
            manifest = RunManifest.from_mapping({
                "plan_id": "plan", "plan_revision": 1, "run_id": "run",
                "base_sha": "a" * 40, "target_branch": "codex/v38",
                "execution_epoch": 0, "authorization_digest": "",
                "evidence_ref": "events.jsonl",
            })
            with self.assertRaises(ValueError):
                save_run_manifest(paths, manifest)
            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel")


if __name__ == "__main__":
    unittest.main()
