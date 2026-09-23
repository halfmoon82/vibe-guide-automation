"""Producer/validator round trip: the product must accept the cards it issues.

The V4.5 audit ran ``validate_authorization_card_consistency(build_*())`` and
every live builder was rejected, for both ``allow`` and ``deny``.  Two drifts:
the validator's remote-action vocabulary carried two alias names the producer
never emits (so ``allow`` was never "complete"), and the producer's baseline
action set granted ``commit`` under ``deny`` although the confirmed design
(2026-09-14 session entry design) says ``deny`` forbids commit/push/PR/MR/merge.
The validator was also never called on the issuing path, which is why nobody
noticed.  These tests pin the round trip for every live builder.
"""
import json
import unittest

from vibe_guide.authorization import (
    BACKGROUND_MODE_DISCLOSURES,
    _REMOTE_GIT_ACTIONS_SCOPE,
    AuthorizationRecord,
    _canonical_digest,
    authorize,
    build_authorization_card,
    is_authorization_integrity_valid,
    refresh_authorization_card,
    validate_authorization_card_consistency,
)
from vibe_guide.models import AgentCapabilities, DAGNode, Plan


def _plan():
    node = DAGNode(
        "n1", "N1", [], [], None,
        {"input": "i", "output": "o", "error_behavior": "e", "acceptance_example": "a",
         "adapter_id": "claude-code", "files": ["a.py"]},
        "planned",
    )
    plan = Plan("p1", 1, ".vibe/plans/p1/prd.md", ["n1"], "confirmed_pending_authorization",
                authorization_required=True, nodes=[node])
    return plan, [node]


CAPS = AgentCapabilities("claude-code", True, True, True, False, True, "full")


class IssuedCardsPassTheirOwnValidatorTests(unittest.TestCase):
    def test_deny_card_round_trips_and_grants_no_remote_git_action(self):
        plan, nodes = _plan()
        card = build_authorization_card(plan, nodes, CAPS, remote_git_actions="deny")
        self.assertTrue(validate_authorization_card_consistency(card))
        self.assertFalse(set(card.allowed_actions) & set(_REMOTE_GIT_ACTIONS_SCOPE), card.allowed_actions)
        self.assertNotIn("commit", card.allowed_actions)

    def test_allow_card_round_trips_and_grants_exactly_the_remote_scope(self):
        plan, nodes = _plan()
        card = build_authorization_card(plan, nodes, CAPS, remote_git_actions="allow")
        self.assertTrue(validate_authorization_card_consistency(card))
        self.assertTrue(set(_REMOTE_GIT_ACTIONS_SCOPE) <= set(card.allowed_actions), card.allowed_actions)

    def test_refreshed_cards_round_trip_for_both_switches(self):
        for switch in ("deny", "allow"):
            plan, nodes = _plan()
            card = build_authorization_card(plan, nodes, CAPS, remote_git_actions=switch)
            refreshed = refresh_authorization_card(plan, nodes, card)
            self.assertTrue(validate_authorization_card_consistency(refreshed), switch)
            self.assertEqual(refreshed.remote_git_actions, switch)

    def test_builder_refuses_an_inconsistent_explicit_action_list(self):
        # The issuing path now self-checks: a caller cannot hand-pick remote
        # actions under deny, nor a partial remote set under allow.
        plan, nodes = _plan()
        with self.assertRaises(ValueError):
            build_authorization_card(plan, nodes, CAPS, remote_git_actions="deny", allowed_actions=("develop", "push"))
        with self.assertRaises(ValueError):
            build_authorization_card(plan, nodes, CAPS, remote_git_actions="allow", allowed_actions=("develop", "commit"))

    def test_validator_vocabulary_is_the_producer_scope_plus_legacy_aliases(self):
        from vibe_guide.authorization import validate_remote_git_permissions
        # allow: complete means the producer's scope, nothing more
        validate_remote_git_permissions("allow", ("develop",) + tuple(_REMOTE_GIT_ACTIONS_SCOPE))
        # deny: legacy alias names are still rejected (fail-closed)
        for alias in ("pr", "mr"):
            with self.assertRaises(ValueError):
                validate_remote_git_permissions("deny", ("develop", alias))


class WorkersRoundTripTests(unittest.TestCase):
    """V4.6 ISSUE-02 round trips: disclosed background cards validate and
    reload; pre-ISSUE-02 records without workers fields stay readable."""

    def test_background_card_with_full_disclosure_round_trips(self):
        plan, nodes = _plan()
        card = build_authorization_card(
            plan,
            nodes,
            CAPS,
            workers={
                "n1": {
                    "mode": "background",
                    "limitations": list(BACKGROUND_MODE_DISCLOSURES),
                }
            },
        )
        self.assertTrue(validate_authorization_card_consistency(card))

        record = authorize(card, "AUTHORIZE")
        restored = AuthorizationRecord.from_dict(
            json.loads(json.dumps(record.to_dict()))
        )
        self.assertTrue(is_authorization_integrity_valid(restored))
        workers = {entry["node_id"]: entry for entry in restored.workers}
        self.assertEqual(workers["n1"]["topology"], "background")
        self.assertEqual(workers["n1"]["mode"], "background")

    def test_validator_refuses_main_session_developer_workers(self):
        from dataclasses import replace

        plan, nodes = _plan()
        card = build_authorization_card(plan, nodes, CAPS)
        forged_workers = [
            {
                "node_id": "n1",
                "topology": "dual-visible",
                "mode": "visible",
                "role": "developer",
                "session_source": "main session",
                "limitations": (),
            }
        ]
        with self.assertRaises(ValueError):
            validate_authorization_card_consistency(replace(card, workers=forged_workers))

    def test_from_dict_refuses_main_session_developer_workers(self):
        plan, nodes = _plan()
        record = authorize(build_authorization_card(plan, nodes, CAPS), "AUTHORIZE")
        data = record.to_dict()
        data["workers"] = [
            {
                "node_id": "n1",
                "topology": "dual-visible",
                "mode": "visible",
                "role": "developer",
                "session_source": "主会话",
                "limitations": [],
            }
        ]
        with self.assertRaises(ValueError):
            AuthorizationRecord.from_dict(data)

    def test_from_dict_refuses_background_workers_without_full_disclosure(self):
        plan, nodes = _plan()
        record = authorize(build_authorization_card(plan, nodes, CAPS), "AUTHORIZE")
        data = record.to_dict()
        data["workers"] = [
            {
                "node_id": "n1",
                "topology": "background",
                "mode": "background",
                "role": "developer",
                "session_source": "subagent-1",
                "limitations": ["不可见：不显示"],
            }
        ]
        with self.assertRaises(ValueError):
            AuthorizationRecord.from_dict(data)

    def test_from_dict_refuses_topology_summary_without_workers(self):
        plan, nodes = _plan()
        record = authorize(build_authorization_card(plan, nodes, CAPS), "AUTHORIZE")
        data = record.to_dict()
        data["workers"] = []
        with self.assertRaises(ValueError):
            AuthorizationRecord.from_dict(data)

    def test_legacy_record_without_workers_fields_still_loads_and_validates(self):
        plan, nodes = _plan()
        record = authorize(build_authorization_card(plan, nodes, CAPS), "AUTHORIZE")
        data = record.to_dict()
        data.pop("workers", None)
        data.pop("topology_summary", None)
        payload = {
            key: value
            for key, value in data.items()
            if key
            not in {
                "digest",
                "remote_git_actions_options",
                "remote_git_actions_scope",
                "deploy_authorization",
            }
        }
        # The payload intentionally hashes engine_authorization_digest as ""
        # (the card digest itself cannot be part of its own hash).
        payload["engine_authorization_digest"] = ""
        data["digest"] = _canonical_digest(payload)

        restored = AuthorizationRecord.from_dict(data)
        self.assertEqual(restored.workers, ())
        self.assertTrue(is_authorization_integrity_valid(restored))


if __name__ == "__main__":
    unittest.main()
