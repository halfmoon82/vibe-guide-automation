"""The mailbox template must satisfy the fields the dispatch reader binds.

Regression for run-91c6d4ab (2026-10-30): the ``create`` template carried only
``threadId`` while the reader requires a host identity, and the ``locate``
template carried ``status`` while the reader requires ``located``.  A
supervisor that copied the printed template therefore produced a result the
dispatcher refused with ``dispatch_contract_invalid`` after the visible task
had already been created, and the consumed result could not be rewritten.
"""

import unittest

from vibe_guide.runners.provider_action import validate_result_shape
from vibe_guide.supervisor import _template_payload


class CreateTemplateTests(unittest.TestCase):
    def test_template_carries_task_and_host_identity(self):
        example = _template_payload("create", "developer")
        validate_result_shape("create", example, role="developer")
        binding = example["binding"]
        self.assertIn("threadId", binding)
        self.assertIn("hostId", binding)

    def test_result_without_host_identity_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            validate_result_shape(
                "create", {"binding": {"threadId": "thread-1"}}, role="developer",
            )
        self.assertIn("host", str(caught.exception))

    def test_legacy_host_key_is_still_accepted(self):
        validate_result_shape(
            "create",
            {"binding": {"task_id": "thread-1", "host": "local"}},
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


if __name__ == "__main__":
    unittest.main()
