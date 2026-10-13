"""PRD #177 D1: `supervisor_single_shift` turns off growth-based rotation.

With the switch on, one supervisor session serves the whole run and the
preflight rotates only at the absolute hard cap.  With the switch absent the
shipped growth rule is unchanged (covered by the existing rotation tests).
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.cli import run_cli
from vibe_guide.config import FIELD_SUPERVISOR_SINGLE_SHIFT, load_project_config
from vibe_guide.paths import ProjectPaths
from vibe_guide.protocols import load_protocol
from vibe_guide.supervisor import register_supervisor_address, supervisor_preflight


class SingleShiftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = ProjectPaths(self.root)
        self.record = self.root / 'session.json'
        # Takeover baseline 20k: 130k is 110k growth, past the 80k threshold.
        self.record.write_text(json.dumps({'context_tokens': 20000}))
        register_supervisor_address(
            self.paths, 'run-1',
            {'provider': 'codex', 'session_id': 'shift', 'host': 'local'},
            session_record=self.record,
        )

    def write_config(self, data):
        (self.root / '.vibe').mkdir(exist_ok=True)
        (self.root / '.vibe' / 'config.json').write_text(json.dumps(data), encoding='utf-8')

    def preflight(self, tokens, **kwargs):
        self.record.write_text(json.dumps({'context_tokens': tokens}))
        with patch('vibe_guide.supervisor._mailbox_state', return_value={'state': 'work'}):
            return supervisor_preflight(self.paths, 'run-1', self.record, **kwargs)

    def test_default_keeps_growth_rotation(self):
        result = self.preflight(130000)
        self.assertEqual(result['state'], 'rotate')
        self.assertEqual(result['reason'], 'context grew past threshold since takeover')

    def test_explicit_false_keeps_growth_rotation(self):
        self.write_config({FIELD_SUPERVISOR_SINGLE_SHIFT: False})
        self.assertEqual(self.preflight(130000)['state'], 'rotate')

    def test_switch_on_does_not_rotate_on_growth(self):
        self.write_config({FIELD_SUPERVISOR_SINGLE_SHIFT: True})
        result = self.preflight(130000)
        self.assertEqual(result['state'], 'work')
        self.assertTrue(result['single_shift'])
        # The handoff summary is still prepared ahead of the hard cap.
        self.assertTrue(result['prepare_handoff'])

    def test_switch_on_still_rotates_at_hard_cap(self):
        self.write_config({FIELD_SUPERVISOR_SINGLE_SHIFT: True})
        result = self.preflight(150000)
        self.assertEqual(result['state'], 'rotate')
        self.assertEqual(result['reason'], 'context over hard cap')

    def test_switch_on_respects_explicit_temporary_cap(self):
        self.write_config({FIELD_SUPERVISOR_SINGLE_SHIFT: True})
        self.assertEqual(self.preflight(200000, context_hard_cap=250000)['state'], 'work')
        self.assertEqual(self.preflight(250000, context_hard_cap=250000)['state'], 'rotate')

    def test_argument_overrides_config(self):
        self.write_config({FIELD_SUPERVISOR_SINGLE_SHIFT: True})
        self.assertEqual(self.preflight(130000, single_shift=False)['state'], 'rotate')

    def test_invalid_switch_is_unknown_not_silently_default(self):
        self.write_config({FIELD_SUPERVISOR_SINGLE_SHIFT: 'yes'})
        with self.assertRaises(ValueError):
            load_project_config(self.root)
        result = self.preflight(130000)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['reason'], 'supervisor_single_shift invalid')

    def test_invalid_switch_cannot_hide_hard_cap(self):
        self.write_config({FIELD_SUPERVISOR_SINGLE_SHIFT: 'yes'})
        self.assertEqual(self.preflight(150000)['state'], 'rotate')

    def test_unrelated_invalid_field_does_not_block_preflight(self):
        self.write_config({'max_active_worker_sessions': 0, FIELD_SUPERVISOR_SINGLE_SHIFT: True})
        result = self.preflight(130000)
        self.assertEqual(result['state'], 'work')
        self.assertTrue(result['single_shift'])

    def test_unreadable_config_keeps_shipped_rotation_with_warning(self):
        (self.root / '.vibe').mkdir(exist_ok=True)
        (self.root / '.vibe' / 'config.json').write_text('{broken', encoding='utf-8')
        result = self.preflight(130000)
        self.assertEqual(result['state'], 'rotate')
        self.assertEqual(self.preflight(150000)['state'], 'rotate')
        low = self.preflight(60000)
        self.assertEqual(low['state'], 'work')
        self.assertFalse(low['single_shift'])
        self.assertIn('unreadable', low['config_warning'])

    def test_cli_preflight_reads_switch(self):
        self.write_config({FIELD_SUPERVISOR_SINGLE_SHIFT: True})
        self.record.write_text(json.dumps({'context_tokens': 130000}))
        with patch('vibe_guide.supervisor._mailbox_state', return_value={'state': 'work'}), \
                patch('vibe_guide.cli.supervisor_handoff', return_value=({'handoff_summary': {}}, '')):
            result = run_cli(['supervisor-preflight', '--run-id', 'run-1',
                              '--session-record', str(self.record), '--json'], self.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertEqual(result.payload['state'], 'work')
        self.assertTrue(result.payload['single_shift'])

    def test_protocol_documents_switch_and_keeps_rotate_branch(self):
        text = load_protocol('prd-guide')
        self.assertIn('"supervisor_single_shift": true', text)
        self.assertIn('输出 `rotate`', text)


if __name__ == '__main__':
    unittest.main()
