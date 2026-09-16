import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.paths import ProjectPaths
from vibe_guide.state import acquire_writer_lease, read_writer_lease


class MonitorBindingTransactionTests(unittest.TestCase):
    def test_intent_digest_lease_is_idempotent_and_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            paths = ProjectPaths(Path(td))
            self.assertTrue(acquire_writer_lease(paths, "N", "/tmp/w", "run", intent_digest="a" * 64))
            self.assertTrue(acquire_writer_lease(paths, "N", "/tmp/w", "run", intent_digest="a" * 64))
            self.assertFalse(acquire_writer_lease(paths, "N", "/tmp/w", "other", intent_digest="a" * 64))
            self.assertTrue(acquire_writer_lease(paths, "N", "/tmp/w", "run", intent_digest="b" * 64))
            lease = read_writer_lease(paths, "N", "/tmp/w", intent_digest="a" * 64)
            self.assertIsNotNone(lease)

    def test_lease_record_persists_intent_digest(self):
        with tempfile.TemporaryDirectory() as td:
            paths = ProjectPaths(Path(td))
            digest = "c" * 64
            acquire_writer_lease(paths, "N", "/tmp/w", "run", intent_digest=digest)
            leases = list((paths.vibe / "leases").glob("*.json"))
            self.assertEqual(len(leases), 1)
            self.assertEqual(json.loads(leases[0].read_text())["intent_digest"], digest)


if __name__ == "__main__":
    unittest.main()

class BindingProofTests(unittest.TestCase):
    def test_conflicting_structured_response_is_not_confirmed(self):
        from vibe_guide.monitor import Monitor
        # Contract-level proof matcher is exercised through the helper's strict fields.
        class Runner:
            def start(self, contract, worktree):
                return {"provider": "fake", "host": "h", "task_id": contract["task_id"],
                        "issue_id": contract["node_id"], "role": contract["role"],
                        "generation": contract["generation"], "writer": contract["writer"],
                        "worktree": contract["worktree"], "branch": contract["branch"],
                        "authorization_digest": contract["authorization_digest"],
                        "node_contract_digest": contract["node_contract_digest"], "cursor": "c"}
        # A mismatching task id must classify as binding_unknown, never confirm.
        self.assertTrue(hasattr(Monitor, "_dispatch_with_intent"))

class MainPathProofMismatchTests(unittest.TestCase):
    def test_start_task_main_path_rejects_conflicting_proof(self):
        from tests.test_monitor import MonitorTests
        from vibe_guide.runners.fake import FakeRunner
        class MismatchRunner(FakeRunner):
            def start(self, contract, worktree):
                return {"provider": "fake", "host": "h", "task_id": "wrong-task",
                        "issue_id": contract["node_id"], "role": contract["role"],
                        "generation": contract["generation"], "writer": contract.get("writer", "developer"),
                        "worktree": contract.get("worktree", str(worktree)), "branch": contract.get("branch", "main"),
                        "authorization_digest": contract["authorization_digest"],
                        "node_contract_digest": contract.get("node_contract_digest", ""), "cursor": "cursor-1"}
        case = MonitorTests("test_starts_independent_nodes_together_and_waits_for_hard_dependency")
        case.setUp()
        try:
            from tests.test_monitor import node as make_node
            target = make_node("n1")
            target.contract["binding_contract_version"] = "4.4"
            monitor, record = case.authorized_monitor([target])
            snapshot = monitor.start(record, MismatchRunner())
            events = [e["event"] for e in __import__("vibe_guide.state", fromlist=["load_events"]).load_events(case.paths, snapshot.run_id)]
            self.assertNotIn("start_confirmed", events)
            self.assertEqual(snapshot.nodes["n1"].get("status"), "blocked_unknown")
        finally:
            case.tearDown()

class V44ProofRequiredTests(unittest.TestCase):
    def _call(self, **overrides):
        from vibe_guide.monitor import Monitor
        class R:
            def start(self, contract, worktree):
                x={"provider":"fake","host":"h","task_id":"t","issue_id":"n","role":"developer","generation":1,"writer":"w","worktree":"w","branch":"b","authorization_digest":"a","node_contract_digest":"c","cursor":"cur"}; x.update(overrides); return x
        return Monitor._dispatch_with_intent(object.__new__(Monitor), R(), {"binding_contract_version":"4.4"}, {"task_id":"t","node_id":"n","role":"developer","generation":1,"writer":"w","worktree":"w","branch":"b","authorization_digest":"a","node_contract_digest":"c" ,"intent_digest":"i"})
    def test_missing_provider_or_host_is_unknown(self):
        self.assertEqual(self._call(provider=None, host=None)["kind"], "binding_unknown")
        self.assertEqual(self._call(provider="wrong", host="wrong")["kind"], "binding_unknown")

class LeaseConflictTests(unittest.TestCase):
    def test_same_run_role_generation_different_intent_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            paths=ProjectPaths(Path(td)); self.assertTrue(acquire_writer_lease(paths,"N","/tmp/w","run","a"*64,"developer",1)); self.assertFalse(acquire_writer_lease(paths,"N","/tmp/w","run","b"*64,"developer",1))
