"""A Codex worker created by ``fork_thread`` is not a ``create_thread`` result.

Reproduces run-63e7cfc2 (2026-10-26 14:46 onward): after ``create_thread``
with a worktree target returned "invalid arguments", the supervisor moved
itself into the node worktree, called ``fork_thread`` and wrote the fork's
``{threadId, hostId}`` back as the result of the ``create`` action.  Every
worker after that started with the supervisor's ~650k-token history.

``complete()`` must look the thread up in the Codex session record and refuse
a fork: keep the reply as evidence, leave the request pending, and tell the
caller to use ``create_thread`` with the project target.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.adapters.task_provider import (
    ForkedProviderSession,
    ProviderActionStore,
    _canonical_digest,
)
from vibe_guide.paths import ProjectPaths

THREAD = "01a17b54-13d0-7a62-8542-1f92447e7dda"


class ForkedWorkerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        (root / "project").mkdir()
        self.store = ProviderActionStore(ProjectPaths(root / "project"))
        self.codex_home = root / "codex"
        env = patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)})
        env.start()
        self.addCleanup(env.stop)

    def _request(self, operation="create", native_tool="codex_app__create_thread",
                 provider="codex-app-visible"):
        basis = {"operation": operation, "provider": provider, "run_id": "run-1",
                 "issue_id": "n1", "role": "developer", "generation": 1, "sequence": 0}
        action_id = "action-" + _canonical_digest(basis)[:32]
        payload = {"schema_version": 1, "action_id": action_id, **basis,
                   "native_tool": native_tool, "request": {}}
        payload["request_digest"] = _canonical_digest(payload)
        self.store._atomic(self.store._directory("requests") / (action_id + ".json"), payload)
        return action_id

    def _record(self, thread, forked_from=None, archived=False):
        base = self.codex_home / ("archived_sessions" if archived else "sessions/2026/10/27")
        base.mkdir(parents=True, exist_ok=True)
        meta = {"id": thread}
        if forked_from:
            meta["forked_from_id"] = forked_from
        path = base / "rollout-2026-10-27T09-42-40-{}.jsonl".format(thread)
        path.write_text(json.dumps({"type": "session_meta", "payload": meta}) + "\n",
                        encoding="utf-8")

    def _observations(self, action_id):
        return sorted(self.store._directory("observations").glob(action_id + "-forked-*.json"))

    def test_forked_create_result_is_refused(self):
        action_id = self._request()
        self._record(THREAD, forked_from="01a1776d-09ee-73b0-8c6d-b6ea22deac0c")
        payload = {"binding": {"hostId": "local", "threadId": THREAD}}
        with self.assertRaises(ForkedProviderSession) as caught:
            self.store.complete(action_id, payload)
        message = str(caught.exception)
        self.assertIn("fork_thread", message)
        self.assertIn("create_thread", message)
        self.assertIsNone(self.store.result(action_id))
        self.assertEqual(len(self._observations(action_id)), 1)
        record = json.loads(self._observations(action_id)[0].read_text())
        self.assertEqual(record["kind"], "forked_provider_session")
        self.assertEqual(record["forked_from"], "01a1776d-09ee-73b0-8c6d-b6ea22deac0c")

    def test_flat_thread_id_payload_is_checked_too(self):
        action_id = self._request()
        self._record(THREAD, forked_from="x", archived=True)
        with self.assertRaises(ForkedProviderSession):
            self.store.complete(action_id, {"threadId": THREAD, "hostId": "local"})

    def test_fresh_create_result_is_stored(self):
        action_id = self._request()
        self._record(THREAD)
        payload = {"binding": {"hostId": "local", "threadId": THREAD}}
        self.assertTrue(self.store.complete(action_id, payload))
        self.assertEqual(self.store.result(action_id), payload)

    def test_missing_record_is_not_evidence_of_a_fork(self):
        action_id = self._request()
        payload = {"binding": {"hostId": "local", "threadId": THREAD}}
        self.assertTrue(self.store.complete(action_id, payload))

    def test_only_codex_create_is_checked(self):
        self._record(THREAD, forked_from="x")
        wait = self._request(operation="wait", native_tool="codex_app__wait_threads")
        self.assertTrue(self.store.complete(wait, {"threadId": THREAD}))
        other = self._request(native_tool="ccd_session__spawn_task", provider="claude-code-visible")
        self.assertTrue(self.store.complete(other, {"threadId": THREAD}))

    def test_unsafe_thread_id_is_not_globbed(self):
        action_id = self._request()
        self._record(THREAD, forked_from="x")
        self.assertTrue(self.store.complete(action_id, {"threadId": "*"}))


if __name__ == "__main__":
    unittest.main()
