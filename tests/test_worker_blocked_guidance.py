"""Execute shipped role/stage examples through the public reporting entries.

The examples are data, not new required fields. No provider, generation gate,
landing gate or scope transaction is mocked. All writes stay in temp fixtures.
"""
import copy
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import support_vibe_home as hermetic
from tests import test_scope_reconcile_entry as scope_fixture
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.protocols import load_protocol
from vibe_guide.state import load_snapshot
from vibe_guide.supervisor import register_supervisor_address
from vibe_guide.task_registry import TaskBinding, load_task_binding, save_task_binding


setUpModule, tearDownModule = hermetic.module_hooks()


def examples(case):
    path = Path(__file__).resolve().parents[1] / 'vibe_guide/protocols/blocked-engineering-examples.md'
    case.assertTrue(path.is_file(), 'missing executable blocked-engineering role/stage examples')
    blocks = re.findall(r'```json\n(.*?)\n```', load_protocol('blocked-engineering-examples'), re.S)
    case.assertEqual(len(blocks), 1, 'one executable example table')
    values = json.loads(blocks[0])
    case.assertEqual({(v['role'], v['stage']) for v in values}, {
        ('developer', 'initial'), ('developer', 'continuation'), ('developer', 'rework'),
        ('reviewer', 'initial'), ('reviewer', 'continuation'), ('reviewer', 'rework'),
    })
    case.assertEqual(len(values), 6)
    return values


class BlockedGuidanceEntryTests(unittest.TestCase):
    maxDiff = None

    def fixture(self, example, missing_writer=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)],
                       check=True, capture_output=True)
        writer = root / '.worktrees/guidance'
        if not missing_writer:
            writer.mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', '-b', 'node/guidance', str(writer)],
                           check=True, capture_output=True)
        paths = ProjectPaths(root)
        store = ProviderActionStore(paths)
        role, generation = example['role'], example['generation']
        binding = TaskBinding(provider='codex', mode='visible', issue_id='guidance',
            role=role, task_id=example['task_id'], host='local', run_id='run-guidance',
            worktree='.worktrees/guidance', branch='node/guidance', generation=generation,
            cursor='original:7', status='running', allowlist=['README.md'])
        # Public mailbox request creates only an isolated pending wait, no
        # native task. Landing reads this request child_binding, not just the
        # registry. The real unborn Git fixture needs no commit or user config;
        # it exercises branch/status checks, not committed-history acceptance.
        action = store.request(operation='wait', provider='codex', run_id='run-guidance',
            issue_id='guidance', role=role, generation=generation,
            native_tool='codex_app__wait_threads', request={
                'threadId': example['task_id'], 'child_binding': binding.to_dict()})
        self.assertEqual(action['request']['child_binding'], binding.to_dict())
        save_task_binding(paths, binding)
        return root, paths, store, action

    def report(self, root, example, payload=None, role=None, generation=None):
        return run_cli(['worker-deliver', '--project-root', str(root),
            '--run-id', 'run-guidance', '--node', 'guidance',
            '--role', role or example['role'],
            '--generation', str(generation if generation is not None else example['generation']),
            '--payload', json.dumps(payload if payload is not None else example),
            '--json'], root)

    def files(self, paths):
        return {str(p.relative_to(paths.vibe)): p.read_bytes()
                for p in paths.vibe.rglob('*') if p.is_file() and not p.name.endswith('.lock')}

    def test_all_role_stage_examples_archive_real_facts_without_completion_or_identity_change(self):
        for example in examples(self):
            with self.subTest(role=example['role'], stage=example['stage']):
                payload = example
                self.assertEqual(payload['event'], 'blocked_engineering')
                self.assertEqual(payload['status'], 'blocked')
                self.assertIs(payload['implementation_complete'], False)
                self.assertNotIn('delivery_evidence', payload)
                # These facts enable diagnosis; the engine deliberately does
                # not turn this illustrative table into a universal form gate.
                for key in ('failed_step', 'attempted_actions', 'missing_conditions',
                            'evidence_refs', 'observed_at', 'next_action'):
                    self.assertTrue(payload[key], key)
                root, paths, store, action = self.fixture(example)
                before = load_task_binding(paths, 'guidance', example['role'],
                                           run_id='run-guidance').to_dict()
                result = self.report(root, example)
                self.assertEqual(result.exit_code, 0, result.payload)
                self.assertTrue(result.payload['recorded'])
                self.assertTrue(result.payload['consumed'])
                self.assertEqual(store.archived_payload('run-guidance', 'guidance',
                    example['role'], example['generation']), payload)
                receipt = store.result(action['action_id'])
                self.assertEqual(receipt['event'], 'blocked_engineering')
                # Transport status=completed is not business completion.
                self.assertIs(receipt['implementation_complete'], False)
                self.assertEqual((receipt['task_id'], receipt['role'], receipt['generation']),
                    (example['task_id'], example['role'], example['generation']))
                self.assertEqual(load_task_binding(paths, 'guidance', example['role'],
                    run_id='run-guidance').to_dict(), before)
                count = len(list((store.root / 'results').glob('*.json')))
                duplicate = self.report(root, example)
                self.assertTrue(duplicate.payload['duplicate'])
                self.assertEqual(len(list((store.root / 'results').glob('*.json'))), count)

    def test_missing_writer_still_archives_blocked_facts_without_granting_write_permission(self):
        for example in examples(self):
            with self.subTest(role=example['role'], stage=example['stage']):
                root, paths, store, action = self.fixture(example, missing_writer=True)
                before = load_task_binding(paths, 'guidance', example['role'],
                                           run_id='run-guidance').to_dict()
                result = self.report(root, example)
                receipt = store.result(action['action_id'])
                # Collect every observable even on RED: a refused report must
                # not hide its missing archive/result behind an exit assertion.
                actual = {
                    'exit_code': result.exit_code,
                    'recorded': result.payload.get('recorded'),
                    'consumed': result.payload.get('consumed'),
                    'archive': store.archived_payload('run-guidance', 'guidance',
                        example['role'], example['generation']),
                    'receipt_event': receipt.get('event') if receipt else None,
                    'receipt_incomplete': receipt.get('implementation_complete') if receipt else None,
                    'receipt_identity': tuple(receipt.get(k) for k in
                        ('task_id', 'role', 'generation')) if receipt else None,
                    'binding_unchanged': load_task_binding(paths, 'guidance', example['role'],
                        run_id='run-guidance').to_dict() == before,
                    'writer_created': (root / '.worktrees/guidance').exists(),
                }
                self.assertEqual(actual, {
                    'exit_code': 0, 'recorded': True, 'consumed': True,
                    'archive': example, 'receipt_event': 'blocked_engineering',
                    'receipt_incomplete': False, 'binding_unchanged': True,
                    'receipt_identity': (example['task_id'], example['role'], example['generation']),
                    'writer_created': False,
                }, result.payload)

    def test_undispatched_role_stale_or_future_generation_is_rejected_without_mutation(self):
        for example in examples(self):
            root, paths, store, action = self.fixture(example)
            before = self.files(paths)
            wrong_role = 'reviewer' if example['role'] == 'developer' else 'developer'
            candidates = [(wrong_role, example['generation']),
                          (example['role'], example['generation'] + 1)]
            if example['generation'] > 1:
                candidates.append((example['role'], example['generation'] - 1))
            for role, generation in candidates:
                with self.subTest(role=role, generation=generation, example=example['stage']):
                    result = self.report(root, example, role=role, generation=generation)
                    self.assertNotEqual(result.exit_code, 0, result.payload)
                    self.assertEqual(result.payload['status'], 'blocked_invalid')
                    self.assertEqual(self.files(paths), before)
                    self.assertIsNone(store.result(action['action_id']))

    def test_blocked_facts_cannot_be_relabeled_as_a_completed_delivery(self):
        for example in examples(self):
            root, paths, _, _ = self.fixture(example)
            for event in ('delivered', 'complete'):
                with self.subTest(role=example['role'], stage=example['stage'], event=event):
                    payload = copy.deepcopy(example)
                    payload.update(event=event, delivery_evidence={
                        'completion_marker': 'NOT_A_REAL_COMPLETION',
                        'delivery_path': 'blocked.md', 'thread_status': 'completed'})
                    before = self.files(paths)
                    result = self.report(root, example, payload=payload)
                    self.assertNotEqual(result.exit_code, 0, result.payload)
                    self.assertIn('blocked self-report', result.payload['reason'])
                    self.assertEqual(self.files(paths), before)

    def test_blocked_facts_cannot_be_relabeled_as_accepted_or_enter_the_acceptance_mailbox(self):
        for example in examples(self):
            with self.subTest(role=example['role'], stage=example['stage']):
                root, paths, store, action = self.fixture(example)
                payload = copy.deepcopy(example)
                payload.update(event='accepted', delivery_evidence={
                    'completion_marker': 'NOT_A_REAL_COMPLETION',
                    'delivery_path': 'blocked.md', 'thread_status': 'completed'})
                before = self.files(paths)
                binding = load_task_binding(paths, 'guidance', example['role'],
                                            run_id='run-guidance').to_dict()
                result = self.report(root, example, payload=payload)
                self.assertEqual({
                    'rejected': result.exit_code != 0,
                    'archive': store.archived_payload('run-guidance', 'guidance',
                        example['role'], example['generation']),
                    'result': store.result(action['action_id']),
                    'business_files_unchanged': self.files(paths) == before,
                    'binding_unchanged': load_task_binding(paths, 'guidance', example['role'],
                        run_id='run-guidance').to_dict() == binding,
                }, {
                    'rejected': True, 'archive': None, 'result': None,
                    'business_files_unchanged': True, 'binding_unchanged': True,
                }, result.payload)

    def test_early_report_is_not_lost_and_is_consumed_once_by_same_generation_wait(self):
        for example in examples(self):
            root, paths, store, action = self.fixture(example)
            # A completed visibility poll is not a report consumer. A later
            # wait must pick up the report without creating another task.
            self.assertTrue(store.complete(action['action_id'], {'status': 'running'},
                request_digest=action['request_digest'], native=False))
            early = self.report(root, example)
            self.assertEqual(early.exit_code, 0, early.payload)
            self.assertFalse(early.payload['consumed'])
            (paths.vibe / 'state.json').write_text(json.dumps({
                'workflow_version': 4, 'execution_mode': 'sdd_first',
                'session_gate': 's0_required', 'capability_contract_required': True}))
            late = store.request(operation='wait', provider='codex', run_id='run-guidance',
                issue_id='guidance', role=example['role'], generation=example['generation'],
                sequence=1, native_tool='codex_app__wait_threads',
                request={'threadId': example['task_id'],
                         'child_binding': action['request']['child_binding']})
            self.assertEqual(store.result(late['action_id'])['event'], 'blocked_engineering')
            self.assertTrue(self.report(root, example).payload['duplicate'])
            self.assertEqual(len(list((store.root / 'requests').glob('*.json'))), 2)

    def test_wake_address_is_queried_for_exact_role_generation_and_missing_address_is_unknown(self):
        for example in examples(self):
            root, paths, store, _ = self.fixture(example)
            args = ['supervisor-address', '--project-root', str(root),
                    '--run-id', 'run-guidance', '--node', 'guidance',
                    '--role', example['role'], '--generation', str(example['generation']), '--json']
            missing = run_cli(args, root)
            self.assertEqual(missing.payload['status'], 'unknown')
            self.assertNotIn('wake', missing.payload)
            register_supervisor_address(paths, 'run-guidance', {
                'provider': 'codex', 'session_id': 'guidance-supervisor', 'host': 'local'})
            before = self.files(paths)
            result = run_cli(args, root)
            self.assertEqual(result.exit_code, 0, result.payload)
            self.assertEqual(result.payload['wake']['channel'], 'send_message_to_thread')
            self.assertEqual(result.payload['wake']['target'], 'guidance-supervisor')
            self.assertEqual(result.payload['wake']['signal'],
                'vibe-wake run=run-guidance node=guidance role={} generation={}'.format(
                    example['role'], example['generation']))
            self.assertEqual(self.files(paths), before)
            self.assertIsNone(store.archived_payload('run-guidance', 'guidance',
                example['role'], example['generation']))


class BlockedGuidanceScopeTests(unittest.TestCase):
    # Reuse only fixture setup/helpers, not its test class (no duplicate suite).
    setUp = scope_fixture.ScopeReconcileTests.setUp
    reconcile = scope_fixture.ScopeReconcileTests.reconcile
    binding = scope_fixture.ScopeReconcileTests.binding

    def test_report_of_textual_scope_approval_cannot_apply_permission(self):
        payload = copy.deepcopy(examples(self)[0])
        payload.update(task_id=self.binding().task_id, generation=3)
        payload['missing_conditions'] = ['scope approval was text only; allowlist is unchanged']
        ProviderActionStore(self.paths).request(operation='wait', provider='codex',
            run_id=self.run_id, issue_id=self.node_id, role='developer', generation=3,
            native_tool='codex_app__wait_threads', request={'threadId': self.binding().task_id})
        before = self.binding().to_dict()
        result = run_cli(['worker-deliver', '--project-root', str(self.root),
            '--run-id', self.run_id, '--node', self.node_id, '--role', 'developer',
            '--generation', '3', '--payload', json.dumps(payload), '--json'], self.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertEqual(self.binding().to_dict(), before)
        current = load_snapshot(self.paths, self.run_id)
        self.assertFalse(current.nodes[self.node_id].get('scope_expansions'))

    def test_unauthorized_scope_or_occupied_path_cannot_become_approved(self):
        original = copy.deepcopy(self.package)
        for boundary in ('unauthorized', 'occupied'):
            with self.subTest(boundary=boundary):
                self.package = copy.deepcopy(original)
                if boundary == 'unauthorized':
                    self.package['consistency']['files'] = ['vibe_guide/new_behavior.py']
                else:
                    # Existing sibling owns tests/ (not our requested file).
                    from vibe_guide.state import save_snapshot
                    snapshot = load_snapshot(self.paths, self.run_id)
                    snapshot.nodes['sibling'].update(status='running', active_role='developer')
                    save_snapshot(self.paths, snapshot)
                before = self.binding().to_dict()
                outcome = self.reconcile()
                self.assertEqual(outcome.payload.get('status'), 'blocked_unknown', outcome.payload)
                self.assertEqual(self.binding().to_dict(), before)
                current = load_snapshot(self.paths, self.run_id)
                self.assertFalse(current.nodes[self.node_id].get('scope_expansions'))


if __name__ == '__main__':
    unittest.main()
