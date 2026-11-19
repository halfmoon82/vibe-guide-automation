"""远端交付 merge 步骤遵守 PR 审阅占位（与控制面 multiagent-autoloop 的 src/review-claim.mjs 同一评论格式）。

2026-11-19：同一 PR 同时只许一方审阅与合并。他人（非 vibe 自身）持有有效占位时，
vibe 自动交付不得合并，按可重试失败处理，待占位释放或过期后再合并。
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from vibe_guide.remote_delivery import RemoteDeliveryFailed, SubprocessRemoteDeliveryExecutor
from vibe_guide.review_claim import active_claim_holder

PR_URL = "https://github.com/halfmoon82/vibe-guide-automation/pull/999"
H1 = "a" * 40
H2 = "b" * 40
NOW = datetime(2026, 11, 19, 6, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# 逐字复制自控制面 claimMarker() 的输出（JSON.stringify 无空格 + 第二行说明），是两端格式的契约。
def js_marker(holder, head, state):
    note = "{} 正在审阅（head {}）".format(holder, head) if state == "active" else "{} 已释放".format(holder)
    return '<!-- review-claim:v1 {{"holder":"{}","head":"{}","state":"{}"}} -->\n审阅占位：{}'.format(holder, head, state, note)


class ActiveClaimHolderTests(unittest.TestCase):
    def test_contract_reads_control_plane_marker(self):
        comments = [{"body": js_marker("codex-9f94e800", "aaa1111", "active"), "createdAt": iso(NOW - timedelta(minutes=5))}]
        self.assertEqual(active_claim_holder(comments, NOW), "codex-9f94e800")

    def test_released_and_expired_claims_are_ignored(self):
        comments = [
            {"body": js_marker("codex-a", "aaa1111", "active"), "createdAt": iso(NOW - timedelta(minutes=30))},
            {"body": js_marker("codex-a", "aaa1111", "released"), "createdAt": iso(NOW - timedelta(minutes=20))},
            {"body": js_marker("claude-b", "aaa1111", "active"), "createdAt": iso(NOW - timedelta(hours=4))},
        ]
        self.assertIsNone(active_claim_holder(comments, NOW))

    def test_earliest_live_claim_wins_and_expired_restart_does_not_jump_queue(self):
        comments = [
            {"body": js_marker("codex-a", "aaa1111", "active"), "createdAt": iso(NOW - timedelta(hours=6))},
            {"body": js_marker("claude-b", "aaa1111", "active"), "createdAt": iso(NOW - timedelta(minutes=2))},
            {"body": js_marker("codex-a", "aaa1111", "active"), "createdAt": iso(NOW - timedelta(minutes=1))},
        ]
        self.assertEqual(active_claim_holder(comments, NOW), "claude-b")

    def test_plain_text_is_not_a_claim(self):
        comments = [{"body": "review-claim holder=evil", "createdAt": iso(NOW)}]
        self.assertIsNone(active_claim_holder(comments, NOW))


class DeliveryMergeClaimTests(unittest.TestCase):
    def _run(self, comments, merge_output=H2):
        executor = SubprocessRemoteDeliveryExecutor("/repo", plan_id="plan-1")
        calls = []

        def fake_gh(repo, args, timeout=120):
            calls.append(list(args))
            if args[:2] == ["pr", "view"]:
                return json.dumps({"state": "OPEN", "headRefOid": H1, "mergeCommit": None, "comments": comments})
            if args[:2] == ["pr", "merge"]:
                return merge_output
            raise AssertionError("unexpected gh call: {}".format(args))

        with mock.patch("vibe_guide.remote_delivery._run_gh", side_effect=fake_gh), \
                mock.patch("vibe_guide.remote_delivery._utcnow", return_value=NOW):
            result = executor.execute("merge", {"pr_url": PR_URL, "head_sha": H1})
        return result, calls

    def test_claim_by_another_reviewer_blocks_merge_retryably(self):
        comments = [{"body": js_marker("codex-9f94e800", H1, "active"), "createdAt": iso(NOW - timedelta(minutes=5))}]
        with self.assertRaises(RemoteDeliveryFailed) as caught:
            self._run(comments)
        self.assertTrue(caught.exception.retryable)
        self.assertIn("codex-9f94e800", caught.exception.reason)

    def test_no_merge_command_is_sent_while_claimed(self):
        comments = [{"body": js_marker("claude-8932a769", H1, "active"), "createdAt": iso(NOW - timedelta(minutes=5))}]
        executor = SubprocessRemoteDeliveryExecutor("/repo", plan_id="plan-1")
        merged = []

        def fake_gh(repo, args, timeout=120):
            if args[:2] == ["pr", "merge"]:
                merged.append(args)
            return json.dumps({"state": "OPEN", "headRefOid": H1, "mergeCommit": None, "comments": comments})

        with mock.patch("vibe_guide.remote_delivery._run_gh", side_effect=fake_gh), \
                mock.patch("vibe_guide.remote_delivery._utcnow", return_value=NOW):
            with self.assertRaises(RemoteDeliveryFailed):
                executor.execute("merge", {"pr_url": PR_URL, "head_sha": H1})
        self.assertEqual(merged, [])

    def test_unclaimed_or_own_claim_merges_locked_to_reviewed_head(self):
        for comments in ([], [{"body": js_marker("vibe-plan-1", H1, "active"), "createdAt": iso(NOW - timedelta(minutes=5))}]):
            result, calls = self._run(comments)
            self.assertEqual(result["merge_commit"], H2)
            merge = [c for c in calls if c[:2] == ["pr", "merge"]][0]
            self.assertIn("--squash", merge)
            self.assertEqual(merge[merge.index("--match-head-commit") + 1], H1)


if __name__ == "__main__":
    unittest.main()
