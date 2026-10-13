import importlib.util
import json
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


if __name__ == '__main__':
    unittest.main()
