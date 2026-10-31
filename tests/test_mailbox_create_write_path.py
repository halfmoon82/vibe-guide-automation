"""A create result that the reader would refuse must fail before the write.

Goes through ``complete_mailbox_action`` and the provider store, not only the
shape function: the point is that a rejected result leaves the request pending
and stores nothing, because a stored result cannot be rewritten.
"""
import unittest

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.runners.provider_action import complete_mailbox_action
from tests.test_supervisor_runtime_consolidation import RUN, _Case


class CreateWritePathTests(_Case):
    def assertRefusedAndPending(self, payload, needle):
        action = self.request("create")
        with self.assertRaises(ValueError) as caught:
            complete_mailbox_action(self.paths, RUN, action["action_id"], payload)
        self.assertIn(needle, str(caught.exception))
        store = ProviderActionStore(self.paths)
        self.assertIsNone(store.result(action["action_id"]))
        self.assertIn(action["action_id"], [a["action_id"] for a in store.pending(RUN)])

    def test_first_key_the_reader_binds_on_must_be_valid(self):
        self.assertRefusedAndPending(
            {"binding": {"threadId": 5, "task_id": "t", "hostId": "h"}}, "task identity")

    def test_padded_fullwidth_and_partial_placeholders_are_refused_unwritten(self):
        for value in ("＜填写＞", " <填写> ", "<填写>-1"):
            with self.subTest(value=value):
                self.assertRefusedAndPending(
                    {"binding": {"threadId": value, "hostId": "local"}}, "placeholder")

    def test_failed_create_is_stored_so_the_reader_reports_it(self):
        action = self.request("create")
        payload = {"failed": True, "reason": "host refused"}
        self.assertTrue(complete_mailbox_action(self.paths, RUN, action["action_id"], payload, native=False))
        self.assertEqual(ProviderActionStore(self.paths).result(action["action_id"])["reason"], "host refused")

    def test_legacy_task_id_and_host_binding_is_still_stored(self):
        action = self.request("create")
        payload = {"binding": {"task_id": "thread-1", "host": "local"}}
        self.assertTrue(complete_mailbox_action(self.paths, RUN, action["action_id"], payload, native=False))


if __name__ == "__main__":
    unittest.main()
