"""Authorization card options ``daemon`` (default on) and ``launchd_keepalive`` (default off).

Records issued before the options existed must read back with the defaults and
keep their digest; a non-default choice is signed and cannot be edited after.
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide.authorization import (
    AuthorizationCard,
    AuthorizationRecord,
    authorize,
    build_authorization_card,
    is_authorization_integrity_valid,
    refresh_authorization_card,
    validate_authorization_card_consistency,
)
from vibe_guide.models import AgentCapabilities, DAGNode, Plan

CAPS = AgentCapabilities("claude-code", True, True, True, False, True, "full")


def _plan():
    node = DAGNode("n1", "N1", [], [], None,
                   {"input": "i", "output": "o", "error_behavior": "e", "acceptance_example": "a",
                    "adapter_id": "claude-code", "files": ["a.py"]}, "planned")
    plan = Plan("p1", 1, ".vibe/plans/p1/prd.md", ["n1"], "confirmed_pending_authorization",
                authorization_required=True, nodes=[node])
    return plan, [node]


# A record issued by vibe 5.1.16 (git tag v5.1.16) and archived as JSON, before the
# daemon options existed.
RECORD_5_1_16 = json.loads(r'''{"active_pair_limit": 1, "agent_id": "claude-code", "allowed_actions": ["accept", "develop", "review", "rework", "test"], "dag_revision": 1, "decision_digest": "c48b1bab7511cffceec33b6a6ec9f13a9d7a609146e7f06a085f87ab485cae48", "deploy_authorization": "separate", "digest": "391b8fdb09b2c3b7d51fc81c8b6c735d1dc81663beaae3c22a4b7af9f45b01d2", "engine_authorization_digest": "391b8fdb09b2c3b7d51fc81c8b6c735d1dc81663beaae3c22a4b7af9f45b01d2", "engine_evidence_ref": "", "engine_mode": "", "excluded_actions": ["create_mr", "deploy", "merge", "push"], "execution_engine": "", "explicit_execution_mode_override": {}, "file_scope": ["a.py"], "integration_contract_digest": "", "integration_node_id": "", "integration_review_scope": [], "node_contract_digest": "458c5119302a519c89031e66add4f9347bd12c22757f10c541a2f1e0ce47b943", "node_ids": ["n1"], "node_scope_digests": {"n1": "4dd8f2ff77cc6ca82f3f3c3ee2208a66e1e3b8ac8928b6c67585a38dbf2c5468"}, "plan_id": "p1", "plan_version": 1, "remote_git_actions": "deny", "remote_git_actions_options": ["allow", "deny"], "remote_git_actions_scope": ["commit", "push", "create_pr", "create_mr", "merge"], "required_workflow": ["s0", "s1"], "schema_version": 2, "skipped_nodes": [], "topology_summary": {"background": 0, "by_topology": {"background": 0, "dual-visible": 1, "visible-sdd": 0}, "total": 1, "visible": 1}, "worker_scope": [], "workers": [{"limitations": [], "mode": "visible", "node_id": "n1", "role": "developer", "session_source": "", "topology": "dual-visible"}]}''')


def _stored(record):
    return json.loads(json.dumps(record.to_dict()))


class DefaultsTests(unittest.TestCase):
    def test_new_card_defaults_daemon_on_keepalive_off(self):
        plan, nodes = _plan()
        card = build_authorization_card(plan, nodes, CAPS)
        self.assertIs(card.daemon, True)
        self.assertIs(card.launchd_keepalive, False)

    def test_default_choices_do_not_change_the_digest_or_the_stored_keys(self):
        plan, nodes = _plan()
        explicit = build_authorization_card(plan, nodes, CAPS, daemon=True, launchd_keepalive=False)
        implicit = build_authorization_card(plan, nodes, CAPS)
        self.assertEqual(explicit.digest, implicit.digest)
        stored = authorize(implicit, "AUTHORIZE").to_dict()
        self.assertNotIn("daemon", stored)
        self.assertNotIn("launchd_keepalive", stored)


class LegacyRecordTests(unittest.TestCase):
    def test_record_without_the_keys_loads_with_defaults_and_valid_digest(self):
        plan, nodes = _plan()
        card = build_authorization_card(plan, nodes, CAPS)
        stored = _stored(authorize(card, "AUTHORIZE"))
        for key in ("daemon", "launchd_keepalive"):
            stored.pop(key, None)
        restored = AuthorizationRecord.from_dict(stored)
        self.assertIs(restored.daemon, True)
        self.assertIs(restored.launchd_keepalive, False)
        self.assertEqual(restored.digest, card.digest)
        self.assertTrue(is_authorization_integrity_valid(restored))


class Archived5_1_16RecordTests(unittest.TestCase):
    def test_record_issued_by_5_1_16_reads_with_defaults_and_keeps_its_digest(self):
        restored = AuthorizationRecord.from_dict(json.loads(json.dumps(RECORD_5_1_16)))
        self.assertIs(restored.daemon, True)
        self.assertIs(restored.launchd_keepalive, False)
        self.assertEqual(restored.digest, RECORD_5_1_16["digest"])
        self.assertTrue(is_authorization_integrity_valid(restored))

    def test_new_code_issues_the_same_digest_for_the_same_card(self):
        plan, nodes = _plan()
        card = build_authorization_card(plan, nodes, CAPS)
        self.assertEqual(card.digest, RECORD_5_1_16["digest"])


class NonDefaultChoiceTests(unittest.TestCase):
    def test_choice_is_signed_and_round_trips(self):
        plan, nodes = _plan()
        base = build_authorization_card(plan, nodes, CAPS)
        card = build_authorization_card(plan, nodes, CAPS, daemon=True, launchd_keepalive=True)
        self.assertNotEqual(card.digest, base.digest)
        self.assertTrue(validate_authorization_card_consistency(card))
        restored = AuthorizationRecord.from_dict(_stored(authorize(card, "AUTHORIZE")))
        self.assertIs(restored.launchd_keepalive, True)
        self.assertTrue(is_authorization_integrity_valid(restored))

    def test_daemon_off_is_signed(self):
        plan, nodes = _plan()
        card = build_authorization_card(plan, nodes, CAPS, daemon=False)
        restored = AuthorizationRecord.from_dict(_stored(authorize(card, "AUTHORIZE")))
        self.assertIs(restored.daemon, False)
        self.assertTrue(is_authorization_integrity_valid(restored))

    def test_editing_a_stored_choice_breaks_the_digest(self):
        plan, nodes = _plan()
        stored = _stored(authorize(build_authorization_card(plan, nodes, CAPS), "AUTHORIZE"))
        stored["launchd_keepalive"] = True
        self.assertFalse(is_authorization_integrity_valid(AuthorizationRecord.from_dict(stored)))

    def test_keepalive_without_daemon_and_non_booleans_are_refused(self):
        plan, nodes = _plan()
        with self.assertRaises(ValueError):
            build_authorization_card(plan, nodes, CAPS, daemon=False, launchd_keepalive=True)
        with self.assertRaises(ValueError):
            build_authorization_card(plan, nodes, CAPS, daemon="yes")
        stored = _stored(authorize(build_authorization_card(plan, nodes, CAPS), "AUTHORIZE"))
        stored["daemon"] = 1
        with self.assertRaises(ValueError):
            AuthorizationRecord.from_dict(stored)

    def test_refresh_carries_the_choice_forward(self):
        plan, nodes = _plan()
        card = build_authorization_card(plan, nodes, CAPS, launchd_keepalive=True)
        refreshed = refresh_authorization_card(plan, nodes, card)
        self.assertIs(refreshed.launchd_keepalive, True)
        self.assertEqual(refreshed.digest, card.digest)


class AutoStartTests(unittest.TestCase):
    """monitor's post-authorization hook: failures are recorded, never raised."""

    def setUp(self):
        import tempfile
        from vibe_guide.paths import ProjectPaths
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name), vibe_home=Path(self.tmp.name) / "home")

    def hook(self, card, **kwargs):
        from vibe_guide.cli import _auto_start_daemon
        return _auto_start_daemon(self.paths, "p1", "run-abc12345", card, None, **kwargs)

    def card(self, **choices):
        plan, nodes = _plan()
        return build_authorization_card(plan, nodes, CAPS, **choices)

    def test_default_card_starts_the_daemon_and_leaves_launchd_alone(self):
        calls = []
        keep = mock.Mock()
        result = self.hook(self.card(), starter=lambda *a, **k: calls.append(a) or {"status": "started", "pid": 7},
                           keepalive=keep)
        self.assertEqual(result["status"], "started")
        self.assertEqual(len(calls), 1)
        keep.assert_not_called()
        self.assertNotIn("launchd", result)

    def test_daemon_off_does_nothing(self):
        starter = mock.Mock()
        result = self.hook(self.card(daemon=False), starter=starter)
        self.assertEqual(result, {"enabled": False, "status": "off"})
        starter.assert_not_called()

    def test_start_failure_is_recorded_not_raised(self):
        def boom(*a, **k):
            raise OSError("no spawn")
        result = self.hook(self.card(), starter=boom)
        self.assertEqual(result["status"], "start_failed")
        self.assertIn("no spawn", result["reason"])

    def test_keepalive_card_calls_the_injected_installer(self):
        seen = []
        starter = mock.Mock()
        result = self.hook(self.card(launchd_keepalive=True),
                           starter=starter,
                           keepalive=lambda paths, run_id, command: seen.append((run_id, command)) or {"status": "loaded"})
        self.assertEqual(result["launchd"]["status"], "loaded")
        self.assertEqual(result["status"], "launchd_started")
        starter.assert_not_called()  # one instance only: launchd's
        self.assertEqual(seen[0][0], "run-abc12345")
        self.assertEqual(seen[0][1][-2:], ["--run-id", "run-abc12345"][-2:])

    def test_keepalive_failure_is_recorded_not_raised(self):
        def boom(*a, **k):
            raise RuntimeError("launchctl missing")
        result = self.hook(self.card(launchd_keepalive=True),
                           starter=lambda *a, **k: {"status": "started"}, keepalive=boom)
        self.assertEqual(result["launchd"]["status"], "failed")
        self.assertEqual(result["status"], "started")  # falls back to a direct start


class CardTextTests(unittest.TestCase):
    def test_card_text_states_the_recovery_rule_and_unverified_platforms(self):
        from vibe_guide.cli import _daemon_option_lines
        text = "\n".join(_daemon_option_lines(AuthorizationCard.__new__(AuthorizationCard)))
        for needle in ("守护程序：开", "开机保活（launchd）：关", "不会自动恢复", "只报告、不重启", "未验证"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
