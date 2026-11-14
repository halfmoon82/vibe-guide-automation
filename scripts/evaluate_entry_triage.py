"""Local functional triage evaluation, not a gateway/production benchmark.

Gold labels and canned forecasts are independently authored synthetic inputs.
The public entry computes predictions; confidence is a supplied forecast, never
derived from gold correctness. Only the standard library is required.
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vibe_guide.session_entry import build_session_entry
from vibe_guide.planner import parse_s1_context


DEFAULT_CASES = ROOT / 'tests/fixtures/entry-routing-cases.json'
ROUTES = {'simple', 'light_plan', 'complex'}


def _validate_cases(cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError('cases must be a nonempty list of labelled synthetic examples')
    ids = set()
    for row in cases:
        if not isinstance(row, dict):
            raise ValueError('each case must be an object')
        case_id = row.get('id')
        if not isinstance(case_id, str) or not case_id.strip() or case_id in ids:
            raise ValueError('case ids must be nonempty and unique')
        ids.add(case_id)
        if not isinstance(row.get('request'), str) or not row['request'].strip():
            raise ValueError('each case needs a nonempty request')
        if not isinstance(row.get('expected_route'), str) or row['expected_route'] not in ROUTES:
            raise ValueError('each case needs an independently labelled expected_route')
        if 'response' in row:
            response = row['response']
            if not isinstance(response, dict) or parse_s1_context(response.get('s1')) is None:
                raise ValueError('fixture response must contain valid S1')
            confidence = response.get('confidence')
            if confidence is not None and (
                not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
                or not 0 <= confidence <= 1 or not math.isfinite(confidence)
            ):
                raise ValueError('confidence must be finite in [0, 1] or absent')


def evaluate_cases(cases, *, source, clock=time.perf_counter):
    """Measure one public entry call per case; do not infer calibration data.

    ECE uses ten equal-width bins, weighted by sample count. A missing actual
    prediction confidence makes whole-set ECE unavailable (not a filtered ECE).
    Latency is synchronous build_session_entry including the local hook only;
    p95 is nearest-rank, in milliseconds, not end-to-end CLI/network latency.
    """
    _validate_cases(cases)
    if not isinstance(source, str) or not source.strip():
        raise ValueError('dataset source is required')
    observations = []
    for row in cases:
        response = row.get('response')
        hook = (lambda _request, response=response: response) if response is not None else None
        started = clock()
        entry = build_session_entry(row['request'], triage_hook=hook,
                                    triage_source='synthetic-fixture' if hook else None)
        elapsed = (clock() - started) * 1000.0
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError('latency clock must produce finite nondecreasing observations')
        observations.append({
            'id': row['id'], 'predicted_route': entry.route.route,
            'expected_route': row['expected_route'],
            'correct': entry.route.route == row['expected_route'],
            'confidence': entry.triage['confidence'], 'latency_ms': elapsed,
            'triage_status': entry.triage['status'],
        })
    count = len(observations)
    known = [row for row in observations if row['confidence'] is not None]
    ece = None
    if len(known) == count:
        bins = [[] for _ in range(10)]
        for row in known:
            bins[min(9, int(row['confidence'] * 10))].append(row)
        ece = sum(len(bucket) / count * abs(
            sum(row['correct'] for row in bucket) / len(bucket)
            - sum(row['confidence'] for row in bucket) / len(bucket)
        ) for bucket in bins if bucket)
    latencies = sorted(row['latency_ms'] for row in observations)
    return {
        'accuracy': sum(row['correct'] for row in observations) / count,
        'ece': ece, 'p95': latencies[math.ceil(0.95 * count) - 1],
        'samples': count, 'confidence_samples': len(known),
        'ece_status': 'computed' if ece is not None else 'unavailable_missing_confidence',
        'source': source, 'latency_unit': 'ms',
        'scope': 'local synthetic functional fixtures; not real gateway or production quality',
        'confidence_definition': 'independent canned forecast for predicted route; not derived from gold',
        'ece_definition': '10 equal-width confidence bins; sum(n_bin/n * abs(acc_bin - mean_confidence_bin))',
        'latency_scope': 'build_session_entry including local fixture hook; excludes process/import/network/CLI',
        'p95_definition': 'nearest rank ceil(0.95 * n) of observed latency_ms',
        'predictions': observations,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--cases', type=Path, default=DEFAULT_CASES)
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.cases.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('dataset must be an object with source and cases')
        report = evaluate_cases(data.get('cases'), source=data.get('source'))
    except (OSError, ValueError, TypeError):
        # Do not echo arbitrary dataset contents, private paths or exceptions.
        error = {'error': 'invalid local evaluation dataset; require source and nonempty valid labelled cases'}
        print(json.dumps(error) if args.json else error['error'])
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False)
          if args.json else json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
