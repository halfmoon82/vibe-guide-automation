"""host-detect: the calling platform comes from session-id variables only.

Every case states the host environment it runs under (``environ=``) instead of
inheriting this process's, so the suite gives the same result in a Claude
Code session and in a plain terminal.
"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from vibe_guide import cli as cli_module
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.cli import _load_plan, run_cli
from vibe_guide.doctor import capability_registrations
from vibe_guide.handoff_rebind import HandoffNothingToMove, require_unfinished_nodes
from vibe_guide.host_detect import (
    HostMismatchError,
    HostUnknownError,
    detect_host,
    resolve_adapter,
)
from vibe_guide.paths import ProjectPaths

from tests import test_platform_handoff_rebind as rebind
from tests.support_v45_authorize import with_display_receipt

FIXTURE = Path(__file__).parent / "fixtures" / "pm-path" / "product-spec.json"
COMPLEX_REQUEST = "设计并实现支付系统，迁移数据、集成接口、编写测试并部署"
PROBES = rebind.PROBES

CLAUDE_ENV = {"CLAUDE_CODE_SESSION_ID": "cc-1", "CLAUDECODE": "1", "AI_AGENT": "claude-code_2-1-316_agent"}
CODEX_ENV = {"CODEX_THREAD_ID": "th-1"}
WORKBUDDY_ENV = {"CODEBUDDY_SESSION_ID": "wb-1"}
PLAIN_ENV = {"PATH": "/usr/bin", "HOME": "/tmp"}


def facts(adapter):
    return {adapter + "." + name: True for name in PROBES}


class DetectHostTests(unittest.TestCase):
    def test_claude_code_needs_the_session_id_and_the_marker(self):
        found = detect_host(CLAUDE_ENV)
        self.assertEqual(found.platform, "claude-code")
        self.assertEqual(found.session_id, "cc-1")
        self.assertEqual(found.host_version, "2.1.316")
        self.assertIsNone(detect_host({"CLAUDECODE": "1"}).platform)
        self.assertIsNone(detect_host({"CLAUDE_CODE_SESSION_ID": "cc-1"}).platform)

    def test_workbuddy_and_codex_are_read_before_claude_code(self):
        self.assertEqual(detect_host(dict(CLAUDE_ENV, **WORKBUDDY_ENV)).platform, "workbuddy")
        self.assertEqual(detect_host(dict(CLAUDE_ENV, **CODEX_ENV)).platform, "codex")
        self.assertEqual(detect_host({"CODEX_SESSION_ID": "s"}).platform, "codex")

    def test_an_inherited_agent_name_never_lends_its_version(self):
        # A Claude CLI started from WorkBuddy carries AI_AGENT=claude-code_...
        found = detect_host(dict(CLAUDE_ENV, **WORKBUDDY_ENV))
        self.assertEqual(found.platform, "workbuddy")
        self.assertIsNone(found.host_version)

    def test_config_directories_are_not_identity(self):
        for env in ({"WORKBUDDY_CONFIG_DIR": "/x"}, {"CODEBUDDY_CONFIG_DIR": "/x"},
                    {"CODEX_HOME": "/x"}, PLAIN_ENV, {}):
            with self.subTest(env=env):
                self.assertIsNone(detect_host(env).platform)

    def test_workbuddy_with_codex_is_a_contradiction_that_stays_unknown(self):
        found = detect_host(dict(WORKBUDDY_ENV, **CODEX_ENV))
        self.assertIsNone(found.platform)
        self.assertIn("矛盾", found.reason)
        with self.assertRaises(HostUnknownError):
            resolve_adapter(None, found)
        self.assertEqual(resolve_adapter("codex", found), "codex")

    def test_no_environment_means_no_detection(self):
        self.assertIsNone(detect_host(None).platform)
        self.assertIsNone(resolve_adapter(None, detect_host(None)))

    def test_an_explicit_adapter_that_contradicts_the_host_is_refused(self):
        with self.assertRaises(HostMismatchError) as caught:
            resolve_adapter("workbuddy", detect_host(CODEX_ENV))
        self.assertIn("codex", str(caught.exception))
        # A provider spelling of the same platform is not a contradiction.
        self.assertEqual(resolve_adapter("claude-code-background", detect_host(CLAUDE_ENV)),
                         "claude-code-background")


class _Project(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="host-detect-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = ProjectPaths(self.root)
        self.assertEqual(run_cli(["init", "--confirm", "--json"], self.root).payload["status"], "ok")

    def cli(self, argv, env=None):
        with_display_receipt(argv, self.root, env)
        return run_cli(list(argv) + ["--json"], self.root, environ=env)

    def attest(self, adapter, env=None, extra=()):
        name = adapter + "-facts.json"
        (self.root / name).write_text(json.dumps(facts(adapter)), encoding="utf-8")
        argv = ["attest", "--facts", name, "--provenance", "host-detect test",
                "--project-id", "hdprobe"] + list(extra)
        if env is None:
            argv += ["--adapter", adapter]
        return self.cli(argv, env)

    def publish(self, env, *extra, plan_id="hd"):
        shutil.copy(FIXTURE, self.root / "product-spec.json")
        return self.cli(["plan", "--request", COMPLEX_REQUEST, "--plan-id", plan_id,
                         "--s1", "5,5,5,5,5", "--from-prd", "product-spec.json", *extra], env)

    def provider_files(self):
        directory = self.root / ".vibe" / "provider-actions"
        return {path.name: path.read_bytes() for path in sorted(directory.glob("*")) if path.is_file()}


class PublishUsesTheHostTests(_Project):
    def test_claude_code_session_publishes_a_claude_code_card_without_adapter(self):
        self.attest("claude-code")
        self.attest("codex")
        published = self.publish(CLAUDE_ENV)
        self.assertEqual(published.payload["status"], "ok", published.payload)
        self.assertEqual(_load_plan(self.paths, "hd")[3].agent_id, "claude-code")

    def test_a_contradicting_adapter_is_refused_naming_the_environment(self):
        self.attest("workbuddy")
        refused = self.publish(CODEX_ENV, "--adapter", "workbuddy")
        self.assertEqual(refused.payload["status"], "blocked_invalid", refused.payload)
        self.assertIn("codex", refused.payload["reason"])
        self.assertFalse((self.root / ".vibe" / "plans" / "hd" / "authorization-card.json").exists())

    def test_contradictory_host_variables_require_the_flag(self):
        self.attest("codex")
        refused = self.publish(dict(CODEX_ENV, **WORKBUDDY_ENV))
        self.assertEqual(refused.payload["status"], "blocked_invalid", refused.payload)
        self.assertIn("--adapter", refused.payload["reason"])
        ok = self.publish(dict(CODEX_ENV, **WORKBUDDY_ENV), "--adapter", "codex")
        self.assertEqual(ok.payload["status"], "ok", ok.payload)

    def test_authorize_refuses_a_card_of_another_platform(self):
        self.attest("claude-code")
        self.assertEqual(self.publish(CLAUDE_ENV).payload["status"], "ok")
        refused = self.cli(["authorize", "--plan", "hd", "--authorize", "AUTHORIZE"], CODEX_ENV)
        self.assertEqual(refused.payload["status"], "blocked_invalid", refused.payload)
        self.assertIn("claude-code", refused.payload["reason"])
        mismatch = self.cli(["authorize", "--plan", "hd", "--authorize", "AUTHORIZE",
                             "--adapter", "workbuddy"], CLAUDE_ENV)
        self.assertEqual(mismatch.payload["status"], "blocked_invalid", mismatch.payload)
        ok = self.cli(["authorize", "--plan", "hd", "--authorize", "AUTHORIZE"], CLAUDE_ENV)
        self.assertEqual(ok.payload["status"], "ok", ok.payload)

    def test_authorize_with_contradictory_or_partial_host_requires_the_flag(self):
        self.attest("claude-code")
        self.assertEqual(self.publish(CLAUDE_ENV).payload["status"], "ok")
        argv = ["authorize", "--plan", "hd", "--authorize", "AUTHORIZE"]
        for env in (dict(CODEX_ENV, **WORKBUDDY_ENV), {"CLAUDECODE": "1"}):
            with self.subTest(env=env):
                refused = self.cli(argv, env)
                self.assertEqual(refused.payload["status"], "blocked_invalid", refused.payload)
                self.assertIn("--adapter", refused.payload["reason"])
        ok = self.cli(argv + ["--adapter", "claude-code"], dict(CODEX_ENV, **WORKBUDDY_ENV))
        self.assertEqual(ok.payload["status"], "ok", ok.payload)

    def test_authorize_without_host_evidence_keeps_the_card_as_authority(self):
        self.attest("claude-code")
        self.assertEqual(self.publish(CLAUDE_ENV).payload["status"], "ok")
        ok = self.cli(["authorize", "--plan", "hd", "--authorize", "AUTHORIZE"], PLAIN_ENV)
        self.assertEqual(ok.payload["status"], "ok", ok.payload)

    def test_supervisor_register_checks_the_provider_against_the_host(self):
        argv = ["supervisor-register", "--run-id", "run-1", "--session-id", "S", "--host", "mac"]
        refused = self.cli(argv + ["--provider", "workbuddy"], CODEX_ENV)
        self.assertEqual(refused.payload["status"], "blocked_invalid", refused.payload)
        self.assertIn("codex", refused.payload["reason"])
        detected = self.cli(argv, CODEX_ENV)
        self.assertEqual(detected.payload["status"], "ok", detected.payload)
        self.assertEqual(detected.payload["current"]["provider"], "codex")


class AttestRecordsTheObservationTests(_Project):
    def test_attest_records_time_version_and_session_of_its_own_platform(self):
        result = self.attest("claude-code", env=CLAUDE_ENV)
        self.assertEqual(result.payload["status"], "ok", result.payload)
        record = ProviderActionStore(self.paths).capabilities("claude-code")
        self.assertEqual(record["host_version"], "2.1.316")
        self.assertEqual(record["session_id"], "cc-1")
        self.assertTrue(record["observed_at"].endswith("Z"))
        self.assertEqual(result.payload["record_path"],
                         ".vibe/provider-actions/capabilities-claude-code.json")
        self.assertEqual(sorted(self.provider_files()), ["capabilities-claude-code.json"])

    def test_attest_for_another_platform_is_refused(self):
        refused = self.attest("workbuddy", env=CLAUDE_ENV, extra=["--adapter", "workbuddy"])
        self.assertEqual(refused.payload["status"], "blocked_invalid", refused.payload)
        self.assertEqual(self.provider_files(), {})

    def test_the_legacy_single_file_is_migrated_once_by_attest(self):
        store = self.root / ".vibe" / "provider-actions"
        store.mkdir(parents=True, exist_ok=True)
        (store / "capabilities.json").write_text(json.dumps({
            "schema_version": 1, "adapter_id": "codex", "facts": facts("codex"),
            "provenance": "older vibe"}), encoding="utf-8")
        self.assertEqual(self.attest("claude-code", env=CLAUDE_ENV).payload["status"], "ok")
        self.assertEqual(sorted(self.provider_files()),
                         ["capabilities-claude-code.json", "capabilities-codex.json"])
        self.assertEqual(ProviderActionStore(self.paths).capabilities("codex")["provenance"], "older vibe")


class ReadPathsWriteNothingTests(_Project):
    def test_plan_status_and_doctor_leave_provider_actions_untouched(self):
        store = self.root / ".vibe" / "provider-actions"
        store.mkdir(parents=True, exist_ok=True)
        (store / "capabilities.json").write_text(json.dumps({
            "schema_version": 1, "adapter_id": "claude-code", "facts": facts("claude-code"),
            "provenance": "older vibe", "project_id": "hdprobe"}), encoding="utf-8")
        before = self.provider_files()
        self.assertEqual(self.publish(CLAUDE_ENV).payload["status"], "ok")
        self.cli(["status"], CLAUDE_ENV)
        self.cli(["doctor"], CLAUDE_ENV)
        self.cli(["doctor"], None)
        self.assertEqual(self.provider_files(), before)


class DoctorTests(_Project):
    def test_a_lowered_host_version_needs_review(self):
        self.attest("claude-code", env=CLAUDE_ENV)
        path = self.root / ".vibe" / "provider-actions" / "capabilities-claude-code.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["host_version"] = "2.1.315"
        path.write_text(json.dumps(record), encoding="utf-8")
        payload = self.cli(["doctor"], CLAUDE_ENV).payload
        rows = {row["adapter_id"]: row for row in payload["capability_registrations"]}
        self.assertTrue(rows["claude-code"]["needs_review"])
        self.assertIn("2.1.315", "".join(rows["claude-code"]["review_reasons"]))
        text = run_cli(["doctor"], self.root, environ=CLAUDE_ENV).text
        self.assertIn("需复核", text)
        self.assertIn("当前平台：claude-code", text)

    def test_an_old_registration_needs_review(self):
        now = datetime(2026, 11, 20, tzinfo=timezone.utc)
        rows = capability_registrations({
            "codex": {"observed_at": (now - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ")},
            "workbuddy": {"observed_at": (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")},
        }, detect_host(PLAIN_ENV), now=now)
        self.assertEqual([row["needs_review"] for row in rows], [True, False])

    def test_the_project_verdict_does_not_depend_on_the_caller(self):
        self.attest("claude-code", env=CLAUDE_ENV)
        verdicts = []
        for env in (CLAUDE_ENV, PLAIN_ENV, CODEX_ENV, None):
            payload = self.cli(["doctor"], env).payload
            verdicts.append({key: payload[key] for key in (
                "status", "diagnostic_status", "ok", "issues", "facts", "provider_bridge")})
        self.assertTrue(all(verdict == verdicts[0] for verdict in verdicts), verdicts)

    def test_an_unreadable_ledger_is_a_doctor_error(self):
        with mock.patch.object(cli_module.platform_ledger, "LEDGER_ERROR", "platform ledger unreadable"):
            payload = self.cli(["doctor"], PLAIN_ENV).payload
        self.assertEqual(payload["diagnostic_status"], "blocked")
        self.assertFalse(payload["ok"])


class FinishedRunHandoffTests(rebind.HandoffCase):
    def test_a_fully_accepted_run_is_not_handed_off(self):
        """socialmedia run-63e7cfc2: 8/8 accepted on Codex, then flipped to workbuddy."""
        self.published_on("codex")
        self.serve(rebind.SECOND)
        self.serve("integration-review")
        self.assertTrue(all(node["status"] == "accepted" for node in self.snapshot().nodes.values()))
        self.attest("workbuddy")
        refused = run_cli(["monitor", "--plan", rebind.PLAN, "--authorize", "AUTHORIZE",
                           "--platform-handoff", "--json"], self.root, environ=WORKBUDDY_ENV)
        self.assertEqual(refused.payload["status"], "blocked_invalid", refused.payload)
        self.assertIn("没有未完成节点", refused.payload["reason"])
        self.assertEqual(self.plan_card().agent_id, "codex")

    def test_require_unfinished_nodes(self):
        with self.assertRaises(HandoffNothingToMove):
            require_unfinished_nodes([], "codex")
        require_unfinished_nodes(["n1"], "codex")


class EntryReadsTheProcessEnvironmentTests(unittest.TestCase):
    def test_main_hands_the_process_environment_to_detection(self):
        seen = {}

        def fake_run_cli(argv, cwd, runner=None, **kwargs):
            seen["environ"] = kwargs.get("environ")
            return cli_module.CLIResult(0, {"status": "ok"}, "ok", False)

        with mock.patch.object(cli_module, "run_cli", fake_run_cli), \
                mock.patch("builtins.print"):
            cli_module.main(["doctor"])
        self.assertIs(seen["environ"], os.environ)


if __name__ == "__main__":
    unittest.main()
