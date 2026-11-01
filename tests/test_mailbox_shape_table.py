"""One field table drives the mailbox template, write-time check, hints and docs.

Cluster A of run-recovery-5-2-5: the template, ``validate_result_shape``, the
error hint and the prd-guide §6.2 table used to be four hand-kept copies.  The
copies are now read from ``MAILBOX_FIELD_TABLE``; these tests fail when one of
them is changed without the table.
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.protocols import load_protocol
from vibe_guide.runners import provider_action as pa
from vibe_guide.runners.provider_action import (
    DOC_TABLE_BEGIN,
    DOC_TABLE_END,
    MAILBOX_FIELD_TABLE,
    TEMPLATE_PLACEHOLDER,
    ProviderActionRunner,
    complete_mailbox_action,
    mailbox_example,
    render_doc_table,
    validate_result_shape,
)
from vibe_guide.state import ERROR_TEXT_LIMIT, _sanitize_event_data, bounded_error_text
from vibe_guide.supervisor import _template_payload, mailbox_result_template
from tests.test_supervisor_runtime_consolidation import RUN, _Case


def filled(example):
    return json.loads(json.dumps(example, ensure_ascii=False).replace(TEMPLATE_PLACEHOLDER, "real-value"))


class TableDrivesEveryCopy(unittest.TestCase):
    def test_every_row_passes_the_write_check_once_filled(self):
        for operation, role, branch, example, _ in MAILBOX_FIELD_TABLE:
            with self.subTest(operation=operation, role=role, branch=branch):
                validate_result_shape(operation, filled(example), role=role or "developer")

    def test_template_is_the_table_row(self):
        for operation, role in (("create", "developer"), ("locate", "developer"),
                                ("visibility", "developer"), ("resume", "developer"),
                                ("wait", "developer"), ("wait", "reviewer")):
            with self.subTest(operation=operation, role=role):
                self.assertEqual(_template_payload(operation, role),
                                 mailbox_example(operation, role, "ok"))

    def test_changing_a_row_turns_template_check_and_doc_red(self):
        # Drop ``hostId`` from the create row: the template, the check and the
        # doc table all follow, so they stay consistent with each other...
        rows = tuple(
            (op, role, branch, {"binding": {"threadId": TEMPLATE_PLACEHOLDER}}, note)
            if (op, branch) == ("create", "ok") else (op, role, branch, ex, note)
            for op, role, branch, ex, note in MAILBOX_FIELD_TABLE
        )
        with mock.patch.object(pa, "MAILBOX_FIELD_TABLE", rows):
            self.assertNotIn("hostId", json.dumps(_template_payload("create", "developer")))
            self.assertNotIn("hostId", pa.render_doc_table())
        # ... and the shipped doc no longer equals what the changed table renders.
        self.assertNotEqual(self.shipped_doc_table(), rows_render(rows))

    @staticmethod
    def shipped_doc_table():
        text = load_protocol("prd-guide")
        return text[text.index(DOC_TABLE_BEGIN) + len(DOC_TABLE_BEGIN):text.index(DOC_TABLE_END)].strip()

    def test_prd_guide_table_is_the_rendered_table(self):
        self.assertEqual(self.shipped_doc_table(), render_doc_table())

    def test_prd_guide_rows_hold_each_example_verbatim(self):
        doc = self.shipped_doc_table().splitlines()
        for operation, role, branch, example, _ in MAILBOX_FIELD_TABLE:
            wanted = json.dumps(example, ensure_ascii=False, sort_keys=True)
            self.assertTrue(
                any(line.startswith("| `{}`".format(operation)) and "`{}`".format(wanted) in line
                    for line in doc),
                (operation, role, branch))


def rows_render(rows):
    with mock.patch.object(pa, "MAILBOX_FIELD_TABLE", rows):
        return pa.render_doc_table()


class PlaceholderJudgement(unittest.TestCase):
    VARIANTS = ("<填写>", "＜填写＞", " <填写> ", "<填写>-1", "　＜填写＞\n")

    def test_every_operation_refuses_every_placeholder_spelling(self):
        shapes = {
            "create": lambda v: {"binding": {"threadId": v, "hostId": "h"}},
            "create-host": lambda v: {"binding": {"threadId": "t", "hostId": v}},
            "failed": lambda v: {"failed": True, "reason": v},
            "locate-failed": lambda v: {"failed": True, "error": v},
        }
        for name, build in shapes.items():
            operation = "create" if name.startswith("create") or name == "failed" else "locate"
            for value in self.VARIANTS:
                with self.subTest(shape=name, value=value):
                    with self.assertRaises(ValueError) as caught:
                        validate_result_shape(operation, build(value), role="developer")
                    self.assertIn("placeholder", str(caught.exception))

    def test_blank_identity_is_missing_not_a_value(self):
        for value in ("", "  ", "　"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    validate_result_shape("create", {"binding": {"threadId": value, "hostId": "h"}},
                                          role="developer")
                self.assertIn("task identity", str(caught.exception))


class FailedBranches(unittest.TestCase):
    def test_create_locate_visibility_accept_failed_with_reason(self):
        for operation in ("create", "locate", "visibility"):
            with self.subTest(operation=operation):
                validate_result_shape(operation, {"failed": True, "reason": "host refused"},
                                      role="developer")

    def test_failed_without_reason_is_refused_with_the_failed_example(self):
        for operation in ("create", "locate", "visibility"):
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError) as caught:
                    validate_result_shape(operation, {"failed": True}, role="developer")
                self.assertIn("reason is required", str(caught.exception))
                self.assertIn('"failed": true', str(caught.exception))


class ErrorHints(unittest.TestCase):
    def test_every_rejection_names_the_expected_shape_with_a_copyable_example(self):
        for operation, payload in (("create", {}), ("locate", {}), ("visibility", {}),
                                   ("resume", {}), ("wait", {})):
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError) as caught:
                    validate_result_shape(operation, payload, role="developer")
                message = str(caught.exception)
                self.assertIn("expected shape", message)
                example = mailbox_example(operation, "developer", "ok")
                self.assertIn(json.dumps(example, ensure_ascii=False, sort_keys=True), message)

    def test_navigated_locate_names_located(self):
        with self.assertRaises(ValueError) as caught:
            validate_result_shape("locate", {"navigated": True}, role="developer")
        message = str(caught.exception)
        self.assertIn("navigated", message)
        self.assertIn('{"located": true}', message)


class WritePath(_Case):
    def test_navigated_locate_is_refused_and_no_result_file_exists(self):
        action = self.request("locate")
        with self.assertRaises(ValueError) as caught:
            complete_mailbox_action(self.paths, RUN, action["action_id"], {"navigated": True})
        self.assertIn("located", str(caught.exception))
        store = ProviderActionStore(self.paths)
        self.assertIsNone(store.result(action["action_id"]))
        results = list((self.root / ".vibe" / "provider-actions").rglob("*%s*" % action["action_id"]))
        self.assertEqual([p.name for p in results if "result" in str(p)], [])

    def test_template_command_output_lists_the_failed_alternative(self):
        action = self.request("locate")
        out = mailbox_result_template(self.paths, RUN, action["action_id"])
        self.assertEqual(out["template"], {"located": True})
        self.assertEqual(out["alternatives"]["failed"], {"failed": True, "reason": TEMPLATE_PLACEHOLDER})


class IndependentReasonCodes(unittest.TestCase):
    def runner(self):
        return ProviderActionRunner(mock.Mock(), "a", "claude-code")

    def test_blank_worktree_has_its_own_reason_code(self):
        contract = {"project_id": "p", "worktree": "", "managed_root": "/tmp/x", "branch": "b",
                    "base_sha": "a" * 40}
        with self.assertRaises(Exception) as caught:
            self.runner()._validate_v39_create_binding(contract, Path("/tmp/x/w"))
        self.assertEqual(caught.exception.reason_code, "worktree_missing")
        # The channel the dispatcher already reads a provider failure's code from.
        self.assertEqual(caught.exception.observation["reason_code"], "worktree_missing")

    def test_other_path_failures_carry_no_mailbox_code(self):
        contract = {"project_id": "p", "worktree": "/tmp/x/w", "managed_root": "", "branch": "b",
                    "base_sha": "a" * 40}
        with self.assertRaises(Exception) as caught:
            self.runner()._validate_v39_create_binding(contract, Path("/tmp/x/w"))
        self.assertIsNone(getattr(caught.exception, "reason_code", None))


class ContractErrorEventPersistence(unittest.TestCase):
    def test_error_type_error_and_action_ref_survive_sanitising(self):
        secret = "sk-" + "a" * 40
        long_text = "worktree bad token=" + secret + " " + "x" * 400
        data = _sanitize_event_data({
            "run_id": "run-1", "node_id": "n", "role": "developer", "phase": "start",
            "reason_code": "mailbox_host_missing", "action_ref": "dispatch-abc",
            "error_type": "ValueError", "error": long_text,
        })
        self.assertEqual(data["action_ref"], "dispatch-abc")
        self.assertEqual(data["error_type"], "ValueError")
        self.assertEqual(data["reason_code"], "mailbox_host_missing")
        self.assertLessEqual(len(data["error"]), ERROR_TEXT_LIMIT)
        self.assertNotIn(secret, data["error"])
        self.assertTrue(data["error"].startswith("worktree bad token="))

    def test_scrub_runs_before_the_cut(self):
        secret = "sk-" + "b" * 40
        text = "y" * (ERROR_TEXT_LIMIT - 10) + secret
        self.assertNotIn("sk-", bounded_error_text(text))

    def test_commits_key_is_kept(self):
        data = _sanitize_event_data({"run_id": "run-1", "commits": ["abc"]})
        self.assertEqual(data["commits"], ["abc"])


if __name__ == "__main__":
    unittest.main()
