import json
import os
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from vibe_guide import short_session as ss


def _script(directory, name, body):
    path = Path(directory) / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class BuildCommandTests(unittest.TestCase):
    def test_each_platform_uses_its_own_one_shot_form(self):
        self.assertEqual(
            ss.build_command("claude-code", "hi", "opus")[:6],
            ["claude", "-p", "--output-format", "json", "--model", "opus"])
        self.assertEqual(ss.build_command("codex", "hi", "m1"), ["codex", "exec", "-m", "m1", "hi"])
        self.assertEqual(ss.build_command("kimi-code", "hi"), ["kimi", "-p", "hi"])

    def test_no_model_means_platform_default(self):
        self.assertNotIn("--model", ss.build_command("claude-code", "hi"))

    def test_workbuddy_has_no_command_line(self):
        with self.assertRaises(ValueError):
            ss.build_command("workbuddy", "hi")

    def test_capability_report_marks_unverified_paths(self):
        report = ss.capability_report()
        self.assertEqual(report["codex"]["status"], "unverified")
        self.assertEqual(report["kimi-code"]["status"], "unverified")
        self.assertEqual(report["workbuddy"]["status"], "unverified")
        self.assertIsNone(report["workbuddy"]["command"])


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_claude_json_answer_and_usage(self):
        payload = json.dumps({"result": "VIBE_HEAL: verified", "usage": {
            "input_tokens": 10, "output_tokens": 5,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 3}})
        exe = _script(self.tmp.name, "claude", "echo '%s'" % payload)
        result = ss.launch("claude-code", "p", executable=exe, timeout=10)
        self.assertEqual(result.status, "ok")
        self.assertIn("VIBE_HEAL: verified", result.text)
        self.assertEqual(result.usage, {"input": 10, "output": 5, "cache_read": 0, "cache_creation": 3})

    def test_usage_missing_a_field_is_not_filled_with_zero(self):
        text, usage = ss.parse_claude_output(json.dumps({"result": "x", "usage": {"input_tokens": 1}}))
        self.assertEqual(text, "x")
        self.assertIsNone(usage)

    def test_missing_command_is_unknown_not_success(self):
        result = ss.launch("claude-code", "p", executable="/nonexistent/claude-xyz", timeout=5)
        self.assertEqual(result.status, "unknown")
        self.assertIn("not available", result.reason)

    def test_timeout_is_unknown_and_kills_the_process(self):
        exe = _script(self.tmp.name, "codex", "sleep 30")
        result = ss.launch("codex", "p", executable=exe, timeout=0.5)
        self.assertEqual(result.status, "unknown")
        self.assertIn("timed out", result.reason)

    def test_nonzero_exit_is_failed(self):
        exe = _script(self.tmp.name, "kimi", "echo oops; exit 3")
        result = ss.launch("kimi-code", "p", executable=exe, timeout=10)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.returncode, 3)
        self.assertIsNone(result.usage)

    def test_poll_is_called_while_waiting(self):
        exe = _script(self.tmp.name, "codex", "sleep 2.5")
        calls = []
        result = ss.launch("codex", "p", executable=exe, timeout=10, poll=lambda: calls.append(1))
        self.assertEqual(result.status, "ok")
        self.assertTrue(calls)

    def test_workbuddy_writes_mailbox_and_is_queued_not_done(self):
        box = Path(self.tmp.name) / "box"
        result = ss.launch("workbuddy", "fix it", mailbox_dir=box, run_id="r", node_id="n")
        self.assertEqual(result.status, "queued")
        record = json.loads(Path(result.mailbox_path).read_text(encoding="utf-8"))
        self.assertEqual(record["prompt"], "fix it")
        self.assertEqual(record["node_id"], "n")

    def test_workbuddy_without_mailbox_is_unknown(self):
        self.assertEqual(ss.launch("workbuddy", "x").status, "unknown")

    def test_unsupported_platform_is_unknown(self):
        self.assertEqual(ss.launch("cursor", "x").status, "unknown")


if __name__ == "__main__":
    unittest.main()


class HostEnvironmentTests(unittest.TestCase):
    """5.2.1 rehearsal: a short session started by a daemon that inherited the
    desktop host's marker came up "Not logged in" and its answer was thrown away."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_probe(self, **kwargs):
        exe = _script(self.tmp.name, "claude",
                      'echo "{\\"result\\": \\"marker=${CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST:-absent}\\"}"')
        return ss.launch("claude-code", "p", executable=exe, timeout=10, **kwargs)

    def test_the_host_marker_is_not_passed_to_the_one_shot_session(self):
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1"}):
            result = self.run_probe()
        self.assertIn("marker=absent", result.text)

    def test_the_host_auth_marker_is_not_passed_either(self):
        exe = _script(self.tmp.name, "claude",
                      'echo "{\\"result\\": \\"auth=${CLAUDE_CODE_HOST_AUTH_ENV_VAR:-absent}\\"}"')
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_CODE_HOST_AUTH_ENV_VAR": "ANTHROPIC_AUTH_TOKEN"}):
            result = ss.launch("claude-code", "p", executable=exe, timeout=10)
        self.assertIn("auth=absent", result.text)

    def test_other_environment_still_reaches_the_session(self):
        with unittest.mock.patch.dict(os.environ, {"VIBE_PROBE": "kept"}):
            exe = _script(self.tmp.name, "claude", 'echo "{\\"result\\": \\"v=$VIBE_PROBE\\"}"')
            result = ss.launch("claude-code", "p", executable=exe, timeout=10)
        self.assertIn("v=kept", result.text)

    def test_an_explicit_env_is_used_as_given(self):
        result = self.run_probe(env={"PATH": os.environ["PATH"], "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "explicit"})
        self.assertIn("marker=explicit", result.text)

    def test_a_json_error_result_is_failed_and_keeps_its_message(self):
        payload = json.dumps({"is_error": True, "result": "Not logged in · Please run /login"})
        exe = _script(self.tmp.name, "claude", "echo '%s'; exit 1" % payload)
        result = ss.launch("claude-code", "p", executable=exe, timeout=10)
        self.assertEqual(result.status, "failed")
        self.assertIn("Not logged in", result.reason)
        self.assertEqual(result.text, "Not logged in · Please run /login")

    def test_a_json_error_with_exit_zero_is_still_failed(self):
        payload = json.dumps({"is_error": True, "result": "Invalid API key"})
        exe = _script(self.tmp.name, "claude", "echo '%s'" % payload)
        result = ss.launch("claude-code", "p", executable=exe, timeout=10)
        self.assertEqual(result.status, "failed")
        self.assertIn("Invalid API key", result.reason)
