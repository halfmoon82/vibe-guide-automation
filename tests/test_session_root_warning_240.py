"""ISSUE-240: the session-root advisory must reach the monitor's wait result.

#219 made ``vibe worker-deliver`` warn when the worker session reports a root
outside the project and its node worktree, but that warning only ever reached
the worker's own stdout: the monitor's ``wait`` result was built from the
payload alone, so a worker that did not pass the note on left the supervisor
looking at a completely normal delivery.

The fix is a bypass observation: the store recomputes the advisory at hand-off
and attaches it to the wait result *beside* the delivery.  It never enters the
payload copy, so ``idempotent_key`` -- the payload digest that decides
duplicate vs. new -- stays bit-for-bit what it was before the field existed,
and the delivery gate (which reads only ``delivery_evidence``) cannot see it.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.adapters.task_provider import (
    ProviderActionStore,
    _canonical_digest,
    _requested_project_mount,
)
from vibe_guide.paths import ProjectPaths

GOOD = {
    "delivery_evidence": {
        "completion_marker": "DELIVERY_COMPLETE",
        "delivery_path": ".vibe/runs/run-1/n1/delivery.md",
        "thread_status": "completed",
    },
}


_ABSENT = object()


def _payload(session_cwd=_ABSENT, **extra):
    delivery = dict(GOOD["delivery_evidence"])
    if session_cwd is not _ABSENT:
        delivery["session_cwd"] = session_cwd
    payload = {"delivery_evidence": delivery}
    payload.update(extra)
    return payload


class _WaitFixture(unittest.TestCase):
    """A real git repo + worktree: the landing check reads git facts.

    Kept as a fixture base so both the wait-result tests and the supervisor
    visibility tests build the same sandbox without one inheriting (and
    silently re-running) the other's cases.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()

        def git(*args, cwd=self.root):
            subprocess.run(["git", "-C", str(cwd)] + list(args), check=True,
                           capture_output=True)

        self.git = git
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        (self.root / "app.py").write_text("v1\n", encoding="utf-8")
        (self.root / ".gitignore").write_text(".vibe/\n.worktrees/\n", encoding="utf-8")
        git("add", "app.py", ".gitignore")
        git("commit", "-q", "-m", "init")
        git("worktree", "add", "-q", "-b", "node/n1", ".worktrees/n1")
        # A live run always has state.json; the entry gate itself is covered
        # elsewhere, so it is stubbed to let the waits through.
        (self.root / ".vibe").mkdir()
        (self.root / ".vibe" / "state.json").write_text("{}", encoding="utf-8")
        gate = patch("vibe_guide.adapters.task_provider.require_entry")
        gate.start()
        self.addCleanup(gate.stop)
        self.store = ProviderActionStore(ProjectPaths(self.root))
        self._dispatch()

    def _dispatch(self):
        request = {
            "child_binding": {
                "worktree": ".worktrees/n1", "branch": "node/n1",
                "allowlist": ["app.py"],
            },
            "target": {"type": "project", "projectId": "project-1"},
        }
        action_id = "action-create-run-1-n1-developer-g1"
        (self.store._directory("requests") / (action_id + ".json")).write_text(json.dumps({
            "schema_version": 1, "action_id": action_id, "operation": "create",
            "provider": "workbuddy", "run_id": "run-1", "issue_id": "n1",
            "role": "developer", "generation": 1, "sequence": 0, "native_tool": "x",
            "request": request,
            "request_digest": "0" * 64,
        }), encoding="utf-8")
        self.store._record_dispatch_head(
            "run-1", "n1", "developer", 1, request["child_binding"],
            _requested_project_mount(request, "create"),
        )

    def _wait(self, sequence=1):
        return self.store.request(
            operation="wait", provider="codex", run_id="run-1", issue_id="n1",
            role="developer", generation=1, sequence=sequence,
            native_tool="codex_app__wait_threads",
            request={"threadId": "t-1", "timeoutMs": 0},
        )

    def _deliver(self, payload):
        return self.store.record_worker_delivery("run-1", "n1", "developer", payload, 1)

    def _stray_root(self):
        stray = Path(self.tmp.name) / "Codex" / "2026-10-15" / "m-en-00"
        stray.mkdir(parents=True)
        return stray

    def _archived(self):
        return list((self.root / ".vibe" / "provider-actions" / "deliveries").glob("*.json"))


class SessionRootWarningWaitTests(_WaitFixture):
    """The warning rides beside the delivery, never inside it."""

    # --- the warning rides beside the delivery -----------------------------

    def test_wait_result_carries_the_session_root_warning(self):
        action = self._wait()
        payload = _payload(session_cwd=str(self._stray_root()))
        self._deliver(payload)
        result = self.store.result(action["action_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["event"], "delivered")
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("very likely projectless", result["warnings"][0])
        # Delivery is registered exactly as before, and the warning is not in
        # the payload the monitor gates on.
        self.assertEqual(result["delivery_evidence"], payload["delivery_evidence"])
        self.assertNotIn("warnings", payload)
        self.assertEqual(len(self._archived()), 1)

    def test_warning_never_becomes_part_of_the_payload_digest(self):
        # idempotent_key is the payload digest.  A warned delivery must keep
        # the exact digest its payload hashed to *before* the delivery ran --
        # hashing the caller's dict afterwards would also pass if the store
        # wrote the warning into the payload and digested that.
        action = self._wait()
        payload = _payload(session_cwd=str(self._stray_root()))
        snapshot = json.loads(json.dumps(payload))
        expected = _canonical_digest(snapshot)
        self._deliver(payload)
        result = self.store.result(action["action_id"])
        self.assertTrue(result["warnings"])
        self.assertEqual(result["idempotent_key"], expected)
        # The delivery must not mutate the caller's payload, and the warning
        # must never be written into it.
        self.assertEqual(payload, snapshot)
        self.assertNotIn("warnings", payload)

    def test_archived_report_is_picked_up_with_the_warning(self):
        # The worker reports before the monitor's wait exists: the archive is
        # what the next wait takes, so the warning must ride that path too.
        payload = _payload(session_cwd=str(self._stray_root()))
        self.assertFalse(self._deliver(payload)["consumed"])
        self.assertEqual(self.store.unconsumed_deliveries("run-1"), ["n1"])
        action = self._wait()
        result = self.store.result(action["action_id"])
        self.assertIn("very likely projectless", result["warnings"][0])
        self.assertEqual(self.store.unconsumed_deliveries("run-1"), [])

    def test_duplicate_report_keeps_the_warning_and_the_dedupe(self):
        action = self._wait()
        payload = _payload(session_cwd=str(self._stray_root()))
        self.assertTrue(self._deliver(payload)["consumed"])
        again = self._deliver(payload)
        self.assertTrue(again["duplicate"])
        self.assertFalse(again["consumed"])
        self.assertTrue(again["warnings"])
        results = list(
            (self.root / ".vibe" / "provider-actions" / "results").glob("action-*.json")
        )
        self.assertEqual(len(results), 1, "no second result for a duplicate")
        result = self.store.result(action["action_id"])
        self.assertIn("very likely projectless", result["warnings"][0])

    # --- silent cases: the #219 boundary must not regress ------------------

    def test_session_root_inside_the_project_is_not_warned(self):
        action = self._wait()
        self._deliver(_payload(session_cwd=str(self.root)))
        result = self.store.result(action["action_id"])
        self.assertNotIn("warnings", result)
        self.assertEqual(result["event"], "delivered")

    def test_session_root_inside_the_node_worktree_is_not_warned(self):
        action = self._wait()
        self._deliver(_payload(session_cwd=str(self.root / ".worktrees" / "n1")))
        self.assertNotIn("warnings", self.store.result(action["action_id"]))

    def test_missing_session_root_is_an_old_template_not_a_warning(self):
        # Old deliveries and old worker prompts carry no field at all.
        action = self._wait()
        self._deliver(_payload())
        result = self.store.result(action["action_id"])
        self.assertNotIn("warnings", result)
        self.assertEqual(result["event"], "delivered")
        self.assertEqual(len(self._archived()), 1)

    def test_unreadable_session_roots_are_treated_as_not_reported(self):
        # #219's boundary: a value that cannot be read as an absolute path is
        # "not reported", never a refusal and never a warning.
        raws = (7, None, "", "   ", {"path": "/tmp"}, "~nosuchuser12345/x",
                "\x00", ".", "relative/path")
        for sequence, raw in enumerate(raws, start=1):
            with self.subTest(session_cwd=raw):
                action = self._wait(sequence=sequence)
                outcome = self._deliver(_payload(session_cwd=raw))
                self.assertTrue(outcome["recorded"])
                result = self.store.result(action["action_id"])
                self.assertIsNotNone(result, "delivery must still be registered")
                self.assertNotIn("warnings", result)

    # --- the observation cannot be forged or escalated --------------------

    def test_worker_cannot_forge_a_warning_through_the_payload(self):
        # The key is recomputed, never copied: a payload that writes its own
        # "warnings" must not reach the monitor's observation.
        action = self._wait()
        self._deliver(_payload(warnings=["forged advisory"]))
        result = self.store.result(action["action_id"])
        self.assertNotIn("warnings", result)
        self.assertEqual(result["event"], "delivered")

    def test_unreadable_check_never_fails_the_wait(self):
        # The advisory now runs on the wait-creation path too, so it must be
        # total: an unreadable record is "not reported", never a failed wait
        # and never a refused delivery.
        payload = _payload(session_cwd=str(self._stray_root()))
        self._deliver(payload)  # archived first; the wait below takes it
        with patch.object(
            self.store, "_session_root_warning", side_effect=ValueError("torn record")
        ):
            action = self._wait()
            result = self.store.result(action["action_id"])
        self.assertEqual(result["event"], "delivered")
        self.assertNotIn("warnings", result)

    def test_programming_errors_are_not_swallowed(self):
        # The wrapper is total only over failures with a real source.  A
        # TypeError means the check itself is broken (a changed signature, a
        # wrong argument type), so it must surface instead of turning the
        # advisory silently blind.
        payload = _payload(session_cwd=str(self._stray_root()))
        with patch.object(
            self.store, "_session_root_warning",
            side_effect=TypeError("programming error"),
        ):
            with self.assertRaises(TypeError):
                self._deliver(payload)


class WaitResultForwardingTests(unittest.TestCase):
    """The runner forwards the observation into the monitor's event data."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".vibe").mkdir()
        (self.root / ".vibe" / "state.json").write_text("{}", encoding="utf-8")
        gate = patch("vibe_guide.adapters.task_provider.require_entry")
        gate.start()
        self.addCleanup(gate.stop)
        from vibe_guide.runners.provider_action import ProviderActionRunner

        self.runner = ProviderActionRunner(
            ProjectPaths(self.root), "codex", "codex-app-visible"
        )
        self.handle = type("Handle", (), {"run_id": "h1"})()
        self.metadata = {"node_id": "n1", "role": "developer", "run_id": "run-1"}
        self.runner.store._atomic(self.runner._handle_path("h1"), self.metadata)

    def _events(self, result):
        return self.runner._wait_result(
            self.handle, dict(self.metadata), {"node_id": "n1"}, result
        )

    def test_warning_is_forwarded_as_event_data(self):
        result = {
            "status": "completed", "event": "delivered",
            "delivery_evidence": dict(GOOD["delivery_evidence"]),
            "warnings": ["session root is outside the project"],
        }
        events = self._events(result)
        self.assertEqual([event.event for event in events], ["delivered"])
        self.assertEqual(
            events[0].data["warnings"], ["session root is outside the project"]
        )
        # The gate still reads the delivery evidence, unchanged.
        self.assertEqual(events[0].data["delivery_evidence"], GOOD["delivery_evidence"])

    def test_absent_warning_leaves_the_event_shape_untouched(self):
        result = {
            "status": "completed", "event": "delivered",
            "delivery_evidence": dict(GOOD["delivery_evidence"]),
        }
        events = self._events(result)
        self.assertEqual([event.event for event in events], ["delivered"])
        self.assertNotIn("warnings", events[0].data)

    def test_malformed_warning_is_dropped_not_fatal(self):
        for value in ("text", {"note": "x"}, [None, 7, ""], [], [""]):
            with self.subTest(warnings=value):
                result = {
                    "status": "completed", "event": "delivered",
                    "delivery_evidence": dict(GOOD["delivery_evidence"]),
                    "warnings": value,
                }
                events = self._events(result)
                self.assertEqual([event.event for event in events], ["delivered"])
                self.assertNotIn("warnings", events[0].data)

    def test_oversized_warning_is_bounded(self):
        result = {
            "status": "completed", "event": "delivered",
            "delivery_evidence": dict(GOOD["delivery_evidence"]),
            "warnings": ["x" * 5000] + ["note{}".format(i) for i in range(10)],
        }
        notes = self._events(result)[0].data["warnings"]
        self.assertEqual(len(notes), 5)
        self.assertTrue(all(len(note) <= 2000 for note in notes))


class WarningPersistenceTests(unittest.TestCase):
    """The warning must survive the event boundary, not just the wait result.

    #240's first hop attached the advisory to ``RunEvent.data`` and stopped
    there.  ``state._sanitize_event_data`` keeps only whitelisted keys, so the
    warning was dropped on the way to events.jsonl: the supervisor's next
    preflight read a run in which the delivery looked completely normal.  The
    integration review caught it because no test asserted the round trip.
    """

    def test_warning_is_whitelisted_for_durable_event_data(self):
        from vibe_guide.state import _EVENT_DATA_KEYS, _sanitize_event_data

        self.assertIn("warnings", _EVENT_DATA_KEYS)

    def test_warning_survives_sanitization_verbatim(self):
        from vibe_guide.state import _sanitize_event_data

        warning = "会话根 /tmp/Codex/2026-10-15/m-en-00 不在项目树内，very likely projectless"
        data = _sanitize_event_data({
            "run_id": "run-1", "node_id": "n1", "warnings": [warning],
        })
        # The observation survives bit for bit ...
        self.assertEqual(data["warnings"], [warning])
        # ... while provider free text next to it stays redacted.
        self.assertEqual(
            _sanitize_event_data({"run_id": "r", "reason": "boom"})["reason"],
            "[REDACTED_PROVIDER_TEXT]",
        )


class SupervisorVisibilityTests(_WaitFixture):
    """The supervisor's own view of the warning (the contract's output ②)."""

    def test_mailbox_state_surfaces_the_warning_to_the_supervisor(self):
        from vibe_guide.supervisor import _mailbox_state

        action = self._wait()
        payload = _payload(session_cwd=str(self._stray_root()))
        self.assertTrue(self._deliver(payload)["consumed"])
        result = self.store.result(action["action_id"])
        self.assertTrue(result["warnings"])
        state = _mailbox_state(ProjectPaths(self.root), "run-1", {})
        self.assertEqual(state["state"], "work")
        self.assertIn("warnings", state)
        self.assertEqual(
            state["warnings"],
            [{"node_id": "n1", "warnings": result["warnings"]}],
        )

    def test_mailbox_state_without_a_warning_says_nothing_about_it(self):
        from vibe_guide.supervisor import _mailbox_state

        action = self._wait()
        self._deliver(_payload(session_cwd=str(self.root / ".worktrees" / "n1")))
        result = self.store.result(action["action_id"])
        self.assertNotIn("warnings", result)
        state = _mailbox_state(ProjectPaths(self.root), "run-1", {})
        self.assertNotIn("warnings", state)


if __name__ == "__main__":
    unittest.main()
