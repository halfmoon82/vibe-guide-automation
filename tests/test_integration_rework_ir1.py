"""IR-1 (integration review): the aggregated protocol tree's digests are
registered and the supervisor card is within its byte budget.

Anchors to the registry entry and the shipped file, not whole-file asserts.
"""
import hashlib
import unittest
from pathlib import Path

from vibe_guide.protocols import SHIPPED_PROTOCOL_DIGESTS, protocol_path, published_protocol_digest

ROOT = Path(__file__).resolve().parent.parent
PRD_GUIDE_AGGREGATE_DIGEST = (
    "4336b8a2977974248dbec483a4e8440ed60768234d76ffc0a6ac685e12ee97c5"
)
CARD_AGGREGATE_INTERMEDIATE_DIGEST = (
    "3d66a84077b893c6798fd0ea66adf6c16ff3b23bdd83524d2efd51e9c5df9df8"
)
CARD_BYTE_BUDGET = 3072


class AggregateDigestRegistrationTests(unittest.TestCase):
    def test_aggregate_prd_guide_digest_registered(self):
        self.assertIn(PRD_GUIDE_AGGREGATE_DIGEST, SHIPPED_PROTOCOL_DIGESTS["prd-guide"])

    def test_aggregate_card_intermediate_digest_registered(self):
        # The 3312-byte aggregate intermediate stays registered as a shipped
        # text, even though only the compressed version is the current file.
        self.assertIn(CARD_AGGREGATE_INTERMEDIATE_DIGEST, SHIPPED_PROTOCOL_DIGESTS["supervisor-card"])

    def test_current_card_within_budget_and_registered(self):
        raw = protocol_path("supervisor-card").read_bytes()
        self.assertLessEqual(len(raw), CARD_BYTE_BUDGET)
        self.assertIn(hashlib.sha256(raw).hexdigest(), SHIPPED_PROTOCOL_DIGESTS["supervisor-card"])
        self.assertEqual(published_protocol_digest("supervisor-card"),
                         hashlib.sha256(raw).hexdigest())


class Ir1DocContractKeptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prd = (ROOT / "vibe_guide" / "protocols" / "prd-guide.md").read_text(encoding="utf-8")
        cls.card = (ROOT / "vibe_guide" / "protocols" / "supervisor-card.md").read_text(encoding="utf-8")

    def test_locate_and_worktree_missing_docs_survived_aggregation(self):
        s63 = self.prd[self.prd.index("### 6.3 "):self.prd.index("### 6.4 ")]
        self.assertIn("worktree_missing", s63)
        self.assertIn("需要监工建工作树", s63)
        s64 = self.prd[self.prd.index("### 6.4 "):self.prd.index("### 6.5")]
        locate = next(line for line in s64.splitlines()
                      if line.startswith("|") and "`locate`" in line)
        self.assertIn("threadId", locate)
        self.assertIn("不要多传 hostId", locate)

    def test_card_keeps_locate_and_project_root(self):
        self.assertIn("## locate", self.card)
        self.assertIn("native_args", self.card)
        self.assertIn("## 项目根", self.card)
        self.assertIn("--project-root", self.card)
