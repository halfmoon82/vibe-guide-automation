"""Entry tightening: complex mode must be earned; replies never re-score."""

import json
import unittest
from pathlib import Path

from vibe_guide.planner import is_continuation_message, parse_s1_context, route_task
from vibe_guide.session_entry import (
    CONTINUE_CURRENT,
    ContinuationMessage,
    build_session_entry,
    entry_route,
)

FIXTURE = Path(__file__).parent / "fixtures" / "entry_route_complex_messages.json"
MESSAGES = {row["id"]: row["message"] for row in json.loads(FIXTURE.read_text(encoding="utf-8"))}
CONTINUATION_IDS = (
    "vr-008", "vr-025", "vr-060", "vr-071", "vr-094", "vr-096", "vr-097", "vr-098",
)


class ComplexMessageSetTests(unittest.TestCase):
    def test_fixture_is_the_24_message_eval_slice(self):
        self.assertEqual(len(MESSAGES), 24)

    def test_confirmations_continue_current_tier(self):
        for message_id in CONTINUATION_IDS:
            with self.subTest(message_id):
                result = entry_route(MESSAGES[message_id], current_tier="light_plan")
                self.assertEqual(result["route"], CONTINUE_CURRENT)
                self.assertEqual(result["tier"], "light_plan")
                with self.assertRaises(ContinuationMessage):
                    build_session_entry(MESSAGES[message_id])

    def test_no_message_routes_to_complex_without_explicit_upgrade(self):
        for message_id, message in MESSAGES.items():
            with self.subTest(message_id):
                self.assertNotEqual(entry_route(message)["route"], "complex")

    def test_real_new_requests_are_not_swallowed(self):
        for text in (
            "设计并实现一个支付系统，迁移数据、集成接口、编写测试并部署", "帮我重构登录模块",
            "好的，帮我重构整个支付模块", "可以帮我写一个登录页吗", "继续开发订单系统的退款功能",
            "合并两个分支并解决冲突", "确认一下库存同步为什么失败", "行吧，那你重写整个后端",
            "对了，还要加一个导出", "ok 那就开始做完整的复杂流程吧",
        ):
            self.assertFalse(is_continuation_message(text), text)

    def test_explicit_complex_request_enters_complex(self):
        for text in ("请走复杂流程，帮我做支付系统", "用复杂模式做这个"):
            result = entry_route(text)
            self.assertEqual(result["route"], "complex", text)
            self.assertIn("产品经理显式要求", result["score_line"])
        self.assertEqual(entry_route("帮我做支付系统")["route"], "light_plan")

    def test_negated_or_incidental_mentions_are_not_explicit(self):
        for text in ("不要走复杂流程，直接改", "别用复杂模式", "这个复杂流程太麻烦，帮我简化", "跑一遍完整流程测试"):
            self.assertNotEqual(entry_route(text)["route"], "complex", text)


class ComplexConditionTests(unittest.TestCase):
    def test_17_points_with_two_parallel_nodes_is_light_plan(self):
        entry = build_session_entry("改一下结算流程", s1="5,3,3,3,3;parallel=2;resume=yes")
        self.assertEqual(entry.route.route, "light_plan")
        self.assertIn("可并行节点不足 3", entry.route.unmet_conditions)
        self.assertIn("可并行节点不足 3", entry.route.score_line)
        self.assertIn("17", entry.route.score_line)

    def test_no_interrupt_need_is_light_plan(self):
        entry = build_session_entry("改一下结算流程", s1="5,3,3,3,3;parallel=4;resume=no")
        self.assertEqual(entry.route.route, "light_plan")
        self.assertEqual(entry.route.unmet_conditions, ["无需跨会话/跨中断续跑"])

    def test_both_conditions_met_is_complex(self):
        entry = build_session_entry("改一下结算流程", s1="5,3,3,3,3;parallel=3;resume=yes")
        self.assertEqual(entry.route.route, "complex")
        self.assertEqual(entry.route.unmet_conditions, [])

    def test_explicit_five_fives_stays_complex(self):
        self.assertEqual(build_session_entry("做个大系统", s1="5,5,5,5,5").route.route, "complex")

    def test_unknown_condition_is_light_plan_marked_uncertain(self):
        entry = build_session_entry("改一下结算流程", s1="5,3,3,3,3;parallel=5")
        self.assertEqual(entry.route.route, "light_plan")
        self.assertTrue(any("拿不准" in item for item in entry.route.unmet_conditions))

    def test_heuristic_default_never_enters_complex(self):
        entry = build_session_entry("设计并实现支付系统，迁移数据、集成接口、编写测试并部署")
        self.assertEqual(entry.route.route, "light_plan")
        self.assertIn("拿不准", entry.route.score_line)

    def test_pm_force_upgrade_flag_still_complex(self):
        context = parse_s1_context("1,1,1,1,1")
        from dataclasses import replace
        forced = replace(context, force_upgrade_flags=["pm_explicit"])
        self.assertEqual(route_task(forced).route, "complex")

    def test_below_ceiling_scores_are_untouched(self):
        self.assertEqual(build_session_entry("改个字", s1="1,1,1,1,1").route.route, "simple")
        self.assertEqual(build_session_entry("改个字", s1="3,3,3,1,1").route.route, "light_plan")

    def test_bad_suffix_is_rejected_not_ignored(self):
        with self.assertRaises(ValueError):
            build_session_entry("改个字", s1="5,3,3,3,3;parallel=many")


if __name__ == "__main__":
    unittest.main()
