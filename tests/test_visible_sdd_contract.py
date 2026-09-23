"""visible-sdd-worker protocol contract: in-session SDD with reviewer independence.

ISSUE-05 pins the worker-session protocol shipped with the package:
(1) the fixed in-session flow -- dev subagent implements, review subagent
    (independent context, read-only, non-author) reviews, P0-P2 rework returns
    to dev, re-review, all under one session identity;
(2) the review subagent prompt must explicitly ban git write operations, with
    cross-branch reads only via ``git show <ref>:<path>`` / ``git diff``;
(3) evidence chain: each implement/review/rework round's conclusion is written
    into the session delivery for the supervisor to close into events.jsonl;
(4) a review subagent editing business code is a violation: node -> blocked.

The contract elements must not weaken: a missing element keeps the tests red.
Missing or illegal protocol names reuse load_protocol's existing error paths
(FileNotFoundError / ValueError); no new branches are added for this protocol.
"""
import re
import unittest
from pathlib import Path

PROTOCOL_NAME = "visible-sdd-worker"


def _load() -> str:
    from vibe_guide.protocols import load_protocol
    return load_protocol(PROTOCOL_NAME)


class VisibleSddProtocolShippingTests(unittest.TestCase):
    def test_protocol_file_is_shipped_as_package_data(self):
        from vibe_guide import protocols
        path = Path(protocols.__file__).parent / (PROTOCOL_NAME + ".md")
        self.assertTrue(path.is_file(), "protocol file must ship with the package")

    def test_protocol_loads_by_simple_name(self):
        text = _load()
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip())


class VisibleSddDualRoleOrderTests(unittest.TestCase):
    """Element 1: fixed in-session dual-role flow under one session identity."""

    def setUp(self):
        self.text = _load()

    def test_names_dev_then_review_subagent_order(self):
        """Order is pinned inside the fixed-flow code block, not raw text.

        Bare substring positions drift ("preview" contains "review");
        the fenced flow block is the authoritative statement of order.
        """
        match = re.search(r"```text\n(.*?)\n```", self.text, re.S)
        self.assertIsNotNone(match, "protocol must contain the fixed-flow code block")
        flow = match.group(1)
        dev_pos = flow.find("dev")
        review_pos = flow.find("review")
        self.assertNotEqual(dev_pos, -1, "flow block must name the dev subagent")
        self.assertNotEqual(review_pos, -1, "flow block must name the review subagent")
        self.assertLess(dev_pos, review_pos, "dev implements before review reviews")

    def test_review_subagent_is_independent_read_only_non_author(self):
        for token in ("独立上下文", "只读", "非作者"):
            self.assertIn(token, self.text, token)

    def test_p0_p2_rework_returns_to_dev_then_re_review(self):
        text = self.text
        self.assertIn("P0", text)
        self.assertIn("P2", text)
        rework_pos = text.find("返工")
        re_review_pos = text.find("复审")
        self.assertNotEqual(rework_pos, -1, "rework (返工) must be part of the flow")
        self.assertNotEqual(re_review_pos, -1, "re-review (复审) must follow rework")
        self.assertLess(rework_pos, re_review_pos, "复审 follows 返工")

    def test_rework_target_role_is_dev_subagent(self):
        """Rework must go back to the dev subagent, never to any role.

        Mutant check: replacing "由 dev 子代理修复" with "由任意角色修复"
        must turn this red -- a reviewer (or anyone else) performing the
        rework itself is exactly the independence violation element 4 bans.
        """
        self.assertRegex(
            self.text,
            r"由\s*dev\s*子代理修复",
            "rework must be pinned to the dev subagent role",
        )

    def test_flow_keeps_single_session_identity(self):
        self.assertIn("同一会话", self.text)


class VisibleSddGitWriteBanTests(unittest.TestCase):
    """Element 2: review subagent prompt must ban git writes explicitly."""

    def setUp(self):
        self.text = _load()

    def test_git_write_ban_list_is_explicit(self):
        """checkout/switch/reset/clean/stash must each appear on a ban line.

        Presence alone is not enough: naming ``git checkout`` in an
        "allowed reads" sentence must turn this test red, so every banned
        op needs at least one line that also carries a ban marker.
        """
        ban_markers = ("禁", "不得")
        for op in ("checkout", "switch", "reset", "clean", "stash"):
            lines = [line for line in self.text.splitlines() if op in line]
            self.assertTrue(lines, op + " must be named in the protocol")
            self.assertTrue(
                any(any(marker in line for marker in ban_markers) for line in lines),
                op + " must appear on a ban line, got: " + repr(lines),
            )

    def test_extended_git_write_ban_list_is_named(self):
        """Beyond the five mandatory ops, the protocol bans all git writes."""
        for op in ("git add", "git commit", "git push", "git merge",
                   "git rebase", "git cherry-pick"):
            self.assertIn(op, self.text, op)

    def test_cross_branch_reads_limited_to_show_and_diff(self):
        self.assertIn("git show", self.text)
        self.assertIn("git diff", self.text)
        for line in self.text.splitlines():
            if "git show" in line and "<ref>:" in line:
                break
        else:
            self.fail("protocol must show the git show <ref>:<path> read pattern")


class VisibleSddEvidenceChainTests(unittest.TestCase):
    """Element 3: evidence write-back into session delivery and events.jsonl."""

    def setUp(self):
        self.text = _load()

    def test_each_round_writes_conclusion_into_session_delivery(self):
        for token in ("证据", "交付"):
            self.assertIn(token, self.text, token)

    def test_evidence_granularity_rounds_distinguishable_old_kept_new_appended(self):
        """Granularity, not just presence: rounds distinguishable, history kept."""
        for token in ("轮次", "保留", "追加"):
            self.assertIn(token, self.text, token)

    def test_supervisor_closes_evidence_into_events_jsonl(self):
        self.assertIn("events.jsonl", self.text)
        self.assertIn("监工", self.text)


class VisibleSddReviewerViolationTests(unittest.TestCase):
    """Element 4: reviewer editing business code = violation, node -> blocked."""

    def setUp(self):
        self.text = _load()

    def test_reviewer_editing_business_code_is_violation(self):
        self.assertIn("代改", self.text)
        self.assertIn("违规", self.text)

    def test_violation_blocks_node_and_is_recorded(self):
        self.assertIn("blocked", self.text)
        self.assertIn("记录", self.text)


class VisibleSddErrorPathTests(unittest.TestCase):
    """Missing/illegal names reuse load_protocol's existing error paths."""

    def test_unknown_protocol_raises_file_not_found(self):
        from vibe_guide.protocols import load_protocol
        with self.assertRaises(FileNotFoundError):
            load_protocol("no-such-protocol")

    def test_illegal_protocol_name_raises_value_error(self):
        from vibe_guide.protocols import load_protocol
        for bad in ("Visible-SDD", "../etc/passwd", "a b", "", "x" * 300 + "!"):
            with self.assertRaises(ValueError, msg=bad):
                load_protocol(bad)


if __name__ == "__main__":
    unittest.main()
