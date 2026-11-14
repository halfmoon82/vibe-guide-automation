"""AC16/25: real init side effects, with only the external directory fixed."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide import model_catalog
from vibe_guide.initializer import init_project
from vibe_guide.paths import ProjectPaths
from vibe_guide.scanner import scan_project


class ModelCatalogInitializationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="r4-model-init-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home = self.base / "user"
        self.home.mkdir()
        self.root = self.base / "project"
        self.root.mkdir()
        self.paths = ProjectPaths(self.root)
        self.table = self.root / ".vibe/model-catalog.json"
        self.models = [
            {"slug": "large", "efforts": ["low", "medium", "high"], "default": "medium"},
            {"slug": "small", "efforts": ["low", "medium", "high"], "default": "medium"},
        ]
        env = patch.dict(os.environ, {"VIBE_HOME": str(self.home), "HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        probe = patch.dict(model_catalog._PROBES, {"codex": lambda: self.models})
        probe.start()
        self.addCleanup(probe.stop)

    def init(self):
        return init_project(self.paths, True)

    def read(self):
        self.assertTrue(self.table.is_file(), "confirmed init must produce a model display")
        return json.loads(self.table.read_text())

    def test_confirmed_init_generates_provenance_without_user_writes(self):
        (self.home / "config.json").write_text(json.dumps({
            "keep": {"untouched": True},
            "model_map": {"codex": {"light": ["manual-small", "backup"]}},
        }))
        before = {p.name: p.read_bytes() for p in self.home.iterdir()}
        result = self.init()
        table = self.read()
        rows = {(r["platform"], r["tier"]): r for r in table["resolutions"]}
        self.assertEqual(rows["codex", "light"]["model"], "manual-small")
        self.assertEqual(rows["codex", "light"]["fallbacks"], ["backup"])
        self.assertEqual(rows["codex", "light"]["source"], "config")
        self.assertEqual(rows["codex", "deep"]["model"], "large")
        self.assertEqual(rows["claude-code", "standard"]["model"], "sonnet")
        self.assertEqual(table["purpose"], "generated_display_only")
        self.assertEqual(table["manual_input"], "$VIBE_HOME/config.json:model_map")
        self.assertTrue(table["generated_at"].endswith("Z"))
        self.assertIn(".vibe/model-catalog.json", result.paths)
        self.assertEqual({p.name: p.read_bytes() for p in self.home.iterdir()}, before)
        self.assertNotIn(str(self.base), self.table.read_text())

    def test_no_confirmation_has_no_model_or_project_side_effect(self):
        result = init_project(self.paths, False)
        self.assertFalse(result.changed)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_same_inputs_preserve_generated_bytes_and_change_flag(self):
        self.init()
        before = self.read()
        before_bytes = self.table.read_bytes()
        result = self.init()
        self.assertFalse(result.changed, result.paths)
        self.assertEqual(self.table.read_bytes(), before_bytes)
        self.assertEqual(self.read()["generated_at"], before["generated_at"])

    def test_directory_change_refreshes_display_without_changing_global_or_tasks(self):
        self.init()
        self.read()
        tasks = self.root / ".vibe/runs/fixture/tasks.json"
        tasks.parent.mkdir(parents=True)
        tasks.write_text('{"bound_model":"keep-in-flight"}\n')
        self.models[0] = dict(self.models[0], slug="new-large")
        result = self.init()
        rows = {(r["platform"], r["tier"]): r for r in self.read()["resolutions"]}
        self.assertEqual(rows["codex", "deep"]["model"], "new-large")
        self.assertIn(".vibe/model-catalog.json", result.paths)
        self.assertEqual(tasks.read_text(), '{"bound_model":"keep-in-flight"}\n')
        self.assertEqual(list(self.home.iterdir()), [])

    def test_manually_edited_table_is_preserved_and_never_becomes_input(self):
        self.init()
        table = self.read()
        table["resolutions"][0]["model"] = "edited-project-only"
        self.table.write_text(json.dumps(table))
        before = self.table.read_bytes()
        result = self.init()
        self.assertEqual(self.table.read_bytes(), before)
        self.assertTrue(any("不是派发输入" in note for note in result.notes), result.notes)
        choice = model_catalog.resolve("codex", "deep", vibe_home=self.home)
        self.assertEqual(choice.model, "large")

    def test_timeout_is_disclosed_without_blocking_or_claiming_validation(self):
        def timeout():
            raise TimeoutError("private path must not enter the display")
        with patch.dict(model_catalog._PROBES, {"codex": timeout}):
            result = self.init()
        rows = [r for r in self.read()["resolutions"] if r["platform"] == "codex"]
        self.assertEqual([(r["model"], r["status"]) for r in rows], [("default", "unknown")] * 3)
        self.assertTrue(any("codex" in note and "默认模型" in note for note in result.notes))
        self.assertNotIn("private path", self.table.read_text())

    def test_invalid_manual_mapping_fails_before_initialization_writes(self):
        (self.home / "config.json").write_text('{"model_map":{"codex":{"normal":["bad"]}}}')
        with self.assertRaisesRegex(model_catalog.ModelMapConfigError, "model_map.codex.normal"):
            self.init()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_two_projects_share_only_global_input_and_keep_project_configuration(self):
        (self.home / "config.json").write_text('{"model_map":{"codex":{"deep":["manual-big"]}}}')
        self.init()
        self.read()
        second = self.base / "second"
        second.mkdir()
        (second / ".vibe").mkdir()
        config = second / ".vibe/config.json"
        config.write_text('{"keep":"project-owned"}\n')
        init_project(ProjectPaths(second), True)
        display = json.loads((second / ".vibe/model-catalog.json").read_text())
        deep = next(r for r in display["resolutions"] if (r["platform"], r["tier"]) == ("codex", "deep"))
        self.assertEqual(deep["model"], "manual-big")
        self.assertEqual(config.read_text(), '{"keep":"project-owned"}\n')
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["config.json"])

    def test_symlinked_display_is_rejected_before_any_initialization_write(self):
        (self.root / ".vibe").mkdir()
        outside = self.base / "outside"
        outside.write_text("keep")
        self.table.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.init()
        self.assertEqual(outside.read_text(), "keep")
        self.assertEqual(sorted(p.name for p in (self.root / ".vibe").iterdir()), ["model-catalog.json"])

    def test_model_identifiers_cannot_leak_personal_paths_into_generated_table(self):
        (self.home / "config.json").write_text(json.dumps({"model_map": {"codex": {
            "deep": ["/Users/private/local-model", "/Users/private/fallback"]
        }}}))
        self.init()
        self.read()
        self.assertNotIn("/Users/private", self.table.read_text())
        self.assertEqual(model_catalog.resolve("codex", "deep", vibe_home=self.home).model,
                         "/Users/private/local-model", "display redaction must not alter existing manual input")

    def test_whitespace_prefixed_personal_paths_are_redacted_without_changing_routing(self):
        self.init()
        tasks = self.root / '.vibe/runs/fixture/tasks.json'
        tasks.parent.mkdir(parents=True)
        tasks.write_text('{"bound_model":"existing-in-flight"}\n')
        bound = tasks.read_bytes()
        config = self.home / 'config.json'
        for whitespace in (' ', '\t', ' \t'):
            for location in ('/Users/synthetic-private/model', '~/synthetic-private/model',
                             r'~\synthetic-private\model', 'C:/Users/synthetic-private/model',
                             r'C:\Users\synthetic-private\model'):
                with self.subTest(whitespace=repr(whitespace), location=location):
                    models = [whitespace + location + '-primary', whitespace + location + '-fallback']
                    config.write_text(json.dumps({'model_map': {'codex': {'deep': models}}}))
                    manual = config.read_bytes()
                    self.init()
                    row = next(item for item in self.read()['resolutions']
                               if (item['platform'], item['tier']) == ('codex', 'deep'))
                    self.assertEqual((row['model'], row['fallbacks']),
                                     ('[redacted path]', ['[redacted path]']))
                    choice = model_catalog.resolve('codex', 'deep', vibe_home=self.home, cache_write=False)
                    self.assertEqual((choice.model, choice.fallbacks), (models[0], (models[1],)))
                    self.assertEqual(config.read_bytes(), manual)
                    self.assertEqual(tasks.read_bytes(), bound)
        config.write_text(json.dumps({'model_map': {'codex': {'deep': [' model-name', '\tfallback-name']}}}))
        self.init()
        row = next(item for item in self.read()['resolutions']
                   if (item['platform'], item['tier']) == ('codex', 'deep'))
        self.assertEqual((row['model'], row['fallbacks']), (' model-name', ['\tfallback-name']))

    def test_init_discloses_available_other_platform_with_the_same_resolver(self):
        report = scan_project(self.paths)
        report.agent_commands = {'workbuddy': True}
        with patch('vibe_guide.initializer.scan_project', return_value=report):
            result = self.init()
        rows = [r for r in self.read()['resolutions'] if r['platform'] == 'workbuddy']
        self.assertEqual([(r['model'], r['status']) for r in rows], [('default', 'unknown')] * 3)
        self.assertTrue(any('workbuddy' in note for note in result.notes))
