"""Contract: no plan inherits another machine's identity as a default.

`_load_plan` filled a missing node contract with a literal project id, a
literal codex worker name, and a literal adapter id captured from one
historical V3.9 run.  The fill had no plan-id guard, so any plan whose
nodes.json omitted `contract` silently adopted that foreign identity, and the
authorization card then bound file scope against it.  Identity must come from
the plan's own attested capabilities or fail closed.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "vibe_guide" / "cli.py"

# The identity captured from one historical run on another machine.
FOREIGN_PROJECT_ID = "dbd30713-4842-4030-90ad-1789a85cbc58"


class NoForeignIdentityDefaultsTests(unittest.TestCase):
    def test_product_code_carries_no_foreign_project_id(self):
        offenders = []
        for path in sorted((ROOT / "vibe_guide").rglob("*.py")):
            if FOREIGN_PROJECT_ID in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], "a captured project id must not ship in product code")

    def test_a_missing_node_contract_does_not_get_a_default_identity(self):
        """The fill must not invent adapter/worker/project identity."""
        source = CLI.read_text(encoding="utf-8")
        match = re.search(r'if "contract" not in item:(.*?)normalized_nodes\.append', source, re.S)
        self.assertIsNotNone(match, "the missing-contract fill moved; re-point this test")
        block = match.group(1)
        for field in ("project_id", "adapter_id", "worker"):
            self.assertNotIn(
                '"%s"' % field, block,
                "a missing contract must not be given a literal %s" % field,
            )


if __name__ == "__main__":
    unittest.main()
