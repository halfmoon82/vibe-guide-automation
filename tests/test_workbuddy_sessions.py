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

    def __init__(
        self,
        sessions,
        replies=None,
        accept=True,
        refuse=None,
        unknown=None,
        lose_reply=None,
        vanish=None,
    ):
        self._sessions = sessions
        self._replies = replies or {}
        #: Whether a window accepts a delivered turn.  A busy window answers
        #: ``delivered: false`` and the servicer must refuse, not fake success.
        self.accept = accept
        #: Session ids that individually refuse (a busy window), so a sweep can
        #: be tested falling through to the next one.
        self.refuse = set(refuse or ())
        #: Session ids whose reply is unreadable (neither accepted nor refused);
        #: a sweep must fail closed rather than retry those on another window.
        self.unknown = set(unknown or ())
        #: Session ids whose POST dies in transit (timeout, reset).  The turn
        #: may have been queued before the answer was lost, so these are
        #: ``unknown`` too -- never a refusal, never retried.
        self.lose_reply = set(lose_reply or ())
        #: Session ids that are gone by the time we look up their endpoint, so
        #: the POST never leaves.  A plain error, not an unknown outcome.
        self.vanish = set(vanish or ())
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
        if session_id in self.vanish:
            raise module.SessionError("no live window session %s" % session_id)
        if session_id in self.lose_reply:
            raise module.DeliveryUnknown(
                "the reply to window %s was lost in transit" % session_id
            )
        if not self.accept or session_id in self.refuse:
            state = "refused"
        elif session_id in self.unknown:
            state = "unknown"
        else:
            state = "accepted"
        return {
            "delivered": state == "accepted",
            "state": state,
            "session_id": session_id,
        }

    def save_handle(self, record):
        self.handles[record["id"]] = dict(record)

    def load_handle(self, handle_id):
        return self.handles.get(handle_id)


class _VibePaths:
    """The one method ``ProviderActionStore`` needs, pointed at a tmp dir."""

    def __init__(self, vibe_dir):
        self._vibe_dir = Path(vibe_dir)

    def resolve_vibe_path(self, name):
        return self._vibe_dir / name


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        self.mailbox = self.project / ".vibe" / "provider-actions"
        (self.mailbox / "requests").mkdir(parents=True)
        (self.mailbox / "results").mkdir(parents=True)
        self.saved = {
            name: os.environ.get(name)
            for name in tuple(module.PROJECT_DIR_ENVS) + (module.HOST_SESSION_ENV,)
        }
        for name in module.PROJECT_DIR_ENVS:
            os.environ.pop(name, None)
        # Deterministic: the host's own session id is excluded from candidates,
        # and the machine running the suite has a real one set.
        os.environ.pop(module.HOST_SESSION_ENV, None)

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

    def _store(self):
        """The real provider-side mailbox reader, pointed at this tmp project."""
        from vibe_guide.adapters.task_provider import ProviderActionStore

        return ProviderActionStore(_VibePaths(self.project / ".vibe"))

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

    def test_an_unknown_delivery_outcome_is_answered_terminally(self):
        # An unreadable gateway reply must not be treated as a refusal: the
        # turn may already be queued, so rotating would deliver it twice.  It
        # must also not be left pending, or the next sweep delivers it again --
        # the answer is a terminal negative result nobody retries.
        first = "77777777-7777-4777-8777-777777777777"
        second = "88888888-8888-4888-8888-888888888888"
        dispatch = FakeDispatch(
            [
                {"pid": 5, "session_id": first, "cwd": str(self.project),
                 "endpoint": "http://127.0.0.1:1", "alive": True},
                {"pid": 6, "session_id": second, "cwd": str(self.project),
                 "endpoint": "http://127.0.0.1:2", "alive": True},
            ],
            unknown={first},
        )
        self._request("action-18", "create", {"prompt": "work"})
        servicer = module.MailboxServicer(dispatch)
        outcome = servicer.serve(project_dir=str(self.project))
        self.assertEqual(outcome["served"], [])
        # The second window was never tried, so the prompt cannot be doubled.
        self.assertEqual(dispatch.delivered, [(first, "work")])
        self.assertEqual([item["action_id"] for item in outcome["failed"]], ["action-18"])
        result = self._result("action-18")
        self.assertTrue(result["payload"]["failed"])
        # No `binding`: the provider reads that as a failed action, never as a
        # worker it should wait for.
        self.assertNotIn("binding", result["payload"])
        # And read it the way the consumer does, so "not pending" is measured
        # rather than asserted: the store must hand back the payload and must
        # stop listing the request as outstanding.
        store = self._store()
        self.assertIsNotNone(store.result("action-18"))
        self.assertEqual(
            [item["action_id"] for item in store.pending()], []
        )
        # A second sweep must not deliver anything: the request is answered.
        again = servicer.serve(project_dir=str(self.project))
        self.assertEqual(again["served"], [])
        self.assertEqual(again["failed"], [])
        self.assertEqual(dispatch.delivered, [(first, "work")])

    def test_a_crashed_delivery_is_never_delivered_twice(self):
        # A sweep that died between the POST and the result write leaves its
        # marker behind.  The turn may be queued, so the next sweep must fail
        # the action closed instead of sending the same prompt again.
        session = "11111111-1111-4111-8111-111111111111"
        dispatch = FakeDispatch(
            [{"pid": 5, "session_id": session, "cwd": str(self.project),
              "endpoint": "http://127.0.0.1:1", "alive": True}]
        )
        self._request("action-19", "create", {"prompt": "work"})
        marker = self.mailbox / "delivering" / "action-19.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"session_id": session}), encoding="utf-8")
        outcome = module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        self.assertEqual(dispatch.delivered, [])
        self.assertEqual([item["action_id"] for item in outcome["failed"]], ["action-19"])
        self.assertTrue(self._result("action-19")["payload"]["failed"])
        self.assertFalse(marker.exists())

    def test_the_host_conversation_is_never_a_target(self):
        # The supervisor runs inside the very conversation it dispatches for,
        # and that window is a live window on the project.  Picking it would
        # deliver the task back into the supervisor's own thread.
        own = "11111111-1111-4111-8111-111111111111"
        other = "22222222-2222-4222-8222-222222222222"
        os.environ[module.HOST_SESSION_ENV] = own
        dispatch = FakeDispatch(
            [
                {"pid": 5, "session_id": own, "cwd": str(self.project),
                 "endpoint": "http://127.0.0.1:1", "alive": True},
                {"pid": 6, "session_id": other, "cwd": str(self.project),
                 "endpoint": "http://127.0.0.1:2", "alive": True},
            ]
        )
        self._request("action-20", "create", {"prompt": "work"})
        module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        self.assertEqual(dispatch.delivered, [(other, "work")])

    def test_the_host_conversation_alone_means_no_window(self):
        # With only the host's own window live there is nothing to dispatch
        # into, and saying otherwise would put the task back in this thread.
        own = "11111111-1111-4111-8111-111111111111"
        os.environ[module.HOST_SESSION_ENV] = own
        dispatch = FakeDispatch(
            [{"pid": 5, "session_id": own, "cwd": str(self.project),
              "endpoint": "http://127.0.0.1:1", "alive": True}]
        )
        self._request("action-21", "create", {"prompt": "work"})
        outcome = module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        self.assertEqual(dispatch.delivered, [])
        self.assertEqual(outcome["served"], [])
        self.assertEqual([item["action_id"] for item in outcome["skipped"]], ["action-21"])
        self.assertIsNone(self._result("action-21"))

    def test_a_lost_reply_is_answered_terminally_not_retried(self):
        # A POST that times out may still have been queued.  It must reach the
        # same terminal answer as an unreadable body: not rotated to another
        # window, and not left pending for the next sweep to deliver again.
        first = "77777777-7777-4777-8777-777777777777"
        second = "88888888-8888-4888-8888-888888888888"
        dispatch = FakeDispatch(
            [
                {"pid": 5, "session_id": first, "cwd": str(self.project),
                 "endpoint": "http://127.0.0.1:1", "alive": True},
                {"pid": 6, "session_id": second, "cwd": str(self.project),
                 "endpoint": "http://127.0.0.1:2", "alive": True},
            ],
            lose_reply={first},
        )
        self._request("action-23", "create", {"prompt": "work"})
        servicer = module.MailboxServicer(dispatch)
        outcome = servicer.serve(project_dir=str(self.project))
        self.assertEqual(outcome["served"], [])
        self.assertEqual(dispatch.delivered, [(first, "work")])
        self.assertEqual([item["action_id"] for item in outcome["failed"]], ["action-23"])
        self.assertTrue(self._result("action-23")["payload"]["failed"])
        self.assertEqual(servicer.serve(project_dir=str(self.project))["failed"], [])
        self.assertEqual(dispatch.delivered, [(first, "work")])

    def test_a_terminal_answer_that_cannot_be_written_keeps_the_marker(self):
        # The marker is the only thing standing between a failed result write
        # and a second delivery.  Lowering it anyway would leave the next sweep
        # with neither marker nor result, and it would send the prompt again.
        first = "77777777-7777-4777-8777-777777777777"
        second = "88888888-8888-4888-8888-888888888888"
        dispatch = FakeDispatch(
            [
                {"pid": 5, "session_id": first, "cwd": str(self.project),
                 "endpoint": "http://127.0.0.1:1", "alive": True},
                {"pid": 6, "session_id": second, "cwd": str(self.project),
                 "endpoint": "http://127.0.0.1:2", "alive": True},
            ],
            unknown={first},
        )
        self._request("action-24", "create", {"prompt": "work"})
        servicer = module.MailboxServicer(dispatch)
        marker = self.mailbox / "delivering" / "action-24.json"

        def unwritable(*args, **kwargs):
            raise OSError("no space left on device")

        servicer._record = unwritable
        outcome = servicer.serve(project_dir=str(self.project))
        self.assertEqual(dispatch.delivered, [(first, "work")])
        self.assertEqual([item["action_id"] for item in outcome["failed"]], ["action-24"])
        self.assertTrue(marker.exists())
        # Second sweep: the marker still stands, so nothing is delivered again.
        again = servicer.serve(project_dir=str(self.project))
        self.assertEqual(again["served"], [])
        self.assertEqual(dispatch.delivered, [(first, "work")])
        self.assertTrue(marker.exists())

    def test_a_bind_failure_after_delivery_is_not_delivered_again(self):
        # The turn is already queued by the time the handle write fails, and the
        # handle lives on a different filesystem from the marker.  That failure
        # must not lower the marker, or the next sweep sends the same prompt a
        # second time -- the same defect the terminal-write guard closes.
        session = "11111111-1111-4111-8111-111111111111"
        dispatch = FakeDispatch(
            [{"pid": 5, "session_id": session, "cwd": str(self.project),
              "endpoint": "http://127.0.0.1:1", "alive": True}]
        )
        self._request("action-25", "create", {"prompt": "work"})

        def unwritable(record):
            raise OSError("handle root is read-only")

        dispatch.save_handle = unwritable
        servicer = module.MailboxServicer(dispatch)
        first = servicer.serve(project_dir=str(self.project))
        self.assertEqual(dispatch.delivered, [(session, "work")])
        self.assertEqual(first["served"], [])
        marker = self.mailbox / "delivering" / "action-25.json"
        self.assertTrue(marker.exists())
        # Second sweep: the marker stands, so the prompt is not sent again, and
        # the request is answered terminally instead of staying pending.
        second = servicer.serve(project_dir=str(self.project))
        self.assertEqual(dispatch.delivered, [(session, "work")])
        self.assertEqual([item["action_id"] for item in second["failed"]], ["action-25"])
        self.assertTrue(self._result("action-25")["payload"]["failed"])
        self.assertFalse(marker.exists())

    def test_a_window_that_vanishes_before_the_post_may_retry(self):
        # Nothing was sent, so the marker must come down: leaving it up would
        # fail the action terminally over a window that merely went away.
        session = "11111111-1111-4111-8111-111111111111"
        dispatch = FakeDispatch(
            [{"pid": 5, "session_id": session, "cwd": str(self.project),
              "endpoint": "http://127.0.0.1:1", "alive": True}],
            vanish={session},
        )
        self._request("action-26", "create", {"prompt": "work"})
        servicer = module.MailboxServicer(dispatch)
        outcome = servicer.serve(project_dir=str(self.project))
        self.assertEqual([item["action_id"] for item in outcome["skipped"]], ["action-26"])
        self.assertFalse((self.mailbox / "delivering" / "action-26.json").exists())
        self.assertIsNone(self._result("action-26"))

    def test_a_stale_marker_next_to_an_answer_is_cleared(self):
        # Once a result exists the request is settled, so a marker left beside
        # it is litter rather than evidence -- and litter that would otherwise
        # never be collected.
        session = "11111111-1111-4111-8111-111111111111"
        dispatch = FakeDispatch(
            [{"pid": 5, "session_id": session, "cwd": str(self.project),
              "endpoint": "http://127.0.0.1:1", "alive": True}]
        )
        self._request("action-27", "create", {"prompt": "work"})
        answered = {
            "schema_version": 1,
            "action_id": "action-27",
            "request_digest": "d1",
            "payload": {"sessionId": session, "binding": {"task_id": "t"}},
        }
        (self.mailbox / "results" / "action-27.json").write_text(
            json.dumps(answered), encoding="utf-8"
        )
        marker = self.mailbox / "delivering" / "action-27.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")
        outcome = module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        self.assertEqual(outcome["served"], [])
        self.assertEqual(dispatch.delivered, [])
        # The answer was left exactly as it was, and the marker is gone.
        self.assertEqual(self._result("action-27"), answered)
        self.assertFalse(marker.exists())

    def test_a_lost_reply_keeps_the_marker_when_the_answer_cannot_be_written(self):
        # Two guards have to hold together.  A POST whose answer was lost may
        # already be queued, and if the terminal answer cannot be written
        # either, the marker is the only thing left saying "do not resend".
        first = "77777777-7777-4777-8777-777777777777"
        second = "88888888-8888-4888-8888-888888888888"
        dispatch = FakeDispatch(
            [
                {"pid": 5, "session_id": first, "cwd": str(self.project),
                 "endpoint": "http://127.0.0.1:1", "alive": True},
                {"pid": 6, "session_id": second, "cwd": str(self.project),
                 "endpoint": "http://127.0.0.1:2", "alive": True},
            ],
            lose_reply={first},
        )
        self._request("action-28", "create", {"prompt": "work"})
        servicer = module.MailboxServicer(dispatch)
        marker = self.mailbox / "delivering" / "action-28.json"

        def unwritable(*args, **kwargs):
            raise OSError("no space left on device")

        servicer._record = unwritable
        servicer.serve(project_dir=str(self.project))
        self.assertEqual(dispatch.delivered, [(first, "work")])
        self.assertTrue(marker.exists())
        # Nothing may be sent again, on this sweep or the next.
        again = servicer.serve(project_dir=str(self.project))
        self.assertEqual(dispatch.delivered, [(first, "work")])
        self.assertTrue(marker.exists())

    def test_a_refused_turn_leaves_no_marker_behind(self):
        # A refusal is proof nothing was queued, so the marker has to come down.
        # Left up, it would fail a request that never left -- and that request
        # would never be retried, even once a window frees up.
        busy = "77777777-7777-4777-8777-777777777777"
        dispatch = FakeDispatch(
            [{"pid": 5, "session_id": busy, "cwd": str(self.project),
              "endpoint": "http://127.0.0.1:1", "alive": True}],
            refuse={busy},
        )
        self._request("action-29", "create", {"prompt": "work"})
        servicer = module.MailboxServicer(dispatch)
        first = servicer.serve(project_dir=str(self.project))
        self.assertEqual([item["action_id"] for item in first["skipped"]], ["action-29"])
        marker = self.mailbox / "delivering" / "action-29.json"
        self.assertFalse(marker.exists())
        self.assertIsNone(self._result("action-29"))
        # The window frees up: the retry really does happen.
        dispatch.refuse = set()
        second = servicer.serve(project_dir=str(self.project))
        self.assertEqual([item["action_id"] for item in second["served"]], ["action-29"])
        self.assertEqual(dispatch.delivered, [(busy, "work"), (busy, "work")])

    def test_a_candidate_that_vanishes_before_the_post_falls_through(self):
        # The window can go away between the history read and the POST.  Nothing
        # was sent, so the sweep must try the next candidate rather than leave
        # the whole action unanswered for another round.
        dying = "11111111-1111-4111-8111-111111111111"
        healthy = "22222222-2222-4222-8222-222222222222"
        dispatch = FakeDispatch(
            [
                {"pid": 5, "session_id": dying, "cwd": str(self.project),
                 "endpoint": "http://127.0.0.1:1", "alive": True},
                {"pid": 6, "session_id": healthy, "cwd": str(self.project),
                 "endpoint": "http://127.0.0.1:2", "alive": True},
            ],
            vanish={dying},
        )
        self._request("action-30", "create", {"prompt": "work"})
        outcome = module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        self.assertEqual([item["action_id"] for item in outcome["served"]], ["action-30"])
        self.assertEqual(dispatch.delivered, [(dying, "work"), (healthy, "work")])
        self.assertEqual(self._result("action-30")["payload"]["sessionId"], healthy)
        # Nothing was queued on the window that went away, so no marker is left.
        self.assertFalse((self.mailbox / "delivering" / "action-30.json").exists())

    def test_a_window_that_dies_mid_sweep_does_not_abandon_the_action(self):
        # ``history`` failing on one candidate is a reason to try the next one,
        # not to leave the whole action unanswered.
        dying = "11111111-1111-4111-8111-111111111111"
        healthy = "22222222-2222-4222-8222-222222222222"
        dispatch = FakeDispatch(
            [
                {"pid": 5, "session_id": dying, "cwd": str(self.project),
                 "endpoint": "http://127.0.0.1:1", "alive": True},
                {"pid": 6, "session_id": healthy, "cwd": str(self.project),
                 "endpoint": "http://127.0.0.1:2", "alive": True},
            ]
        )
        real_history = dispatch.history

        def history(session_id):
            if session_id == dying:
                raise module.SessionError("no live window session %s" % session_id)
            return real_history(session_id)

        dispatch.history = history
        self._request("action-22", "create", {"prompt": "work"})
        outcome = module.MailboxServicer(dispatch).serve(project_dir=str(self.project))
        self.assertEqual([item["action_id"] for item in outcome["served"]], ["action-22"])
        self.assertEqual(dispatch.delivered, [(healthy, "work")])


class _StubDispatch(module.SessionDispatch):
    """``SessionDispatch`` with the HTTP hop replaced by a canned body."""

    def __init__(self, body, sessions, error=None):
        super().__init__(token="stub-token", sessions=sessions)
        self._body = body
        self._error = error
        self.calls = []

    def sessions(self, refresh=False):
        # ``refresh`` would re-scan the machine's processes; the stub stays on
        # the window list it was handed.
        return [dict(item) for item in self._sessions]

    def _call(self, endpoint, path, method="GET", body=None, timeout=30.0):
        self.calls.append((method, path, body))
        if self._error is not None:
            raise self._error
        return self._body


class DeliverOutcomeTests(unittest.TestCase):
    """The three delivery outcomes, read at the transport boundary.

    The servicer tests above build ``state`` themselves, so on their own they
    would stay green if ``deliver`` went back to collapsing everything into a
    boolean.  These exercise the real mapping.
    """

    SESSION = "11111111-1111-4111-8111-111111111111"

    def _dispatch(self, body, error=None):
        return _StubDispatch(
            body,
            [
                {
                    "pid": 5,
                    "session_id": self.SESSION,
                    "cwd": "/repo",
                    "endpoint": "http://127.0.0.1:1",
                    "alive": True,
                }
            ],
            error=error,
        )

    def test_a_queued_turn_is_accepted(self):
        outcome = self._dispatch({"delivered": True}).deliver(self.SESSION, "work")
        self.assertEqual(outcome["state"], "accepted")
        self.assertIs(outcome["delivered"], True)

    def test_a_busy_window_is_refused(self):
        outcome = self._dispatch({"delivered": False}).deliver(self.SESSION, "work")
        self.assertEqual(outcome["state"], "refused")
        self.assertIs(outcome["delivered"], False)

    def test_an_empty_body_is_unknown_not_refused(self):
        # ``_call`` answers ``{}`` for an empty 2xx body.  Reading that as a
        # refusal is what let the same prompt be delivered to two windows.
        outcome = self._dispatch({}).deliver(self.SESSION, "work")
        self.assertEqual(outcome["state"], "unknown")
        self.assertIsNone(outcome["delivered"])

    def test_a_non_boolean_flag_is_unknown(self):
        outcome = self._dispatch({"delivered": "yes"}).deliver(self.SESSION, "work")
        self.assertEqual(outcome["state"], "unknown")
        self.assertIsNone(outcome["delivered"])

    def test_a_body_that_is_not_an_object_is_unknown(self):
        outcome = self._dispatch(["delivered"]).deliver(self.SESSION, "work")
        self.assertEqual(outcome["state"], "unknown")
        self.assertIsNone(outcome["delivered"])

    def test_a_post_that_dies_in_transit_is_unknown(self):
        # A timeout or a reset connection means the request may have arrived
        # and been queued before its answer was lost.  Reading that as a
        # refusal is what lets the caller rotate and run the task twice.
        dispatch = self._dispatch(
            None,
            error=module.TransportError("POST /reply failed: timed out", reached=True),
        )
        with self.assertRaises(module.DeliveryUnknown):
            dispatch.deliver(self.SESSION, "work")

    def test_a_refused_connection_is_not_a_lost_reply(self):
        # A connection the gateway never accepted means nothing was sent, so
        # the caller may retry later.  Failing the node over a gateway that is
        # not listening would be a false alarm, not fail-closed.
        dispatch = self._dispatch(
            None,
            error=module.TransportError("POST /reply failed: refused", reached=False),
        )
        with self.assertRaises(module.SessionError) as caught:
            dispatch.deliver(self.SESSION, "work")
        self.assertNotIsInstance(caught.exception, module.DeliveryUnknown)

    def test_only_a_refusal_or_a_dead_host_counts_as_never_sent(self):
        self.assertFalse(module._may_have_reached(ConnectionRefusedError()))
        self.assertFalse(module._may_have_reached(module.socket.gaierror()))
        # What urllib actually hands over: the real reason is nested.
        self.assertFalse(
            module._may_have_reached(
                module.urllib.error.URLError(ConnectionRefusedError())
            )
        )
        self.assertTrue(module._may_have_reached(TimeoutError()))
        self.assertTrue(module._may_have_reached(ConnectionResetError()))

    def test_a_window_that_is_gone_is_not_a_lost_reply(self):
        # No endpoint means nothing was sent, so the caller may retry later:
        # this must stay a plain error, not a terminal unknown.
        dispatch = _StubDispatch({}, [])
        with self.assertRaises(module.SessionError) as caught:
            dispatch.deliver(self.SESSION, "work")
        self.assertNotIsInstance(caught.exception, module.DeliveryUnknown)


if __name__ == "__main__":
    unittest.main()
