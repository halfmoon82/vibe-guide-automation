"""Read-only Codex usage audit. Counts cumulative deltas, never repeated last usage.

Cache ratios are sensitivity assumptions, not provider prices. No transcript
text is copied into the report. Counter regressions remain unknown because a
reset cannot be distinguished from inherited/replayed history without evidence.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import contextlib
import json
from pathlib import Path
import shutil
import subprocess


FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
          'output_tokens', 'reasoning_output_tokens')


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def _window(points, start, duration=600):
    observed = [(stamp, delta) for stamp, delta in points
                if stamp is not None and start <= stamp < start + duration]
    baseline = [stamp for stamp, _ in points if stamp is not None and stamp < start]
    result = {key: sum(delta[key] for _, delta in observed) for key in FIELDS}
    result.update(status='partial_attribution' if observed else 'unknown_no_observations',
                  usage_events=len(observed), window_start=start, window_end=start+duration,
                  baseline_at=max(baseline) if baseline else None,
                  first_observed_at=observed[0][0] if observed else None,
                  last_observed_at=observed[-1][0] if observed else None,
                  note='Deltas are attributed to completion timestamps; boundary-straddling '
                       'requests and missing timestamps prevent exact window cost. '
                       'Zero with no observations is not evidence of zero usage.')
    return result


@contextlib.contextmanager
def _open_log(path):
    """Binary line stream of a log; .zst is decompressed via the zstd CLI
    (no Python dependency). Missing zstd or a failed decode raises OSError so the
    caller records unknown instead of a partial total."""
    if path.suffix != '.zst':
        with path.open('rb') as stream:
            yield stream
        return
    exe = shutil.which('zstd')
    if exe is None:
        raise OSError('zstd CLI not found; cannot read ' + path.name)
    proc = subprocess.Popen([exe, '-dc', '--', str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        yield proc.stdout
    finally:
        proc.stdout.close()
        if proc.wait() != 0:
            raise OSError('zstd failed to decode ' + path.name)


def _unreadable(path, session_id, reason):
    return dict.fromkeys(FIELDS, 0, ) | dict(
        session_id=session_id, status='unknown_unreadable_log', reason=reason, usage_events=0,
        unique_usage_events=0, duplicate_usage_events=0, counter_regressions=0, malformed_lines=0,
        invalid_records=0, source={'path': str(path.resolve())}, native_action_coverage='not_measured')


def read_usage(path, registered_at=None):
    path = Path(path)
    try:
        return _read_usage(path, registered_at)
    except OSError as error:
        # session id of a compressed log is unknown without reading it; the
        # filename carries the full id, which build_report matches on.
        return _unreadable(path, None, str(error))


def _read_usage(path, registered_at=None):
    digest = hashlib.sha256()
    events = duplicates = regressions = malformed = invalid = 0
    previous = None
    session_id = None
    points = []
    first_stamp = last_stamp = None
    with _open_log(path) as stream:
        for raw in stream:
            digest.update(raw)
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                malformed += 1
                continue
            if not isinstance(event, dict):
                invalid += 1
                continue
            payload = event.get('payload') or {}
            if not isinstance(payload, dict):
                continue
            if event.get('type') == 'session_meta':
                session_id = payload.get('id')
            if event.get('type') != 'event_msg' or payload.get('type') != 'token_count':
                continue
            info = payload.get('info')
            if not isinstance(info, dict) or not isinstance(info.get('total_token_usage'), dict):
                continue
            usage = info['total_token_usage']
            if any(type(usage.get(k, 0)) is not int or usage.get(k, 0) < 0 for k in FIELDS):
                invalid += 1
                continue
            total = {key: usage.get(key, 0) for key in FIELDS}
            events += 1
            if total == previous:
                duplicates += 1
                continue
            if previous is not None and any(total[k] < previous[k] for k in FIELDS):
                regressions += 1
            delta = {key: total[key] - (previous or {}).get(key, 0) for key in FIELDS}
            stamp = _time(event.get('timestamp'))
            if first_stamp is None:
                first_stamp = stamp
            last_stamp = stamp
            points.append((stamp, delta))
            previous = total
    total = previous or dict.fromkeys(FIELDS, 0)
    uncached = total['input_tokens'] - total['cached_input_tokens']
    valid = not regressions and not invalid and uncached >= 0
    record = dict(total, session_id=session_id,
                  status=('ok' if valid and events else
                          'unknown_counter_regression' if regressions else 'unknown_usage'),
                  usage_events=events, unique_usage_events=events-duplicates,
                  duplicate_usage_events=duplicates, counter_regressions=regressions,
                  malformed_lines=malformed, invalid_records=invalid,
                  source={'path': str(path.resolve()), 'sha256': digest.hexdigest()},
                  first_usage_at=first_stamp, last_usage_at=last_stamp,
                  usage_events_missing_timestamp=sum(stamp is None for stamp, _ in points),
                  native_action_coverage='not_measured')
    if valid and events:
        record.update(uncached_input_tokens=uncached,
                      cache_hit_ratio=total['cached_input_tokens']/total['input_tokens'] if total['input_tokens'] else 0,
                      effective_input_by_cache_ratio={str(r): uncached+r*total['cached_input_tokens']
                                                     for r in (0.0, 0.1, 0.25, 1.0)})
        if first_stamp is not None:
            record['first_10m'] = _window(points, first_stamp)
        if registered_at is not None:
            record['takeover_window_10m'] = _window(points, registered_at)
    return record


def node_baseline(selected, records, expected_nodes=()):
    """Per-node cumulative uncached input+output. Any member session without
    verified usage makes the node total unknown (never 0); the verified part is
    kept separately as known_partial_tokens for review."""
    by_id = {r['session_id']: r for r in records}
    nodes = {}
    for session_id, entry in selected.items():
        node_id = entry.get('node_id')
        if not node_id:
            continue
        record = by_id.get(session_id)
        known = (record is not None and record.get('status') == 'ok'
                 and 'uncached_input_tokens' in record)
        tokens = record['uncached_input_tokens'] + record['output_tokens'] if known else None
        node = nodes.setdefault(node_id, {'sessions': [], 'known_partial_tokens': 0,
                                          'unknown_sessions': 0})
        node['sessions'].append({'session_id': session_id, 'role': entry.get('role'),
                                 'generation': entry.get('generation'),
                                 'uncached_input_output_tokens': tokens,
                                 'status': record['status'] if record else 'unknown_missing_log'})
        if known:
            node['known_partial_tokens'] += tokens
        else:
            node['unknown_sessions'] += 1
    for node_id in expected_nodes:
        # Expected node with no verifiable session: unknown, never 0.
        nodes.setdefault(node_id, {'sessions': [], 'known_partial_tokens': 0, 'unknown_sessions': 1})
    for node in nodes.values():
        node['total_uncached_input_output_tokens'] = (
            None if node['unknown_sessions'] else node['known_partial_tokens'])
        node['status'] = 'unknown' if node['unknown_sessions'] else 'ok'
    return nodes


def build_report(registry, roots, node_map=None):
    node_map = node_map or {}
    mapped = node_map.get('sessions', {})
    registrations = registry.get('history', []) + [registry.get('current', {})]
    selected = {}
    for entry in registrations:
        if entry.get('session_id'):
            selected.setdefault(entry['session_id'], dict(entry))
    for session_id in mapped:
        # Mapped session absent from the registry: still counted; no log -> unknown.
        selected.setdefault(session_id, {'session_id': session_id})
    for session_id, entry in selected.items():
        for key, value in (mapped.get(session_id) or {}).items():
            if key in ('node_id', 'role', 'generation') and entry.get(key) is None:
                entry[key] = value
    records = []
    missing = []
    ambiguous = []
    for session_id, entry in selected.items():
        candidates = set()
        for root in roots:
            # Full id plus metadata prevents a similarly named child from
            # entering the supervisor ledger. Archived logs count as sources.
            for pattern in ('*.jsonl', '*.jsonl.zst'):
                candidates.update(Path(root).rglob('*' + session_id + pattern[1:]))
        matches = [read_usage(path, entry.get('registered_at')) for path in sorted(candidates)]
        for record, path in zip(matches, sorted(candidates)):
            if record['status'] == 'unknown_unreadable_log' and session_id in path.name:
                record['session_id'] = session_id  # name carries the full id; usage stays unknown
        matches = [record for record in matches if record['session_id'] == session_id]
        if not matches:
            missing.append(session_id)
        elif len(matches) != 1:
            ambiguous.append(session_id)
        else:
            record = matches[0]
            record['registered_at'] = entry.get('registered_at')
            record['baseline_context'] = entry.get('baseline_context')
            for key in ('node_id', 'role', 'generation'):
                record[key] = entry.get(key)
            records.append(record)
    nodes = node_baseline(selected, records, node_map.get('expected_nodes', ()))
    return {'schema_version': 2, 'nodes': nodes, 'measured_at': datetime.now(timezone.utc).isoformat(),
            'coverage': {'registered': len(selected), 'found': len(records)},
            'missing_sessions': missing, 'ambiguous_sessions': ambiguous, 'sessions': records,
            'method': 'Cumulative counters; identical repeats removed. Input includes cached input; '
                      'reasoning output is a subset of output. Cache-write retained separately, '
                      'not added to input. First observed counter includes earlier usage if the log '
                      'starts late. Ten-minute windows are attribution windows, not causal savings.',
            'limits': 'No equivalent no-rotation control or provider price evidence. '
                      'Native action counts and successful DAG transitions are not measured. '
                      'A changing live source is measured only through the last readable event.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--log-root', type=Path, action='append', required=True)
    parser.add_argument('--node-map', type=Path, help='JSON: {"sessions": {session_id: {node_id, role, generation}}, "expected_nodes": [node_id]}')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raw = args.registry.read_bytes()
    report = build_report(json.loads(raw), args.log_root,
                          json.loads(args.node_map.read_text()) if args.node_map else None)
    report['registry_evidence'] = {'path': str(args.registry.resolve()),
                                   'sha256': hashlib.sha256(raw).hexdigest()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'output': str(args.output.resolve()), 'coverage': report['coverage'],
                      'missing': len(report['missing_sessions']), 'ambiguous': len(report['ambiguous_sessions'])}))


if __name__ == '__main__':
    main()
