import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.models import (
    BindingIntent,
    BindingObservation,
    SupervisorLeaseObservation,
    WaitThreadsCursorObservation,
)
from vibe_guide.paths import ProjectPaths
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.contracts import RunEvent
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.state import (
    LEASE_SCHEMA_VERSION,
    RunSnapshot,
    acquire_writer_lease,
    append_event,
    load_snapshot,
    read_writer_lease,
    save_snapshot,
    supervisor_lease_id,
)
from vibe_guide.task_registry import TaskBinding, load_task_binding, save_task_binding, validate_binding


class V39BindingContractTests(unittest.TestCase):
    def _intent(self, **overrides):
        values = {
            "project_id": "project-1",
            "task_id": "task-1",
            "node_id": "V39-CONTRACT",
            "host_id": "local",
            "worktree": "/tmp/project/.worktrees/n1",
            "managed_root": "/tmp/project",
            "branch": "codex/n1",
            "base_sha": "a" * 40,
            "lease_id": supervisor_lease_id("V39-CONTRACT", "/tmp/project/.worktrees/n1", "run-1"),
            "head_sha": "b" * 40,
            "clean": True,
            "cursor": "cursor-1:24",
        }
        values.update(overrides)
        return BindingIntent(**values)

    def _observation(self, **overrides):
        with tempfile.TemporaryDirectory() as lease_directory:
            lease_paths = ProjectPaths(Path(lease_directory))
            self.assertTrue(
                acquire_writer_lease(
                    lease_paths,
                    "V39-CONTRACT",
                    "/tmp/project/.worktrees/n1",
                    "run-1",
                )
            )
            supervisor_lease = read_writer_lease(
                lease_paths, "V39-CONTRACT", "/tmp/project/.worktrees/n1"
            )
        self.assertIsInstance(supervisor_lease, SupervisorLeaseObservation)
        values = {
            "project_id": "project-1",
            "task_id": "task-1",
            "node_id": "V39-CONTRACT",
            "host_id": "local",
            "worktree": "/tmp/project/.worktrees/n1",
            "managed_root": "/tmp/project",
            "branch": "codex/n1",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "clean": True,
            "lease": supervisor_lease,
            "cursor": "cursor-1:24",
            "source": "codex_app__wait_threads",
            "observed_at": "2026-09-01T00:00:00Z",
            "cursor_source": "codex_app__wait_threads",
            "cursor_task_id": "task-1",
            "cursor_host_id": "local",
            "cursor_lineage": "wait_threads:task-1:local:cursor-1:24",
            "cursor_observation": WaitThreadsCursorObservation.from_wait_threads(
                "task-1", "local", "cursor-1:24"
            ),
        }
        values.update(overrides)
        return BindingObservation(**values)

    def test_binding_contract_is_json_safe_and_round_trips(self):
        intent = self._intent()
        observation = self._observation()
        self.assertEqual(
            BindingIntent.from_dict(json.loads(json.dumps(intent.to_dict()))), intent
        )
        restored = BindingObservation.from_dict(
            json.loads(json.dumps(observation.to_dict()))
        )
        self.assertNotEqual(restored, observation)
        self.assertEqual(
            validate_binding(intent, restored).binding_state, "blocked_unknown"
        )

    def test_validate_binding_rejects_plain_dict_even_with_matching_markers(self):
        result = validate_binding(
            self._intent().to_dict(), self._observation().to_dict()
        )
        self.assertEqual(result.binding_state, "blocked_unknown")
        self.assertFalse(result.business_write_allowed)

    def test_supervisor_lease_allows_missing_provider_lease(self):
        result = validate_binding(self._intent(), self._observation())
        self.assertEqual(result.binding_state, "binding_verified")
        self.assertTrue(result.business_write_allowed)

    def test_verified_binding_requires_unforgeable_supervisor_lease_and_cursor_proof(self):
        intent = self._intent().to_dict()
        observation = self._observation().to_dict()
        observation["lease"] = {
            "active": True,
            "schema_version": LEASE_SCHEMA_VERSION,
            "node_id": "V39-CONTRACT",
            "worktree": observation["worktree"],
            "run_id": "run-1",
            "lease_id": intent["lease_id"],
        }
        observation.update(
            {
                "cursor_source": "codex_app__wait_threads",
                "cursor_task_id": observation["task_id"],
                "cursor_host_id": observation["host_id"],
                "cursor_lineage": "wait_threads:task-1:local:cursor-1:24",
            }
        )
        result = validate_binding(intent, observation)
        self.assertEqual(result.binding_state, "blocked_unknown")
        self.assertFalse(result.business_write_allowed)

    def test_lease_requires_read_writer_lease_proof_and_active_status(self):
        for lease_update in (
            {"active": False},
            {"status": "quarantined"},
            {"source": "caller"},
            {"proof": "caller"},
        ):
            with self.subTest(lease_update=lease_update):
                lease = self._observation().lease.to_dict()
                lease.update(lease_update)
                result = validate_binding(
                    self._intent(), self._observation(lease=lease)
                )
                self.assertEqual(result.binding_state, "blocked_unknown")
                self.assertTrue(
                    "lease" in result.conflicts or "lease" in result.missing
                )

    def test_lease_internal_factory_cannot_be_called_without_reader_provenance(self):
        payload = self._observation().lease.to_dict()
        with self.assertRaises(TypeError):
            SupervisorLeaseObservation._from_read(payload)

    def test_lease_or_cursor_proof_tampering_blocks_verified_state(self):
        intent = self._intent().to_dict()
        observation = self._observation().to_dict()
        observation.update(
            {
                "lease": {
                    "active": True,
                    "schema_version": LEASE_SCHEMA_VERSION,
                    "node_id": "V39-CONTRACT",
                    "worktree": observation["worktree"],
                    "run_id": "run-1",
                    "lease_id": "wrong-lease",
                },
                "cursor_source": "codex_app__wait_threads",
                "cursor_task_id": "other-task",
                "cursor_host_id": observation["host_id"],
                "cursor_lineage": "wait_threads:other-task:local:cursor-1:24",
            }
        )
        result = validate_binding(intent, observation)
        self.assertEqual(result.binding_state, "blocked_unknown")

    def test_lease_proof_requires_all_supervisor_fields(self):
        result = validate_binding(
            self._intent(),
            self._observation(lease={"active": True, "lease_id": self._intent().lease_id}),
        )
        self.assertEqual(result.binding_state, "blocked_unknown")
        self.assertIn("lease", result.missing)

    def test_lease_id_must_be_deterministic_for_intent_and_observation(self):
        result = validate_binding(
            self._intent(),
            self._observation(
                lease={
                    **self._observation().lease.to_dict(),
                    "lease_id": "forged-lease-id",
                }
            ),
        )
        self.assertEqual(result.binding_state, "blocked_unknown")
        self.assertIn("lease", result.missing)

    def test_observation_node_id_must_match_intent(self):
        result = validate_binding(
            self._intent(), self._observation(node_id="other-node")
        )
        self.assertEqual(result.binding_state, "blocked_unknown")
        self.assertIn("node_id", result.conflicts)

    def test_cursor_lineage_must_be_exact_wait_threads_proof(self):
        result = validate_binding(
            self._intent(),
            self._observation(cursor_lineage="forged:task-1:local:cursor-1:24"),
        )
        self.assertEqual(result.binding_state, "blocked_unknown")
        self.assertIn("cursor", result.conflicts)

    def test_binding_intent_key_fields_missing_cannot_be_verified(self):
        for field in ("lease_id", "head_sha", "clean", "cursor"):
            with self.subTest(field=field):
                intent = self._intent().to_dict()
                intent.pop(field)
                observation = self._observation().to_dict()
                observation["lease"] = {
                    "active": True,
                    "schema_version": LEASE_SCHEMA_VERSION,
                    "node_id": "V39-CONTRACT",
                    "worktree": observation["worktree"],
                    "run_id": "run-1",
                    "lease_id": "lease-1",
                }
                observation.update(
                    {
                        "cursor_source": "codex_app__wait_threads",
                        "cursor_task_id": observation["task_id"],
                        "cursor_host_id": observation["host_id"],
                        "cursor_lineage": "wait_threads:task-1:local:cursor-1:24",
                    }
                )
                result = validate_binding(intent, observation)
                self.assertEqual(result.binding_state, "blocked_unknown")

    def test_missing_or_untrusted_cursor_is_blocked(self):
        missing = validate_binding(self._intent(), self._observation(cursor=None))
        self.assertEqual(missing.binding_state, "blocked_unknown")
        self.assertIn("cursor", missing.missing)

        untrusted = validate_binding(
            self._intent(), self._observation(source="contract")
        )
        self.assertEqual(untrusted.binding_state, "blocked_unknown")
        self.assertIn("cursor", untrusted.conflicts)

    def test_any_identity_or_git_drift_is_blocked(self):
        for field, value in (
            ("task_id", "other-task"),
            ("worktree", "/tmp/other"),
            ("managed_root", "/tmp/other-root"),
            ("branch", "codex/other"),
            ("base_sha", "c" * 40),
            ("head_sha", "d" * 40),
            ("clean", False),
        ):
            with self.subTest(field=field):
                result = validate_binding(
                    self._intent(), self._observation(**{field: value})
                )
                self.assertEqual(result.binding_state, "blocked_unknown")

    def test_supervisor_lease_can_be_read_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            self.assertTrue(acquire_writer_lease(paths, "n1", ".worktrees/n1", "run-1"))
            lease = read_writer_lease(paths, "n1", ".worktrees/n1")
            self.assertTrue(lease["active"])
            self.assertEqual(lease["run_id"], "run-1")
            self.assertEqual(lease["status"], "active")
            self.assertEqual(lease["source"], "supervisor.read_writer_lease")
            self.assertEqual(lease["proof"], "read_writer_lease")

    def test_malformed_supervisor_lease_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            self.assertTrue(acquire_writer_lease(paths, "n1", ".worktrees/n1", "run-1"))
            lease_path = next((paths.root / ".vibe" / "leases").glob("*.json"))
            lease_path.write_bytes(b"\xff")
            lease = read_writer_lease(paths, "n1", ".worktrees/n1")
            self.assertIsNone(lease)

    def test_task_binding_round_trip_keeps_optional_binding_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            binding = TaskBinding(
                provider="codex",
                mode="visible",
                issue_id="V39-CONTRACT",
                role="developer",
                task_id="task-1",
                host="local",
                worktree="/tmp/project/.worktrees/n1",
                branch="codex/n1",
                run_id="run-1",
                binding_intent=self._intent(),
                binding_observation=self._observation(),
                binding_state="binding_verified",
                business_write_allowed=True,
            )
            save_task_binding(paths, binding)
            loaded = load_task_binding(paths, "V39-CONTRACT", "developer")
            self.assertEqual(loaded.binding_state, "blocked_unknown")
            self.assertFalse(loaded.business_write_allowed)
            self.assertEqual(loaded.binding_observation["cursor"], "cursor-1:24")

    def test_task_binding_from_dict_semantically_validates_nested_binding_objects(self):
        binding = TaskBinding(
            provider="codex",
            mode="visible",
            issue_id="V39-CONTRACT",
            role="developer",
            task_id="task-1",
            host="local",
            worktree="/tmp/project/.worktrees/n1",
            branch="codex/n1",
            run_id="run-1",
            binding_intent=self._intent().to_dict(),
            binding_observation=self._observation().to_dict(),
        )
        payload = binding.to_dict()
        payload["binding_intent"]["base_sha"] = "not-a-sha"
        with self.assertRaises(ValueError):
            TaskBinding.from_dict(payload)

    def test_verified_task_binding_requires_complete_nested_binding_evidence(self):
        with self.assertRaises(ValueError):
            TaskBinding(
                provider="codex",
                mode="visible",
                issue_id="V39-CONTRACT",
                role="developer",
                task_id="task-1",
                host="local",
                worktree="/tmp/project/.worktrees/n1",
                branch="codex/n1",
                binding_state="binding_verified",
                business_write_allowed=True,
            )

    def test_legacy_verified_task_binding_downgrades_without_nested_evidence(self):
        payload = TaskBinding(
            provider="codex", mode="visible", issue_id="V39-CONTRACT",
            role="developer", task_id="task-1", host="local",
            worktree="/tmp/project/.worktrees/n1", branch="codex/n1",
        ).to_dict()
        payload.update({"binding_state": "binding_verified", "business_write_allowed": True})
        loaded = TaskBinding.from_dict(payload)
        self.assertEqual(loaded.binding_state, "blocked_unknown")
        self.assertFalse(loaded.business_write_allowed)

    def test_run_snapshot_round_trip_keeps_optional_binding_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
            node = DAGNode("n1", "n1", [], [], None, {"files": []}, "running")
            record = authorize(
                build_authorization_card(
                    plan,
                    [node],
                    AgentCapabilities("fake", True, True, True, True, True, "full"),
                ),
                "AUTHORIZE",
            )
            append_event(
                paths,
                RunEvent(
                    "run_started",
                    {
                        "run_id": "run-1",
                        "authorization_digest": record.digest,
                        "node_contract_digest": record.node_contract_digest,
                        "node_ids": ["n1"],
                    },
                ),
            )
            snapshot = RunSnapshot(
                "run-1", "plan-1", 1, "running", {"n1": {"status": "running"}}, {},
                authorization=record.to_dict(),
                authorization_digest=record.digest,
                node_contract_digest=record.node_contract_digest,
                event_sequence=1,
                binding_intent=self._intent(),
                binding_observation=self._observation(),
                binding_state="binding_verified",
                business_write_allowed=True,
            )
            save_snapshot(paths, snapshot)
            loaded = load_snapshot(paths, "run-1")
            self.assertEqual(loaded.binding_state, "blocked_unknown")
            self.assertFalse(loaded.business_write_allowed)
            self.assertEqual(loaded.binding_intent["task_id"], "task-1")

    def test_run_snapshot_from_dict_semantically_validates_nested_binding_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
            node = DAGNode("n1", "n1", [], [], None, {"files": []}, "running")
            record = authorize(
                build_authorization_card(
                    plan,
                    [node],
                    AgentCapabilities("fake", True, True, True, True, True, "full"),
                ),
                "AUTHORIZE",
            )
            append_event(
                paths,
                RunEvent(
                    "run_started",
                    {
                        "run_id": "run-1",
                        "authorization_digest": record.digest,
                        "node_contract_digest": record.node_contract_digest,
                        "node_ids": ["n1"],
                    },
                ),
            )
            snapshot = RunSnapshot(
                "run-1", "plan-1", 1, "running", {"n1": {"status": "running"}}, {},
                authorization=record.to_dict(),
                authorization_digest=record.digest,
                node_contract_digest=record.node_contract_digest,
                event_sequence=1,
                binding_intent=self._intent().to_dict(),
                binding_observation=self._observation().to_dict(),
            )
            payload = snapshot.to_dict()
            payload["binding_observation"]["head_sha"] = "invalid"
            with self.assertRaises(ValueError):
                RunSnapshot.from_dict(payload)

    def test_verified_run_snapshot_requires_complete_nested_binding_evidence(self):
        with self.assertRaises(ValueError):
            RunSnapshot(
                "run-1",
                "plan-1",
                1,
                "running",
                {"n1": {"status": "running"}},
                {},
                binding_state="binding_verified",
                business_write_allowed=True,
            )

    def test_legacy_verified_run_snapshot_downgrades_without_nested_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
            record = authorize(
                build_authorization_card(
                    plan,
                    [DAGNode("n1", "n1", [], [], None, {"files": []}, "running")],
                    AgentCapabilities("fake", True, True, True, True, True, "full"),
                ),
                "AUTHORIZE",
            )
            payload = RunSnapshot(
                "run-1", "plan-1", 1, "running", {}, {},
                authorization=record.to_dict(),
                authorization_digest=record.digest,
                node_contract_digest=record.node_contract_digest,
            ).to_dict()
        payload.pop("binding_intent", None)
        payload.pop("binding_observation", None)
        payload.update({"binding_state": "binding_verified", "business_write_allowed": True})
        loaded = RunSnapshot.from_dict(payload)
        self.assertEqual(loaded.binding_state, "blocked_unknown")
        self.assertFalse(loaded.business_write_allowed)


if __name__ == "__main__":
    unittest.main()
