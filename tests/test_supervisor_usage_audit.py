import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


spec = importlib.util.spec_from_file_location('usage_audit', Path(__file__).resolve().parents[1] / 'scripts/supervisor_usage_audit.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class UsageAuditTests(unittest.TestCase):
    def log(self, directory, name, session, totals):
        events = [{'type': 'session_meta', 'payload': {'id': session}}]
        for i, total in enumerate(totals):
            events.append({'type': 'event_msg', 'timestamp': '2026-10-09T00:00:%02dZ' % i,
                'payload': {'type': 'token_count', 'info': {'total_token_usage': total,
                    'last_token_usage': total}}})
        path = Path(directory) / name
        path.write_text('\n'.join(json.dumps(e) for e in events) + '\n{"partial')
        return path

    def test_duplicates_are_not_requests_and_cache_is_not_extra_input(self):
        with tempfile.TemporaryDirectory() as root:
            a = {'input_tokens': 100, 'cached_input_tokens': 80, 'cache_write_input_tokens': 10,
                 'output_tokens': 20, 'reasoning_output_tokens': 5}
            b = dict(a, input_tokens=200, cached_input_tokens=160, output_tokens=40, reasoning_output_tokens=10)
            path = self.log(root, 's.jsonl', 's1', [a, a, b])
            record = audit.read_usage(path)
            self.assertEqual(record['usage_events'], 3)
            self.assertEqual(record['unique_usage_events'], 2)
            self.assertEqual(record['duplicate_usage_events'], 1)
            self.assertEqual(record['uncached_input_tokens'], 40)
            self.assertEqual(record['output_tokens'], 40)
            self.assertEqual(record['effective_input_by_cache_ratio']['0.1'], 56)

    def test_counter_regression_is_explicit_not_assumed_a_free_reset(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.log(root, 's.jsonl', 's1', [{'input_tokens': 100, 'output_tokens': 5},
                                                  {'input_tokens': 20, 'output_tokens': 2}])
            record = audit.read_usage(path)
            self.assertEqual(record['status'], 'unknown_counter_regression')
            self.assertEqual(record['counter_regressions'], 1)

    def test_session_matching_uses_full_metadata_id_and_reports_missing(self):
        with tempfile.TemporaryDirectory() as root:
            self.log(root, 'rollout-s1-child.jsonl', 's1-child', [{'input_tokens': 100}])
            path = self.log(root, 'rollout-s1.jsonl', 's1', [{'input_tokens': 20}])
            registry = {'history': [{'session_id': 's1', 'registered_at': 1791504001}],
                        'current': {'session_id': 'missing', 'registered_at': 1791505000}}
            report = audit.build_report(registry, [Path(root)])
            self.assertEqual(report['sessions'][0]['source']['path'], str(path.resolve()))
            self.assertEqual(report['sessions'][0]['takeover_window_10m']['input_tokens'], 0)
            self.assertEqual(report['sessions'][0]['takeover_window_10m']['status'], 'unknown_no_observations')
            self.assertEqual(report['missing_sessions'], ['missing'])
            self.assertEqual(report['coverage']['found'], 1)

    def test_node_baseline_sums_generations_and_marks_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            a = {'input_tokens': 100, 'cached_input_tokens': 80, 'output_tokens': 20}
            b = dict(a, input_tokens=200, cached_input_tokens=160, output_tokens=40)
            self.log(root, 'rollout-d0.jsonl', 'd0', [a, a, b])
            self.log(root, 'rollout-r0.jsonl', 'r0', [a])
            registry = {'history': [
                {'session_id': 'd0', 'node_id': 'n1', 'role': 'developer', 'generation': 0},
                {'session_id': 'd0', 'node_id': 'n1', 'role': 'developer', 'generation': 0},
                {'session_id': 'r0', 'node_id': 'n1', 'role': 'reviewer', 'generation': 0}],
                'current': {'session_id': 'gone', 'node_id': 'n2', 'role': 'developer', 'generation': 1}}
            nodes = audit.build_report(registry, [Path(root)])['nodes']
            self.assertEqual(nodes['n1']['total_uncached_input_output_tokens'], 80 + 40)
            self.assertEqual(len(nodes['n1']['sessions']), 2)
            self.assertIsNone(nodes['n2']['total_uncached_input_output_tokens'])
            self.assertEqual(nodes['n2']['status'], 'unknown')

    def test_node_with_regression_stays_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            self.log(root, 'rollout-d0.jsonl', 'd0', [{'input_tokens': 100}, {'input_tokens': 5}])
            nodes = audit.build_report({'current': {'session_id': 'd0', 'node_id': 'n1'}}, [Path(root)])['nodes']
            self.assertIsNone(nodes['n1']['total_uncached_input_output_tokens'])

    def test_mapped_session_missing_from_registry_makes_node_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            self.log(root, 'rollout-d0.jsonl', 'd0', [{'input_tokens': 100, 'cached_input_tokens': 80, 'output_tokens': 20}])
            node_map = {'sessions': {'d0': {'node_id': 'n1', 'role': 'developer', 'generation': 0},
                                     'r1': {'node_id': 'n1', 'role': 'reviewer', 'generation': 0}}}
            report = audit.build_report({'history': [{'session_id': 'd0'}], 'current': {}}, [Path(root)], node_map)
            self.assertIn('r1', report['missing_sessions'])
            self.assertIsNone(report['nodes']['n1']['total_uncached_input_output_tokens'])
            self.assertEqual(report['nodes']['n1']['known_partial_tokens'], 40)

    def test_node_map_supplies_identity_for_real_shaped_registry(self):
        with tempfile.TemporaryDirectory() as root:
            self.log(root, 'rollout-d0.jsonl', 'd0', [{'input_tokens': 100, 'cached_input_tokens': 80, 'output_tokens': 20}])
            registry = {'history': [{'session_id': 'd0'}], 'current': {}}
            node_map = {'sessions': {'d0': {'node_id': 'n1', 'role': 'developer', 'generation': 0}},
                        'expected_nodes': ['n1', 'n3']}
            nodes = audit.build_report(registry, [Path(root)], node_map)['nodes']
            self.assertEqual(nodes['n1']['total_uncached_input_output_tokens'], 40)
            self.assertEqual(nodes['n3']['status'], 'unknown')
            self.assertIsNone(nodes['n3']['total_uncached_input_output_tokens'])
            self.assertEqual(audit.build_report(registry, [Path(root)])['nodes'], {})

    @unittest.skipUnless(shutil.which('zstd'), 'zstd CLI unavailable')
    def test_zst_log_is_read_like_plain_jsonl(self):
        with tempfile.TemporaryDirectory() as root:
            t = {'input_tokens': 100, 'cached_input_tokens': 80, 'output_tokens': 20}
            plain = self.log(root, 'rollout-z1.jsonl', 'z1', [t])
            subprocess.run(['zstd', '-q', '--rm', str(plain)], check=True)
            report = audit.build_report({'current': {'session_id': 'z1', 'node_id': 'n1'}}, [Path(root)])
            self.assertEqual(report['nodes']['n1']['total_uncached_input_output_tokens'], 40)

    def test_corrupt_zst_log_is_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'rollout-z2.jsonl.zst').write_bytes(b'not zstd data')
            report = audit.build_report({'current': {'session_id': 'z2', 'node_id': 'n1'}}, [Path(root)])
            self.assertIsNone(report['nodes']['n1']['total_uncached_input_output_tokens'])
            self.assertEqual(report['nodes']['n1']['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
