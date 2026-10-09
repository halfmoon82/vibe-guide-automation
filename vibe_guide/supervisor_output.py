"""One UTF-8 budget for an entire supervisor tool batch."""
import hashlib
import json

from .state import _atomic_bytes, run_dir

OUTPUT_LIMIT = 8191  # Reserve one UTF-8 byte for the CLI trailing newline.


def _brief(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)[:200]


def _summary(output):
    if not isinstance(output, dict):
        return {'type': type(output).__name__}
    # MCP native tools wrap their JSON result in text content blocks.
    if isinstance(output.get('content'), list):
        summaries = []
        for block in output['content']:
            if not isinstance(block, dict) or block.get('type') != 'text':
                continue
            text = block.get('text', '')
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                summaries.append({'message': _brief(text)})
            else:
                summaries.append(_summary(parsed))
        result = {'isError': output.get('isError', False), 'content': summaries}
        if result['isError']:
            result['error'] = _brief(summaries)
        return result
    result = {k: _brief(output[k]) for k in ('status', 'state', 'reason', 'error', 'exit_code', 'cursor', 'session_id', 'output', 'stdout', 'stderr') if k in output}
    if 'errors' in output:
        result['errors'] = _brief(output['errors'])
    polls = output.get('polls')
    if isinstance(polls, list):
        result['polls'] = []
        for poll in polls:
            if not isinstance(poll, dict):
                result['polls'].append({'status': 'unknown', 'error': 'malformed poll'})
                continue
            thread = poll.get('thread') if isinstance(poll.get('thread'), dict) else {}
            turn = poll.get('latestTurn') if isinstance(poll.get('latestTurn'), dict) else {}
            message = poll.get('latestAssistantMessage') if isinstance(poll.get('latestAssistantMessage'), dict) else {}
            result['polls'].append({
                'thread_id': _brief(thread.get('id', 'unknown')),
                'cursor': _brief(poll.get('cursor', 'unknown')),
                'status': _brief(turn.get('status', 'unknown')),
                'error': _brief(turn.get('error')),
                'message': _brief(message.get('text', '')),
            })
    return result


def supervisor_output_batch(paths, run_id, items):
    """Persist raw results once; return summaries or references on repeat.

    This is a presentation boundary, never a provider result or state update.
    Callers still consume the original objects and bind actual provider results.
    """
    if not isinstance(items, list) or not items or any(
        not isinstance(item, dict) or set(item) != {'name', 'output'}
        or not isinstance(item['name'], str) or not item['name']
        for item in items
    ):
        raise ValueError('batch requires nonempty name/output records')
    raw = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    directory = run_dir(paths, run_id, create=True) / 'tool-output'
    if directory.is_symlink():
        raise ValueError('tool-output directory contains a symlink')
    path = directory / (hashlib.sha256(raw).hexdigest() + '.json')
    if path.is_symlink():
        raise ValueError('tool-output artifact contains a symlink')
    repeated = path.exists()
    if repeated and path.read_bytes() != raw:
        raise ValueError('tool-output artifact digest mismatch')
    if not repeated:
        _atomic_bytes(path, raw)
    payload = {'command': 'supervisor-output', 'run_id': run_id, 'count': len(items),
               'repeated': repeated, 'output_evidence': {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}}
    if repeated:
        return payload
    results = [{'name': _brief(item['name']), **_summary(item['output'])} for item in items]
    payload['results'] = results
    if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > OUTPUT_LIMIT:
        # Preserve failure/unknown classification even if detailed rows spill.
        states = set()
        alerts = []
        critical = {'failed', 'unknown', 'blocked_unknown', 'error', 'nonzero_exit'}
        def classification(value):
            for prefix in ('unknown', 'failed', 'error', 'blocked'):
                if str(value).lower().startswith(prefix):
                    return prefix
            return value
        def observations(result):
            yield result
            for nested in result.get('content', []):
                yield from observations(nested)
            for poll in result.get('polls', []):
                yield poll
        for result in (observation for item in results for observation in observations(item)):
            important = False
            for key in ('state', 'status'):
                if key in result:
                    states.add(result[key])
                    category = classification(result[key])
                    if category in ('unknown', 'failed', 'error', 'blocked'):
                        states.add(category)
                        critical.add(category)
                        important = True
                    important = important or result[key] in critical
            if result.get('isError') or result.get('error') not in (None, 'None', 'null', '') or result.get('errors'):
                states.add('error')
                important = True
            if result.get('exit_code') not in (None, '0'):
                states.add('nonzero_exit')
                important = True
            if important:
                alerts.append({key: value for key, value in result.items()
                               if key not in ('content', 'polls', 'stdout', 'stderr', 'output')})
        payload.pop('results')
        payload['states'] = sorted(states & critical) + sorted(states - critical)[:10]
        payload['alerts'] = []
        # Full alerts remain in the evidence; append as many short alerts as
        # fit while preserving all critical classifications above.
        for alert in alerts:
            payload['alerts'].append(alert)
            if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > OUTPUT_LIMIT - 128:
                payload['alerts'].pop()
                break
        payload['alerts_omitted'] = len(alerts) - len(payload['alerts'])
        payload['details_omitted'] = True
    if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > OUTPUT_LIMIT:
        raise ValueError('batch reference exceeds output budget')
    return payload
