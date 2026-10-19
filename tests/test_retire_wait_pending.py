"""retire_wait clears the handle's dangling pending_action / pending_operation."""
import tempfile
import unittest
from pathlib import Path

from vibe_guide.adapters.task_provider import ProviderActionStore, _canonical_digest
from vibe_guide.paths import ProjectPaths

RUN, NODE = 'run-1', 'node-a'


class RetireWaitPendingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = ProviderActionStore(ProjectPaths(Path(directory.name)))

    def action(self, operation, sequence):
        basis = {'operation': operation, 'provider': 'p', 'run_id': RUN, 'issue_id': NODE,
                 'role': 'developer', 'generation': 1, 'sequence': sequence}
        action_id = 'action-' + _canonical_digest(basis)[:32]
        payload = {'schema_version': 1, 'action_id': action_id, **basis,
                   'native_tool': 't', 'request': {}}
        payload['request_digest'] = _canonical_digest(payload)
        self.store._atomic(self.store._directory('requests') / (action_id + '.json'), payload)
        return action_id

    def handle(self, pending, operation):
        path = self.store._directory('handles') / 'h.json'
        self.store._atomic(path, {'handle_id': 'h', 'run_id': RUN, 'node_id': NODE,
                                  'role': 'developer', 'generation': 1,
                                  'pending_action': pending, 'pending_operation': operation})
        return path

    def test_retire_clears_the_handle(self):
        wait = self.action('wait', 1)
        path = self.handle(wait, 'wait')
        self.store.retire_wait(wait, 'why')
        metadata = self.store._read(path)
        self.assertIsNone(metadata['pending_action'])
        self.assertIsNone(metadata['pending_operation'])
        self.assertTrue((self.store.root / 'stale' / (wait + '.json')).is_file())
        self.assertEqual(self.store.pending(RUN), [])

    def test_other_pending_action_is_refused_untouched(self):
        wait = self.action('wait', 1)
        other = self.action('resume', 2)
        path = self.handle(other, 'resume')
        with self.assertRaises(ValueError) as caught:
            self.store.retire_wait(wait, 'why')
        self.assertIn('h', str(caught.exception))
        self.assertIn(other, str(caught.exception))
        self.assertEqual(self.store._read(path)['pending_action'], other)
        self.assertFalse((self.store.root / 'stale' / (wait + '.json')).exists())

    def test_no_handle_still_retires(self):
        wait = self.action('wait', 1)
        self.store.retire_wait(wait, 'why')
        self.assertTrue((self.store.root / 'stale' / (wait + '.json')).is_file())


if __name__ == '__main__':
    unittest.main()
