"""Integration fixes for the heartbeat contract: unreadable address source, digest registry."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.protocols import SHIPPED_PROTOCOL_DIGESTS, is_shipped_protocol, load_protocol
from vibe_guide.state import RunSnapshot
from vibe_guide.supervisor import heartbeat_interval

BROKEN = ValueError('supervisor lease malformed')


class UnreadableAddressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = ProjectPaths(self.root)
        self.record = self.root / 'usage.json'
        self.record.write_text(json.dumps({'context_tokens': 30000}))
        directory = self.paths.vibe / 'runs/run-1'
        directory.mkdir(parents=True)
        (directory / 'state.json').write_text('{}')
        (self.paths.vibe / 'state.json').write_text('{}')
        self.addCleanup(patch.stopall)
        snapshot = RunSnapshot('run-1', 'plan-1', 1, 'running',
            {'n1': {'status': 'review'}}, {},
            authorization_digest='a' * 64, node_contract_digest='b' * 64)
        patch('vibe_guide.supervisor.load_snapshot', return_value=snapshot).start()
        patch('vibe_guide.adapters.task_provider.require_entry').start()

    def broken(self):
        patch('vibe_guide.supervisor.current_supervisor_address', side_effect=BROKEN).start()

    def check(self, interval):
        self.assertEqual(interval['minutes'], 10)
        self.assertEqual(interval['mode'], 'default')
        self.assertIn('supervisor address unreadable', interval['reason'])
        self.assertIn('lease malformed', interval['reason'])

    def test_interval_defaults_to_ten_minutes(self):
        self.broken()
        self.check(heartbeat_interval(self.paths, 'run-1', decision_only=False))

    def test_decision_tier_does_not_read_address(self):
        self.broken()
        self.assertEqual(heartbeat_interval(self.paths, 'run-1', decision_only=True)['minutes'], 60)

    def test_beat_and_preflight_survive(self):
        self.broken()
        base = ['--run-id', 'run-1', '--accept-decision', '--session-record', str(self.record), '--json']
        beat = run_cli(['supervisor-beat', *base], self.root)
        self.check(beat.payload['heartbeat_interval'])
        pre = run_cli(['supervisor-preflight', *base], self.root)
        self.check(pre.payload['heartbeat_interval'])
        self.assertEqual(beat.payload['state'], pre.payload['state'])


class DigestRegistryTests(unittest.TestCase):
    def test_merge_digests_registered(self):
        digests = SHIPPED_PROTOCOL_DIGESTS['prd-guide']
        for value in ('5b7aa976a0f9b2ac20ca014a38a101635d274bad40d1a57bc4920b179ed5f38e',
                      '578fd47330714a53c2de616aafff3a0a9604a79ff272374aa50d2baa58a5bb35'):
            self.assertIn(value, digests)

    def test_current_text_registered(self):
        self.assertTrue(is_shipped_protocol('prd-guide', load_protocol('prd-guide')))


if __name__ == '__main__':
    unittest.main()
