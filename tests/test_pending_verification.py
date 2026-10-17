"""Structural pending-verification items, hold/decision boundary, idle cadence."""
import tempfile
import unittest
from pathlib import Path

import test_monitor as tm
from vibe_guide.decisions import BOUNDARY_CATEGORIES, validate_boundary_category
from vibe_guide.monitor import (
    BLOCKED_UNKNOWN_CLEARING_DISPOSITIONS,
    BLOCKED_UNKNOWN_DISPOSITION_KINDS,
    is_structural_pending_verification,
    same_pending_verification,
)
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import load_events
from vibe_guide.supervisor import set_supervisor_hold
from vibe_guide.supervisor_wakeup import idle_heartbeat_minutes

ITEM = "微信实机扫码 10/10 只能验收期验证"
REWORDED = "实机扫码需验收期完成"
UNJUDGEABLE = "无法确认 2.3.7 的 props"


class ClassifierTests(unittest.TestCase):
    def test_structural_items_are_recognised(self):
        for text in (
            ITEM,
            REWORDED,
            "生产环境数据只能在验收期验证",
            "临时验证脚本不入库，验证脚本仅本地运行",
            "real device pairing can only be verified in the acceptance period",
            "真机支付流程只能验收期验证",
            "生产环境推送到达率只能上线后验证",
            "真机蓝牙配对只能验收期验证",
        ):
            self.assertTrue(is_structural_pending_verification(text), text)

    def test_unjudgeable_items_are_not_structural(self):
        for text in (
            UNJUDGEABLE,
            "B1 scope: whether manifests count as new dependency needs PM ruling",
            "生产环境日志显示崩溃，需验收期确认",
            "生产环境订单金额算错，只能线上验证",
            "线上数据丢失，验收期再看",
            "production database corrupted; verify in acceptance period",
            "真机偶尔收不到推送，验收期确认",
            "真机登录不上，只能验收期验证",
            "生产环境接口返回500，需验收期确认",
            "真机无法打开页面，验收期验证",
            "线上用户反馈打不开，验收期确认",
            "真机发热严重，只能验收期验证",
            "真机闪烁，只能验收期验证",
            "线上页面加载很慢，验收期确认",
            "真机收不到验证码，只能验收期验证",
            "真机收不到验证码只能验收期验证",
            "真机登录不上只能验收期验证",
            "真机发热严重只能验收期验证",
            "真机闪烁只能验收期验证",
            "real device login is not working, only verified in the acceptance period",
            "production api returns 500 can only be verified in the acceptance period",
            "真机上app闪退，只能验收期验证",
            "真机卡死，只能验收期验证",
            "真机 hang 住，只能验收期验证",
            "线上支付出现重复扣款，验收期确认",
            "生产环境偶发超时，需验收期确认",
            "真机发现白屏，只能验收期验证",
            "实机扫码失败，需要修复",  # a defect report, no acceptance-period deferral
            "",
            None,
            42,
        ):
            self.assertFalse(is_structural_pending_verification(text), text)

    def test_reworded_same_item_matches_but_other_item_does_not(self):
        self.assertTrue(same_pending_verification(ITEM, REWORDED))
        self.assertFalse(same_pending_verification(ITEM, "生产环境支付回调只能验收期验证"))

    def test_extra_substantive_words_make_a_different_item(self):
        base = "微信实机扫码 只能验收期验证"
        self.assertFalse(same_pending_verification(base, "微信实机扫码失败崩溃 只能验收期验证"))
        self.assertFalse(same_pending_verification(base, "微信实机分享 只能验收期验证"))
        self.assertFalse(same_pending_verification("微信实机扫码 只能验收期验证", "微信 只能验收期验证"))

    def test_disposition_kind_is_registered_and_clearing(self):
        self.assertIn("deferred_to_acceptance", BLOCKED_UNKNOWN_DISPOSITION_KINDS)
        self.assertIn("deferred_to_acceptance", BLOCKED_UNKNOWN_CLEARING_DISPOSITIONS)


class DeliveryTests(unittest.TestCase):
    setUp = tm.VisibleDispatchTests.setUp
    tearDown = tm.VisibleDispatchTests.tearDown
    authorized_monitor = tm.VisibleDispatchTests.authorized_monitor

    def _start(self, *item_lists):
        payload, nodes = tm.load_v46_dispatch_fixture()
        monitor, record = self.authorized_monitor(
            nodes, topology_rulings=payload["topology_rulings"]
        )
        events = [
            ("complete", {
                "evidence": "delivery",
                "in_session_review": {
                    "protocol": tm.VISIBLE_SDD_PROTOCOL_REF,
                    "evidence_ref": "session-delivery#review-round-1",
                    "clearance": {"p0": 0, "p1": 0, "p2": 0},
                    "coverage": {"mode": "ocr", "listed": 3, "reviewed": 3, "skipped": []},
                    "environment_facts_ref": "none",
                    "blocked_unknowns": list(items),
                },
            })
            for items in item_lists
        ]
        runner = tm.VisibleSddRunner(events={("sdd-a", "developer"): events})
        return monitor, monitor.start(record, runner), runner

    def _dispositions(self, snapshot):
        return [
            r for r in load_events(self.paths, snapshot.run_id)
            if r["event"] == "blocked_unknown_disposition"
            and r["data"].get("node_id") == "sdd-a"
        ]

    def test_structural_item_does_not_block_and_is_listed_for_acceptance(self):
        monitor, snapshot, runner = self._start([ITEM])
        snapshot = monitor.tick(snapshot.run_id, runner)
        current = snapshot.nodes["sdd-a"]
        self.assertEqual(current["status"], "accepted")
        self.assertEqual(current["blocked_unknowns_pending"], [])
        self.assertEqual(
            [entry["item"] for entry in current["acceptance_pending_verification"]], [ITEM]
        )
        events = self._dispositions(snapshot)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["disposition"], "deferred_to_acceptance")
        self.assertFalse(any(
            r["event"] == "blocked_unknown_disposition_required"
            for r in load_events(self.paths, snapshot.run_id)
        ))

    def test_observed_defect_never_reaches_accepted(self):
        for text in ("生产环境日志显示崩溃，需验收期确认", "线上数据丢失，验收期再看"):
            monitor, snapshot, runner = self._start([text])
            snapshot = monitor.tick(snapshot.run_id, runner)
            self.assertEqual(snapshot.nodes["sdd-a"]["status"], "blocked_unknown", text)
            self.tearDown()
            self.setUp()

    def test_unjudgeable_item_stays_blocked_unknown(self):
        monitor, snapshot, runner = self._start([UNJUDGEABLE])
        snapshot = monitor.tick(snapshot.run_id, runner)
        current = snapshot.nodes["sdd-a"]
        self.assertEqual(current["status"], "blocked_unknown")
        self.assertEqual(current["blocked_unknowns_pending"], [UNJUDGEABLE])
        self.assertEqual(self._dispositions(snapshot), [])

    def test_reworded_report_reuses_original_disposition(self):
        # First delivery pairs the structural item with an unjudgeable one, so
        # the node pauses; the re-report rewords only the structural item.
        monitor, snapshot, runner = self._start(
            [ITEM, UNJUDGEABLE], [REWORDED, UNJUDGEABLE]
        )
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "blocked_unknown")
        self.assertEqual(len(self._dispositions(snapshot)), 1)
        snapshot = monitor.record_blocked_unknown_disposition(
            snapshot.run_id, "sdd-a", UNJUDGEABLE, "confirmed_benign", note="已复核"
        )
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "accepted")
        deferred = [
            r for r in self._dispositions(snapshot)
            if r["data"]["disposition"] == "deferred_to_acceptance"
        ]
        self.assertEqual(len(deferred), 1)


class BoundaryTests(unittest.TestCase):
    def test_engineering_category_is_rejected_with_self_heal_hint(self):
        with self.assertRaises(ValueError) as caught:
            validate_boundary_category("engineering")
        message = str(caught.exception)
        self.assertIn("supervisor", message)
        self.assertIn("自愈", message)

    def test_supervisor_hold_rejects_engineering_category(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            with self.assertRaises(ValueError):
                set_supervisor_hold(
                    paths, "run-x", "n1", "等人", boundary_category="engineering",
                    authorization_gap="x",
                )

    def test_four_cases_map_onto_canonical_categories(self):
        self.assertEqual(BOUNDARY_CATEGORIES, ("product_acceptance", "external_authorization"))
        expected = {
            "product_acceptance": "product_acceptance",
            "product_design": "product_acceptance",
            "deploy": "external_authorization",
            "external_authorization": "external_authorization",
            "credentials": "external_authorization",
            "system_permission": "external_authorization",
            "irreversible_action": "external_authorization",
        }
        for name, canonical in expected.items():
            self.assertEqual(validate_boundary_category(name), canonical, name)


class IdleCadenceTests(unittest.TestCase):
    def test_idle_without_servable_items_backs_off(self):
        slow = idle_heartbeat_minutes({"state": "idle", "reason": "nothing pending"}, base_minutes=10)
        self.assertGreater(slow, 10)
        waiting = idle_heartbeat_minutes({"state": "idle", "reason": "workers active"}, base_minutes=10)
        self.assertGreater(waiting, 10)

    def test_work_and_unknown_keep_base_cadence(self):
        for state in ("work", "rotate", "decision", "unknown"):
            self.assertEqual(idle_heartbeat_minutes({"state": state}, base_minutes=10), 10)

    def test_new_delivery_restores_base_cadence(self):
        self.assertEqual(
            idle_heartbeat_minutes({"state": "idle", "reason": "workers active"},
                                   base_minutes=10, new_delivery=True),
            10,
        )


class IdleBackoffWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name))
        (self.paths.vibe / "runs/run-1").mkdir(parents=True)

    def beat(self, state, **extra):
        from vibe_guide.cli import _beat_interval
        return _beat_interval(self.paths, "run-1", {"state": state, **extra}, persist=True)

    def test_consecutive_idle_beats_back_off_then_delivery_restores(self):
        from vibe_guide.supervisor import read_heartbeat_interval
        first = self.beat("idle")
        self.assertEqual(first["minutes"], 10)
        second = self.beat("idle")
        self.assertEqual((second["minutes"], second["mode"]), (30, "idle_backoff"))
        third = self.beat("idle")
        self.assertEqual(third["minutes"], 30)
        self.assertEqual(read_heartbeat_interval(self.paths, "run-1")["minutes"], 30)
        restored = self.beat("work")
        self.assertEqual((restored["minutes"], restored["mode"]), (10, "default"))
        self.assertEqual(read_heartbeat_interval(self.paths, "run-1")["minutes"], 10)
        self.assertEqual(self.beat("idle")["minutes"], 10)  # streak restarted

    def test_decision_only_stays_at_60(self):
        for _ in range(3):
            interval = self.beat("decision", decision_only=True)
            self.assertEqual((interval["minutes"], interval["mode"]), (60, "decision"))

    def test_backoff_is_capped_at_60_from_a_higher_base(self):
        from unittest.mock import patch
        high = {"minutes": 30, "mode": "push_verified", "reason": "r", "provider": "p"}
        with patch("vibe_guide.cli.heartbeat_interval", return_value=high):
            self.beat("idle")
            self.assertEqual(self.beat("idle")["minutes"], 60)

    def test_preflight_does_not_persist_but_reads_streak(self):
        from vibe_guide.cli import _beat_interval
        self.beat("idle")
        shown = _beat_interval(self.paths, "run-1", {"state": "idle"}, persist=False)
        self.assertEqual(shown["minutes"], 30)
        from vibe_guide.supervisor import read_heartbeat_interval
        self.assertEqual(read_heartbeat_interval(self.paths, "run-1")["idle_streak"], 1)


if __name__ == "__main__":
    unittest.main()
