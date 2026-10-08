"""In-session dispatch on WorkBuddy: the host conversation as a worker target.

Three separate things had to hold before a ``visible-sdd`` node could be
dispatched on a WorkBuddy host, and each was measured on the real machine
(macOS, 2026-10-08):

1. The conversation registers a UUID with **no** endpoint; the
   ``codebuddy --serve`` process registers an endpoint under a placeholder id
   the gateway itself answers SESSION_NOT_FOUND for.  Requiring both in one
   file returned *zero* targets, so every dispatch had nowhere to go.
2. The inherited ``CODEBUDDY_GATEWAY_PASSWORD`` belonged to another app
   instance's prewarm pool and every request answered ``401 AUTH_REQUIRED``,
   while the password of the conversation process opened the same endpoint.
3. ``_answer_create`` excluded the host conversation unconditionally.  That is
   right for ``dual-visible``, which needs two windows, and wrong for the
   in-session protocol, which is defined as dev and review subagents inside one
   session identity -- and on this host the host conversation is the only
   target that exists at all.

These tests pin all three, plus the fail-closed edges around them.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from vibe_guide.mcp_servers import workbuddy_sessions as module

HOST = "fb153bad-9882-424b-aca0-92374f5ffac7"
GATEWAY = "http://127.0.0.1:64063"

#: "Use this test process's pid" -- distinct from an explicit ``None``, which
#: is how a session file with no pid at all (a dead window) is written.
_ALIVE_PID = object()


def _session_file(directory: Path, name: str, payload: dict) -> None:
    directory.joinpath(name).write_text(json.dumps(payload), encoding="utf-8")


class EndpointJoinTests(unittest.TestCase):
    """A session and its endpoint live in different registration files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _conversation(self, name="1.json", sid=HOST, cwd="/repo"):
        _session_file(
            self.root,
            name,
            {"pid": os.getpid(), "kind": "interactive", "sessionId": sid, "cwd": cwd},
        )

    def _gateway(self, name="9.json", endpoint=GATEWAY, pid=_ALIVE_PID):
        _session_file(
            self.root,
            name,
            {
                "pid": os.getpid() if pid is _ALIVE_PID else pid,
                "kind": "interactive",
                "sessionId": "interactive-98851",
                "cwd": "/repo",
                "endpoint": endpoint,
                "url": endpoint,
            },
        )

    def test_a_session_without_an_endpoint_inherits_the_gateways(self):
        self._conversation()
        self._gateway()
        found = module.discover_sessions(root=self.root)
        self.assertEqual([item["session_id"] for item in found], [HOST])
        self.assertEqual(found[0]["endpoint"], GATEWAY)

    def test_the_gateway_file_itself_is_never_a_target(self):
        # Its id is a placeholder and the gateway answers SESSION_NOT_FOUND for
        # it, so offering it would turn a dispatch into a 404.
        self._gateway()
        self.assertEqual(module.discover_sessions(root=self.root), [])

    def test_a_dead_gateway_lends_nothing(self):
        self._conversation()
        self._gateway(pid=None)  # no pid on disk means the process is gone
        self.assertEqual(module.discover_sessions(root=self.root), [])

    def test_without_a_gateway_a_session_with_no_endpoint_is_not_offered(self):
        self._conversation()
        self.assertEqual(module.discover_sessions(root=self.root), [])

    def test_a_session_with_its_own_endpoint_keeps_it(self):
        self._conversation()
        _session_file(
            self.root,
            "2.json",
            {
                "pid": os.getpid(),
                "kind": "interactive",
                "sessionId": HOST,
                "cwd": "/repo",
                "endpoint": "http://127.0.0.1:1",
            },
        )
        self._gateway()
        found = module.discover_sessions(root=self.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["endpoint"], "http://127.0.0.1:1")

    def test_one_session_registered_twice_is_offered_once(self):
        # The conversation and its prewarmed pool both claim the same id.
        self._conversation("1.json")
        self._conversation("2.json")
        self._gateway()
        found = module.discover_sessions(root=self.root)
        self.assertEqual([item["session_id"] for item in found], [HOST])


class _FakeDispatch:
    """The slice of ``SessionDispatch`` the create path actually uses."""

    def __init__(self, sessions, refused=()):
        self._sessions = list(sessions)
        self._refused = set(refused)
        self.delivered = []
        self.handles = {}

    def sessions(self, refresh=False):
        return [dict(item) for item in self._sessions]

    def history(self, session_id):
        return {"session_id": session_id, "requests": [], "count": 3}

    def deliver(self, session_id, text):
        self.delivered.append((session_id, text))
        refused = session_id in self._refused
        return {
            "session_id": session_id,
            "state": "refused" if refused else "accepted",
            "delivered": not refused,
        }

    def save_handle(self, record):
        self.handles[record["id"]] = dict(record)

    def load_handle(self, handle_id):
        return self.handles.get(handle_id)


class HostConversationPolicyTests(unittest.TestCase):
    """In-session work may use the host conversation; two-window work may not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"CODEBUDDY_SESSION_ID": HOST})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def _servicer(self, refused=()):
        dispatch = _FakeDispatch(
            [
                {
                    "pid": os.getpid(),
                    "session_id": HOST,
                    "cwd": str(self.root),
                    "endpoint": GATEWAY,
                    "alive": True,
                }
            ],
            refused=refused,
        )
        return module.MailboxServicer(dispatch), dispatch

    def _create(self, servicer, topology):
        request = {"prompt": "do the thing"}
        if topology is not None:
            request["topology"] = topology
        return servicer._answer_create(
            request, {"action_id": "action-1", "role": "developer"}, self.root
        )

    def test_in_session_work_may_dispatch_into_the_host_conversation(self):
        servicer, dispatch = self._servicer()
        payload = self._create(servicer, module.WORKER_TOPOLOGY_VISIBLE_SDD)
        self.assertEqual(payload["sessionId"], HOST)
        self.assertEqual(dispatch.delivered, [(HOST, "do the thing")])
        # `binding` is what provider_action.create reads.
        self.assertEqual(payload["binding"]["task_id"], "action-1")
        self.assertEqual(payload["binding"]["host"], GATEWAY)
        self.assertEqual(dispatch.handles["action-1"]["session_id"], HOST)

    def test_two_window_work_still_refuses_the_host_conversation(self):
        servicer, dispatch = self._servicer()
        with self.assertRaises(module.SessionError):
            self._create(servicer, "dual-visible")
        self.assertEqual(dispatch.delivered, [])

    def test_a_request_that_names_no_topology_still_refuses_it(self):
        # The default must stay the conservative one: an older caller that
        # never heard of the in-session protocol does not get the new target.
        servicer, dispatch = self._servicer()
        with self.assertRaises(module.SessionError):
            self._create(servicer, None)
        self.assertEqual(dispatch.delivered, [])

    def test_a_busy_host_conversation_is_never_reported_as_dispatched(self):
        servicer, dispatch = self._servicer(refused=[HOST])
        with self.assertRaises(module.SessionError):
            self._create(servicer, module.WORKER_TOPOLOGY_VISIBLE_SDD)
        self.assertEqual(dispatch.handles, {})


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self, size=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CredentialRotationTests(unittest.TestCase):
    """A stale inherited password must not be the end of the story."""

    class _Stub(module.SessionDispatch):
        """A dispatch whose one window never touches the real sessions dir."""

        def sessions(self, refresh=False):
            return [
                {
                    "pid": os.getpid(),
                    "session_id": "sid",
                    "endpoint": "http://127.0.0.1:9",
                    "cwd": "/repo",
                    "alive": True,
                }
            ]

    def setUp(self):
        self.env = mock.patch.dict(
            os.environ,
            # Empty rather than absent: this machine really does export a
            # password, and inheriting it would make the assertions below
            # depend on the operator's environment.
            {module.TOKEN_ENV: "stale", "CODEBUDDY_GATEWAY_PASSWORD": ""},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    @staticmethod
    def _urlopen(accepted):
        """Answer 401 for every credential except ``accepted``."""

        def fake(request, timeout=None):
            if request.get_header("Authorization") == "Bearer " + accepted:
                return _FakeResponse(b'{"data":{"delivered":true}}')
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}")
            )

        return fake

    def _patch(self, target, value):
        patcher = mock.patch.object(module, target, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_401_moves_to_the_next_candidate_and_keeps_it(self):
        self._patch("_scanned_tokens", lambda *a, **k: ["good"])
        dispatch = self._Stub()
        with mock.patch.object(module.urllib.request, "urlopen", self._urlopen("good")):
            self.assertEqual(dispatch.deliver("sid", "hello")["state"], "accepted")
            # The winner is remembered: the second call does not retry `stale`.
            self.assertEqual(dispatch._token, "good")
            self.assertEqual(dispatch.deliver("sid", "again")["state"], "accepted")

    def test_the_process_scan_only_happens_after_a_401(self):
        scanner = mock.Mock(return_value=[])
        self._patch("_scanned_tokens", scanner)
        dispatch = self._Stub()
        with mock.patch.object(module.urllib.request, "urlopen", self._urlopen("good")):
            self.assertEqual(dispatch._token, "stale")
            self.assertEqual(scanner.call_count, 0)
            with self.assertRaises(module.SessionError):
                dispatch.history("sid")
        self.assertEqual(scanner.call_count, 1)

    def test_every_candidate_failing_is_an_error_not_a_loop(self):
        self._patch("_scanned_tokens", lambda *a, **k: [])
        dispatch = self._Stub()
        with mock.patch.object(module.urllib.request, "urlopen", self._urlopen("good")):
            with self.assertRaises(module.SessionError):
                dispatch.history("sid")

    def test_an_explicit_token_is_never_second_guessed(self):
        self._patch(
            "_scanned_tokens",
            mock.Mock(side_effect=AssertionError("must not scan processes")),
        )
        dispatch = self._Stub(token="stale")
        with mock.patch.object(module.urllib.request, "urlopen", self._urlopen("good")):
            with self.assertRaises(module.SessionError):
                dispatch.history("sid")

    def test_the_documented_override_still_short_circuits_the_scan(self):
        scanner = mock.Mock(side_effect=AssertionError("must not scan processes"))
        self.assertEqual(module.gateway_token(scanner=scanner), "stale")
        self.assertEqual(module.gateway_tokens(scanner=scanner), ["stale"])

    def test_every_distinct_scanned_password_is_a_candidate(self):
        text = (
            "CODEBUDDY_GATEWAY_PASSWORD=aaa "
            "CODEBUDDY_GATEWAY_PASSWORD=bbb "
            "CODEBUDDY_GATEWAY_PASSWORD=aaa"
        )
        self.assertEqual(module._scanned_tokens(lambda: text), ["aaa", "bbb"])


if __name__ == "__main__":
    unittest.main()
