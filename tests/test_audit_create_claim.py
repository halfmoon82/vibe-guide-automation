"""Offline legacy create interleavings; no desktop or daemon is invoked."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.paths import ProjectPaths


class LegacyCreateSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ProviderActionStore(ProjectPaths(Path(self.temp.name)))
        self.action = self.store.request(operation='create', provider='fixture',
            run_id='run-fixture', issue_id='node-a', role='developer', generation=1,
            native_tool='fixture_create', request={'authorization_digest': 'a' * 64})

    def test_missing_claim_cannot_prove_request_unconsumed(self):
        self.assertIsNotNone(self.store.claim_evidence('run-fixture', 'node-a'))

    def test_retirement_cannot_infer_no_claim_from_absent_lock(self):
        from vibe_guide.plan_amendment import _retire_claim_evidence
        self.assertEqual(_retire_claim_evidence(self.store, 'run-fixture', 'node-a', 'b' * 64),
                         'unknown_create_exposure')

    def test_legacy_request_cannot_be_superseded_after_it_was_read(self):
        cached, = self.store.pending('run-fixture')
        hidden = self.store.supersede_unclaimed_creates(
            'run-fixture', 'node-a', 'developer', 1, 'a' * 64)
        self.assertEqual(hidden, [])
        self.assertEqual(self.store.pending('run-fixture'), [cached])
        self.assertTrue(self.store.complete(cached['action_id'],
            {'binding': {'task_id': 'late-task'}}, request_digest=cached['request_digest']))
        self.assertEqual(self.store.result(cached['action_id'])['binding']['task_id'], 'late-task')


class ControlledCreateSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ProviderActionStore(ProjectPaths(Path(self.temp.name)))

    def staged(self):
        self.assertTrue(callable(getattr(self.store, 'stage_create', None)),
                        'controlled create staging is not implemented')
        return self.store.request(operation='create', provider='fixture', run_id='run-fixture',
            issue_id='node-a', role='developer', generation=1,
            native_tool='fixture_create', request={'authorization_digest': 'a' * 64, 'prompt': 'fixture'},
            _controlled_create=True)

    def test_claim_and_revoke_are_mutually_exclusive(self):
        action = self.staged()
        with ThreadPoolExecutor(max_workers=2) as executor:
            claim = executor.submit(self.store.stage_create, action['action_id'], 'exposure-a', action['request_digest'])
            revoke = executor.submit(self.store.revoke_create, action['action_id'], action['request_digest'])
            outcomes = [claim.result(), revoke.result()]
        self.assertIn((outcomes[0]['status'], outcomes[1]['status']),
                      {('claimed', 'already_claimed'), ('stale', 'stale')})

    def test_published_pending_create_is_not_proof_of_non_exposure(self):
        action = self.staged()
        self.assertEqual(self.store.claim_evidence('run-fixture', 'node-a'),
                         'controlled_pending_create')
        self.assertTrue((self.store.root / 'requests' / (action['action_id'] + '.json')).is_file())

    def test_controlled_request_directory_is_non_executable_projection(self):
        action = self.staged()
        raw = (self.store.root / 'requests' / (action['action_id'] + '.json')).read_text()
        self.assertNotIn('fixture_create', raw)
        self.assertNotIn('prompt', raw)
        self.assertIn(action['action_id'], raw)
        exposed = self.store.stage_create(action['action_id'], 'exposure-safe', action['request_digest'])
        self.assertEqual(exposed['native_tool'], 'fixture_create')
        self.assertEqual(exposed['request']['prompt'], 'fixture')

    def test_published_projection_can_be_superseded_only_via_controlled_state(self):
        action = self.staged()
        self.assertEqual(self.store.supersede_unclaimed_creates(
            'run-fixture', 'node-a', 'developer', 1, 'a' * 64), [action['action_id']])
        self.assertEqual(self.store.pending('run-fixture'), [])

    def test_controlled_pending_can_be_superseded_only_with_its_claim_state(self):
        action = self.staged()
        self.assertEqual(self.store.supersede_unclaimed_creates(
            'run-fixture', 'node-a', 'developer', 1, 'a' * 64), [action['action_id']])
        self.assertEqual(self.store.pending('run-fixture'), [])

    def test_corrupt_controlled_claim_blocks_supersede(self):
        action = self.staged()
        claim = self.store.root / 'create-claims' / (action['action_id'] + '.json')
        claim.write_text('{"status":"pending"}', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.store.supersede_unclaimed_creates(
                'run-fixture', 'node-a', 'developer', 1, 'a' * 64)

    def test_unclaimed_callback_is_preserved_as_evidence_of_exposure(self):
        action = self.staged()
        payload = {'binding': {'task_id': 'existing-task'}}
        self.assertTrue(self.store.complete(action['action_id'], payload,
                                            request_digest=action['request_digest']))
        self.assertEqual(self.store.result(action['action_id']), payload)

    def test_controlled_callback_defaults_to_bound_digest_when_omitted(self):
        action = self.staged()
        self.store.stage_create(action['action_id'], 'exposure-a', action['request_digest'])
        self.assertTrue(self.store.complete(action['action_id'], {'binding': {'task_id': 'task'}}))
        self.assertEqual(self.store.result(action['action_id']), {'binding': {'task_id': 'task'}})

    def test_private_payload_without_claim_state_fails_closed(self):
        action = self.staged()
        claim = self.store.root / 'create-claims' / (action['action_id'] + '.json')
        claim.unlink()
        with self.assertRaises(PermissionError):
            self.store.complete(action['action_id'], {'binding': {'task_id': 'orphan-private'}})

    def test_tampered_private_payload_cannot_be_claimed(self):
        action = self.staged()
        payload = self.store.root / 'create-payloads' / (action['action_id'] + '.json')
        raw = payload.read_text(encoding='utf-8').replace('fixture_create', 'other_create')
        payload.write_text(raw, encoding='utf-8')
        with self.assertRaises(ValueError):
            self.store.stage_create(action['action_id'], 'exposure-a', action['request_digest'])

    def test_public_digest_tamper_cannot_be_claimed(self):
        action = self.staged()
        public = self.store.root / 'requests' / (action['action_id'] + '.json')
        raw = public.read_text(encoding='utf-8').replace('controlled_create', 'other_create')
        public.write_text(raw, encoding='utf-8')
        with self.assertRaises(ValueError):
            self.store.stage_create(action['action_id'], 'exposure-a', action['request_digest'])

    def test_stale_claim_cannot_be_reopened_by_digest_replay(self):
        action = self.staged()
        self.assertEqual(self.store.revoke_create(action['action_id'], action['request_digest'])['status'], 'stale')
        self.assertEqual(self.store.stage_create(action['action_id'], 'exposure-a', action['request_digest'])['status'], 'stale')
        self.assertFalse(self.store.complete(action['action_id'], {'binding': {'task_id': 'late'}},
                                             request_digest=action['request_digest']))
        self.assertIsNone(self.store.result(action['action_id']))

    def test_claimed_lineage_tamper_blocks_replay(self):
        action = self.staged()
        first = self.store.stage_create(action['action_id'], 'exposure-a', action['request_digest'])
        self.assertEqual(first['status'], 'claimed')
        claim = self.store.root / 'create-claims' / (action['action_id'] + '.json')
        state = self.store._read(claim)
        state['lineage']['authorization_digest'] = 'b' * 64
        state['state_digest'] = __import__('vibe_guide.adapters.task_provider', fromlist=['_canonical_digest'])._canonical_digest(
            {key: value for key, value in state.items() if key != 'state_digest'})
        claim.write_text(__import__('json').dumps(state), encoding='utf-8')
        with self.assertRaises(PermissionError):
            self.store.stage_create(action['action_id'], 'exposure-a', action['request_digest'])

    def test_recovery_fence_blocks_public_claim(self):
        action = self.store.request(operation='create', provider='fixture', run_id='run-fixture',
            issue_id='node-a', role='developer', generation=1, native_tool='fixture_create',
            request={'authorization_digest': 'a' * 64, 'plan_id': 'plan-fence', 'prompt': 'fixture'},
            _controlled_create=True)
        fence = self.store.paths.vibe / 'plans' / 'plan-fence' / '.initial-recovery-pending.json'
        fence.parent.mkdir(parents=True)
        fence.write_text('{}', encoding='utf-8')
        self.assertEqual(self.store.stage_create(action['action_id'], 'exposure-a', action['request_digest'])['status'], 'unknown')

    def test_stale_rejects_late_claim_and_retains_late_callback_without_result(self):
        action = self.staged()
        self.assertEqual(self.store.revoke_create(action['action_id'], action['request_digest'])['status'], 'stale')
        self.assertEqual(self.store.stage_create(action['action_id'], 'exposure-a', action['request_digest'])['status'], 'stale')
        first = self.store.complete(action['action_id'], {'binding': {'task_id': 'late'}},
                                    request_digest=action['request_digest'])
        second = self.store.complete(action['action_id'], {'binding': {'task_id': 'late'}},
                                     request_digest=action['request_digest'])
        self.assertEqual(first, second)
        observations = list((self.store.root / 'observations').glob(action['action_id'] + '-*.json'))
        self.assertEqual(len(observations), 1)
        self.assertIsNone(self.store.result(action['action_id']))
        self.assertEqual(self.store.pending('run-fixture'), [])

    def test_claim_replay_does_not_authorize_second_invocation(self):
        action = self.staged()
        first = self.store.stage_create(action['action_id'], 'exposure-a', action['request_digest'])
        again = self.store.stage_create(action['action_id'], 'exposure-b', action['request_digest'])
        self.assertEqual(first['status'], 'claimed')
        self.assertEqual(again['status'], 'already_claimed')
        self.assertNotIn('request', again)

    def test_same_exposure_replay_is_idempotent(self):
        action = self.staged()
        first = self.store.stage_create(action['action_id'], 'exposure-a', action['request_digest'])
        again = self.store.stage_create(action['action_id'], 'exposure-a', action['request_digest'])
        self.assertEqual(first['status'], 'claimed')
        self.assertEqual(again['status'], 'already_claimed')
        self.assertEqual(again['action_id'], action['action_id'])
        self.assertEqual(again['request_digest'], action['request_digest'])
        self.assertEqual(again['exposure_id'], 'exposure-a')
        for key in ('request', 'native_tool', 'prompt'):
            self.assertNotIn(key, again)


if __name__ == '__main__':
    unittest.main()
