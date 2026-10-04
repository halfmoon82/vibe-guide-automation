"""The `workbuddy_job` MCP server is the session half of vibe's dispatch chain.

The contract that matters is the tool names: `provider_action.NATIVE_TOOL_MAP`
tells the monitor which native tool to name in a mailbox request, and the
session can only answer if an MCP server exposes exactly those names.  These
tests pin that equality, the JSON-RPC surface, and the fail-closed behaviour
when the control plane refuses.
"""
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide.mcp_servers import workbuddy_jobs as module
from vibe_guide.mcp_servers.workbuddy_jobs import (
    Gateway,
    GatewayError,
    TOOL_DEFINITIONS,
    TOOL_NAMES,
    WorkBuddyJobs,
    handle_message,
    serve,
)
from vibe_guide.providers import WORKBUDDY_VISIBLE_PROVIDER
from vibe_guide.runners.provider_action import NATIVE_TOOL_MAP


#: A fake gateway whose TERM trap is observable.  It reaps its own background
#: sleep, records that it was asked to stop, and only then announces readiness
#: -- so a test can fail the banner read at a point where a clean shutdown is
#: genuinely observable instead of racing the shell's own start-up.
_SLOW_CLI = (
    "#!/bin/sh\n"
    "sleep 30 &\n"
    "SLEEP_PID=$!\n"
    "trap 'kill \"$SLEEP_PID\" 2>/dev/null; echo term > \"%s\"; exit 0' TERM\n"
    "echo ready > \"%s\"\n"
    "echo 'nothing useful here'\n"
    "wait\n"
)


def _await_ready(path, timeout=5.0):
    """Wait for a marker file, returning whether it showed up in time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


class FakeGateway:
    """Records calls and replays canned payloads; never touches the network."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}
        self.endpoint = "http://127.0.0.1:0"

    def call(self, method, path, payload=None, timeout=30.0):
        self.calls.append({"method": method, "path": path, "payload": payload})
        response = self.responses.get((method, path))
        if isinstance(response, Exception):
            raise response
        return {} if response is None else response

    def close(self):
        pass


class DispatchContractTests(unittest.TestCase):
    def test_every_native_tool_the_monitor_names_exists_here(self):
        expected = {
            tool.split("__", 1)[1]
            for tool in NATIVE_TOOL_MAP[WORKBUDDY_VISIBLE_PROVIDER].values()
        }
        self.assertEqual(expected, set(TOOL_NAMES))

    def test_the_server_name_is_the_prefix_the_map_assumes(self):
        for tool in NATIVE_TOOL_MAP[WORKBUDDY_VISIBLE_PROVIDER].values():
            self.assertEqual(tool.split("__", 1)[0], module.SERVER_NAME)

    def test_every_tool_declares_a_usable_schema(self):
        for tool in TOOL_DEFINITIONS:
            self.assertEqual(set(tool), {"name", "description", "inputSchema"})
            self.assertTrue(tool["description"])
            self.assertEqual(tool["inputSchema"]["type"], "object")


class JsonRpcSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.jobs = WorkBuddyJobs(gateway=FakeGateway())

    def test_initialize_reports_the_server_identity(self):
        response = handle_message(
            self.jobs,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        )
        self.assertEqual(response["result"]["serverInfo"]["name"], module.SERVER_NAME)
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", response["result"]["capabilities"])

    def test_tools_list_exposes_every_tool(self):
        response = handle_message(self.jobs, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual([t["name"] for t in response["result"]["tools"]], list(TOOL_NAMES))

    def test_notifications_are_not_answered(self):
        self.assertIsNone(handle_message(self.jobs, {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_unknown_method_is_reported(self):
        response = handle_message(self.jobs, {"jsonrpc": "2.0", "id": 3, "method": "tools/nope"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_unknown_tool_is_rejected_before_any_call(self):
        gateway = FakeGateway()
        jobs = WorkBuddyJobs(gateway=gateway)
        response = handle_message(
            jobs, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "bogus", "arguments": {}}}
        )
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(gateway.calls, [])

    def test_unexpected_arguments_are_rejected_before_any_call(self):
        gateway = FakeGateway()
        jobs = WorkBuddyJobs(gateway=gateway)
        response = handle_message(
            jobs,
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "get", "arguments": {"job_id": "a", "shell": "rm -rf /"}}},
        )
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(gateway.calls, [])

    def test_a_wrong_argument_type_is_rejected_before_any_call(self):
        # `wait` takes numbers; a string would otherwise reach the operation and
        # surface as an opaque internal error.
        gateway = FakeGateway()
        jobs = WorkBuddyJobs(gateway=gateway)
        response = handle_message(
            jobs,
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "wait", "arguments": {"job_id": "j1", "timeout_seconds": "abc"}}},
        )
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("timeout_seconds", response["error"]["message"])
        self.assertEqual(gateway.calls, [])

    def test_a_missing_required_argument_is_rejected_before_any_call(self):
        gateway = FakeGateway()
        jobs = WorkBuddyJobs(gateway=gateway)
        response = handle_message(
            jobs,
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
             "params": {"name": "create", "arguments": {}}},
        )
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(gateway.calls, [])

    def test_a_boolean_is_not_accepted_where_a_number_is_declared(self):
        # bool subclasses int in Python, but it is not a JSON number.
        gateway = FakeGateway()
        jobs = WorkBuddyJobs(gateway=gateway)
        response = handle_message(
            jobs,
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
             "params": {"name": "wait", "arguments": {"job_id": "j1", "poll_seconds": True}}},
        )
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(gateway.calls, [])

    def test_initialize_never_claims_a_version_we_do_not_implement(self):
        response = handle_message(
            self.jobs,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "9999-01-01"}},
        )
        self.assertEqual(response["result"]["protocolVersion"], module.PROTOCOL_VERSION)

    def test_an_explicit_null_optional_argument_counts_as_absent(self):
        # Some clients send null rather than omitting an optional key; that is
        # not the same as passing the wrong type.
        gateway = FakeGateway({("GET", "/api/v1/jobs"): {"jobs": []}})
        jobs = WorkBuddyJobs(gateway=gateway)
        response = handle_message(
            jobs,
            {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
             "params": {"name": "list", "arguments": {"cwd": None, "include_all": None}}},
        )
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(gateway.calls[-1]["path"], "/api/v1/jobs")

    def test_parse_errors_and_non_objects_are_reported(self):
        stdout = io.StringIO()
        serve(io.StringIO("not json\n[]\n"), stdout, jobs=WorkBuddyJobs(gateway=FakeGateway()))
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([line["error"]["code"] for line in lines], [-32700, -32600])

    def test_a_full_session_runs_over_stdio(self):
        gateway = FakeGateway({("POST", "/api/v1/jobs"): {"id": "j1", "state": "working"}})
        stdin = io.StringIO("\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "create", "arguments": {"prompt": "do it"}}}),
        ]) + "\n")
        stdout = io.StringIO()
        self.assertEqual(serve(stdin, stdout, jobs=WorkBuddyJobs(gateway=gateway)), 0)
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)  # the notification produced nothing
        payload = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertEqual(payload["id"], "j1")
        self.assertFalse(responses[1]["result"]["isError"])


class OperationMappingTests(unittest.TestCase):
    def _jobs(self, responses=None):
        gateway = FakeGateway(responses)
        return WorkBuddyJobs(gateway=gateway, sleeper=lambda _seconds: None), gateway

    def test_create_sends_only_the_arguments_it_was_given(self):
        jobs, gateway = self._jobs({("POST", "/api/v1/jobs"): {"id": "j1"}})
        jobs.create(prompt="ship it", cwd="/tmp/p", bash=True)
        self.assertEqual(
            gateway.calls,
            [{"method": "POST", "path": "/api/v1/jobs",
              "payload": {"prompt": "ship it", "cwd": "/tmp/p", "bash": True}}],
        )

    def test_create_requires_a_prompt(self):
        jobs, gateway = self._jobs()
        with self.assertRaises(ValueError):
            jobs.create(prompt="   ")
        self.assertEqual(gateway.calls, [])

    def test_get_unwraps_the_job_envelope(self):
        jobs, _ = self._jobs({("GET", "/api/v1/jobs/j1"): {"job": {"id": "j1", "state": "working"}}})
        self.assertEqual(jobs.get("j1"), {"id": "j1", "state": "working"})

    def test_list_asks_for_completed_jobs_only_when_requested(self):
        jobs, gateway = self._jobs({("GET", "/api/v1/jobs"): {"jobs": [{"id": "a", "cwd": "/x"}]},
                                    ("GET", "/api/v1/jobs?all=1"): {"jobs": [{"id": "a", "cwd": "/x"}, {"id": "b", "cwd": "/y"}]}})
        self.assertEqual(jobs.list()["count"], 1)
        self.assertEqual(gateway.calls[-1]["path"], "/api/v1/jobs")
        self.assertEqual(jobs.list(include_all=True)["count"], 2)
        self.assertEqual(gateway.calls[-1]["path"], "/api/v1/jobs?all=1")
        self.assertEqual(jobs.list(include_all=True, cwd="/y")["jobs"], [{"id": "b", "cwd": "/y"}])

    def test_reply_posts_the_message_and_only_sets_bash_when_given(self):
        jobs, gateway = self._jobs({("POST", "/api/v1/jobs/j1/reply"): {"delivered": True}})
        jobs.reply("j1", "continue")
        self.assertEqual(gateway.calls[-1]["payload"], {"text": "continue"})
        jobs.reply("j1", "continue", bash=False)
        self.assertEqual(gateway.calls[-1]["payload"], {"text": "continue", "bash": False})

    def test_wait_returns_as_soon_as_the_job_settles(self):
        gateway = FakeGateway()
        states = [{"state": "working", "settled": False}, {"state": "done", "settled": True}]
        gateway.call = lambda method, path, payload=None, timeout=30.0: states.pop(0)
        jobs = WorkBuddyJobs(gateway=gateway, sleeper=lambda _seconds: None)
        self.assertEqual(jobs.wait("j1")["state"], "done")

    def test_wait_reports_a_timeout_instead_of_raising(self):
        jobs, _ = self._jobs({("GET", "/api/v1/jobs/j1"): {"state": "working", "settled": False}})
        result = jobs.wait("j1", timeout_seconds=0.05, poll_seconds=0.01)
        self.assertTrue(result["wait_timed_out"])
        self.assertEqual(result["state"], "working")

    def test_wait_rejects_nonsense_bounds(self):
        jobs, _ = self._jobs()
        with self.assertRaises(ValueError):
            jobs.wait("j1", timeout_seconds=0)
        with self.assertRaises(ValueError):
            jobs.wait("j1", poll_seconds=0)

    def test_a_gateway_refusal_becomes_an_error_result_not_a_crash(self):
        gateway = FakeGateway({("GET", "/api/v1/jobs/j1"): GatewayError("HTTP 401 AUTH_REQUIRED")})
        jobs = WorkBuddyJobs(gateway=gateway)
        response = handle_message(
            jobs, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                   "params": {"name": "get", "arguments": {"job_id": "j1"}}}
        )
        self.assertTrue(response["result"]["isError"])
        self.assertIn("AUTH_REQUIRED", response["result"]["content"][0]["text"])


class GatewayTransportTests(unittest.TestCase):
    def test_environment_credentials_short_circuit_autostart(self):
        gateway = Gateway(endpoint="http://127.0.0.1:9999", token="secret")
        with mock.patch.object(Gateway, "_autostart") as autostart:
            gateway.ensure()
        autostart.assert_not_called()

    def test_autostart_is_refused_when_disabled(self):
        gateway = Gateway()
        with mock.patch.dict(os.environ, {module.AUTOSTART_ENV: "0"}, clear=False):
            with self.assertRaises(GatewayError):
                gateway.ensure()

    def test_missing_cli_is_reported_not_guessed(self):
        gateway = Gateway()
        with mock.patch.dict(os.environ, {module.AUTOSTART_ENV: "1", module.CLI_ENV: ""}, clear=False):
            with mock.patch.object(module, "find_cli", return_value=None):
                with self.assertRaises(GatewayError) as raised:
                    gateway.ensure()
        self.assertIn(module.CLI_ENV, str(raised.exception))

    def test_autostart_reads_the_endpoint_and_password_from_the_banner(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake-cli"
            # The real banner colours its labels: `Endpoint` is followed by a
            # reset sequence, not by whitespace.
            script.write_text(
                "#!/bin/sh\n"
                "printf '  \\033[38;5;79mCodeBuddy Code\\033[39m HTTP Server\\n'\n"
                "printf '  \\033[38;5;241mEndpoint\\033[39m    http://127.0.0.1:45678\\n'\n"
                "printf '  \\033[38;5;241mPassword\\033[39m    hunter2-not-real\\n'\n"
                "sleep 30\n",
                encoding="utf-8",
            )
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            gateway = Gateway(cli=str(script), startup_timeout=15.0)
            try:
                gateway.ensure()
                self.assertEqual(gateway.endpoint, "http://127.0.0.1:45678")
                self.assertTrue(gateway.started)
            finally:
                gateway.close()
            self.assertFalse(gateway.started)

    def test_a_banner_without_credentials_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "silent-cli"
            script.write_text("#!/bin/sh\necho 'nothing useful here'\nsleep 5\n", encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            gateway = Gateway(cli=str(script), startup_timeout=1.0)
            with self.assertRaises(GatewayError):
                gateway.ensure()
            gateway.close()

    def test_a_failed_banner_is_reported_without_echoing_it(self):
        # The banner carries the gateway password.  When its format drifts the
        # password pattern stops matching, so echoing the raw text would leak
        # the credential precisely in the failure case.
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "drifted-cli"
            script.write_text(
                "#!/bin/sh\n"
                "echo 'Endpoint: http://127.0.0.1:45678'\n"
                "echo 'Password: hunter2-not-real'\n"
                "sleep 5\n",
                encoding="utf-8",
            )
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            gateway = Gateway(cli=str(script), startup_timeout=1.0)
            with self.assertRaises(GatewayError) as raised:
                gateway.ensure()
            gateway.close()
        self.assertNotIn("hunter2-not-real", str(raised.exception))
        # The shape is still useful for diagnosis.
        self.assertIn("captured", str(raised.exception))

    def test_a_failure_while_reading_the_banner_stops_the_gateway(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "terminated"
            ready = Path(tmp) / "ready"
            script = Path(tmp) / "slow-cli"
            script.write_text(_SLOW_CLI % (marker, ready), encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)

            def fail_once_the_gateway_is_up(*_args, **_kwargs):
                _await_ready(ready)
                raise RuntimeError("boom")

            gateway = Gateway(cli=str(script), startup_timeout=15.0)
            with mock.patch.object(module, "_plain", side_effect=fail_once_the_gateway_is_up):
                with self.assertRaises(RuntimeError):
                    gateway.ensure()
            self.assertTrue(_await_ready(marker), "the started gateway was left running")
            self.assertFalse(gateway.started)

    def test_a_failure_while_shaping_the_banner_also_stops_the_gateway(self):
        # Reading the diagnostic happens outside the polling loop; if it can
        # throw, it becomes a second way to orphan the gateway.
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "terminated"
            ready = Path(tmp) / "ready"
            script = Path(tmp) / "slow-cli"
            script.write_text(_SLOW_CLI % (marker, ready), encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)

            def fail_once_the_gateway_is_up(*_args, **_kwargs):
                _await_ready(ready)
                raise OSError("banner unreadable")

            gateway = Gateway(cli=str(script), startup_timeout=1.0)
            with mock.patch.object(module, "_banner_shape", side_effect=fail_once_the_gateway_is_up):
                with self.assertRaises(GatewayError):
                    gateway.ensure()
            self.assertTrue(_await_ready(marker), "the started gateway was left running")
            self.assertFalse(gateway.started)

    def test_http_errors_carry_the_status_and_body(self):
        import urllib.error

        def opener(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {},
                                         io.BytesIO(b'{"error":{"code":"AUTH_REQUIRED"}}'))

        gateway = Gateway(endpoint="http://127.0.0.1:9999", token="t", opener=opener)
        with self.assertRaises(GatewayError) as raised:
            gateway.call("GET", "/api/v1/jobs")
        self.assertIn("401", str(raised.exception))
        self.assertIn("AUTH_REQUIRED", str(raised.exception))

    def test_the_data_envelope_is_unwrapped(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, _limit=None):
                return b'{"data":{"jobs":[]}}'

        gateway = Gateway(endpoint="http://127.0.0.1:9999", token="t",
                          opener=lambda request, timeout=None: Response())
        self.assertEqual(gateway.call("GET", "/api/v1/jobs"), {"jobs": []})


if __name__ == "__main__":
    unittest.main()
