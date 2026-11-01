"""An in-flight successor action survives an authorization refresh (#362).

`monitor --authorize` rewrites the engine proof and with it the authorization
digest.  The proof of an in-flight create now compares only fields that do not
move with that refresh: the plan revision, the node contract digest and the
request digest.  The authorization digest is not compared, and a changed plan
revision, node contract or request still defeats the proof, so a refresh never
yields a second writer.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vibe_guide.adapters.task_provider import _canonical_digest
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.provider_action import ProviderActionRunner

OLD, NEW = "a" * 64, "b" * 64


class StableFieldProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name))
        self.runner = ProviderActionRunner(self.paths, "codex-app-visible", "codex-app-visible")
        self.monitor = Monitor.__new__(Monitor)
        self.monitor.paths = self.paths
        self.monitor.plan = SimpleNamespace(plan_id="plan-1", version=1)
        self.snapshot = SimpleNamespace(run_id="run-1")
        contract = {"node_id": "integration-review", "role": "reviewer", "generation": 2,
                    "authorization_digest": OLD, "plan_id": "plan-1", "plan_version": 1}
        with patch("vibe_guide.adapters.task_provider.require_entry"):
            self.create = self.runner._action(
                contract, "run-1", "create",
                {"successor": True, "predecessor_task_id": "old-reviewer"})

    def proven(self, authorization_digest, action=None):
        action = action or self.create
        return self.monitor._proven_successor_action(
            self.snapshot, "integration-review", "reviewer", 2, self.runner.provider,
            action["action_id"], action["request_digest"], "old-reviewer", authorization_digest)

    def test_example_2_refreshed_authorization_still_proves_the_create(self):
        self.assertTrue(self.proven(OLD))
        self.assertTrue(self.proven(NEW), "the digest a refresh rewrites is not compared")

    def test_a_changed_plan_revision_still_defeats_the_proof(self):
        self.monitor.plan = SimpleNamespace(plan_id="plan-1", version=2)
        self.assertFalse(self.proven(NEW))

    def test_a_changed_request_still_defeats_the_proof(self):
        path = self.runner.store.root / "requests" / (self.create["action_id"] + ".json")
        changed = json.loads(path.read_bytes())
        changed["request"]["predecessor_task_id"] = "other"
        unsigned = dict(changed)
        unsigned.pop("request_digest")
        changed["request_digest"] = _canonical_digest(unsigned)
        path.write_text(json.dumps(changed))
        self.assertFalse(self.proven(NEW))
        self.assertFalse(self.monitor._proven_successor_action(
            self.snapshot, "integration-review", "reviewer", 2, self.runner.provider,
            self.create["action_id"], "0" * 64, "old-reviewer", NEW))

    def test_a_changed_node_contract_defeats_the_proof(self):
        node = SimpleNamespace(id="integration-review")
        self.monitor.nodes = {"integration-review": node}
        self.snapshot.nodes = {"integration-review": {"contract_digest": "stale-digest"}}
        with patch("vibe_guide.monitor.executable_contract_digest", return_value="live-digest"):
            self.assertFalse(self.proven(NEW))
        self.snapshot.nodes = {"integration-review": {"contract_digest": "live-digest"}}
        with patch("vibe_guide.monitor.executable_contract_digest", return_value="live-digest"):
            self.assertTrue(self.proven(NEW))

    def test_a_different_role_or_generation_is_another_transaction(self):
        self.assertFalse(self.monitor._proven_successor_action(
            self.snapshot, "integration-review", "reviewer", 3, self.runner.provider,
            self.create["action_id"], self.create["request_digest"], "old-reviewer", NEW))
        self.assertFalse(self.monitor._proven_successor_action(
            self.snapshot, "integration-review", "developer", 2, self.runner.provider,
            self.create["action_id"], self.create["request_digest"], "old-reviewer", NEW))


if __name__ == "__main__":
    unittest.main()
