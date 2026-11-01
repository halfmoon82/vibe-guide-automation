"""Every key a ``_record`` call site writes must survive ``_sanitize_event_data``.

``state._EVENT_DATA_KEYS`` is an allow list: a key outside it is dropped
silently when the event is persisted, so evidence a call site meant to leave
(``action_ref``, later ``commits``) never reaches ``events.jsonl``.  This scans
the source for literal-dict ``_record`` call sites and fails on the first
unlisted key.
"""
import ast
import unittest
from pathlib import Path

from vibe_guide import state

PACKAGE = Path(state.__file__).resolve().parent


def record_sites():
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_record" and len(node.args) >= 3
                    and isinstance(node.args[2], ast.Dict)):
                yield path, node


def kept(key):
    normalized = state._normalize_data_key(key)
    return normalized in state._EVENT_DATA_KEYS and not state._is_sensitive_data_key(key)


class EventKeyAllowList(unittest.TestCase):
    def test_every_literal_record_key_is_in_the_allow_list(self):
        sites = list(record_sites())
        self.assertGreater(len(sites), 50, "scan found too few _record call sites")
        dropped = sorted({
            (key.value, "{}:{}".format(path.name, node.lineno))
            for path, node in sites for key in node.args[2].keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
            and not state._is_sensitive_data_key(key.value) and not kept(key.value)
        })
        self.assertEqual(dropped, [])

    def test_an_unlisted_key_is_what_the_scan_reports(self):
        # The detector itself: a key outside the table is "not kept".
        self.assertFalse(kept("surely_not_an_allowed_key"))
        self.assertEqual(state._sanitize_event_data({"run_id": "r", "surely_not_an_allowed_key": 1}),
                         {"run_id": "r"})

    def test_action_ref_and_commits_are_persisted(self):
        data = state._sanitize_event_data({"run_id": "r", "action_ref": "dispatch-1", "commits": ["a1"]})
        self.assertEqual(data, {"run_id": "r", "action_ref": "dispatch-1", "commits": ["a1"]})


if __name__ == "__main__":
    unittest.main()
