import json
import unittest

from vibe_guide import context_projection as cp


class SafeExcerptTests(unittest.TestCase):
    def test_short_text_is_untouched(self):
        out = cp.safe_excerpt("ok", 100)
        self.assertEqual(out["text"], "ok")
        self.assertFalse(out["truncated"])

    def test_chinese_never_cut_mid_character_and_bytes_bounded(self):
        out = cp.safe_excerpt("失败原因：" * 400, 300)
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["text"].encode("utf-8")), 300)
        out["text"].encode("utf-8").decode("utf-8")
        json.loads(json.dumps(out, ensure_ascii=False))

    def test_head_and_tail_survive_so_conclusion_is_not_lost(self):
        text = "HEAD-" + "x" * 5000 + "-未通过TAIL"
        out = cp.safe_excerpt(text, 400)
        self.assertIn("HEAD-", out["text"])
        self.assertIn("未通过TAIL", out["text"])
        self.assertEqual(out["total_bytes"], len(text.encode("utf-8")))
        self.assertEqual(len(out["sha256"]), 64)

    def test_non_string_input_becomes_empty_marker_not_crash(self):
        self.assertEqual(cp.safe_excerpt(None, 10)["text"], "")


class PageTests(unittest.TestCase):
    def test_pages_cover_everything_with_source_and_order(self):
        text = "第一行\n" * 500
        pages = cp.page_text(text, source="log:a", page_bytes=400)
        self.assertGreater(len(pages), 1)
        self.assertEqual("".join(p["text"] for p in pages), text)
        for index, page in enumerate(pages):
            self.assertEqual(page["source"], "log:a")
            self.assertEqual(page["page"], index + 1)
            self.assertEqual(page["pages"], len(pages))
            self.assertLessEqual(len(page["text"].encode("utf-8")), 400)

    def test_empty_text_gives_one_empty_page(self):
        self.assertEqual(len(cp.page_text("", source="s", page_bytes=100)), 1)

    def test_page_smaller_than_one_character_still_progresses(self):
        pages = cp.page_text("中中", source="s", page_bytes=1)
        self.assertEqual("".join(p["text"] for p in pages), "中中")


class FindingsTests(unittest.TestCase):
    def test_unresolved_keep_full_text_resolved_keep_only_result_and_source(self):
        findings = [
            {"id": "F1", "severity": "P1", "status": "fixed", "detail": "old long body " * 50, "source": "r1"},
            {"id": "F2", "severity": "P2", "detail": "still broken"},
        ]
        out = cp.project_findings(findings)
        by = {item["id"]: item for item in out["findings"]}
        self.assertNotIn("detail", by["F1"])
        self.assertEqual(by["F1"]["status"], "fixed")
        self.assertEqual(by["F1"]["source"], "r1")
        self.assertEqual(by["F2"]["detail"], "still broken")

    def test_resolved_ids_argument_marks_fixed(self):
        out = cp.project_findings([{"id": "F9", "detail": "x"}], resolved_ids={"F9"})
        self.assertNotIn("detail", out["findings"][0])

    def test_unknown_status_is_kept_conservatively(self):
        out = cp.project_findings([{"id": "F3", "status": "???", "detail": "keep me"}])
        self.assertEqual(out["findings"][0]["detail"], "keep me")

    def test_oversized_finding_is_bounded_valid_json_with_tail(self):
        big = {"id": "F4", "detail": "日志" * 5000 + "结论：未通过"}
        out = cp.project_findings([big], per_item_bytes=500)
        text = json.dumps(out, ensure_ascii=False)
        json.loads(text)
        self.assertIn("结论：未通过", out["findings"][0]["detail"])
        self.assertTrue(out["findings"][0]["detail_truncated"])

    def test_never_returns_empty_for_nonempty_input(self):
        self.assertEqual(len(cp.project_findings([{"id": "a"}, {"id": "b"}])["findings"]), 2)


class DeltaTests(unittest.TestCase):
    ITEMS = [{"id": "d1", "text": "a"}, {"id": "d2", "text": "b"}, {"id": "d3", "text": "c"}]

    def test_only_items_after_watermark_are_sent(self):
        out = cp.delta_since(self.ITEMS, delivered_ids=["d1", "d2"])
        self.assertEqual([i["id"] for i in out["items"]], ["d3"])
        self.assertFalse(out["fallback"])

    def test_lost_watermark_sends_full_minimal_set_and_says_why(self):
        out = cp.delta_since(self.ITEMS, delivered_ids=None)
        self.assertEqual(len(out["items"]), 3)
        self.assertTrue(out["fallback"])
        self.assertEqual(out["reason"], "watermark_missing")

    def test_garbled_watermark_is_treated_as_lost(self):
        out = cp.delta_since(self.ITEMS, delivered_ids="oops")
        self.assertTrue(out["fallback"])
        self.assertEqual(len(out["items"]), 3)

    def test_watermark_naming_unknown_ids_falls_back(self):
        out = cp.delta_since(self.ITEMS, delivered_ids=["zzz"])
        self.assertTrue(out["fallback"])
        self.assertEqual(out["reason"], "watermark_unmatched")

    def test_three_rework_rounds_plus_one_decision_sends_only_new_content(self):
        items = [{"id": "f%d" % n} for n in range(3)] + [{"id": "dec-new"}]
        out = cp.delta_since(items, delivered_ids=["f0", "f1", "f2"])
        self.assertEqual([i["id"] for i in out["items"]], ["dec-new"])

    def test_items_without_id_are_always_sent(self):
        out = cp.delta_since([{"text": "x"}, {"id": "d1"}], delivered_ids=["d1"])
        self.assertEqual(out["items"], [{"text": "x"}])


class FallbackTests(unittest.TestCase):
    def test_projection_failure_returns_fuller_material_not_empty(self):
        def boom():
            raise ValueError("bad")
        out = cp.project_or_full(boom, lambda: ["all", "material"])
        self.assertEqual(out["value"], ["all", "material"])
        self.assertTrue(out["fallback"])
        self.assertIn("ValueError", out["reason"])

    def test_success_passes_through(self):
        out = cp.project_or_full(lambda: [1], lambda: [1, 2])
        self.assertEqual(out["value"], [1])
        self.assertFalse(out["fallback"])


class BudgetTests(unittest.TestCase):
    def test_reports_bytes_sources_and_over_budget_without_dropping(self):
        report = cp.budget_report({"a": "中" * 10, "b": "x" * 5}, soft_budget_bytes=20)
        self.assertEqual(report["total_bytes"], 35)
        self.assertTrue(report["over_budget"])
        self.assertEqual(report["sources"], {"a": 30, "b": 5})
        self.assertTrue(report["soft"])

    def test_within_budget(self):
        self.assertFalse(cp.budget_report({"a": "x"}, soft_budget_bytes=10)["over_budget"])


class CollapseTests(unittest.TestCase):
    def test_repeated_lines_collapse_with_count_new_lines_kept(self):
        out = cp.collapse_repeats(["fail A", "fail A", "fail A", "new B", "fail A"])
        self.assertEqual(out, ["fail A (x3)", "new B", "fail A"])

    def test_empty(self):
        self.assertEqual(cp.collapse_repeats([]), [])


class LocalWaitTests(unittest.TestCase):
    def test_supported_gives_bounded_wait_and_terminal_only(self):
        policy = cp.local_wait_policy(supported=True, max_wait_seconds=600)
        self.assertEqual(policy["mode"], "bounded_local_wait")
        self.assertTrue(policy["report_terminal_only"])
        self.assertEqual(policy["max_wait_seconds"], 600)

    def test_unsupported_discloses_limit_and_cost(self):
        policy = cp.local_wait_policy(supported=False, max_polls=5)
        self.assertEqual(policy["mode"], "limited_polling")
        self.assertEqual(policy["max_polls"], 5)
        self.assertTrue(policy["disclose_cost"])

    def test_unknown_support_is_treated_as_unsupported(self):
        self.assertEqual(cp.local_wait_policy(supported=None)["mode"], "limited_polling")


class CompactionTests(unittest.TestCase):
    BOUNDARY = {"approval": "A1", "writer": "w1", "in_flight": ["act-1"]}

    def test_effective_only_when_real_next_request_input_drops(self):
        out = cp.compaction_effect(before_input_tokens=120000, after_input_tokens=30000,
                                   boundary_before=self.BOUNDARY, boundary_after=dict(self.BOUNDARY))
        self.assertTrue(out["effective"])
        self.assertEqual(out["input_delta_tokens"], -90000)

    def test_smaller_summary_without_input_drop_is_not_effective(self):
        out = cp.compaction_effect(before_input_tokens=100000, after_input_tokens=100000,
                                   boundary_before=self.BOUNDARY, boundary_after=dict(self.BOUNDARY),
                                   summary_bytes_before=9000, summary_bytes_after=100)
        self.assertFalse(out["effective"])
        self.assertEqual(out["reason"], "input_not_reduced")

    def test_lost_in_flight_action_is_not_effective_even_if_input_drops(self):
        lost = {"approval": "A1", "writer": "w1", "in_flight": []}
        out = cp.compaction_effect(before_input_tokens=100000, after_input_tokens=10, 
                                   boundary_before=self.BOUNDARY, boundary_after=lost)
        self.assertFalse(out["effective"])
        self.assertEqual(out["reason"], "boundary_lost")
        self.assertEqual(out["lost"], ["in_flight"])

    def test_missing_usage_is_unknown_not_success(self):
        out = cp.compaction_effect(before_input_tokens=None, after_input_tokens=5,
                                   boundary_before=self.BOUNDARY, boundary_after=self.BOUNDARY)
        self.assertIsNone(out["effective"])
        self.assertEqual(out["reason"], "usage_unknown")


if __name__ == "__main__":
    unittest.main()
