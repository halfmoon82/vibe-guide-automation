"""ISSUE-209: the card offers two operational choices, the runtime honours them.

The card used to hard-code both numbers: the idle heartbeat base came from a
constant (10, or 30 once a platform's push wake-up was verified) and the
rotation hard cap was ``DEFAULT_ROTATE_HARD_CAP``.  The product manager now
picks both at publication, so the two things that can silently go wrong are:

* a tier the host cannot support being offered as usable (a 300k cap on a
  200k window never fires: the host compacts first and the cap quietly means
  "never rotate"), and
* an authorized choice being ignored at runtime.

Both are pinned here, together with the compatibility rule: an authorization
issued before the option existed must run exactly as it ran before.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.authorization import (
    DEFAULT_HEARTBEAT_INTERVAL_MINUTES,
    DEFAULT_ROTATE_HARD_CAP_TOKENS,
    AuthorizationRecord,
    authorize,
    build_authorization_card,
    is_authorization_integrity_valid,
    refresh_authorization_card,
)
from vibe_guide.cli import _publish_plan
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import RunSnapshot
from vibe_guide.supervisor import (
    authorization_choices,
    context_window_from_record,
    heartbeat_interval,
    heartbeat_interval_options,
    register_supervisor_address,
    rotate_hard_cap_for_run,
    rotate_hard_cap_options,
    supervisor_preflight,
)

CAPS = AgentCapabilities("codex", True, True, True, False, True, "full")


def _plan(plan_id="p1"):
    node = DAGNode(
        "n1", "N1", [], [], None,
        {"input": "i", "output": "o", "error_behavior": "e",
         "acceptance_example": "a", "adapter_id": "codex", "files": ["a.py"]},
        "planned",
    )
    plan = Plan(plan_id, 1, ".vibe/plans/{}/prd.md".format(plan_id), ["n1"],
                "confirmed_pending_authorization",
                authorization_required=True, nodes=[node])
    return plan, [node]


def _verified_payload(provider="codex"):
    return {"platforms": {provider: {
        "success": [{"evidence_ref": "wake-success"}],
        "failure": [{"evidence_ref": "wake-failure"}],
    }}}


def _session_record(path, tokens, window=None):
    """One Codex-style JSONL record: usage, and optionally the window."""
    info = {"last_token_usage": {"input_tokens": tokens}}
    if window is not None:
        info["model_context_window"] = window
    lines = [{"type": "event_msg", "payload": {"type": "token_count", "info": info}}]
    Path(path).write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    return str(path)


class CardOptionAvailabilityTests(unittest.TestCase):
    """What the card offers, and why an unusable tier is still listed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = ProjectPaths(self.root)

    def _verify(self, provider="codex"):
        (self.root / ".vibe").mkdir(exist_ok=True)
        (self.root / ".vibe/push-wake-verification.json").write_text(
            json.dumps(_verified_payload(provider)), encoding="utf-8"
        )

    def test_thirty_minutes_is_unavailable_until_the_push_wake_is_verified(self):
        options = heartbeat_interval_options(self.paths, "codex")
        self.assertEqual([item["minutes"] for item in options], [10, 30])
        self.assertTrue(options[0]["available"])
        # Not verified: selectable only as a forced choice with its price.
        self.assertTrue(options[1]["available"])
        self.assertTrue(options[1]["forced"])
        self.assertIn("30 分钟", options[1]["cost"])
        self.assertIn("no push wake verification registered", options[1]["reason"])

        self._verify()
        verified = heartbeat_interval_options(self.paths, "codex")
        self.assertTrue(verified[1]["available"], verified)
        self.assertNotIn("forced", verified[1])

    def test_the_adapter_id_and_the_registered_provider_name_one_platform(self):
        # The card is built from the adapter id (``codex``) while the runtime
        # reads the address's provider (``codex-app-visible``).  If the two
        # spellings did not find the same registration, the card would offer
        # nothing while the beat allowed 30 minutes -- the exact split between
        # what was approved and what runs that this option exists to remove.
        self._verify(provider="codex-app-visible")
        self.assertTrue(heartbeat_interval_options(self.paths, "codex")[1]["available"])
        self.assertTrue(
            heartbeat_interval_options(self.paths, "codex-app-visible")[1]["available"]
        )

    def test_a_background_registration_never_answers_for_the_visible_path(self):
        # ``codex-background`` is the same platform but a different wake path;
        # substituting its registration would offer 30 minutes on a visible task
        # whose wake-up was never verified.
        self._verify(provider="codex-background")
        options = heartbeat_interval_options(self.paths, "codex-app-visible")
        self.assertTrue(options[1]["forced"])
        self.assertIn("not registered", options[1]["reason"])

    def test_claude_code_has_a_channel_so_verification_alone_clears_the_force(self):
        # SendMessage is registered as Claude Code's channel; once both
        # evidences are on file, 30 is an ordinary tier, not a forced one.
        options = heartbeat_interval_options(self.paths, "claude-code")
        self.assertTrue(options[1]["forced"])
        self._verify(provider="claude-code")
        options = heartbeat_interval_options(self.paths, "claude-code")
        self.assertTrue(options[1]["available"])
        self.assertNotIn("forced", options[1])

    def test_an_unknown_provider_cannot_be_forced(self):
        options = heartbeat_interval_options(self.paths, None)
        self.assertFalse(options[1]["available"])
        self.assertNotIn("forced", options[1])

    def test_three_hundred_thousand_needs_a_reported_window(self):
        unknown = rotate_hard_cap_options(None)
        self.assertEqual([item["tokens"] for item in unknown],
                         [150000, 200000, 300000, 500000, 800000])
        self.assertTrue(unknown[0]["available"])
        self.assertTrue(unknown[1]["available"])
        self.assertFalse(unknown[2]["available"])
        self.assertIn("window unknown", unknown[2]["reason"])

        small = rotate_hard_cap_options(249036)
        self.assertFalse(small[2]["available"])
        self.assertIn("249,036", small[2]["reason"])

        large = rotate_hard_cap_options(1000000)
        self.assertTrue(large[2]["available"], large)

    def test_large_caps_require_sufficient_reported_windows(self):
        for cap in (500000, 800000):
            for window in (None, cap - 1, cap, 1000000):
                with self.subTest(cap=cap, window=window):
                    option = next(item for item in rotate_hard_cap_options(window)
                                  if item["tokens"] == cap)
                    self.assertEqual(option["available"],
                                     window is not None and window >= cap)
                    if not option["available"]:
                        self.assertTrue(option["reason"])

    def test_the_window_is_read_from_wherever_the_host_writes_it(self):
        path = Path(self.tmp.name) / "rollout.jsonl"
        _session_record(path, 1000, window=249036)
        self.assertEqual(context_window_from_record(path), 249036)
        self.assertIsNone(context_window_from_record(None))
        self.assertIsNone(context_window_from_record(str(path) + ".missing"))

    def test_a_tilde_path_is_expanded(self):
        # The protocol writes the flag as ``~/.codex/sessions/...``; without
        # expansion the record is silently "unknown" and 300k stays unavailable.
        home = Path(self.tmp.name) / "home"
        home.mkdir()
        _session_record(home / "rollout.jsonl", 1000, window=1000000)
        with patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(context_window_from_record("~/rollout.jsonl"), 1000000)


class CardChoiceTests(unittest.TestCase):
    """Recording a choice: bound into the digest, refused when unusable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = ProjectPaths(self.root)

    def _options(self, verified=False, window=None):
        if verified:
            (self.root / ".vibe").mkdir(exist_ok=True)
            (self.root / ".vibe/push-wake-verification.json").write_text(
                json.dumps(_verified_payload()), encoding="utf-8"
            )
        return (heartbeat_interval_options(
                    self.paths, "codex" if verified else None),
                rotate_hard_cap_options(window))

    def test_a_choice_the_host_cannot_support_is_refused_at_publication(self):
        plan, nodes = _plan()
        heartbeat, rotate = self._options()
        with self.assertRaises(ValueError) as raised:
            build_authorization_card(
                plan, nodes, CAPS,
                heartbeat_interval_minutes=30,
                heartbeat_interval_options=heartbeat,
                rotate_hard_cap_options=rotate,
            )
        self.assertIn("unavailable", str(raised.exception))

        with self.assertRaises(ValueError) as raised:
            build_authorization_card(
                plan, nodes, CAPS,
                rotate_hard_cap=300000,
                heartbeat_interval_options=heartbeat,
                rotate_hard_cap_options=rotate,
            )
        self.assertIn("unavailable", str(raised.exception))

    def test_a_choice_outside_the_offered_tiers_is_refused(self):
        plan, nodes = _plan()
        heartbeat, rotate = self._options(verified=True, window=1000000)
        with self.assertRaises(ValueError):
            build_authorization_card(
                plan, nodes, CAPS,
                rotate_hard_cap=250000,
                heartbeat_interval_options=heartbeat,
                rotate_hard_cap_options=rotate,
            )
        with self.assertRaises(ValueError):
            build_authorization_card(
                plan, nodes, CAPS,
                heartbeat_interval_minutes=45,
                heartbeat_interval_options=heartbeat,
                rotate_hard_cap_options=rotate,
            )

    def test_two_cards_differing_in_one_choice_have_different_digests(self):
        plan, nodes = _plan()
        heartbeat, rotate = self._options(verified=True, window=1000000)
        ten = build_authorization_card(
            plan, nodes, CAPS, heartbeat_interval_minutes=10,
            rotate_hard_cap=200000,
            heartbeat_interval_options=heartbeat,
            rotate_hard_cap_options=rotate,
        )
        thirty = build_authorization_card(
            plan, nodes, CAPS, heartbeat_interval_minutes=30,
            rotate_hard_cap=200000,
            heartbeat_interval_options=heartbeat,
            rotate_hard_cap_options=rotate,
        )
        large = build_authorization_card(
            plan, nodes, CAPS, heartbeat_interval_minutes=10,
            rotate_hard_cap=300000,
            heartbeat_interval_options=heartbeat,
            rotate_hard_cap_options=rotate,
        )
        self.assertNotEqual(ten.digest, thirty.digest)
        self.assertNotEqual(ten.digest, large.digest)

    def test_a_recorded_choice_survives_the_record_round_trip(self):
        plan, nodes = _plan()
        heartbeat, rotate = self._options(verified=True, window=1000000)
        card = build_authorization_card(
            plan, nodes, CAPS, heartbeat_interval_minutes=30, rotate_hard_cap=300000,
            heartbeat_interval_options=heartbeat, rotate_hard_cap_options=rotate,
        )
        record = authorize(card, "AUTHORIZE")
        restored = AuthorizationRecord.from_dict(record.to_dict())
        self.assertEqual(restored.heartbeat_interval_minutes, 30)
        self.assertEqual(restored.rotate_hard_cap, 300000)
        self.assertTrue(is_authorization_integrity_valid(restored))

    def test_large_caps_are_signed_and_round_trip_without_changing_default(self):
        plan, nodes = _plan()
        heartbeat, rotate = self._options(window=1000000)
        digests = set()
        for cap in (500000, 800000):
            with self.subTest(cap=cap):
                card = build_authorization_card(
                    plan, nodes, CAPS, rotate_hard_cap=cap,
                    heartbeat_interval_options=heartbeat,
                    rotate_hard_cap_options=rotate,
                )
                restored = AuthorizationRecord.from_dict(
                    authorize(card, "AUTHORIZE").to_dict())
                self.assertEqual(restored.rotate_hard_cap, cap)
                self.assertTrue(is_authorization_integrity_valid(restored))
                digests.add(card.digest)
        self.assertEqual(len(digests), 2)
        self.assertEqual(DEFAULT_ROTATE_HARD_CAP_TOKENS, 200000)

    def test_a_record_without_the_choices_serializes_exactly_as_before(self):
        plan, nodes = _plan()
        card = build_authorization_card(plan, nodes, CAPS)
        record = authorize(card, "AUTHORIZE")
        data = record.to_dict()
        self.assertNotIn("heartbeat_interval_minutes", data)
        self.assertNotIn("rotate_hard_cap", data)
        # The card carries no option keys either when nothing was observed:
        # a pre-209 card must keep its exact shape, not gain two empty lists.
        self.assertNotIn("heartbeat_interval_options", card.to_dict())
        self.assertNotIn("rotate_hard_cap_options", card.to_dict())
        self.assertTrue(is_authorization_integrity_valid(record))
        self.assertEqual(AuthorizationRecord.from_dict(data).heartbeat_interval_minutes, None)

    def test_a_refresh_carries_the_approved_choice(self):
        plan, nodes = _plan()
        heartbeat, rotate = self._options(verified=True, window=1000000)
        card = build_authorization_card(
            plan, nodes, CAPS, heartbeat_interval_minutes=30, rotate_hard_cap=150000,
            heartbeat_interval_options=heartbeat, rotate_hard_cap_options=rotate,
        )
        refreshed = refresh_authorization_card(plan, nodes, card)
        self.assertEqual(refreshed.heartbeat_interval_minutes, 30)
        self.assertEqual(refreshed.rotate_hard_cap, 150000)
        # Availability is carried too, so the refreshed card still says which
        # tiers are unusable and why.
        self.assertEqual(len(refreshed.heartbeat_interval_options), 2)


class PublishedCardTests(unittest.TestCase):
    """Publication records the default and refuses an unreachable tier."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _spec(self, **choices):
        spec = {
            "title": "options", "objective": "record the two choices",
            "project_id": "proj-1",
            "capabilities": {
                "agent_id": "codex", "shell": True, "subprocess": True,
                "worktree": True, "background": False, "session_resume": True,
                "level": "full",
            },
            "allowed_actions": ["develop", "test", "review"],
            "nodes": [{
                "id": "n1", "title": "N1", "depends_on": [], "integration_after": [],
                "parallel_group": "core", "status": "planned",
                "contract": {
                    "input": "request", "output": "delivery",
                    "error_behavior": "blocked_unknown", "acceptance_example": "pass",
                    "writer": "writer-n1", "worktree": ".vibe/worktrees/n1",
                    "branch": "codex/n1", "files": ["n1.py"],
                    "actions": ["develop", "test", "review"],
                },
            }],
        }
        spec.update(choices)
        path = self.root / "spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def test_publication_records_the_defaults_and_lists_every_tier(self):
        _, _, card = _publish_plan(ProjectPaths(self.root), "options-plan", self._spec())
        self.assertEqual(card.heartbeat_interval_minutes,
                         DEFAULT_HEARTBEAT_INTERVAL_MINUTES)
        self.assertEqual(card.rotate_hard_cap, DEFAULT_ROTATE_HARD_CAP_TOKENS)
        self.assertEqual([item["minutes"] for item in card.heartbeat_interval_options],
                         [10, 30])
        self.assertEqual([item["tokens"] for item in card.rotate_hard_cap_options],
                         [150000, 200000, 300000, 500000, 800000])
        # The unusable tier is on the card, with its reason.
        self.assertFalse(card.rotate_hard_cap_options[2]["available"])
        self.assertTrue(card.rotate_hard_cap_options[2]["reason"])
        stored = card.to_dict()
        self.assertEqual(len(stored["heartbeat_interval_options"]), 2)
        self.assertEqual(len(stored["rotate_hard_cap_options"]), 5)

    def test_publication_refuses_a_tier_the_host_cannot_reach(self):
        # The check lives in the card builder, but publication is the only
        # caller that has observed availability; a wiring mistake there would
        # let a product manager approve a cap the host can never reach.
        with self.assertRaises(ValueError) as raised:
            _publish_plan(
                ProjectPaths(self.root), "bad-plan", self._spec(rotate_hard_cap=300000)
            )
        self.assertIn("rotation hard cap is unavailable", str(raised.exception))

    def test_large_caps_publish_with_window_evidence_and_refuse_without_it(self):
        for cap in (500000, 800000):
            with self.subTest(cap=cap):
                with self.assertRaisesRegex(ValueError, "rotation hard cap is unavailable"):
                    _publish_plan(
                        ProjectPaths(self.root), "unknown-{}".format(cap),
                        self._spec(rotate_hard_cap=cap))
                record = self.root / "large-window.jsonl"
                _session_record(record, 1000, window=1000000)
                _, _, card = _publish_plan(
                    ProjectPaths(self.root), "large-{}".format(cap),
                    self._spec(rotate_hard_cap=cap), session_record=str(record))
                self.assertEqual(card.rotate_hard_cap, cap)
                stored = json.loads((self.root / ".vibe/plans" /
                                     "large-{}".format(cap) /
                                     "authorization-card.json").read_text())
                self.assertEqual(stored["rotate_hard_cap"], cap)

    def test_a_published_choice_is_read_back_from_the_plan(self):
        self.root.joinpath(".vibe").mkdir(exist_ok=True)
        self.root.joinpath(".vibe/push-wake-verification.json").write_text(
            json.dumps(_verified_payload()), encoding="utf-8"
        )
        record = self.root / "rollout.jsonl"
        _session_record(record, 1000, window=1000000)
        _, _, card = _publish_plan(
            ProjectPaths(self.root), "chosen-plan",
            self._spec(heartbeat_interval_minutes=30, rotate_hard_cap=300000),
            session_record=str(record),
        )
        self.assertEqual(card.heartbeat_interval_minutes, 30)
        self.assertEqual(card.rotate_hard_cap, 300000)
        stored = json.loads(
            (self.root / ".vibe/plans/chosen-plan/authorization-card.json").read_text()
        )
        self.assertEqual(stored["heartbeat_interval_minutes"], 30)
        self.assertEqual(stored["rotate_hard_cap"], 300000)


class RuntimeChoiceTests(unittest.TestCase):
    """The preflight and the beat use the authorized value, or say why not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = ProjectPaths(self.root)
        self.snapshot = RunSnapshot(
            "run-1", "p1", 1, "running", {"n1": {"status": "review"}}, {},
            authorization_digest="a" * 64, node_contract_digest="b" * 64,
        )
        directory = self.paths.vibe / "runs/run-1"
        directory.mkdir(parents=True)
        (directory / "state.json").write_text("{}")
        (self.paths.vibe / "state.json").write_text("{}")
        self.addCleanup(patch.stopall)
        patch("vibe_guide.supervisor.load_snapshot", return_value=self.snapshot).start()
        patch("vibe_guide.adapters.task_provider.require_entry").start()
        register_supervisor_address(
            self.paths, "run-1",
            {"provider": "codex", "session_id": "shift-1", "host": "local"},
        )
        self.record = self.root / "rollout.jsonl"

    def _authorization(self, **choices):
        directory = self.paths.vibe / "plans" / "p1"
        directory.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 2, "plan_id": "p1"}
        payload.update(choices)
        (directory / "authorization.json").write_text(json.dumps(payload), encoding="utf-8")

    def _verify(self):
        (self.root / ".vibe/push-wake-verification.json").write_text(
            json.dumps(_verified_payload()), encoding="utf-8"
        )

    def test_a_run_without_a_recorded_choice_keeps_the_old_preflight(self):
        _session_record(self.record, 250000, window=1000000)
        self._authorization()
        legacy = supervisor_preflight(self.paths, "run-1", str(self.record))
        explicit = supervisor_preflight(
            self.paths, "run-1", str(self.record), context_hard_cap=200000
        )
        self.assertEqual(legacy, explicit)
        self.assertNotIn("rotate_hard_cap_downgrade", legacy)
        self.assertEqual(authorization_choices(self.paths, "run-1"), {})

    def test_the_authorized_cap_applies_when_the_window_reaches_it(self):
        _session_record(self.record, 250000, window=1000000)
        self._authorization(rotate_hard_cap=300000)
        payload = supervisor_preflight(self.paths, "run-1", str(self.record))
        self.assertEqual(payload["context_hard_cap"], 300000)
        self.assertNotIn("rotate_hard_cap_downgrade", payload)

    def test_large_caps_apply_and_downgrade_for_a_smaller_takeover_window(self):
        for cap in (500000, 800000):
            for window in (1000000, cap, cap - 1, None):
                with self.subTest(cap=cap, window=window):
                    _session_record(self.record, 250000, window=window)
                    self._authorization(rotate_hard_cap=cap)
                    actual, reason = rotate_hard_cap_for_run(
                        self.paths, "run-1", str(self.record))
                    sufficient = window is not None and window >= cap
                    self.assertEqual(actual, cap if sufficient else 200000)
                    if sufficient:
                        self.assertIsNone(reason)
                    else:
                        self.assertIn("{:,}".format(cap), reason)

    def test_a_smaller_takeover_window_falls_back_and_says_so(self):
        _session_record(self.record, 250000, window=249036)
        self._authorization(rotate_hard_cap=300000)
        payload = supervisor_preflight(self.paths, "run-1", str(self.record))
        self.assertEqual(payload["context_hard_cap"], 200000)
        self.assertIn("300,000", payload["rotate_hard_cap_downgrade"])
        self.assertIn("300,000", payload["reason"])

    def test_a_choice_above_the_default_needs_a_window_at_runtime_too(self):
        _session_record(self.record, 250000)  # no window reported at all
        self._authorization(rotate_hard_cap=300000)
        cap, reason = rotate_hard_cap_for_run(self.paths, "run-1", str(self.record))
        self.assertEqual(cap, 200000)
        self.assertIn("no context window", reason)

    def test_the_chosen_interval_is_used_and_never_upgraded(self):
        self._verify()
        self._authorization(heartbeat_interval_minutes=10)
        self.assertEqual(heartbeat_interval(self.paths, "run-1", decision_only=False)["minutes"], 10)
        self._authorization(heartbeat_interval_minutes=30)
        self.assertEqual(heartbeat_interval(self.paths, "run-1", decision_only=False)["minutes"], 30)

    def test_a_value_outside_the_card_vocabulary_reads_as_no_choice(self):
        # The record is signed, but a corrupted or hand-edited file must not be
        # able to set a cap the vocabulary does not contain: 999,999 is exactly
        # the "silently never rotate" this option exists to prevent.
        self._authorization(heartbeat_interval_minutes=5, rotate_hard_cap=999999)
        self.assertEqual(authorization_choices(self.paths, "run-1"), {})
        _session_record(self.record, 250000, window=1000000)
        payload = supervisor_preflight(self.paths, "run-1", str(self.record))
        self.assertEqual(payload["context_hard_cap"], 200000)
        self.assertNotIn("rotate_hard_cap_downgrade", payload)
        interval = heartbeat_interval(self.paths, "run-1", decision_only=False)
        self.assertEqual(interval["minutes"], 10)

    def test_an_unknown_token_count_still_names_the_downgrade(self):
        # A record the host is still writing has no usage line yet.  The state
        # is unknown, but the cap downgrade must not be lost with it.
        self.record.write_text(
            json.dumps({"type": "event_msg", "payload": {
                "type": "token_count", "info": {"model_context_window": 249036}}}) + "\n",
            encoding="utf-8",
        )
        self._authorization(rotate_hard_cap=300000)
        payload = supervisor_preflight(self.paths, "run-1", str(self.record))
        self.assertEqual(payload["state"], "unknown")
        self.assertIn("300,000", payload["reason"])

    def test_a_revoked_verification_falls_back_and_names_the_choice(self):
        self._verify()
        self._authorization(heartbeat_interval_minutes=30)  # card carries no force
        (self.root / ".vibe/push-wake-verification.json").unlink()
        interval = heartbeat_interval(self.paths, "run-1", decision_only=False)
        self.assertEqual(interval["minutes"], 10)
        self.assertIn("30", interval["reason"])

    def test_the_old_automatic_upgrade_still_applies_without_a_choice(self):
        self._verify()
        self._authorization()
        self.assertEqual(heartbeat_interval(self.paths, "run-1", decision_only=False)["minutes"], 30)


if __name__ == "__main__":
    unittest.main()
