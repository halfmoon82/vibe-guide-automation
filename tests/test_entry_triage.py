"""Local transport fixtures exercise entry routing; no gateway or network."""
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide import session_entry as entry
from vibe_guide.paths import ProjectPaths
from vibe_guide.planner import S1Score, TaskContext


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/evaluate_entry_triage.py'
REQUEST = '设计并实现数据迁移与测试'


def evaluator(case):
    case.assertTrue(SCRIPT.is_file(), 'missing entry triage evaluation scaffold')
    spec = importlib.util.spec_from_file_location('entry_triage_eval_fixture', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EntryTriageTests(unittest.TestCase):
    def setUp(self):
        # addCleanup works on 3.9 too; do not depend on TestCase.enterContext.
        for target in ('socket.create_connection', 'urllib.request.urlopen'):
            guard = patch(target, side_effect=AssertionError('network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_unconfigured_is_repeatable_zero_attempts_not_gateway_failure(self):
        first = entry.build_session_entry(REQUEST)
        second = entry.build_session_entry(REQUEST)
        self.assertIn('triage', first.to_dict(), 'entry has no triage fallback projection')
        triage = first.to_dict()['triage']
        self.assertEqual((triage['status'], triage['reason_code'], triage['attempts']),
                         ('degraded', 'unconfigured', 0))
        self.assertEqual(triage['source'], 'unconfigured')
        self.assertIsNone(triage['confidence'])
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.node_spec['triage'], triage)
        self.assertNotIn('elapsed', json.dumps(triage))

    def test_unconfigured_fallback_keeps_a_reason_in_public_plan_projection(self):
        from vibe_guide.cli import run_cli
        with tempfile.TemporaryDirectory() as root:
            result = run_cli(["plan", "--request", REQUEST, "--json"], Path(root))
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertEqual(result.payload["triage"]["status"], "degraded")
        self.assertTrue(result.payload["triage"]["reason"])

    def test_hook_reaches_public_entry_and_existing_s1_rules(self):
        calls = []
        def hook(request):
            calls.append(request)
            return {'s1': '5,3,3,3,3;parallel=3;resume=yes', 'confidence': 0.8}
        result = entry.build_session_entry(REQUEST, triage_hook=hook, triage_source='fixture-v1')
        self.assertEqual(calls, [REQUEST.lower()])
        self.assertEqual(result.route.route, 'complex')
        self.assertEqual(result.plan_id, entry.stable_plan_id(REQUEST))
        triage = result.to_dict()['triage']
        self.assertEqual((triage['status'], triage['attempts'], triage['source']),
                         ('ok', 1, 'fixture-v1'))
        self.assertEqual(triage['confidence'], 0.8)
        self.assertEqual(triage['request_digest'], entry.default_s1_context(REQUEST).rationale['request_digest'])
        self.assertEqual(result.node_spec['triage'], triage)
        self.assertNotIn('provider_task', result.node_spec)

    def test_attempted_failures_fallback_with_safe_source_reason_and_no_permissions(self):
        baseline = entry.build_session_entry(REQUEST)
        for error, code in ((TimeoutError('token=secret'), 'timeout'),
                            (ConnectionRefusedError('private body'), 'unavailable'),
                            (RuntimeError('sensitive response'), 'hook_error')):
            with self.subTest(code=code):
                calls = []
                def hook(request):
                    calls.append(request)
                    raise error
                result = entry.build_session_entry(REQUEST, triage_hook=hook, triage_source='fixture-off')
                triage = result.to_dict()['triage']
                self.assertEqual(len(calls), 1)
                self.assertEqual((triage['status'], triage['reason_code'], triage['attempts']),
                                 ('degraded', code, 1))
                self.assertEqual(triage['source'], 'fixture-off')
                self.assertEqual(result.route, baseline.route)
                self.assertEqual(result.s1, baseline.s1)
                self.assertEqual(result.plan_id, baseline.plan_id)
                self.assertNotIn(str(error), json.dumps(triage))
                with tempfile.TemporaryDirectory() as root:
                    paths = ProjectPaths(Path(root))
                    directory = entry.materialize_session_entry(paths, result)
                    node_spec = json.loads((directory / 'node-spec.json').read_text())
                    self.assertEqual(node_spec['triage'], triage)
                    self.assertFalse((paths.vibe / 'provider-actions').exists())
                    self.assertFalse((directory / 'authorization.json').exists())

    def test_gateway_down_is_a_real_injected_transport_attempt_not_a_route_mock(self):
        def local_transport(request):
            self.assertEqual(request, REQUEST.lower())
            socket.create_connection(('synthetic.invalid', 9), timeout=0.01)
        with patch('socket.create_connection', side_effect=ConnectionRefusedError('fixture off')) as transport:
            result = entry.build_session_entry(REQUEST, triage_hook=local_transport,
                                               triage_source='fixture-transport-off')
        transport.assert_called_once_with(('synthetic.invalid', 9), timeout=0.01)
        self.assertEqual(result.triage['attempts'], 1)
        self.assertEqual(result.triage['reason_code'], 'unavailable')
        self.assertEqual(result.route, entry.build_session_entry(REQUEST).route)

    def test_invalid_response_falls_back_without_trusting_scores_or_confidence(self):
        baseline = entry.build_session_entry(REQUEST)
        for response in (None, [], {'s1': '9,0,0,0,0'}, {'s1': '1,1,1,1,1', 'confidence': True},
                         {'s1': '1,1,1,1,1', 'confidence': -0.1},
                         {'s1': '1,1,1,1,1', 'confidence': float('nan')},
                         {'s1': '1,1,1,1,1', 'confidence': 1.1}):
            with self.subTest(response=response):
                result = entry.build_session_entry(REQUEST, triage_hook=lambda _: response,
                                                   triage_source='fixture-invalid')
                self.assertEqual(result.route, baseline.route)
                self.assertEqual(result.to_dict()['triage']['reason_code'], 'invalid_response')

    def test_overflowing_confidence_and_rejected_parser_response_safely_fallback(self):
        baseline = entry.build_session_entry(REQUEST)
        rejected_score = S1Score(50, 10, 10, 10, 10, 10)
        responses = [
            {'s1': '1,1,1,1,1', 'confidence': 10**400},
            {'s1': '1,1,1,1,1', 'confidence': -(10**400)},
            {'s1': rejected_score},
        ]
        for response in responses:
            with self.subTest(response=response):
                calls = []
                def hook(request):
                    calls.append(request)
                    return response
                result = entry.build_session_entry(REQUEST, triage_hook=hook,
                                                   triage_source='fixture-invalid-boundary')
                self.assertEqual(calls, [REQUEST.lower()])
                self.assertEqual((result.triage['status'], result.triage['reason_code'],
                                  result.triage['attempts']), ('degraded', 'invalid_response', 1))
                self.assertEqual((result.s1, result.route, result.plan_id),
                                 (baseline.s1, baseline.route, baseline.plan_id))
                self.assertIsNone(result.triage['confidence'])
                self.assertEqual(result.triage['source'], 'fixture-invalid-boundary')
                self.assertEqual(result.triage['request_digest'], baseline.triage['request_digest'])
                self.assertNotIn(str(10**400), json.dumps(result.triage))
                self.assertNotIn('OverflowError', json.dumps(result.triage))
                self.assertNotIn('ValueError', json.dumps(result.triage))
        with self.assertRaises(ValueError):
            entry.build_session_entry(REQUEST, s1=rejected_score)

    def test_overrides_s0_and_continuation_never_invoke_hook(self):
        def forbidden(_):
            self.fail('hook must not run for explicit S1/S0/continuation')
        explicit = entry.build_session_entry(REQUEST, s1='1,1,1,1,1',
            triage_hook=forbidden, triage_source='fixture-bypass')
        self.assertEqual(explicit.s1.total, 5)
        self.assertEqual(explicit.to_dict()['triage']['reason_code'], 'explicit_s1')
        direct = entry.build_session_entry('解释什么是 API', triage_hook=forbidden,
                                           triage_source='fixture-bypass')
        self.assertEqual(direct.to_dict()['triage']['reason_code'], 's0_direct')
        result = entry.entry_route('继续', current_tier='light_plan',
                                  triage_hook=forbidden, triage_source='fixture-bypass')
        self.assertEqual((result['route'], result['tier']), ('continue_current', 'light_plan'))
        with self.assertRaises(entry.ContinuationMessage):
            entry.build_session_entry('继续', triage_hook=forbidden, triage_source='fixture-bypass')
        with self.assertRaises(ValueError):
            entry.build_session_entry(REQUEST, s1='invalid', triage_hook=forbidden)

    def test_missing_confidence_stays_unknown_and_bad_hook_configuration_is_rejected(self):
        result = entry.build_session_entry(REQUEST, triage_hook=lambda _: {'s1': '1,1,1,1,1'},
                                          triage_source='fixture-no-confidence')
        self.assertIsNone(result.to_dict()['triage']['confidence'])
        for kwargs in ({'triage_hook': 'arbitrary command'},
                       {'triage_hook': lambda _: {}, 'triage_source': '/private/path'},
                       {'triage_hook': lambda _: {}}):
            with self.subTest(kwargs=tuple(kwargs)), self.assertRaises((TypeError, ValueError)):
                entry.build_session_entry(REQUEST, **kwargs)

    def test_hook_scores_are_not_explicit_user_complex_authority(self):
        context = TaskContext(5, 3, 3, 3, 3, force_upgrade_flags=['user_requested_complex_mode'])
        for proposed in ('5,3,3,3,3', context):
            with self.subTest(proposed=type(proposed).__name__):
                result = entry.entry_route(REQUEST, triage_hook=lambda _: {'s1': proposed},
                                           triage_source='fixture-no-authority')
                self.assertEqual(result['route'], 'light_plan')
                self.assertTrue(result['unmet_conditions'])
                self.assertEqual(result['triage']['attempts'], 1)


class EntryTriageEvaluationTests(unittest.TestCase):
    def cases(self):
        return [{'id': 'correct', 'request': '实现导出功能', 'expected_route': 'simple',
                 'response': {'s1': '1,1,1,1,1', 'confidence': 0.8}},
                {'id': 'wrong', 'request': '设计并实现数据迁移与测试', 'expected_route': 'complex',
                 'response': {'s1': '1,1,1,1,1', 'confidence': 0.2}}]

    def test_metrics_use_actual_predictions_labels_confidence_and_observed_latency(self):
        module = evaluator(self)
        ticks = iter([0.0, 0.001, 1.0, 1.003])
        report = module.evaluate_cases(self.cases(), source='synthetic-formula', clock=lambda: next(ticks))
        self.assertAlmostEqual(report['accuracy'], 0.5)
        self.assertAlmostEqual(report['ece'], 0.2)
        self.assertAlmostEqual(report['p95'], 3.0)
        self.assertEqual(report['samples'], 2)
        self.assertEqual(report['latency_unit'], 'ms')
        self.assertEqual(report['source'], 'synthetic-formula')
        self.assertEqual(report['predictions'][1]['predicted_route'], 'simple')
        self.assertFalse(report['predictions'][1]['correct'])

    def test_confidence_absent_does_not_fabricate_calibration(self):
        module = evaluator(self)
        rows = self.cases()[:1]
        rows[0]['response'].pop('confidence')
        report = module.evaluate_cases(rows, source='synthetic-no-confidence')
        self.assertIsNone(report['ece'])
        self.assertEqual(report['confidence_samples'], 0)
        self.assertEqual(report['ece_status'], 'unavailable_missing_confidence')

    def test_invalid_empty_or_unlabelled_data_and_confidence_are_rejected(self):
        module = evaluator(self)
        invalid = [[], {}, [{'request': '实现导出'}]]
        for confidence in (-1, 2, True, float('nan'), float('inf'), '0.5'):
            rows = self.cases()
            rows[0]['response']['confidence'] = confidence
            invalid.append(rows)
        rows = self.cases(); rows[0]['expected_route'] = 'invented'; invalid.append(rows)
        rows = self.cases(); rows[0]['expected_route'] = []; invalid.append(rows)
        rows = self.cases(); rows[0]['request'] = ''; invalid.append(rows)
        rows = self.cases(); rows[1]['id'] = rows[0]['id']; invalid.append(rows)
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                module.evaluate_cases(rows, source='synthetic-invalid')

    def test_oversized_integer_confidence_is_rejected_by_evaluator(self):
        module = evaluator(self)
        for confidence in (10**400, -(10**400)):
            rows = self.cases()
            rows[0]['response']['confidence'] = confidence
            with self.subTest(confidence=confidence), self.assertRaises(ValueError):
                module.evaluate_cases(rows, source='synthetic-invalid-boundary')

    def test_boundary_confidence_bins_and_nearest_rank_use_every_observation(self):
        module = evaluator(self)
        rows = self.cases()
        rows[0]['response']['confidence'] = 1.0
        rows[1]['response']['confidence'] = 0.0
        ticks = iter([0.0, 0.001, 1.0, 1.002])
        report = module.evaluate_cases(rows, source='synthetic-boundaries', clock=lambda: next(ticks))
        self.assertEqual(report['ece'], 0)
        self.assertEqual(report['accuracy'], 0.5)
        self.assertAlmostEqual(report['p95'], 2.0)
        for index in range(2, 20):
            rows.append(dict(rows[0], id='more-{}'.format(index)))
        ticks = iter(value for index in range(20) for value in (float(index), index + (index + 1) / 1000))
        report = module.evaluate_cases(rows, source='synthetic-rank', clock=lambda: next(ticks))
        self.assertAlmostEqual(report['p95'], 19.0)

    def test_script_json_and_bad_input_exit_codes_are_real(self):
        evaluator(self)
        done = subprocess.run([sys.executable, str(SCRIPT), '--json'], cwd=ROOT,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        self.assertGreater(report['samples'], 0)
        self.assertLessEqual(report['ece'], 0.05)
        self.assertGreaterEqual(report['p95'], 0)
        self.assertIn('synthetic', report['scope'])
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / 'bad.json'; data.write_text('[]')
            done = subprocess.run([sys.executable, str(SCRIPT), '--json', '--cases', str(data)],
                                  cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('error', json.loads(done.stdout))
    def test_invalid_cli_datasets_return_fixed_json_without_traceback(self):
        evaluator(self)
        expected = {'error': 'invalid local evaluation dataset; require source and nonempty valid labelled cases'}
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / 'invalid.json'
            invalid_data = [b'\xff', b'{', b'[]']
            for confidence in (10**400, -(10**400)):
                rows = self.cases()
                rows[0]['response']['confidence'] = confidence
                invalid_data.append(json.dumps({'source': 'synthetic-invalid-cli', 'cases': rows}).encode())
            for contents in invalid_data + [None]:
                if contents is None:
                    target = Path(root) / 'missing.json'
                else:
                    data.write_bytes(contents)
                    target = data
                with self.subTest(contents=contents):
                    done = subprocess.run([sys.executable, str(SCRIPT), '--json', '--cases', str(target)],
                                          cwd=ROOT, capture_output=True, text=True, timeout=30)
                    self.assertEqual(done.returncode, 2, done.stderr)
                    self.assertEqual(json.loads(done.stdout), expected)
                    self.assertEqual(done.stderr, '')


if __name__ == '__main__':
    unittest.main()
