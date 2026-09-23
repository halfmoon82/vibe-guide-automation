import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.config import (
    DEFAULT_MAX_ACTIVE_WORKER_SESSIONS,
    FIELD_MAX_ACTIVE_WORKER_SESSIONS,
    SOURCE_CONFIG,
    SOURCE_DEFAULT,
    load_project_config,
)
from vibe_guide.initializer import init_project
from vibe_guide.paths import ProjectPaths

FIELD = FIELD_MAX_ACTIVE_WORKER_SESSIONS


def _write_config(root, payload):
    vibe = Path(root) / '.vibe'
    vibe.mkdir(parents=True, exist_ok=True)
    (vibe / 'config.json').write_text(payload, encoding='utf-8')


class LoadProjectConfigTests(unittest.TestCase):
    def test_missing_config_file_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as d:
            config = load_project_config(Path(d))
            self.assertEqual(config.max_active_worker_sessions, DEFAULT_MAX_ACTIVE_WORKER_SESSIONS)
            self.assertEqual(config.source, SOURCE_DEFAULT)

    def test_missing_field_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as d:
            _write_config(d, '{}\n')
            config = load_project_config(Path(d))
            self.assertEqual(config.max_active_worker_sessions, 5)
            self.assertEqual(config.source, SOURCE_DEFAULT)

    def test_explicit_value_is_used_and_sourced_from_config(self):
        with tempfile.TemporaryDirectory() as d:
            _write_config(d, json.dumps({FIELD: 12}) + '\n')
            config = load_project_config(Path(d))
            self.assertEqual(config.max_active_worker_sessions, 12)
            self.assertEqual(config.source, SOURCE_CONFIG)

    def test_boundary_values_one_and_sixty_four_are_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            for value in (1, 64):
                _write_config(d, json.dumps({FIELD: value}) + '\n')
                config = load_project_config(Path(d))
                self.assertEqual(config.max_active_worker_sessions, value)
                self.assertEqual(config.source, SOURCE_CONFIG)

    def test_out_of_range_values_raise_and_name_the_field(self):
        with tempfile.TemporaryDirectory() as d:
            for value in (0, -1, 65):
                _write_config(d, json.dumps({FIELD: value}) + '\n')
                with self.assertRaises(ValueError) as caught:
                    load_project_config(Path(d))
                self.assertIn(FIELD, str(caught.exception))

    def test_non_integer_values_raise_and_name_the_field(self):
        with tempfile.TemporaryDirectory() as d:
            for value in ('5', 2.5, True, False, None, [5]):
                _write_config(d, json.dumps({FIELD: value}) + '\n')
                with self.assertRaises(ValueError) as caught:
                    load_project_config(Path(d))
                self.assertIn(FIELD, str(caught.exception))

    def test_invalid_config_raises_instead_of_silent_default(self):
        with tempfile.TemporaryDirectory() as d:
            _write_config(d, 'not json\n')
            with self.assertRaises(ValueError):
                load_project_config(Path(d))
            _write_config(d, json.dumps([1, 2]) + '\n')
            with self.assertRaises(ValueError):
                load_project_config(Path(d))


class InitMaterializesConfigTests(unittest.TestCase):
    def test_fresh_init_writes_default_max_active_worker_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            p = ProjectPaths.from_cwd(Path(d))
            result = init_project(p, True)
            self.assertTrue(result.changed)
            data = json.loads((p.root / '.vibe' / 'config.json').read_text(encoding='utf-8'))
            self.assertEqual(data[FIELD], 5)

    def test_reinit_preserves_user_changed_value(self):
        with tempfile.TemporaryDirectory() as d:
            p = ProjectPaths.from_cwd(Path(d))
            init_project(p, True)
            config_path = p.root / '.vibe' / 'config.json'
            config_path.write_text(json.dumps({FIELD: 9}) + '\n', encoding='utf-8')

            init_project(p, True)

            data = json.loads(config_path.read_text(encoding='utf-8'))
            self.assertEqual(data[FIELD], 9)
            self.assertEqual(load_project_config(p.root).max_active_worker_sessions, 9)

    def test_legacy_project_without_field_is_untouched_and_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = ProjectPaths.from_cwd(Path(d))
            vibe = p.root / '.vibe'
            vibe.mkdir()
            (vibe / 'config.json').write_text('{}\n', encoding='utf-8')

            init_project(p, True)

            # Existing bytes survive: init must not rewrite a config that
            # predates the field, and loading still yields the default.
            self.assertEqual((vibe / 'config.json').read_text(encoding='utf-8'), '{}\n')
            config = load_project_config(p.root)
            self.assertEqual(config.max_active_worker_sessions, 5)
            self.assertEqual(config.source, SOURCE_DEFAULT)


if __name__ == '__main__':
    unittest.main()
