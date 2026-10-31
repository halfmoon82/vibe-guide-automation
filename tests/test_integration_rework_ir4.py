"""IR-4 regression: the integration aggregation's ``git diff --check`` stays clean.

Recorded failure (integration-review reviewer g5, IR-4 / P2): the aggregated
``git diff --check 7a16035...HEAD`` reported
``tests/test_landing_required_before_review.py:198: new blank line at EOF``,
so the whitespace defect is pinned here against every file this node owns,
with the same rules ``git diff --check`` applies to added lines: no trailing
whitespace and no blank line at EOF.
"""
import unittest
from pathlib import Path

NODE_FILES = (
    "vibe_guide/adapters/task_provider.py",
    "vibe_guide/evidence.py",
    "vibe_guide/monitor.py",
    "vibe_guide/scope_landing.py",
    "tests/test_delivery_self_report_shape.py",
    "tests/test_landing_required_before_review.py",
)


class Ir4DiffCheckRegressionTests(unittest.TestCase):
    def test_node_files_have_no_diff_check_whitespace_errors(self):
        root = Path(__file__).resolve().parent.parent
        for relative in NODE_FILES:
            path = root / relative
            with self.subTest(file=relative):
                self.assertTrue(path.is_file(), "node file is missing")
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.endswith("\n"),
                                "file must end with a newline")
                self.assertFalse(text.endswith("\n\n"),
                                 "blank line at EOF")
                for number, line in enumerate(text.splitlines(), 1):
                    self.assertEqual(
                        line, line.rstrip(" \t"),
                        "trailing whitespace on line {}".format(number),
                    )


if __name__ == "__main__":
    unittest.main()
