"""The mailbox template must satisfy the fields the dispatch reader binds.

Regression for run-91c6d4ab (2026-10-30): the ``create`` template carried only
``threadId`` while the reader requires a host identity, and the ``locate``
template carried ``status`` while the reader requires ``located``.  A
supervisor that copied the printed template therefore produced a result the
dispatcher refused with ``dispatch_contract_invalid`` after the visible task
had already been created, and the consumed result could not be rewritten.
"""

import json
import unittest

from vibe_guide.runners.provider_action import TEMPLATE_PLACEHOLDER, validate_result_shape
from vibe_guide.supervisor import _template_payload


def filled(example):
    """The template with every placeholder replaced by a real value."""
    return json.loads(json.dumps(example, ensure_ascii=False).replace(TEMPLATE_PLACEHOLDER, "real-value"))


class CreateTemplateTests(unittest.TestCase):
    def test_filled_template_carries_task_and_host_identity(self):
        example = filled(_template_payload("create", "developer"))
        binding = example["binding"]
        self.assertIn("threadId", binding)
        self.assertIn("hostId", binding)
        validate_result_shape("create", example, role="developer")

    def test_unedited_template_is_refused_not_stored(self):
        # A stored placeholder binding points at a session that never existed
        # and cannot be rewritten afterwards, so it must fail before the write.
        with self.assertRaises(ValueError) as caught:
            validate_result_shape(
                "create", _template_payload("create", "developer"), role="developer",
            )
        self.assertIn("placeholder", str(caught.exception))

    def test_result_without_host_identity_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            validate_result_shape(
                "create", {"binding": {"threadId": "thread-1"}}, role="developer",
            )
        self.assertIn("host", str(caught.exception))

    def test_identity_the_reader_does_not_read_is_refused(self):
        # The reader binds on threadId or task_id only; accepting
        # clientThreadId here only moved the failure past the write.
        with self.assertRaises(ValueError):
            validate_result_shape(
                "create", {"binding": {"clientThreadId": "x", "host": "h"}},
                role="developer",
            )

    def test_legacy_host_key_is_still_accepted(self):
        validate_result_shape(
            "create",
            {"binding": {"task_id": "thread-1", "host": "local"}},
            role="developer",
        )

    def test_failed_create_is_accepted_with_a_reason(self):
        # The reader turns failed / status=failed into ProviderUnavailable for
        # every operation, so create must be able to report an honest failure.
        validate_result_shape("create", {"failed": True, "reason": "host refused"}, role="developer")
        validate_result_shape("create", {"status": "failed", "reason": "host refused"}, role="developer")

    def test_failed_create_may_carry_the_fields_the_reader_consumes(self):
        # ``task_created: false`` is what the parameter-recovery gate requires;
        # ``login_required`` and friends decide external vs engineering.
        for extra in ({"task_created": False}, {"login_required": True},
                      {"credential_required": True}, {"permission_required": True},
                      {"reason_class": "capacity"}):
            with self.subTest(extra=extra):
                validate_result_shape(
                    "create", {"status": "failed", "reason": "x", **extra}, role="developer",
                )

    def test_failed_create_still_refuses_unknown_fields(self):
        with self.assertRaises(ValueError) as caught:
            validate_result_shape(
                "create", {"status": "failed", "reason": "x", "bogus": 1}, role="developer",
            )
        self.assertIn("bogus", str(caught.exception))

    def test_failed_create_without_a_reason_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            validate_result_shape("create", {"failed": True}, role="developer")
        self.assertIn("reason", str(caught.exception))

    def test_identity_is_judged_on_the_key_the_reader_binds(self):
        # The reader takes ``threadId or task_id``; a valid task_id must not
        # hide a non-string threadId that the reader would pick first.
        with self.assertRaises(ValueError) as caught:
            validate_result_shape(
                "create",
                {"binding": {"threadId": 5, "task_id": "t", "hostId": "h"}},
                role="developer",
            )
        self.assertIn("task identity", str(caught.exception))

    def test_blank_identity_is_refused(self):
        for binding in ({"threadId": "t", "hostId": "  "}, {"threadId": "  ", "hostId": "h"}):
            with self.subTest(binding=binding):
                with self.assertRaises(ValueError):
                    validate_result_shape("create", {"binding": binding}, role="developer")

    def test_placeholder_variants_are_refused(self):
        for value in ("＜填写＞", " <填写> ", "<填写>-1", "thread-<填写>"):
            for field in ("task", "host"):
                binding = {"threadId": "thread-1", "hostId": "local"}
                binding["threadId" if field == "task" else "hostId"] = value
                with self.subTest(value=value, field=field):
                    with self.assertRaises(ValueError) as caught:
                        validate_result_shape("create", {"binding": binding}, role="developer")
                    self.assertIn("placeholder", str(caught.exception))

    def test_real_values_close_to_the_placeholder_are_not_refused(self):
        validate_result_shape(
            "create",
            {"binding": {"threadId": "<fill>", "hostId": "<h>"}},
            role="developer",
        )


class LocateTemplateTests(unittest.TestCase):
    def test_template_uses_the_located_flag(self):
        example = _template_payload("locate", "developer")
        self.assertIs(example.get("located"), True)
        validate_result_shape("locate", example, role="developer")

    def test_result_without_located_flag_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            validate_result_shape("locate", {"status": "located"}, role="developer")
        self.assertIn("located", str(caught.exception))

    def test_failed_observation_is_accepted_with_a_reason(self):
        # A supervisor that could not locate the task must be able to say so
        # instead of being pushed into claiming success.
        validate_result_shape(
            "locate", {"status": "error", "reason": "tool unavailable"}, role="developer",
        )
        validate_result_shape(
            "locate", {"failed": True, "reason": "tool unavailable"}, role="developer",
        )

    def test_failed_observation_without_a_reason_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            validate_result_shape("locate", {"failed": True}, role="developer")
        self.assertIn("reason", str(caught.exception))


class VisibilityTemplateTests(unittest.TestCase):
    def test_template_carries_both_flags(self):
        example = _template_payload("visibility", "developer")
        self.assertIs(example.get("visible"), True)
        self.assertIs(example.get("direct_enter"), True)
        validate_result_shape("visibility", example, role="developer")

    def test_result_without_direct_enter_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            validate_result_shape("visibility", {"visible": True}, role="developer")
        self.assertIn("direct_enter", str(caught.exception))

    def test_failed_observation_is_accepted_with_a_reason(self):
        validate_result_shape(
            "visibility", {"failed": True, "reason": "thread not visible"},
            role="developer",
        )


if __name__ == "__main__":
    unittest.main()
