"""Issue authorization-card (5.2.7): the card carries its dependency graph,
authorization needs a display receipt (decision D5 = A), and the heartbeat
tier shows exactly one selected value (#391).

Each test names the acceptance example it pins.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vibe_guide.authorization import (
    COST_ACCEPTANCE_MARKER,
    authorize,
    build_authorization_card,
    is_authorization_integrity_valid,
    refresh_authorization_card,
)
from vibe_guide.authorize_entry import (
    DISPLAY_RECEIPT_NAME,
    display_receipt_problem,
    materialize_workflow_evidence,
)
from vibe_guide.cli import (
    _authorization_card,
    _card_field_diff,
    _DISPLAY_CARD_FIELDS,
    _VOLATILE_CARD_FIELDS,
    _render_heartbeat_card_line,
    run_cli,
)
from vibe_guide.dag import DependencyGraphError, dependency_graph, render_dependency_graph_text
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.paths import ProjectPaths
from vibe_guide.publish import _default_heartbeat_minutes
from vibe_guide.supervisor import (
    heartbeat_interval,
    heartbeat_interval_options,
    register_supervisor_address,
)

from tests import support_v45_authorize as support

PLAN = "probe-plan"
PLATFORMS = ("codex", "claude-code", "workbuddy", "cursor", "grok", "kimi-code", "deepseek-harness")


def _node(node_id, depends_on=(), files=("a.py",), group=None):
    return DAGNode(node_id, node_id.upper(), list(depends_on), [], group,
                   {"input": "i", "output": "o", "error_behavior": "e",
                    "acceptance_example": "a", "adapter_id": "claude-code", "files": list(files)},
                   "planned")


def _plan(nodes, plan_id="p1"):
    return Plan(plan_id, 1, ".vibe/plans/{}/prd.md".format(plan_id), [n.id for n in nodes],
                "confirmed_pending_authorization", authorization_required=True, nodes=list(nodes))


def _authorize_cli(root, plan_id=PLAN):
    return run_cli(["authorize", "--json", "--plan", plan_id, "--authorize", "AUTHORIZE"], root)


def _show_cli(root, plan_id=PLAN):
    return run_cli(["authorize", "--json", "--plan", plan_id, "--show-card"], root)


class _PublishedWithoutReceipt(unittest.TestCase):
    """A plan published through ``vibe plan`` with no ``--show-card`` yet."""

    def setUp(self):
        self.root = support.publish_complex_probe(self)
        self.plan_dir = self.root / ".vibe" / "plans" / PLAN
        (self.plan_dir / DISPLAY_RECEIPT_NAME).unlink()


class CardOutputCarriesTheGraphTests(unittest.TestCase):
    """Acceptance 1 and 4."""

    def test_1_the_shown_card_carries_width_and_critical_path(self):
        root = support.publish_complex_probe(self)
        shown = _show_cli(root)
        self.assertEqual(shown.payload["status"], "card_shown", shown.payload)
        graph = shown.payload["dependency_graph"]
        self.assertEqual(graph["max_parallel_width"], 1)
        self.assertEqual(graph["critical_path_length"], 1)
        self.assertIn("依赖图：最大并行宽度 1｜关键路径长度 1", shown.text)
        self.assertIn("probe-node-a ← 无", shown.text)
        # The persisted card itself carries the graph: nothing for the agent to paste.
        card = json.loads((root / ".vibe/plans" / PLAN / "authorization-card.json").read_text())
        self.assertEqual(card["dependency_graph"], graph)
        # And the summary every publish/authorize payload prints renders it.
        self.assertIn("关键路径长度", shown.payload["authorization_card"]["dependency_graph_text"])

    def test_1_publish_output_already_renders_the_graph(self):
        with patch.object(support, "show_card", lambda *_: None):
            root = support.publish_complex_probe(self)
        card = json.loads((root / ".vibe/plans" / PLAN / "authorization-card.json").read_text())
        self.assertIn("dependency_graph", card)
        self.assertFalse((root / ".vibe/plans" / PLAN / DISPLAY_RECEIPT_NAME).exists())

    def test_4_a_reasonless_serial_edge_is_on_the_card(self):
        a = _node("a", files=("a.py",))
        b = _node("b", depends_on=("a",), files=("b.py",))
        c = _node("c", files=("c.py",))
        card = build_authorization_card(_plan([a, b, c]), [a, b, c],
                                        AgentCapabilities("claude-code", True, True, True, False, True, "full"))
        graph = card.dependency_graph
        self.assertEqual(graph["unreasoned_edges"], [{"from": "a", "to": "b"}])
        self.assertEqual(graph["max_parallel_width"], 2)
        self.assertEqual(graph["critical_path_length"], 2)
        self.assertEqual(graph["parallel_waves"], [["a", "c"], ["b"]])
        text = render_dependency_graph_text(graph)
        self.assertIn("无理由串行边（两端写范围不重叠、也没写数据依赖理由）：a → b", text)
        self.assertIn("b ← a", text)
        self.assertIn("第 1 波（可同时开工）：a、c", text)

    def test_4_a_reasoned_or_overlapping_edge_is_not_listed(self):
        a = _node("a", files=("shared.py",))
        b = _node("b", depends_on=("a",), files=("shared.py",))
        graph = dependency_graph([a, b])
        self.assertEqual(graph["unreasoned_edges"], [])
        self.assertIn("无理由串行边：无", render_dependency_graph_text(graph))


class DisplayReceiptGateTests(_PublishedWithoutReceipt):
    """Acceptance 2, 3, 7 (bypassing the helper is refused)."""

    def test_2_no_receipt_refuses_then_showing_authorizes(self):
        refused = _authorize_cli(self.root)
        self.assertEqual(refused.payload["status"], "blocked", refused.payload)
        self.assertIn("dependency_graph_not_shown", refused.payload["reason"])
        self.assertIn("--show-card", refused.payload["reason"])
        state = json.loads((self.root / ".vibe/state.json").read_text())
        self.assertNotIn(PLAN, state.get("task_workflow") or {})

        self.assertEqual(_show_cli(self.root).payload["status"], "card_shown")
        granted = _authorize_cli(self.root)
        self.assertEqual(granted.payload["status"], "ok", granted.payload)
        record = json.loads((self.root / ".vibe/state.json").read_text())["task_workflow"][PLAN]
        evidence = record["node_records"]["authorization_card"]["evidence"]
        self.assertEqual(evidence["display_receipt"], "matched")
        self.assertRegex(evidence["dependency_graph_digest"], r"^[0-9a-f]{64}$")

    def test_2_python_entry_is_held_to_the_same_gate(self):
        from vibe_guide.authorize_entry import AuthorizationDenied
        with self.assertRaises(AuthorizationDenied) as raised:
            materialize_workflow_evidence(ProjectPaths(self.root), PLAN, "AUTHORIZE")
        self.assertIn("dependency_graph_not_shown", str(raised.exception))

    def _receipt(self):
        _show_cli(self.root)
        return json.loads((self.plan_dir / DISPLAY_RECEIPT_NAME).read_text())

    def _rewrite_receipt(self, **changes):
        receipt = dict(self._receipt(), **changes)
        (self.plan_dir / DISPLAY_RECEIPT_NAME).write_text(json.dumps(receipt))

    def test_3_a_receipt_for_another_plan_version_is_void(self):
        self._rewrite_receipt(plan_version=0)
        refused = _authorize_cli(self.root)
        self.assertEqual(refused.payload["status"], "blocked", refused.payload)
        self.assertIn("计划版本已变化", refused.payload["reason"])

    def test_3_a_changed_plan_version_voids_the_receipt(self):
        self._receipt()
        plan_path = self.plan_dir / "plan.json"
        plan = json.loads(plan_path.read_text())
        plan["version"] = 2
        plan_path.write_text(json.dumps(plan))
        # The planning gate already refuses a hand-edited plan; either way the
        # old receipt never authorizes the new version.
        refused = _authorize_cli(self.root)
        self.assertEqual(refused.payload["status"], "blocked", refused.payload)
        receipt = json.loads((self.plan_dir / DISPLAY_RECEIPT_NAME).read_text())
        problem = display_receipt_problem(
            receipt, PLAN, 2, receipt["card_digest"], receipt["dependency_graph_digest"])
        self.assertIn("计划版本已变化（回执对应版本 1，当前 2）", problem)
        self.assertIsNone(display_receipt_problem(
            receipt, PLAN, 1, receipt["card_digest"], receipt["dependency_graph_digest"]))

    def test_3_a_changed_graph_or_card_voids_the_receipt(self):
        self._rewrite_receipt(dependency_graph_digest="0" * 64)
        self.assertIn("依赖图已变化", _authorize_cli(self.root).payload["reason"])
        self._rewrite_receipt(card_digest="0" * 64)
        self.assertIn("授权卡已变化", _authorize_cli(self.root).payload["reason"])

    def test_3_a_corrupt_receipt_reads_as_absent(self):
        (self.plan_dir / DISPLAY_RECEIPT_NAME).write_text("{not json")
        self.assertIn("没有依赖图展示回执", _authorize_cli(self.root).payload["reason"])

    def test_3_a_card_graph_that_no_longer_matches_the_nodes_is_refused(self):
        card_path = self.plan_dir / "authorization-card.json"
        card = json.loads(card_path.read_text())
        card["dependency_graph"]["max_parallel_width"] = 9
        card_path.write_text(json.dumps(card))
        shown = _show_cli(self.root)
        self.assertEqual(shown.payload["status"], "blocked", shown.payload)
        self.assertIn("与当前节点不一致", shown.payload["reason"])


class GraphRenderFailureTests(_PublishedWithoutReceipt):
    """Acceptance 8: a graph that cannot be rendered refuses, naming why."""

    def test_8_show_card_and_authorize_both_refuse_with_the_reason(self):
        failure = DependencyGraphError("依赖图渲染失败：依赖关系有环，算不出并行宽度和关键路径")
        with patch("vibe_guide.authorize_entry.dependency_graph", side_effect=failure):
            shown = _show_cli(self.root)
            self.assertEqual(shown.payload["status"], "blocked", shown.payload)
            self.assertIn("依赖关系有环", shown.payload["reason"])
            self.assertFalse((self.plan_dir / DISPLAY_RECEIPT_NAME).exists())
            refused = _authorize_cli(self.root)
        self.assertEqual(refused.payload["status"], "blocked", refused.payload)
        self.assertIn("依赖关系有环", refused.payload["reason"])
        self.assertNotIn("dependency_graph_not_shown", refused.payload["reason"])

    def test_8_a_card_published_with_a_graph_error_cannot_be_authorized(self):
        card_path = self.plan_dir / "authorization-card.json"
        card = json.loads(card_path.read_text())
        card["dependency_graph"] = {"error": "依赖图渲染失败：节点 id 重复"}
        card_path.write_text(json.dumps(card))
        refused = _authorize_cli(self.root)
        self.assertEqual(refused.payload["status"], "blocked", refused.payload)
        self.assertIn("节点 id 重复", refused.payload["reason"])

    def test_8_the_graph_builder_names_each_failure(self):
        with self.assertRaisesRegex(DependencyGraphError, "有环"):
            dependency_graph([_node("a", depends_on=("b",)), _node("b", depends_on=("a",))])
        with self.assertRaisesRegex(DependencyGraphError, "不存在的节点 ghost"):
            dependency_graph([_node("a", depends_on=("ghost",))])
        with self.assertRaisesRegex(DependencyGraphError, "没有节点"):
            dependency_graph([])


class HeartbeatSelectedTierTests(unittest.TestCase):
    """Acceptance 5 (#391)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".vibe").mkdir()
        self.paths = ProjectPaths(self.root)

    def _verify(self, *providers):
        (self.root / ".vibe/push-wake-verification.json").write_text(json.dumps({"platforms": {
            provider: {"success": [{"evidence_ref": "s"}], "failure": [{"evidence_ref": "f"}]}
            for provider in providers}}))

    def _card(self, provider):
        """A card the way ``publish_plan`` builds it: default from the options."""
        options = heartbeat_interval_options(self.paths, provider)
        node = _node("n1")
        return build_authorization_card(
            _plan([node]), [node], AgentCapabilities(provider, True, True, True, False, True, "full"),
            heartbeat_interval_minutes=_default_heartbeat_minutes(options),
            heartbeat_interval_options=options)

    def _selected(self, card):
        return [item["minutes"] for item in card.heartbeat_interval_options if item.get("selected")]

    def test_5_without_a_registry_every_platform_selects_ten(self):
        for provider in PLATFORMS:
            with self.subTest(provider=provider):
                card = self._card(provider)
                self.assertEqual(self._selected(card), [10])
                self.assertEqual(card.heartbeat_interval_minutes, 10)
                for item in card.heartbeat_interval_options:
                    self.assertNotIn("forced", item)

    def test_5_verified_platforms_with_a_channel_select_sixty(self):
        self._verify("codex", "claude-code", "workbuddy")
        for provider, expected in (("codex", 60), ("claude-code", 60), ("workbuddy", 10)):
            with self.subTest(provider=provider):
                self.assertEqual(self._selected(self._card(provider)), [expected])

    def test_5_workbuddy_card_marks_one_tier_and_matches_the_preflight(self):
        card = self._card("workbuddy")
        line = _render_heartbeat_card_line(card)
        self.assertEqual(line.count("【已选中】"), 1)
        self.assertIn("心跳间隔：选中 10 分钟", line)
        self.assertIn("【已选中】10 分钟", line)
        self.assertNotIn("强制", line)
        long_tier = card.heartbeat_interval_options[1]
        self.assertTrue(long_tier[COST_ACCEPTANCE_MARKER])
        self.assertFalse(long_tier["selected"])
        # Same wording as prd-guide 6.0: unverified push wake-up means 10 minutes.
        self.assertIn("推送唤醒未验证的平台默认 10 分钟", long_tier["reason"])
        guide = (Path(__file__).resolve().parents[1] / "vibe_guide/protocols/prd-guide.md").read_text("utf-8")
        self.assertIn("推送唤醒未验证的平台 10 分钟", guide)

        directory = self.paths.vibe / "plans" / "p1"
        directory.mkdir(parents=True)
        (directory / "authorization.json").write_text(json.dumps(authorize(card, "AUTHORIZE").to_dict()))
        (directory / "authorization-card.json").write_text(json.dumps(card.to_dict()))
        register_supervisor_address(self.paths, "run-1", {"provider": "workbuddy", "session_id": "S", "host": "mac"})
        with patch("vibe_guide.supervisor.load_snapshot", return_value=SimpleNamespace(plan_id="p1")):
            interval = heartbeat_interval(self.paths, "run-1", decision_only=False)
        self.assertEqual(interval["minutes"], self._selected(card)[0])


class OldCardsStayValidTests(unittest.TestCase):
    """Acceptance 6: a 5.2.6 card still verifies and its run is not re-gated."""

    def _legacy(self, card):
        """Rewrite a current card to the 5.2.6 on-disk shape (all unsigned fields)."""
        data = card.to_dict()
        data.pop("dependency_graph", None)
        for key in ("heartbeat_interval_options", "rotate_hard_cap_options"):
            legacy = []
            for item in data.get(key, []):
                item = {k: v for k, v in item.items() if k != "selected"}
                if item.pop(COST_ACCEPTANCE_MARKER, None):
                    item["forced"] = True
                legacy.append(item)
            data[key] = legacy
        return data

    def test_6_a_526_card_loads_verifies_and_refreshes_without_drift(self):
        root = Path(tempfile.mkdtemp(prefix="legacy-card-"))
        self.addCleanup(__import__("shutil").rmtree, root, True)
        paths = ProjectPaths(root)
        (root / ".vibe").mkdir()
        node = _node("n1")
        plan = _plan([node])
        current = build_authorization_card(
            plan, [node], AgentCapabilities("workbuddy", True, True, True, False, True, "full"),
            heartbeat_interval_minutes=60,
            heartbeat_interval_options=heartbeat_interval_options(paths, "workbuddy"))
        legacy_data = self._legacy(current)
        self.assertIn("forced", legacy_data["heartbeat_interval_options"][1])
        loaded = _authorization_card(legacy_data)
        self.assertEqual(loaded.digest, current.digest)
        record = authorize(loaded, "AUTHORIZE")
        self.assertTrue(is_authorization_integrity_valid(record))
        self.assertIs(record.heartbeat_interval_forced, True)
        # A refresh re-renders the display fields; only those may differ.
        refreshed = refresh_authorization_card(plan, [node], loaded)
        drift = set(_card_field_diff(loaded, refreshed)) - _VOLATILE_CARD_FIELDS - _DISPLAY_CARD_FIELDS
        self.assertEqual(drift, set())
        self.assertEqual(refreshed.digest, current.digest)
        # New and old marker together: the new key wins.
        both = dict(legacy_data["heartbeat_interval_options"][1], **{COST_ACCEPTANCE_MARKER: True})
        legacy_data["heartbeat_interval_options"][1] = both
        self.assertTrue(_authorization_card(legacy_data).heartbeat_interval_options[1]["forced"])

    def test_6_an_existing_run_is_not_held_to_the_receipt(self):
        root = support.publish_complex_probe(self)
        self.assertEqual(_authorize_cli(root).payload["status"], "ok")
        started = run_cli(["monitor", "--json", "--plan", PLAN, "--authorize", "AUTHORIZE"], root)
        self.assertIsNotNone(started.payload.get("run_id"), started.payload)
        plan_dir = root / ".vibe/plans" / PLAN
        (plan_dir / DISPLAY_RECEIPT_NAME).unlink()
        # A run authorized before the receipt existed re-derives its evidence
        # (same-run reauthorization, engine renewal) without one.
        workflow = materialize_workflow_evidence(ProjectPaths(root), PLAN, "AUTHORIZE")
        evidence = workflow["node_records"]["authorization_card"]["evidence"]
        self.assertEqual(evidence["display_receipt"], "not_required_existing_run")
        resumed = run_cli(["resume", "--json", "--plan", PLAN], root)
        self.assertNotIn("dependency_graph_not_shown", json.dumps(resumed.payload, ensure_ascii=False))


class HelperProvidesTheReceiptTests(unittest.TestCase):
    """Acceptance 7: the shared helper adds the receipt, nothing else does."""

    def test_7_the_helper_published_plan_authorizes(self):
        root = support.publish_complex_probe(self)
        self.assertTrue((root / ".vibe/plans" / PLAN / DISPLAY_RECEIPT_NAME).is_file())
        self.assertEqual(_authorize_cli(root).payload["status"], "ok")

    def test_7_with_display_receipt_only_touches_granting_authorize_calls(self):
        root = support.publish_complex_probe(self)
        receipt = root / ".vibe/plans" / PLAN / DISPLAY_RECEIPT_NAME
        receipt.unlink()
        support.with_display_receipt(["status", "--plan", PLAN], root)
        support.with_display_receipt(["authorize", "--plan", PLAN], root)
        self.assertFalse(receipt.exists())
        support.with_display_receipt(["authorize", "--plan", PLAN, "--authorize", "AUTHORIZE"], root)
        self.assertTrue(receipt.is_file())


if __name__ == "__main__":
    unittest.main()
