"""Answered decisions wake the heartbeat until the node leaves blocked_design."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.cli import run_cli
from vibe_guide.decisions import DecisionMailbox
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import RunSnapshot
from vibe_guide.supervisor import set_supervisor_hold, supervisor_preflight


class AnsweredDecisionWakeTests(unittest.TestCase):
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
        patch('vibe_guide.adapters.task_provider.require_entry').start()
        self.request = DecisionMailbox(self.paths).raise_decision(
            run_id='run-1', node_id='n1', question='Pick', options=['A', 'B'])
        self.use_status('blocked_design')

    def use_status(self, status):
        snapshot = RunSnapshot('run-1', 'plan-1', 1, 'running',
            {'n1': {'status': status, 'decision_ids': [self.request['decision_id']]}}, {},
            authorization_digest='a' * 64, node_contract_digest='b' * 64)
        patch('vibe_guide.supervisor.load_snapshot', return_value=snapshot).start()

    def answer(self):
        return DecisionMailbox(self.paths).answer(
            self.request['decision_id'], 'A',
            request_digest=self.request['request_digest'])

    def preflight(self):
        return supervisor_preflight(self.paths, 'run-1', self.record,
                                    single_shift=False, accept_decision=True)

    def test_unanswered_stays_decision(self):
        self.assertEqual(self.preflight()['state'], 'decision')

    def test_answered_blocked_design_is_work_then_idle_after_consumption(self):
        self.answer()
        result = self.preflight()
        self.assertEqual(result['state'], 'work')
        self.assertIn('answered', result['reason'])
        self.assertEqual(result['nodes'], ['n1'])
        self.use_status('running')
        self.assertEqual(self.preflight()['state'], 'idle')

    def test_hold_on_answered_decision_does_not_keep_waking_after_consumption(self):
        set_supervisor_hold(self.paths, 'run-1', 'n1', 'waiting',
                            'product_acceptance', decision_id=self.request['decision_id'])
        self.answer()
        self.assertEqual(self.preflight()['state'], 'work')
        self.use_status('running')
        self.assertEqual(self.preflight()['state'], 'idle')

    def test_corrupt_answer_is_unknown_with_reason(self):
        self.answer()
        path = self.paths.vibe / 'decisions/results/{}.json'.format(self.request['decision_id'])
        path.write_text('{not json')
        result = self.preflight()
        self.assertEqual(result['state'], 'unknown')
        self.assertIn('decision mailbox unreadable', result['reason'])

    def test_decision_answer_prints_resume_hint(self):
        outcome = run_cli(['decision-answer', '--id', self.request['decision_id'],
                           '--choice', 'A', '--digest', self.request['request_digest'],
                           '--json'], self.root)
        self.assertIn('去监工会话说继续', outcome.payload['resume_hint'])
        self.assertIn('下一次心跳自动接上', outcome.payload['resume_hint'])

    def test_protocol_section_describes_auto_consumption(self):
        text = (Path(__file__).resolve().parents[1] / 'vibe_guide/protocols/prd-guide.md').read_text(encoding='utf-8')
        self.assertIn('由运行自动消费', text)
        self.assertNotIn('用户回复后先 `vibe supervisor-hold', text)


if __name__ == '__main__':
    unittest.main()
