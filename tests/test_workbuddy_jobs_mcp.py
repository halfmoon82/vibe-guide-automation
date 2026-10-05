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
import subprocess
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

    def test_an_explicit_null_numeric_argument_falls_back_to_the_default(self):
        # `wait` compares its bounds, so a null that reached the operation would
        # surface as an opaque internal error instead of the documented default.
        gateway = FakeGateway({("GET", "/api/v1/jobs/j1"): {"state": "done", "settled": True}})
        jobs = WorkBuddyJobs(gateway=gateway)
        response = handle_message(
            jobs,
            {"jsonrpc": "2.0", "id": 12, "method": "tools/call",
             "params": {"name": "wait",
                        "arguments": {"job_id": "j1", "timeout_seconds": None, "poll_seconds": None}}},
        )
        self.assertFalse(response["result"]["isError"])
        self.assertNotIn("internal error", response["result"]["content"][0]["text"])

    def test_a_required_argument_sent_as_null_is_reported_as_missing(self):
        gateway = FakeGateway()
        jobs = WorkBuddyJobs(gateway=gateway)
        response = handle_message(
            jobs,
            {"jsonrpc": "2.0", "id": 13, "method": "tools/call",
             "params": {"name": "get", "arguments": {"job_id": None}}},
        )
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(gateway.calls, [])

    def test_an_unexpected_null_argument_is_still_rejected(self):
        # Dropping nulls must not become a way to smuggle an unknown key past
        # the argument whitelist.
        gateway = FakeGateway()
        jobs = WorkBuddyJobs(gateway=gateway)
        response = handle_message(
            jobs,
            {"jsonrpc": "2.0", "id": 14, "method": "tools/call",
             "params": {"name": "get", "arguments": {"job_id": "a", "shell": None}}},
        )
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(gateway.calls, [])

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


#: The host's shim directory, as it appears in PATH / NODE_OPTIONS / PYTHONPATH.
_HOST_SHIM = (
    "/Applications/WorkBuddy AI.app/Contents/Resources/app.asar.unpacked"
    "/cli/vendor/shim"
)

#: What a job's shell does.  `command -v` resolves through PATH, so a shimmed
#: PATH shows up here; the trailing `echo` reports the *inner* exit code,
#: because the outer bash always exits 0 on a successful echo.
_SHELL_PROBE = "command -v ls; ls /tmp >/dev/null; echo rc=$?"

#: The first fix on this branch withdrew exactly these and nothing else.
_HALF_FIX_VARS = (
    "CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS",
    "CODEBUDDY_SANDBOX_BROKER_SESSION_ID",
    "CODEBUDDY_SANDBOX_HOST_FILE_OPERATION_COMMAND",
    "CODEBUDDY_SANDBOX_BROKER_TOOL_CALL_ID",
    "CODEBUDDY_SANDBOX_BROKER_TRACE_ID",
)


def _run_shell_probe(env):
    return subprocess.run(
        ["/bin/bash", "-c", _SHELL_PROBE], env=env,
        capture_output=True, text=True, timeout=60,
    )


def _probe_rc(result):
    for line in result.stdout.splitlines():
        if line.startswith("rc="):
            return int(line[3:])
    raise AssertionError("the shell probe reported no exit code: %r" % (result.stdout,))


#: The host's safe-delete guardrail is a *function* defined from `BASH_ENV`,
#: not a PATH entry, so a fixture that only plants PATH cannot see it.
_DELETE_PROBE = 'rm -f "$TARGET"'

#: `_SHELL_PROBE` resolves `ls`, which the guardrail does not wrap.  This one
#: resolves `rm`, which it does -- the difference between "the shell runs at
#: all" and "the shell still deletes through the guardrail".
_RM_PROBE = 'command -v rm; type -t rm'

#: The broker-bound half of the host's injection surface: everything that ties
#: a process to one live agent tool call.  All of it must be withdrawn.
_BROKER_BOUND_PREFIXES = (
    "CODEBUDDY_SANDBOX_",
    "CODEBUDDY_BROKERED_",
    "CODEBUDDY_TOYBOX_",
    "SANDBOX_CENTER_",
)
_BROKER_BOUND_VARS = ("TOYBOX_SANDBOX_SOCK", "CODEBUDDY_BROKER_IPC_CLIENT")

#: The guardrail half: it needs no broker and must survive, or the gateway and
#: every job it dispatches falls back to unmediated native deletes.
_GUARDRAIL_CARRIERS = (
    "BASH_ENV",
    "CODEBUDDY_SAFE_DELETE_BIN_DIR",
    "CODEBUDDY_SAFE_DELETE_ENABLED",
    "NODE_OPTIONS",
    "PYTHONPATH",
)


def _build_fake_shim(root):
    """A stand-in for the host's shim, wired in the same order as the real one.

    `shell-runtime-bash-env.sh` sources the safe-delete wrappers first -- which
    define `rm` as a *function* -- and the brokered block second, which unsets
    that function and prepends `brokered-bin` to PATH.  The order is the whole
    reason a name-by-name fix does not work, so the fixture reproduces it.

    Each carrier leaves its own mark, so a test can tell which one a shell
    actually reached rather than inferring it from PATH.
    """
    brokered = root / "brokered-bin"
    safe = root / "safe-bin"
    marks = root / "marks"
    for path in (brokered, safe, marks):
        path.mkdir(parents=True)

    for directory, marker in ((brokered, "brokered"), (safe, "safe-delete")):
        wrapper = directory / "rm"
        if marker == "safe-delete":
            # Reproduce the two observable halves of the real
            # `safe-delete-common.sh` contract -- hand the target to a trash
            # stand-in, and record the operation -- so a test can assert the
            # *route* a delete took and not merely that it happened.  Guarded,
            # so the mark-only tests need not set these up.  `/bin/rm`, not
            # `rm`: see the note on the stand-in genie-trash further down.
            body = (
                'if [ -n "$target" ] && [ -n "${GENIE_TRASH_LOG:-}" ]; then '
                'printf \'%s\\n\' "$target" >> "$GENIE_TRASH_LOG"; fi\n'
                'if [ -n "$target" ] && [ -n "${CODEBUDDY_SAFE_DELETE_REPORT_PATH:-}" ]; then '
                'printf \'{"operation":"trash","path":"%s"}\\n\' "$target" >> '
                '"$CODEBUDDY_SAFE_DELETE_REPORT_PATH"; fi\n'
            )
        else:
            body = ""
        wrapper.write_text(
            '#!/bin/sh\n'
            'printf \'%s\\n\' "$*" > "%s/%s"\n' % ("%s", marks, marker)
            # `rm -f <path>` puts the flag in `$1`, so pick the operand out
            # rather than assuming position one.  `/bin/rm`, not `rm`: on macOS
            # `/bin/sh` is bash, so a bare `rm` here would pick the guardrail
            # function back up from `BASH_ENV` and call itself -- a fork bomb
            # that looks like a hang.
            + 'target=\n'
            + 'for arg in "$@"; do case "$arg" in --|-*) ;; *) target="$arg" ;; esac; done\n'
            + body
            + '/bin/rm -f -- "$target"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

    (safe / "safe-delete-bash-env.sh").write_text(
        'if [ -n "${CODEBUDDY_SAFE_DELETE_BIN_DIR:-}" ]; then\n'
        '    rm() { "${CODEBUDDY_SAFE_DELETE_BIN_DIR}/rm" "$@"; }\n'
        '    export -f rm\n'
        'fi\n',
        encoding="utf-8",
    )
    safe_env = safe / "safe-delete-bash-env.sh"
    (root / "shell-runtime-bash-env.sh").write_text(
        '[ ! -f "%s" ] || . "%s"\n'
        'if [ -n "${CODEBUDDY_TOYBOX_BIN:-}" ] && [ -n "${CODEBUDDY_BROKERED_BIN_DIR:-}" ]; then\n'
        '    unset -f rm 2>/dev/null || true\n'
        '    export PATH="${CODEBUDDY_BROKERED_BIN_DIR}:$PATH"\n'
        'fi\n' % (safe_env, safe_env),
        encoding="utf-8",
    )
    return marks


def _fake_shim_env(root):
    """The fixture's carriers, planted the way the host plants its own."""
    return {
        "PATH": os.pathsep.join(["/usr/bin", "/bin"]),
        "HOME": os.path.expanduser("~"),
        "BASH_ENV": str(root / "shell-runtime-bash-env.sh"),
        "CODEBUDDY_SESSION_ID": "planted-session",
        "CODEBUDDY_SAFE_DELETE_BIN_DIR": str(root / "safe-bin"),
        "CODEBUDDY_BROKERED_BIN_DIR": str(root / "brokered-bin"),
        "CODEBUDDY_TOYBOX_BIN": "/planted/toybox",
        "CODEBUDDY_SANDBOX_PROGRAM_POLICY_COMMAND": "CheckProgramPolicy",
        "CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS": "/planted/broker.sock",
    }


def _whole_class_strip(env, shim_markers=("/cli/vendor/shim",)):
    """The rule this branch shipped first, kept as a control.

    It withdraws the guardrail along with the broker -- variables, `BASH_ENV`
    and the shim PATH entries -- so `rm` falls through to `/bin/rm`.  Dropping
    the PATH entries matters: leave `safe-bin` on PATH and the control still
    reaches the trash through the wrapper binary, which would make it useless
    as a control.  Reproduced here so "the guardrail survives" has a
    counterpart showing what losing it looks like; without one, that test would
    pass even if nothing were wired at all.

    `shim_markers` names the PATH entries that count as shims.  It defaults to
    the host's; a fixture-driven test passes its own temporary root instead,
    because the host's marker matches nothing there.

    This control withdraws the *shell* carriers only -- `BASH_ENV` and the shim
    PATH entries.  It leaves `NODE_OPTIONS` and `PYTHONPATH` alone, so it is a
    control for the shell arm and nothing else: a node or python negative
    control that reused it would still reach the trash through the language
    shim and would therefore prove the wrong thing.
    """
    stripped = {
        name: value for name, value in env.items()
        if not name.startswith(_BROKER_BOUND_PREFIXES + ("CODEBUDDY_SAFE_DELETE_",))
        and name not in ("TOYBOX_SANDBOX_SOCK", "BASH_ENV")
    }
    path = stripped.get("PATH")
    if path:
        kept = [
            part for part in path.split(os.pathsep)
            if part and not any(marker in part for marker in shim_markers)
        ]
        if kept:
            stripped["PATH"] = os.pathsep.join(kept)
    return stripped


def _mark_for(env, root, name):
    """Run one delete under `env` and report which carrier it reached."""
    marks = root / "marks"
    for stale in marks.iterdir():
        stale.unlink()
    subprocess.run(
        ["/bin/bash", "-c", _DELETE_PROBE],
        env=dict(env, TARGET=str(root / name)),
        capture_output=True, text=True, timeout=60,
    )
    return sorted(path.name for path in marks.iterdir())


class GatewayEnvironmentTests(unittest.TestCase):
    """A gateway we start must not inherit the host's *broker binding*.

    WorkBuddy injects one surface carrying two independent things:

    * the **broker binding** -- `CODEBUDDY_SANDBOX_*` naming one live session's
      socket, `CODEBUDDY_BROKERED_*`, `CODEBUDDY_TOYBOX_*`, `SANDBOX_CENTER_*`,
      and the `brokered-bin` PATH entry that consults them.  It only honours
      operations approved for an *agent tool call*, and a gateway started from
      this module is not such a call.
    * the **safe-delete guardrail** -- `BASH_ENV` plus the `safe-bin` wrappers,
      `CODEBUDDY_SAFE_DELETE_*`, `NODE_OPTIONS`, `PYTHONPATH`.  It needs no
      broker: with the broker unreachable it degrades to the trash.

    Measured 2026-10-05 on WorkBuddy AI (macOS, CLI 2.147.0):

    * host env intact -> an `fs.rmdirSync` under `~/.workbuddy-ai/jobs/.locks`
      raises `[safe-delete] broker denied delete`, so `POST /api/v1/jobs`
      answers HTTP 500 while cleaning up the lock directory it just made, and
      leaves an empty `<job>.state.lock.guard` behind;
    * broker variables *alone* withdrawn -> worse.  The surviving `brokered-bin`
      PATH entry still sees `CODEBUDDY_SANDBOX_PROGRAM_POLICY_COMMAND` and can
      no longer reach the broker it just lost, so it fails closed: every
      command run inside the gateway's shells exits 13 with `Brokered program
      policy check unavailable`;
    * the whole injection surface withdrawn -> the exit-13 shell goes away, but
      so does the guardrail.  `rm` resolves to `/bin/rm`, so the gateway and
      every job it dispatches does unmediated native deletes.  Review caught
      this; it is the regression these tests exist to prevent.

    So both directions are pinned.  The fixture-driven tests do not depend on
    this machine being a host child, and the last two are deterministic
    negative controls rather than skips.
    """

    #: Names that bind a process to one live broker session: all of them go.
    BROKER_BOUND = (
        "CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS",
        "CODEBUDDY_SANDBOX_BROKER_SESSION_ID",
        "CODEBUDDY_SANDBOX_BROKER_TOOL_CALL_ID",
        "CODEBUDDY_SANDBOX_BROKER_TRACE_ID",
        "CODEBUDDY_SANDBOX_BROKER_READ_TIMEOUT_MS",
        "CODEBUDDY_SANDBOX_HOST_FILE_OPERATION_COMMAND",
        "CODEBUDDY_SANDBOX_FILE_TOKEN_OPERATION_COMMAND",
        "CODEBUDDY_SANDBOX_PROGRAM_POLICY_COMMAND",
        "CODEBUDDY_SANDBOX_ZSH_BIN",
        "CODEBUDDY_BROKERED_BIN_DIR",
        "CODEBUDDY_BROKERED_SHELL_ENV",
        "CODEBUDDY_BROKERED_FS_HOOK_ENABLED",
        "CODEBUDDY_TOYBOX_BIN",
        "CODEBUDDY_TOYBOX_SANDBOX_PROFILE",
        "TOYBOX_SANDBOX_SOCK",
        "SANDBOX_CENTER_IPC_ADDRESS",
        "SANDBOX_CENTER_UID",
    )

    def _planted(self):
        """The whole injection surface, planted with the host's real paths."""
        return {
            "CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS": "/tmp/cbb-planted/broker.sock",
            "CODEBUDDY_SANDBOX_BROKER_SESSION_ID": "planted-session",
            "CODEBUDDY_SANDBOX_BROKER_TOOL_CALL_ID": "call_00_planted",
            "CODEBUDDY_SANDBOX_BROKER_TRACE_ID": "broker-planted",
            "CODEBUDDY_SANDBOX_BROKER_READ_TIMEOUT_MS": "125000",
            "CODEBUDDY_SANDBOX_HOST_FILE_OPERATION_COMMAND": "HostFileOperation",
            "CODEBUDDY_SANDBOX_FILE_TOKEN_OPERATION_COMMAND": "FileTokenRequest",
            "CODEBUDDY_SANDBOX_PROGRAM_POLICY_COMMAND": "CheckProgramPolicy",
            "CODEBUDDY_SANDBOX_ZSH_BIN": "/planted/zsh",
            "CODEBUDDY_BROKERED_BIN_DIR": _HOST_SHIM + "/brokered-bin",
            "CODEBUDDY_BROKERED_SHELL_ENV": _HOST_SHIM + "/broker-env.sh",
            "CODEBUDDY_BROKERED_FS_HOOK_ENABLED": "1",
            "CODEBUDDY_TOYBOX_BIN": "/planted/toybox",
            "CODEBUDDY_TOYBOX_SANDBOX_PROFILE": "/planted/toybox.sb",
            "TOYBOX_SANDBOX_SOCK": "/tmp/cbb-planted/broker.sock",
            "SANDBOX_CENTER_IPC_ADDRESS": "/tmp/sandbox-center.sock",
            "SANDBOX_CENTER_UID": "planted-uid",
            # The guardrail: no broker needed, so all of it stays.
            "CODEBUDDY_SAFE_DELETE_ENABLED": "1",
            "CODEBUDDY_SAFE_DELETE_BIN_DIR": _HOST_SHIM + "/safe-bin",
            "CODEBUDDY_SAFE_DELETE_SANDBOX": "1",
            "BASH_ENV": _HOST_SHIM + "/shell-runtime-bash-env.sh",
            "NODE_OPTIONS": '--require="%s/node-language-shim.cjs"' % _HOST_SHIM,
            "PYTHONPATH": os.pathsep.join([_HOST_SHIM, "/usr/lib/python3"]),
            "PATH": os.pathsep.join([
                _HOST_SHIM + "/brokered-bin",
                _HOST_SHIM + "/safe-bin",
                "/usr/bin",
                "/bin",
            ]),
            # The turn identity: whose agent turn a delete is charged to, plus
            # the session the guardrail gates itself on.  The first two go --
            # see TURN_IDENTITY_ENV_VARS -- and the third stays, because
            # `safe-bin/rm` falls through to `$REAL_RM` without it.
            "CODEBUDDY_CONVERSATION_REQUEST_ID": "turn-planted",
            "CODEBUDDY_TOOL_CALL_ID": "call_00_planted",
            "CODEBUDDY_SESSION_ID": "planted-session",
            # Things the gateway legitimately needs must survive.
            "VIBE_GATEWAY_ENV_PROBE": "kept",
            "CODEBUDDY_PROJECT_DIR": "/tmp/planted-project",
        }

    def _gateway_env_with(self, planted):
        with mock.patch.dict(os.environ, planted, clear=True):
            return module._gateway_env()

    # -- the removal rule --------------------------------------------------

    def test_every_broker_binding_variable_is_stripped(self):
        env = self._gateway_env_with(self._planted())
        # Guard the guard: the planted set must exercise the rule, or this
        # test proves nothing.
        self.assertEqual(
            sorted(self.BROKER_BOUND),
            sorted(
                name for name in self._planted()
                if name.startswith(_BROKER_BOUND_PREFIXES)
                or name in _BROKER_BOUND_VARS
            ),
            "BROKER_BOUND drifted away from the rule it is meant to cover",
        )
        for name in self.BROKER_BOUND:
            self.assertNotIn(name, env, "the gateway inherited %s" % name)

    def test_the_safe_delete_guardrail_survives(self):
        planted = self._planted()
        env = self._gateway_env_with(planted)
        for name in _GUARDRAIL_CARRIERS:
            self.assertIn(name, env, "the gateway lost the guardrail carrier %s" % name)
            self.assertEqual(env[name], planted[name])

    def test_only_the_brokered_path_entry_is_dropped(self):
        env = self._gateway_env_with(self._planted())
        self.assertEqual(
            env["PATH"],
            os.pathsep.join([_HOST_SHIM + "/safe-bin", "/usr/bin", "/bin"]),
            "safe-bin must stay in front of the bare system tools",
        )

    def test_unrelated_variables_survive(self):
        env = self._gateway_env_with(self._planted())
        self.assertEqual(env.get("VIBE_GATEWAY_ENV_PROBE"), "kept")
        self.assertEqual(env.get("CODEBUDDY_PROJECT_DIR"), "/tmp/planted-project")

    def test_the_turn_identity_is_not_inherited(self):
        """The third thing on the surface: *whose turn* a delete counts as.

        The host charges every non-temp delete to the turn named by these two
        variables, and the guard trips at 50 of them.  A gateway outlives the
        turn that started it, so inheriting them charges the gateway's own
        lock-directory bookkeeping to a turn that may long since have ended --
        at roughly 6 counts per ``create``, an ordinary turn runs out of budget
        after a handful of dispatches, and then ``create`` answers HTTP 500
        with ``SAFE_DELETE_BULK_CONFIRM_REQUIRED``.  Whether it trips depends
        on whether the tool-call id inherited at startup happens to sit in the
        guard's approvals, so the very same call succeeds in one session and
        500s in the next.
        """
        planted = self._planted()
        env = self._gateway_env_with(planted)
        for name in module.TURN_IDENTITY_ENV_VARS:
            self.assertIn(name, planted,
                          "the planted env lost %s, so this rule is exercised by nothing" % name)
            self.assertNotIn(
                name, env,
                "the gateway inherited the host's turn identity %s" % name,
            )

    def test_the_session_id_survives_so_the_guardrail_stays_armed(self):
        """`safe-bin/rm` falls through to `$REAL_RM` when no session id is set.

        Pinned separately because it is easy to lose by accident: it shares the
        `CODEBUDDY_` prefix with the turn identity above and reads like one
        more binding to withdraw.  Withdraw it and every gateway delete stops
        being a move to the trash and becomes a native unlink -- so the reason
        it stays is written down here rather than left to be inferred from the
        name.
        """
        env = self._gateway_env_with(self._planted())
        self.assertEqual(env.get("CODEBUDDY_SESSION_ID"), "planted-session")

    def test_the_sandbox_flag_is_kept_and_is_not_the_guardrail(self):
        """`CODEBUDDY_SAFE_DELETE_SANDBOX` reads like the guardrail.  It is not.

        Measured across the host's shim tree, the name occurs exactly twice --
        `node-language-shim.cjs:24` and `sitecustomize.py:34` -- and both uses
        feed the *brokered fs hook* switch rather than the safe-delete one.  The
        safe-delete shim is gated by `CODEBUDDY_SAFE_DELETE_ENABLED`, which
        this rule leaves alone.  So the flag is inert here either way: the
        broker binding it switches on has already been withdrawn.

        It is kept and pinned rather than stripped on the strength of its name.
        An earlier note justified keeping it by saying stripping it would
        assert, on the host's behalf, that we are not inside a sandbox; that
        claim does not survive measurement -- dropping the flag changes nothing
        observable -- so it is not repeated here as the reason.  What is pinned
        is that the flag stays, and which switch it actually throws.
        """
        env = self._gateway_env_with(self._planted())
        self.assertEqual(env.get("CODEBUDDY_SAFE_DELETE_SANDBOX"), "1")
        self.assertEqual(env.get("CODEBUDDY_SAFE_DELETE_ENABLED"), "1")

    # -- the rule, measured against a fixture (never skipped) --------------

    def test_the_fixture_shim_reproduces_the_real_wiring(self):
        """Guard the guard: the fixture must be able to show all three states.

        A fixture that quietly failed to define `rm` would make the next test
        pass for the wrong reason, and one that never let `brokered-bin` take
        over would make it prove nothing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_fake_shim(root)
            planted = _fake_shim_env(root)
            observed = {
                "host": _mark_for(planted, root, "host"),
                "narrowed": _mark_for(self._gateway_env_with(planted), root, "narrowed"),
                "whole-class-strip": _mark_for(_whole_class_strip(planted), root, "strip"),
            }

        self.assertEqual(observed["host"], ["brokered"],
                         "the fixture does not reproduce the broker takeover")
        self.assertEqual(observed["narrowed"], ["safe-delete"],
                         "the gateway env did not reach the safe-delete guardrail")
        self.assertEqual(observed["whole-class-strip"], [],
                         "the control kept the guardrail, so the narrowed "
                         "reading above proves nothing")

    # -- the rule, measured against the host's real shim -------------------

    def test_a_shell_under_the_gateway_environment_really_works(self):
        """The measurement that matters, run against the real host env.

        Skipped when this process is not itself a host-injected child, because
        then there is nothing to reproduce.  `test_the_fixture_shim_*` above
        covers the same property without that dependency.
        """
        if not any(name.startswith(_BROKER_BOUND_PREFIXES) for name in os.environ):
            self.skipTest("not running inside a host-injected environment")
        env = module._gateway_env()
        result = _run_shell_probe(env)
        self.assertEqual(_probe_rc(result), 0, result.stdout + result.stderr)
        self.assertNotIn("policy check unavailable", result.stderr)
        self.assertNotIn("brokered-bin", result.stdout.splitlines()[0],
                         "a gateway shell still resolves through the broker shim")

        rm_probe = subprocess.run(
            ["/bin/bash", "-c", _RM_PROBE], env=env,
            capture_output=True, text=True, timeout=60,
        )
        resolved = rm_probe.stdout.splitlines()
        self.assertGreaterEqual(len(resolved), 2, rm_probe.stdout + rm_probe.stderr)
        self.assertNotIn("brokered-bin", resolved[0],
                         "a gateway shell still deletes through the broker shim")
        self.assertTrue(
            resolved[1] == "function" or "safe-bin" in resolved[0],
            "a gateway shell bypasses the safe-delete guardrail: %r" % (rm_probe.stdout,),
        )

    def test_the_probe_sees_the_half_fix(self):
        """Prove the probe above can see the bug it exists to catch.

        Without this, a fix that only withdraws the broker variables would pass
        every name-shaped assertion while shipping the exit-13 shell.
        """
        if not os.environ.get("CODEBUDDY_SANDBOX_PROGRAM_POLICY_COMMAND"):
            self.skipTest("host did not inject the brokered program-policy variable")
        if not os.environ.get("CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS"):
            self.skipTest("host did not inject the broker wiring")
        half = {name: value for name, value in os.environ.items() if name not in _HALF_FIX_VARS}
        result = _run_shell_probe(half)
        self.assertNotEqual(
            _probe_rc(result), 0,
            "the half-fix arm unexpectedly worked, so the probe proves nothing: %r"
            % (result.stdout,),
        )

    def test_a_delete_still_routes_to_the_trash(self):
        """The property the first fix broke: deletes must not go native.

        Driven by the fixture shim rather than the host's real one, on purpose.
        Against the real shim this test was two false greens at once:

        * it read the real ``os.environ``.  In a process the host never
          injected, ``_gateway_env()`` is the identity function -- no
          ``BASH_ENV``, no ``safe-bin`` -- so ``rm`` was ``/bin/rm`` and the
          trash assertions rested on nothing.  The skip guard only checked that
          the shim's files existed, which is true on any machine that has
          WorkBuddy, whether or not this process was wired up.
        * it depended on the host's turn-scoped bulk-delete counter.  Once the
          surrounding turn had deleted past the threshold, the arm stopped with
          ``SAFE_DELETE_BULK_CONFIRM_REQUIRED`` and the victim survived -- so
          the same code passed or failed on unrelated global state.

        The fixture's ``safe-bin/rm`` reproduces the observable half of the
        real contract -- the target is handed to a trash stand-in, and a
        ``{"operation":"trash"}`` line lands in the report file -- so this
        asserts the *route* a delete took, not merely that it happened, and
        does it without touching the host or the turn counter.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_fake_shim(root)
            log = root / "trashed.txt"
            report = root / "report.jsonl"
            victim = root / "victim.txt"
            planted = dict(
                _fake_shim_env(root),
                GENIE_TRASH_LOG=str(log),
                CODEBUDDY_SAFE_DELETE_REPORT_PATH=str(report),
            )

            def delete_under(env):
                victim.write_text("probe\n", encoding="utf-8")
                for stale in (log, report):
                    if stale.exists():
                        stale.unlink()
                return subprocess.run(
                    ["/bin/bash", "-c", 'rm -f "%s"' % victim],
                    env=env, capture_output=True, text=True, timeout=60,
                )

            narrowed = delete_under(self._gateway_env_with(planted))
            self.assertFalse(victim.exists(),
                             "the delete did not happen: %s" % narrowed.stderr)
            self.assertTrue(log.exists(),
                            "the delete bypassed the trash: %s" % narrowed.stderr)
            self.assertIn("victim.txt", log.read_text(encoding="utf-8"))
            self.assertIn('"operation":"trash"', report.read_text(encoding="utf-8"))

            control = delete_under(_whole_class_strip(planted, shim_markers=(str(root),)))
            self.assertFalse(victim.exists(),
                             "the control did not delete anything: %s" % control.stderr)
            self.assertFalse(log.exists(),
                             "the control reached the trash, so the narrowed "
                             "reading above proves nothing")

    def test_the_started_gateway_really_cannot_see_the_broker(self):
        # End-to-end: plant the wiring, start a gateway, then read the
        # environment the child actually received.
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "child-env.txt"
            script = Path(tmp) / "env-cli"
            script.write_text(
                "#!/bin/sh\n"
                'env > "%s"\n'
                "printf '  Endpoint    http://127.0.0.1:45678\\n'\n"
                "printf '  Password    hunter2-not-real\\n'\n"
                "sleep 30\n" % dump,
                encoding="utf-8",
            )
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            planted = self._planted()
            gateway = Gateway(cli=str(script), startup_timeout=15.0)
            try:
                with mock.patch.dict(os.environ, planted, clear=True):
                    gateway.ensure()
                self.assertTrue(_await_ready(dump), "the fake CLI never dumped its environment")
                child = dict(
                    line.split("=", 1)
                    for line in dump.read_text(encoding="utf-8").splitlines()
                    if "=" in line
                )
            finally:
                gateway.close()
        for name in self.BROKER_BOUND:
            self.assertNotIn(name, child, "the gateway inherited %s" % name)
        self.assertNotIn("/cli/vendor/shim/brokered-bin", child.get("PATH", ""))
        for name in module.TURN_IDENTITY_ENV_VARS:
            self.assertNotIn(
                name, child,
                "the gateway inherited the host's turn identity %s" % name,
            )
        for name in _GUARDRAIL_CARRIERS:
            self.assertIn(name, child, "the gateway lost the guardrail carrier %s" % name)
        self.assertEqual(child.get("CODEBUDDY_SESSION_ID"), "planted-session")
        self.assertEqual(child.get("VIBE_GATEWAY_ENV_PROBE"), "kept")


if __name__ == "__main__":
    unittest.main()
