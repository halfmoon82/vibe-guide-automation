"""``model_map``: user-level tier→model-chain config, strict validation, card display.

Covers the model-routing node contract:
- ``$VIBE_HOME/config.json`` ``model_map`` (platform → tier → ordered model
  list; first entry is the primary model, the rest the fallback chain).
- ``resolve`` honours the map (source ``config``) and keeps today's
  positional picking when unconfigured (source ``positional``).
- A malformed map is rejected as a whole, naming the offending key path
  (e.g. ``model_map.codex.normal``), including at card publication time.
- ``model_tiers`` card rows show primary + fallback chain + source, and the
  authorization digest never sees model information.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide import model_catalog
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.models import AgentCapabilities, Plan

from tests.test_integrated_prd_acceptance import _isolate_user_dirs, _node as _fixture_node

CODEX_CAPABILITIES = AgentCapabilities("codex", True, True, True, True, True, "full")


def _codex_probe():
    return [
        {"slug": "big", "efforts": ["low", "medium", "high"], "default": "medium"},
        {"slug": "mid", "efforts": ["low", "medium", "high"], "default": "medium"},
        {"slug": "small", "efforts": ["low", "high"], "default": "high"},
    ]


def _write_vibe_config(home: Path, payload):
    (home / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_model_map(home: Path, model_map):
    _write_vibe_config(home, {"model_map": model_map})


class ResolveModelMapTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def resolve(self, tier="standard", platform="codex", prober=None):
        return model_catalog.resolve(
            platform, tier, vibe_home=self.home, prober=prober or _codex_probe, now=1000
        )

    def test_config_map_selects_primary_and_fallback_chain(self):
        _write_model_map(self.home, {"codex": {"standard": ["kimi-for-coding", "k3.1-pro", "k3"]}})
        calls = []

        def probe():
            calls.append(1)
            return _codex_probe()

        choice = self.resolve(prober=probe)
        self.assertEqual(choice.model, "kimi-for-coding")
        self.assertEqual(tuple(choice.fallbacks), ("k3.1-pro", "k3"))
        self.assertEqual(choice.origin, "config")
        self.assertEqual(choice.status, "ok")
        self.assertEqual(choice.reasoning, "normal")
        self.assertEqual(calls, [], "a configured map resolves without probing")

    def test_unconfigured_resolution_is_positional_and_unchanged(self):
        deep = self.resolve("deep")
        standard = self.resolve("standard")
        light = self.resolve("light")
        self.assertEqual((deep.model, standard.model, light.model), ("big", "mid", "small"))
        self.assertEqual(deep.origin, "positional")
        self.assertEqual(tuple(deep.fallbacks), ())
        self.assertEqual(standard.source, "cache")

    def test_config_map_overrides_claude_aliases(self):
        _write_model_map(self.home, {"claude-code": {"light": ["custom-small", "custom-tiny"]}})
        choice = model_catalog.resolve("claude-code", "light", vibe_home=self.home)
        self.assertEqual((choice.model, tuple(choice.fallbacks), choice.origin),
                         ("custom-small", ("custom-tiny",), "config"))

    def test_map_for_another_tier_leaves_this_tier_positional(self):
        _write_model_map(self.home, {"codex": {"deep": ["only-deep"]}})
        choice = self.resolve("standard")
        self.assertEqual((choice.model, choice.origin), ("mid", "positional"))

    def test_probe_unknown_still_platform_default(self):
        def boom():
            raise TimeoutError("slow")

        choice = model_catalog.resolve("codex", "deep", vibe_home=self.home, prober=boom)
        self.assertEqual((choice.status, choice.model), ("unknown", "default"))
        self.assertEqual(choice.origin, "positional")

    def test_config_without_model_map_is_ignored(self):
        _write_vibe_config(self.home, {"claude_bg": {"settings_path": "/x/s.json", "model": "sonnet"}})
        choice = self.resolve("standard")
        self.assertEqual((choice.model, choice.origin), ("mid", "positional"))


class ModelMapValidationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def assert_rejected(self, model_map, key_path):
        _write_model_map(self.home, model_map)
        with self.assertRaises(model_catalog.ModelMapConfigError) as caught:
            model_catalog.resolve("codex", "standard", vibe_home=self.home, prober=_codex_probe)
        self.assertIn(key_path, str(caught.exception))

    def test_unknown_tier_key_names_the_key_path(self):
        self.assert_rejected({"codex": {"normal": ["k3"]}}, "model_map.codex.normal")

    def test_unknown_platform_key_names_the_key_path(self):
        self.assert_rejected({"grok": {"standard": ["g3"]}}, "model_map.grok")

    def test_empty_list_is_rejected(self):
        self.assert_rejected({"codex": {"standard": []}}, "model_map.codex.standard")

    def test_non_string_element_is_rejected(self):
        self.assert_rejected({"codex": {"standard": ["k3", 42]}}, "model_map.codex.standard")

    def test_non_list_value_is_rejected(self):
        self.assert_rejected({"codex": {"standard": "k3"}}, "model_map.codex.standard")

    def test_non_object_platform_value_is_rejected(self):
        self.assert_rejected({"codex": ["k3"]}, "model_map.codex")

    def test_one_bad_entry_rejects_the_whole_map(self):
        _write_model_map(
            self.home,
            {"codex": {"standard": ["kimi-for-coding", "k3"], "normal": ["oops"]}},
        )
        with self.assertRaises(model_catalog.ModelMapConfigError) as caught:
            # even a query for the *valid* tier refuses: no partial effect
            model_catalog.resolve("codex", "standard", vibe_home=self.home, prober=_codex_probe)
        self.assertIn("model_map.codex.normal", str(caught.exception))

    def test_missing_config_file_means_unconfigured(self):
        self.assertIsNone(model_catalog.load_model_map(self.home))

    def test_non_object_config_file_is_rejected(self):
        (self.home / "config.json").write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(model_catalog.ModelMapConfigError):
            model_catalog.resolve("codex", "standard", vibe_home=self.home, prober=_codex_probe)


class CardModelMapTests(unittest.TestCase):
    def setUp(self):
        _isolate_user_dirs(self)
        self.vibe_home = Path(__import__("os").environ["VIBE_HOME"])

    def _card(self, nodes, capabilities=CODEX_CAPABILITIES):
        plan = Plan("plan-1", 1, "docs/prd.md", [n.id for n in nodes], "draft")
        with patch.dict(model_catalog._PROBES, {"codex": _codex_probe}):
            return plan, build_authorization_card(plan, nodes, capabilities)

    def test_card_shows_chain_and_config_source_with_unchanged_digest(self):
        nodes = [_fixture_node("n1")]
        _plan0, baseline = self._card(nodes)
        _write_model_map(self.vibe_home, {"codex": {"standard": ["kimi-for-coding", "k3.1-pro", "k3"]}})
        _plan1, card = self._card(nodes)

        rows = {row["node_id"]: row for row in card.to_dict()["model_tiers"]}
        row = rows["n1"]
        self.assertEqual(row["resolved_model"], "kimi-for-coding")
        self.assertEqual(row["fallbacks"], ["k3.1-pro", "k3"])
        self.assertEqual(row["source"], "config")
        # Model information is attached after the digest: it never changes it.
        self.assertEqual(card.digest, baseline.digest)
        record = authorize(card, "AUTHORIZE")
        self.assertEqual(record.digest, card.digest)

    def test_card_source_is_positional_without_config(self):
        nodes = [_fixture_node("n1")]
        _plan, card = self._card(nodes)
        rows = {row["node_id"]: row for row in card.to_dict()["model_tiers"]}
        self.assertEqual(rows["n1"]["source"], "positional")
        self.assertEqual(rows["n1"]["resolved_model"], "mid")
        self.assertEqual(rows["n1"]["fallbacks"], [])

    def test_invalid_map_fails_card_publication_naming_the_key(self):
        nodes = [_fixture_node("n1")]
        _write_model_map(self.vibe_home, {"codex": {"normal": ["k3"]}})
        with self.assertRaises(model_catalog.ModelMapConfigError) as caught:
            self._card(nodes)
        self.assertIn("model_map.codex.normal", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
