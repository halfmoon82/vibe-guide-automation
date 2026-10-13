"""Same-task mechanical scope recovery through the public reconcile entry."""
import copy
import json
import unittest
from unittest.mock import patch

from tests import support_v45_authorize as support
from vibe_guide.cli import _load_plan, run_cli
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import load_events, load_snapshot, save_snapshot
from vibe_guide.task_registry import TaskBinding, load_task_binding, save_task_binding


class ScopeReconcileTests(unittest.TestCase):
    def setUp(self):
        spec = copy.deepcopy(support._NODE_SPEC)
        spec['nodes'][0]['contract']['files'] = ['README.md']
        sibling = copy.deepcopy(spec['nodes'][0])
        sibling.update(id='sibling', depends_on=['probe-node-a'])
        sibling['contract']['files'] = ['tests']
        spec['nodes'].append(sibling)
        with patch.object(support, '_NODE_SPEC', spec):
            self.root = support.publish_complex_probe(self)
        self.paths = ProjectPaths(self.root)
        run_cli(['authorize', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE'], self.root)
        result = run_cli(['monitor', '--plan', 'probe-plan', '--authorize', 'AUTHORIZE'], self.root)
        self.run_id = result.payload['run_id']
        _, self.plan, self.nodes, _ = _load_plan(self.paths, 'probe-plan')
        self.monitor = Monitor(self.paths, self.plan, self.nodes)
        self.snapshot = load_snapshot(self.paths, self.run_id)
        self.node_id = 'probe-node-a'
        current = self.snapshot.nodes[self.node_id]
        binding = TaskBinding(provider='claude-code', mode='visible',
            issue_id=self.node_id, role='developer', task_id='original-developer',
            host='local', run_id=self.run_id, worktree=current['worktree'],
            branch=current['branch'], generation=3, status='running',
            cursor='original:11', allowlist=['README.md'])
        save_task_binding(self.paths, binding)
        current.update(status='running', developer_identity=binding.task_id,
            developer_generation=3, active_role='developer',
            active_task={'role': 'developer', 'task_id': binding.task_id,
                         'generation': 3, 'handle_id': 'original-handle'})
        self.snapshot.handles[self.node_id] = 'original-handle'
        save_snapshot(self.paths, self.snapshot)
        node = self.monitor.nodes[self.node_id]
        auth = self.monitor._require_snapshot_authorization(self.snapshot)
        self.package = {'kind': 'scope_correction', 'node_id': self.node_id,
            'task_binding': load_task_binding(self.paths, self.node_id, 'developer',
                                            run_id=self.run_id).to_dict(),
            'consistency': {'field': 'acceptance_example', 'action': 'test',
                'files': ['tests/test_legacy_prompt.py'], 'candidates': [{
                    'source': 'issue_contract', 'value': node.contract['acceptance_example'],
                    'binding': self.monitor._consistency_binding(auth, node)}]}}
        self.requests = {p: p.read_bytes() for p in
            (self.paths.vibe / 'provider-actions' / 'requests').glob('*.json')}

    def reconcile(self):
        (self.paths.vibe / 'scope-reconcile.json').write_text(json.dumps(self.package))
        return run_cli(['reconcile', '--plan', 'probe-plan', '--run-id', self.run_id,
            '--evidence', '.vibe/scope-reconcile.json', '--json'], self.root)

    def binding(self):
        return load_task_binding(self.paths, self.node_id, 'developer', run_id=self.run_id)

    def test_public_entry_synchronizes_scope_without_new_task_or_request(self):
        before = self.binding().to_dict()
        result = self.reconcile()
        self.assertIn('nodes', result.payload, result.payload)
        current = load_snapshot(self.paths, self.run_id)
        self.assertEqual(current.nodes[self.node_id]['scope_expansions'],
                         ['tests/test_legacy_prompt.py'])
        after = self.binding().to_dict()
        self.assertEqual(after['allowlist'], ['README.md', 'tests/test_legacy_prompt.py'])
        self.assertEqual({k: v for k, v in before.items() if k != 'allowlist'},
                         {k: v for k, v in after.items() if k != 'allowlist'})
        self.assertEqual(current.nodes[self.node_id]['active_task'],
                         self.snapshot.nodes[self.node_id]['active_task'])
        self.assertEqual(current.authorization_digest, self.snapshot.authorization_digest)
        self.assertEqual({p: p.read_bytes() for p in
            (self.paths.vibe / 'provider-actions' / 'requests').glob('*.json')}, self.requests)
        count = len(load_events(self.paths, self.run_id))
        self.assertIn('nodes', self.reconcile().payload)
        self.assertEqual(len(load_events(self.paths, self.run_id)), count)

    def test_stale_binding_or_authorization_is_rejected_before_writes(self):
        original = copy.deepcopy(self.package)
        for field, value in [('task_id', 'another'), ('generation', 4),
                             ('cursor', 'original:10'), ('branch', 'another')]:
            with self.subTest(field=field):
                self.package = copy.deepcopy(original)
                self.package['task_binding'][field] = value
                count = len(load_events(self.paths, self.run_id))
                self.assertEqual(self.reconcile().payload.get('status'), 'blocked_unknown')
                self.assertEqual(len(load_events(self.paths, self.run_id)), count)
                self.assertEqual(self.binding().allowlist, ['README.md'])
        self.package = copy.deepcopy(original)
        self.package['consistency']['candidates'][0]['binding']['authorization_digest'] = '0' * 64
        self.assertEqual(self.reconcile().payload.get('status'), 'blocked_unknown')

    def test_nonmechanical_scope_and_changed_acceptance_are_rejected(self):
        original = copy.deepcopy(self.package)
        for change in ('file', 'value'):
            with self.subTest(change=change):
                self.package = copy.deepcopy(original)
                if change == 'file':
                    self.package['consistency']['files'] = ['vibe_guide/new_feature.py']
                else:
                    self.package['consistency']['candidates'][0]['value'] = 'different acceptance'
                self.assertEqual(self.reconcile().payload.get('status'), 'blocked_unknown')
                self.assertEqual(self.binding().allowlist, ['README.md'])

    def test_write_ahead_event_recovers_scope_and_binding_after_interruption(self):
        with patch('vibe_guide.monitor.save_task_binding', side_effect=OSError('disk interruption')):
            result = self.reconcile()
        self.assertEqual(result.payload.get('status'), 'blocked_unknown')
        self.assertEqual(self.binding().allowlist, ['README.md'])
        self.assertTrue(any(e['event'] == 'scope_correction_registered'
                            for e in load_events(self.paths, self.run_id)))
        result = self.reconcile()
        self.assertIn('nodes', result.payload, result.payload)
        self.assertEqual(self.binding().allowlist, ['README.md', 'tests/test_legacy_prompt.py'])
        current = load_snapshot(self.paths, self.run_id)
        self.assertEqual(current.nodes[self.node_id]['scope_expansions'],
                         ['tests/test_legacy_prompt.py'])

    def test_terminated_binding_cannot_gain_scope_from_stale_active_snapshot(self):
        binding = self.binding()
        binding.status = 'archived'
        save_task_binding(self.paths, binding)
        self.package['task_binding'] = self.binding().to_dict()
        count = len(load_events(self.paths, self.run_id))
        self.assertEqual(self.reconcile().payload.get('status'), 'blocked_unknown')
        self.assertEqual(self.binding().allowlist, ['README.md'])
        self.assertEqual(len(load_events(self.paths, self.run_id)), count)

    def test_another_writer_occupying_test_path_is_rejected(self):
        snapshot = load_snapshot(self.paths, self.run_id)
        snapshot.nodes['sibling'].update(status='running', active_role='developer')
        save_snapshot(self.paths, snapshot)
        count = len(load_events(self.paths, self.run_id))
        self.assertEqual(self.reconcile().payload.get('status'), 'blocked_unknown')
        self.assertEqual(self.binding().allowlist, ['README.md'])
        self.assertEqual(len(load_events(self.paths, self.run_id)), count)

    def test_registry_writer_root_drift_is_not_authorized_by_matching_request(self):
        binding = self.binding()
        binding.worktree = '.worktrees/foreign'
        # An externally corrupted registry is observable evidence, not a new
        # authorized writer root. Bypass registry mutation validation only to
        # model that existing corrupt file; public reconcile remains real.
        tasks = self.paths.vibe / 'runs' / self.run_id / 'tasks.json'
        raw = json.loads(tasks.read_text())
        def drift(value):
            if isinstance(value, dict):
                if value.get('task_id') == 'original-developer':
                    value['worktree'] = '.worktrees/foreign'
                for child in value.values(): drift(child)
            elif isinstance(value, list):
                for child in value: drift(child)
        drift(raw)
        tasks.write_text(json.dumps(raw))
        self.package['task_binding'] = self.binding().to_dict()
        self.assertEqual(self.reconcile().payload.get('status'), 'blocked_unknown')
        self.assertEqual(self.binding().allowlist, ['README.md'])

    def test_archive_race_during_scope_save_cannot_resurrect_original_task(self):
        real_save = save_task_binding
        def archived_before_save(paths, binding, **kwargs):
            archived = load_task_binding(paths, self.node_id, 'developer', run_id=self.run_id)
            archived.status = 'archived'
            real_save(paths, archived)
            return real_save(paths, binding, **kwargs)
        with patch('vibe_guide.monitor.save_task_binding', side_effect=archived_before_save):
            result = self.reconcile()
        self.assertEqual(result.payload.get('status'), 'blocked_unknown')
        self.assertEqual(self.binding().status, 'archived')
        self.assertEqual(self.binding().allowlist, ['README.md'])

    def test_malformed_replay_proof_reports_unknown_without_cli_crash(self):
        for value in (None, [], 'bad'):
            with self.subTest(proof=value):
                snapshot = load_snapshot(self.paths, self.run_id)
                previous = snapshot.event_sequence
                self.monitor._record(snapshot, 'scope_correction_registered', {
                    'run_id': self.run_id, 'node_id': self.node_id, 'proof': value})
                result = self.reconcile()
                self.assertEqual(result.payload.get('status'), 'blocked_unknown')
                # Restore this isolated corrupt-event fixture before next case.
                event_path = self.paths.vibe / 'runs' / self.run_id / 'events.jsonl'
                lines = event_path.read_text().splitlines()
                event_path.write_text('\n'.join(lines[:previous]) + '\n')

    def test_missing_active_handle_cannot_authorize_scope(self):
        snapshot = load_snapshot(self.paths, self.run_id)
        snapshot.nodes[self.node_id]['active_task'].pop('handle_id')
        snapshot.handles.pop(self.node_id)
        save_snapshot(self.paths, snapshot)
        self.assertEqual(self.reconcile().payload.get('status'), 'blocked_unknown')
        self.assertEqual(self.binding().allowlist, ['README.md'])


if __name__ == '__main__':
    unittest.main()
