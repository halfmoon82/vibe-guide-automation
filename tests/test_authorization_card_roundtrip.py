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
import unittest

from vibe_guide.authorization import (
    _REMOTE_GIT_ACTIONS_SCOPE,
    build_authorization_card,
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


if __name__ == "__main__":
    unittest.main()
