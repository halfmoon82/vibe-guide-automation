"""Capability tiers: derivation, per-platform resolution, escalation, card display."""

import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide import model_catalog
from vibe_guide.authorization import (
    authorize,
    build_authorization_card,
    executable_contract_digest,
)
from vibe_guide.model_catalog import TierError, adjust_tier, derive_tier, resolve
from vibe_guide.model_router import provider_thinking_for
from vibe_guide.state import load_events, load_snapshot

from tests.test_integrated_prd_acceptance import FULL, V42_STATE, _node as _fixture_node, _ProjectCase


def _node(node_id, files=None, **contract):
    # The planner's placeholder profile (model "default"), as node_spec derives it.
    node = _fixture_node(node_id, files, **contract)
    node.contract["worker_profile"]["model"] = "default"
    return node


def _codex_probe():
    return [
        {"slug": "big", "efforts": ["low", "medium", "high"], "default": "medium"},
        {"slug": "mid", "efforts": ["low", "medium", "high"], "default": "medium"},
        {"slug": "small", "efforts": ["low", "high"], "default": "high"},
    ]


class DeriveTierTests(unittest.TestCase):
    def test_docs_only_is_light(self):
        self.assertEqual(derive_tier({"files": ["docs/a.md", "docs/b/c.md"]}), "light")
        self.assertEqual(derive_tier({"files": ["README.md"]}), "light")

    def test_docs_plus_code_is_standard(self):
        self.assertEqual(derive_tier({"files": ["docs/a.md", "x.py"]}), "standard")

    def test_high_risk_tags_are_deep(self):
        for tag in ("migration", "security", "cross_module"):
            self.assertEqual(derive_tier({"files": ["docs/a.md"], "risk_tags": [tag]}), "deep")

    def test_many_files_or_modules_are_deep(self):
        self.assertEqual(derive_tier({"files": ["m/f%d.py" % i for i in range(8)]}), "deep")
        self.assertEqual(derive_tier({"files": ["m/f%d.py" % i for i in range(7)]}), "standard")
        self.assertEqual(derive_tier({"files": ["a/x.py", "b/x.py", "c/x.py", "d/x.py"]}), "deep")
        self.assertEqual(derive_tier({"files": ["a/x.py", "b/x.py", "c/x.py"]}), "standard")

    def test_reviewer_is_at_least_standard_and_integration_is_deep(self):
        self.assertEqual(derive_tier({"files": ["docs/a.md"]}, role="reviewer"), "standard")
        self.assertEqual(derive_tier({"files": ["x.py"]}, integration=True), "deep")
        self.assertEqual(derive_tier({"files": ["x.py"], "risk_tags": ["integration"]}), "deep")


class AdjustTierTests(unittest.TestCase):
    def test_moves_one_level_with_reason(self):
        self.assertEqual(adjust_tier("standard", 1, "needs more"), "deep")
        self.assertEqual(adjust_tier("standard", -1, "trivial"), "light")

    def test_out_of_range_reasonless_and_big_moves_are_refused(self):
        with self.assertRaisesRegex(TierError, "light..deep"):
            adjust_tier("deep", 1, "x")
        with self.assertRaises(TierError):
            adjust_tier("light", -1, "x")
        with self.assertRaisesRegex(TierError, "reason"):
            adjust_tier("standard", 1, "  ")
        with self.assertRaises(TierError):
            adjust_tier("light", 2, "x")


class ResolveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def test_claude_code_uses_aliases(self):
        self.assertEqual(resolve("claude-code", "light").model, "haiku")
        self.assertEqual(resolve("claude-code", "standard").model, "sonnet")
        self.assertEqual(resolve("claude-code", "deep").model, "opus")

    def test_codex_reads_probe_and_caches_it(self):
        calls = []

        def probe():
            calls.append(1)
            return _codex_probe()

        deep = resolve("codex", "deep", vibe_home=self.home, prober=probe, now=1000)
        light = resolve("codex", "light", vibe_home=self.home, prober=probe, now=1001)
        self.assertEqual((deep.model, deep.effort, deep.status), ("big", "high", "ok"))
        self.assertEqual((light.model, light.effort), ("small", "low"))
        self.assertEqual(len(calls), 1, "second resolve is served from $VIBE_HOME cache")
        self.assertEqual(light.source, "cache")
        self.assertTrue((self.home / "model_catalog.json").is_file())
        resolve("codex", "deep", vibe_home=self.home, prober=probe, now=1000 + 2 * 86400)
        self.assertEqual(len(calls), 2, "stale cache is re-probed")

    def test_probe_failure_is_unknown_with_platform_default(self):
        def boom():
            raise TimeoutError("slow")

        choice = resolve("codex", "deep", vibe_home=self.home, prober=boom)
        self.assertEqual((choice.status, choice.model), ("unknown", "default"))
        self.assertIn("TimeoutError", choice.note)
        self.assertEqual(resolve("codex", "deep", vibe_home=self.home, prober=lambda: []).status, "unknown")
        self.assertEqual(resolve("mystery", "deep").status, "unknown")

    def test_light_tier_has_a_provider_thinking_value(self):
        self.assertEqual(provider_thinking_for(resolve("claude-code", "light").reasoning), "low")
        self.assertEqual(provider_thinking_for(resolve("claude-code", "deep").reasoning), "high")


class CardAndDigestTests(_ProjectCase):
    state = V42_STATE

    def _card(self, nodes):
        from vibe_guide.models import Plan

        plan = Plan("plan-1", 1, "docs/prd.md", [n.id for n in nodes], "draft")
        return plan, build_authorization_card(plan, nodes, FULL)

    def test_card_json_lists_tier_and_resolved_model_per_node(self):
        nodes = [
            _node("doc", files=["docs/a.md"], adapter_id="claude-code"),
            _node("sec", files=["x.py"], risk_tags=["security"], adapter_id="claude-code"),
        ]
        _plan, card = self._card(nodes)
        rows = {row["node_id"]: row for row in card.to_dict()["model_tiers"]}
        self.assertEqual(set(rows), {"doc", "sec"})
        for row in rows.values():
            self.assertIn("tier", row)
            self.assertIn("resolved_model", row)
        self.assertEqual(rows["doc"]["tier"], "light")
        self.assertEqual(rows["doc"]["reviewer_tier"], "standard")
        self.assertEqual(rows["sec"]["tier"], "deep")

    def test_concrete_model_is_not_in_the_digest(self):
        nodes = [_node("n1", adapter_id="claude-code")]
        _plan, card = self._card(nodes)
        text = json.dumps(
            {"digest": card.digest, "contract": executable_contract_digest(nodes)}
        )
        for name in ("haiku", "sonnet", "opus"):
            self.assertNotIn(name, text)
        # model_tiers is display only: stripping it leaves the signed record valid.
        record = authorize(card, "AUTHORIZE")
        self.assertEqual(record.digest, card.digest)

    def test_changing_claude_bg_model_config_keeps_authorization_valid(self):
        nodes = [_node("n1", adapter_id="claude-code")]
        _plan, card = self._card(nodes)
        before = (card.digest, executable_contract_digest(nodes))
        home = Path(tempfile.mkdtemp(prefix="vg-bg-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        for model in ("sonnet", "opus"):
            (home / "config.json").write_text(
                json.dumps({"claude_bg": {"settings_path": "/x/s.json", "model": model}}),
                encoding="utf-8",
            )
            _plan2, card2 = self._card(nodes)
            self.assertEqual((card2.digest, executable_contract_digest(nodes)), before)


class DispatchEscalationTests(_ProjectCase):
    state = V42_STATE

    def _dispatches(self, runner, node_id, role="developer"):
        return [c for c in runner.start_calls if c["node_id"] == node_id and c["role"] == role]

    def test_dispatch_resolves_tier_into_model_and_second_rejection_raises_it(self):
        node = _node("n1", files=["a.py"], adapter_id="claude-code")
        monitor, runner, snapshot = self._start(
            [node],
            {
                ("n1", "developer"): [("complete", {"evidence": "d"})] * 4,
                ("n1", "reviewer"): [
                    ("review_finding", {"finding": "bad", "severity": "P1"}),
                    ("review_finding", {"finding": "bad", "severity": "P1"}),
                    ("accepted", {"evidence": "ok"}),
                ],
            },
        )
        snapshot = self._tick_until(monitor, runner, snapshot, "n1", {"accepted", "blocked_unknown", "blocked_design"}, limit=30)
        dev = self._dispatches(runner, "n1")
        models = [c["worker_profile"]["model"] for c in dev]
        self.assertEqual(models[0], "sonnet", models)
        self.assertEqual(models[-1], "opus", models)
        self.assertIn("deep", [c["worker_profile"]["reasoning"] for c in dev])
        # the Claude background launcher reads child_binding.model (cli factory)
        self.assertEqual([c["child_binding"]["model"] for c in dev][0], "sonnet")
        events = [e for e in load_events(self.paths, snapshot.run_id) if e["event"] == "model_tier_adjusted"]
        self.assertEqual(len(events), 1)
        proof = events[0]["data"]["proof"]
        self.assertEqual((proof["from"], proof["to"]), ("standard", "deep"))
        # the signed node contract still carries no concrete model
        self.assertEqual(node.contract["worker_profile"]["model"], "default")

    def test_supervisor_adjustment_needs_reason_and_range(self):
        node = _node("n1", files=["a.py"], adapter_id="claude-code")
        monitor, runner, snapshot = self._start([node], {("n1", "developer"): [("complete", {"evidence": "d"})]})
        with self.assertRaisesRegex(TierError, "reason"):
            monitor.adjust_model_tier(snapshot.run_id, "n1", 1, "")
        monitor.adjust_model_tier(snapshot.run_id, "n1", 1, "touches payments")
        with self.assertRaisesRegex(TierError, "light..deep"):
            monitor.adjust_model_tier(snapshot.run_id, "n1", 1, "again")
        current = load_snapshot(self.paths, snapshot.run_id).nodes["n1"]
        self.assertEqual(current["model_tier_adjustment"], 1)

    def test_unknown_platform_falls_back_to_default_and_is_recorded(self):
        node = _node("n1", files=["a.py"], adapter_id="mystery")
        monitor, runner, snapshot = self._start([node], {("n1", "developer"): [("complete", {"evidence": "d"})]})
        monitor.tick(snapshot.run_id, runner)
        self.assertEqual(self._dispatches(runner, "n1")[0]["worker_profile"]["model"], "default")
        names = [e["event"] for e in load_events(self.paths, snapshot.run_id)]
        self.assertIn("model_tier_unresolved", names)


if __name__ == "__main__":
    unittest.main()
