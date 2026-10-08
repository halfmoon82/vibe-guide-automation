"""Window-session dispatch and mailbox servicing.

These tests exist because the failure they replace was silent: a background
agent job hangs with zero output, forever, and looks exactly like a slow job.
Every gate here is fail-closed — where a window or a credential is missing the
answer is a refusal, never a fabricated success.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from vibe_guide.mcp_servers import workbuddy_sessions as module


def _session_file(directory: Path, name: str, payload: dict) -> None:
    directory.joinpath(name).write_text(json.dumps(payload), encoding="utf-8")


def _window(directory: Path, name: str, sid: str, cwd: str, alive: bool = True) -> None:
    """Write one session file.  Liveness is real, so it must be our own pid.

    A stale session file is modelled by dropping the pid entirely: inventing a
    pid that "should" be dead is a trap, because some low pid on the machine
    running the test will exist and make a dead window look alive.
    """
    payload = {
        "sessionId": sid,
        "cwd": cwd,
        "kind": "interactive",
        "endpoint": "http://127.0.0.1:1",
    }
    if alive:
        payload["pid"] = os.getpid()
    _session_file(directory, name, payload)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_live_interactive_windows_with_an_endpoint_are_offered(self):
        _window(self.root, "1.json", "aaaaaaaa-0000-4000-8000-000000000001", "/repo", alive=True)
        # prewarm has no endpoint and no real session id
        _session_file(
            self.root, "2.json",
            {"pid": os.getpid(), "kind": "prewarm", "sessionId": "prewarm-x"},
        )
        # interactive but no endpoint: nothing to talk to
        _session_file(
            self.root, "3.json",
            {"pid": os.getpid(), "kind": "interactive", "sessionId": "cccccccc-0000-4000-8000-000000000003"},
        )
        # stale file: the pid is gone from disk and from the process table
        _window(self.root, "4.json", "dddddddd-0000-4000-8000-000000000004", "/repo", alive=False)
        found = module.discover_sessions(root=self.root)
        self.assertEqual([item["session_id"] for item in found], ["aaaaaaaa-0000-4000-8000-000000000001"])

    def test_a_placeholder_session_id_is_not_a_dispatch_target(self):
        # The host-CLI session file says `kind: interactive` and carries an
        # endpoint, but its id is `interactive-<pid>`, which the gateway
        # answers SESSION_NOT_FOUND for.  It also sorts first by pid, so it is
        # precisely what an auto-pick would grab if it were allowed through.
        _session_file(
            self.root,
            "1.json",
            {"pid": os.getpid(), "kind": "interactive", "sessionId": "interactive-9851",
             "cwd": "/repo", "endpoint": "http://127.0.0.1:1"},
        )
        _window(self.root, "2.json", "bbbbbbbb-0000-4000-8000-000000000002", "/repo")
        found = module.discover_sessions(root=self.root)
        self.assertEqual([item["session_id"] for item in found], ["bbbbbbbb-0000-4000-8000-000000000002"])

    def test_a_directory_that_does_not_exist_is_not_an_error(self):
        self.assertEqual(module.discover_sessions(root=self.root / "nope"), [])

    def test_excluded_ids_never_come_back(self):
        _window(self.root, "1.json", "eeeeeeee-0000-4000-8000-000000000005", "/repo")
        _window(self.root, "2.json", "ffffffff-0000-4000-8000-000000000006", "/repo")
        found = module.discover_sessions(root=self.root, exclude=["eeeeeeee-0000-4000-8000-000000000005"])
        self.assertEqual([item["session_id"] for item in found], ["ffffffff-0000-4000-8000-000000000006"])


class PickSessionTests(unittest.TestCase):
    def _sessions(self):
        return [
            {"pid": 1, "session_id": "77777777-7777-4777-8777-777777777777", "cwd": "/other", "endpoint": "e1", "alive": True},
            {"pid": 2, "session_id": "88888888-8888-4888-8888-888888888888", "cwd": "/repo", "endpoint": "e2", "alive": True},
            {"pid": 3, "session_id": "99999999-9999-4999-8999-999999999999", "cwd": "/repo", "endpoint": "e3", "alive": True},
        ]

    def test_a_window_already_open_on_the_project_wins(self):
        picked = module.pick_session(self._sessions(), cwd="/repo")
        self.assertEqual(picked["session_id"], "88888888-8888-4888-8888-888888888888")

    def test_an_explicit_session_id_wins_over_cwd(self):
        picked = module.pick_session(self._sessions(), session_id="77777777-7777-4777-8777-777777777777", cwd="/repo")
        self.assertEqual(picked["session_id"], "77777777-7777-4777-8777-777777777777")

    def test_an_explicit_id_that_is_not_live_is_refused(self):
        self.assertIsNone(module.pick_session(self._sessions(), session_id="gone"))

    def test_the_orchestrator_can_exclude_itself(self):
        # Two windows sit on the project; the orchestrator excludes its own
        # (the first), so the task lands in the other one — never in a window
        # open on some unrelated directory.
        picked = module.pick_session(
            self._sessions(),
            cwd="/repo",
            exclude=["88888888-8888-4888-8888-888888888888"],
        )
        self.assertEqual(picked["session_id"], "99999999-9999-4999-8999-999999999999")

    def test_excluding_every_window_leaves_nothing_to_dispatch_into(self):
        self.assertIsNone(
            module.pick_session(self._sessions(), exclude=["77777777-7777-4777-8777-777777777777", "88888888-8888-4888-8888-888888888888", "99999999-9999-4999-8999-999999999999"])
        )

    def test_a_cwd_miss_is_refused_not_redirected(self):
        # A window open on /other must never receive a task meant for /repo:
        # the worker would run against the wrong repository.  A miss is a
        # refusal, not a fallback to an arbitrary window.
        self.assertIsNone(module.pick_session(self._sessions(), cwd="/nowhere"))


class ReplyAfterTests(unittest.TestCase):
    """`reply_after` binds a reply to the turn that produced it."""

    class _Stub(module.SessionDispatch):
        def __init__(self, requests):
            super().__init__(token="t", sessions=[])
            self._requests = requests

        def history(self, session_id):
            return {
                "session_id": session_id,
                "requests": self._requests,
                "count": len(self._requests),
            }

    def test_reply_after_skips_earlier_turns(self):
        dispatch = self._Stub(
            [
                {"userInput": "old", "finalReply": "OLD"},
                {"userInput": "new", "finalReply": "NEW"},
            ]
        )
        self.assertEqual(dispatch.reply_after("s", 1), "NEW")
        self.assertIsNone(dispatch.reply_after("s", 2))

    def test_reply_after_does_not_leak_the_previous_turn(self):
        # baseline points at a turn that has not answered yet; the earlier
        # reply must not be handed back as this turn's output.
        dispatch = self._Stub(
            [
                {"userInput": "old", "finalReply": "OLD"},
                {"userInput": "pending"},
            ]
        )
        self.assertIsNone(dispatch.reply_after("s", 1))


class GatewayTokenTests(unittest.TestCase):
    #: The host injects the real password into everything it spawns, including
    #: the interpreter running these tests.  Every case here is about the
    #: fallback chain, so it has to start from a clean environment.
    _SCRUB = (module.TOKEN_ENV, "CODEBUDDY_GATEWAY_PASSWORD")

    def setUp(self):
        self.saved = {name: os.environ.get(name) for name in self._SCRUB}
        for name in self._SCRUB:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is not None:
                os.environ[name] = value

    def test_the_documented_override_wins_without_touching_ps(self):
        def explode():
            raise AssertionError("must not scan processes")

        original = os.environ.get(module.TOKEN_ENV)
        os.environ[module.TOKEN_ENV] = "from-env"
        try:
            self.assertEqual(module.gateway_token(scanner=explode), "from-env")
        finally:
            if original is None:
                del os.environ[module.TOKEN_ENV]
            else:
                os.environ[module.TOKEN_ENV] = original

    def test_a_scan_that_finds_nothing_is_a_refusal_not_a_guess(self):
        with self.assertRaises(module.SessionError):
            module.gateway_token(scanner=lambda: "nothing here")

    def test_the_password_is_read_out_of_the_environment_block(self):
        token = module.gateway_token(
            scanner=lambda: "junk CODEBUDDY_GATEWAY_PASSWORD=abc123 tail"
        )
        self.assertEqual(token, "abc123")


class FakeDispatch:
    """Enough of SessionDispatch for the servicer; no network, no processes."""

    def __init__(self, sessions, replies=None, accept=True, refuse=None):
        self._sessions = sessions
        self._replies = replies or {}
        #: Whether a window accepts a delivered turn.  A busy window answers
        #: ``delivered: false`` and the servicer must refuse, not fake success.
        self.accept = accept
        #: Session ids that individually refuse (a busy window), so a sweep can
        #: be tested falling through to the next one.
        self.refuse = set(refuse or ())
        self.delivered = []
        self.handles = {}

    def sessions(self, refresh=False):
        return [dict(item) for item in self._sessions]

    def history(self, session_id):
        count = self._replies.get(session_id, {}).get("count", 0)
        return {"session_id": session_id, "requests": [{}] * count, "count": count}

    def latest_reply(self, session_id):
        return self._replies.get(session_id, {}).get("reply")

    def reply_after(self, session_id, start_index):
        return self._replies.get(session_id, {}).get("reply")

    def deliver(self, session_id, text):
        self.delivered.append((session_id, text))
        ok = self.accept and session_id not in self.refuse
        return {"delivered": ok, "session_id": session_id}

    def save_handle(self, record):
        self.handles[record["id"]] = dict(record)

    def load_handle(self, handle_id):
        return self.handles.get(handle_id)


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        self.mailbox = self.project / ".vibe" / "provider-actions"
        (self.mailbox / "requests").mkdir(parents=True)
        (self.mailbox / "results").mkdir(parents=True)
        self.saved = {name: os.environ.get(name) for name in module.PROJECT_DIR_ENVS}
        for name in module.PROJECT_DIR_ENVS:
            os.environ.pop(name, None)

    def tearDown(self):
        self.tmp.cleanup()
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _request(self, action_id, operation, request, digest="d1"):
        payload = {
            "schema_version": 1,
            "action_id": action_id,
            "operation": operation,
            "request": request,
            "request_digest": digest,
            "issue_id": "n1",
            "role": "developer",
        }
        (self.mailbox / "requests" / (action_id + ".json")).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _result(self, action_id):
        path = self.mailbox / "results" / (action_id + ".json")
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def test_create_is_dispatched_into_a_window_and_bound(self):
        dispatch = FakeDispatch(
            [{"pid": 5, "session_id": "11111111-1111-4111-8111-111111111111", "cwd": str(self.project),
              "endpoint": "http://127.0.0.1:1", "alive": True}]
        )
        self._request("action-1", "create", {"prompt": "do the work"})
        outcome = module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        self.assertEqual([item["action_id"] for item in outcome["served"]], ["action-1"])
        self.assertEqual(dispatch.delivered[0][1], "do the work")
        result = self._result("action-1")
        # The digest is what binds a result to its request; a mismatched one is
        # rejected outright by the store.
        self.assertEqual(result["request_digest"], "d1")
        # `task_id` is the unique handle id (the action id), not the shared
        # session id: two nodes in one window must not collide on identity.
        self.assertEqual(
            result["payload"]["binding"],
            {"task_id": "action-1", "host": "http://127.0.0.1:1"},
        )

    def test_create_with_no_window_is_refused_not_faked(self):
        dispatch = FakeDispatch([])
        self._request("action-2", "create", {"prompt": "do the work"})
        outcome = module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        self.assertEqual(outcome["served"], [])
        self.assertIn("no live WorkBuddy window", outcome["skipped"][0]["reason"])
        self.assertIsNone(self._result("action-2"))

    def test_the_orchestrators_own_window_is_never_a_target(self):
        dispatch = FakeDispatch(
            [{"pid": 5, "session_id": "22222222-2222-4222-8222-222222222222", "cwd": str(self.project),
              "endpoint": "http://127.0.0.1:1", "alive": True}]
        )
        self._request("action-3", "create", {"prompt": "x"})
        module.MailboxServicer(dispatch, exclude=["22222222-2222-4222-8222-222222222222"]).serve(
            project_dir=str(self.project)
        )
        self.assertEqual(dispatch.delivered, [])

    def test_locate_is_fail_closed(self):
        dispatch = FakeDispatch(
            [{"pid": 5, "session_id": "33333333-3333-4333-8333-333333333333", "cwd": "/repo",
              "endpoint": "http://127.0.0.1:1", "alive": True}]
        )
        self._request("action-4", "locate", {"threadId": "33333333-3333-4333-8333-333333333333"})
        self._request("action-5", "locate", {"threadId": "44444444-4444-4444-8444-444444444444"})
        module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        self.assertTrue(self._result("action-4")["payload"]["located"])
        self.assertFalse(self._result("action-5")["payload"]["located"])

    def test_visibility_needs_every_target_alive(self):
        dispatch = FakeDispatch(
            [
                {"pid": 5, "session_id": "33333333-3333-4333-8333-333333333333", "cwd": "/repo",
                 "endpoint": "http://127.0.0.1:1", "alive": True},
                {"pid": 6, "session_id": "55555555-5555-4555-8555-555555555555", "cwd": "/repo",
                 "endpoint": "http://127.0.0.1:2", "alive": True},
            ]
        )
        self._request(
            "action-6",
            "visibility",
            {"targets": [{"threadId": "33333333-3333-4333-8333-333333333333"}, {"threadId": "55555555-5555-4555-8555-555555555555"}]},
        )
        self._request(
            "action-7",
            "visibility",
            {"targets": [{"threadId": "33333333-3333-4333-8333-333333333333"}, {"threadId": "66666666-6666-4666-8666-666666666666"}]},
        )
        module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        ok = self._result("action-6")["payload"]
        self.assertTrue(ok["visible"] and ok["direct_enter"])
        bad = self._result("action-7")["payload"]
        self.assertFalse(bad["visible"])
        self.assertFalse(bad["direct_enter"])

    def test_wait_is_left_to_worker_self_report(self):
        # Answering a wait here would fabricate a delivery, which is exactly
        # the kind of false green this project refuses to produce.
        dispatch = FakeDispatch([])
        self._request("action-8", "wait", {"threadId": "x"})
        outcome = module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        self.assertEqual(outcome["served"], [])
        self.assertIsNone(self._result("action-8"))

    def test_an_already_answered_request_is_not_answered_twice(self):
        dispatch = FakeDispatch(
            [{"pid": 5, "session_id": "11111111-1111-4111-8111-111111111111", "cwd": str(self.project),
              "endpoint": "http://127.0.0.1:1", "alive": True}]
        )
        self._request("action-9", "create", {"prompt": "once"})
        servicer = module.MailboxServicer(dispatch)
        servicer.serve(project_dir=str(self.project))
        servicer.serve(project_dir=str(self.project))
        self.assertEqual(len(dispatch.delivered), 1)

    def test_a_window_that_refuses_the_turn_is_not_faked(self):
        # A busy window answers `delivered: false`.  Writing a success here is
        # the false green this module exists to prevent.
        dispatch = FakeDispatch(
            [{"pid": 5, "session_id": "77777777-7777-4777-8777-777777777777",
              "cwd": str(self.project), "endpoint": "http://127.0.0.1:1", "alive": True}],
            accept=False,
        )
        self._request("action-10", "create", {"prompt": "x"})
        outcome = module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        self.assertEqual(outcome["served"], [])
        self.assertIn("accepted the turn", outcome["skipped"][0]["reason"])
        self.assertIsNone(self._result("action-10"))

    def test_several_nodes_fan_out_across_windows(self):
        # Two nodes in one sweep must not pile onto the same window, and their
        # `task_id`s must differ so the monitor cannot confuse their outputs.
        dispatch = FakeDispatch(
            [
                {"pid": 5, "session_id": "77777777-7777-4777-8777-777777777777",
                 "cwd": str(self.project), "endpoint": "http://127.0.0.1:1", "alive": True},
                {"pid": 6, "session_id": "88888888-8888-4888-8888-888888888888",
                 "cwd": str(self.project), "endpoint": "http://127.0.0.1:2", "alive": True},
            ]
        )
        self._request("action-11", "create", {"prompt": "one"})
        self._request("action-12", "create", {"prompt": "two"})
        module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        used = [session_id for session_id, _ in dispatch.delivered]
        self.assertEqual(len(used), 2)
        self.assertNotEqual(used[0], used[1])
        self.assertNotEqual(
            self._result("action-11")["payload"]["binding"]["task_id"],
            self._result("action-12")["payload"]["binding"]["task_id"],
        )

    def test_resume_is_answered_on_the_owning_window(self):
        dispatch = FakeDispatch(
            [{"pid": 5, "session_id": "99999999-9999-4999-8999-999999999999",
              "cwd": str(self.project), "endpoint": "http://127.0.0.1:1", "alive": True}]
        )
        dispatch.save_handle(
            {"id": "action-13", "session_id": "99999999-9999-4999-8999-999999999999"}
        )
        self._request("action-13", "resume", {"threadId": "action-13", "prompt": "keep going"})
        outcome = module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        self.assertEqual([item["action_id"] for item in outcome["served"]], ["action-13"])
        self.assertTrue(self._result("action-13")["payload"]["resumed"])
        self.assertEqual(
            dispatch.delivered[0],
            ("99999999-9999-4999-8999-999999999999", "keep going"),
        )

    def test_resume_on_a_dead_window_is_refused_not_dropped(self):
        # A dropped resume parks the run forever; it must answer `resumed:false`.
        dispatch = FakeDispatch([])
        self._request("action-14", "resume", {"threadId": "action-14", "prompt": "keep going"})
        module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        self.assertFalse(self._result("action-14")["payload"]["resumed"])

    def test_resume_advances_the_handle_baseline(self):
        # After a resume the handle must point past the earlier turn, or a
        # later read returns the previous reply as this turn's output.
        session = "99999999-9999-4999-8999-999999999999"
        dispatch = FakeDispatch(
            [{"pid": 5, "session_id": session, "cwd": str(self.project),
              "endpoint": "http://127.0.0.1:1", "alive": True}],
            replies={session: {"count": 2, "reply": "OLD"}},
        )
        dispatch.save_handle({"id": "action-16", "session_id": session, "baseline": 0})
        self._request("action-16", "resume", {"threadId": "action-16", "prompt": "again"})
        module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        self.assertTrue(self._result("action-16")["payload"]["resumed"])
        self.assertEqual(dispatch.handles["action-16"]["baseline"], 2)

    def test_a_busy_first_window_falls_through_to_the_next(self):
        # A sweep must not collapse onto the first window when it is occupied;
        # it has to try the next live window on the project.
        busy = "77777777-7777-4777-8777-777777777777"
        idle = "88888888-8888-4888-8888-888888888888"
        dispatch = FakeDispatch(
            [
                {"pid": 5, "session_id": busy, "cwd": str(self.project),
                 "endpoint": "http://127.0.0.1:1", "alive": True},
                {"pid": 6, "session_id": idle, "cwd": str(self.project),
                 "endpoint": "http://127.0.0.1:2", "alive": True},
            ],
            refuse={busy},
        )
        self._request("action-17", "create", {"prompt": "work"})
        outcome = module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        self.assertEqual([item["action_id"] for item in outcome["served"]], ["action-17"])
        # The occupied window was tried first, then the free one accepted it.
        self.assertEqual(dispatch.delivered, [(busy, "work"), (idle, "work")])
        self.assertEqual(self._result("action-17")["payload"]["sessionId"], idle)


if __name__ == "__main__":
    unittest.main()
