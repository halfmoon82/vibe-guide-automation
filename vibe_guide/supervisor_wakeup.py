"""Resume a verified host turn's admission checkpoint, not its entire heartbeat.

This receipt is only a scheduling hint. Native action/result receipts and the
Monitor's authorization/binding gates remain authoritative for execution.
Unrecognized host logs retain the read-only preflight behavior.
"""
import json
from pathlib import Path

from . import supervisor
from .state import _atomic_bytes, interprocess_lock, run_dir


def _host_turn(session_record):
    session = started = context = None
    active = False
    try:
        with Path(session_record).open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # A host may still be appending its last line.
                if not isinstance(entry, dict) or not isinstance(entry.get('payload'), dict):
                    continue
                payload = entry['payload']
                if entry.get('type') == 'session_meta':
                    session = payload.get('id')
                elif entry.get('type') == 'turn_context':
                    context = payload.get('turn_id')
                elif entry.get('type') == 'event_msg':
                    if payload.get('type') == 'task_started':
                        started, active = payload.get('turn_id'), True
                    elif payload.get('type') in {'task_complete', 'task_completed', 'turn_aborted'}:
                        if payload.get('turn_id') == started:
                            active = False
    except (OSError, TypeError, UnicodeError):
        return None
    if (active and isinstance(session, str) and session
            and isinstance(started, str) and started and started == context):
        return session, started
    return None


def preflight_with_checkpoint(paths, run_id, session_record, compute, *,
                              token_threshold=supervisor.DEFAULT_ROTATE_TOKEN_THRESHOLD,
                              context_hard_cap=supervisor.DEFAULT_ROTATE_HARD_CAP,
                              command_prefix='vibe'):
    """Persist admission once for a bound Codex turn, including CLI handoff.

    Work resumes from the durable mailbox after compaction/restart. Idle and
    unknown observations are rechecked so arrivals or repaired evidence cannot
    be hidden. Live context caps, changed contracts and changed shifts always
    override the hint. Never use this receipt as permission to execute an action.
    """
    turn = _host_turn(session_record)
    if turn is None:
        return compute()
    directory = run_dir(paths, run_id)
    registry = supervisor._read_json_file(directory / 'supervisor-registry.json')
    address = registry.get('current') if isinstance(registry, dict) else None
    if (not isinstance(address, dict) or address.get('session_id') != turn[0]
            or address.get('provider') not in {'codex', 'codex-app', 'codex-app-visible'}
            or address.get('session_record_digest') != supervisor._record_digest(session_record)):
        return compute()
    try:
        snapshot = supervisor.load_snapshot(paths, run_id)
    except (OSError, ValueError, TypeError):
        return compute()
    binding = {
        'run_id': run_id, 'plan_id': snapshot.plan_id, 'plan_version': snapshot.plan_version,
        'authorization_digest': snapshot.authorization_digest,
        'node_contract_digest': snapshot.node_contract_digest,
        'capability_contract_digest': snapshot.capability_contract_digest,
        'topology_digest': snapshot.topology_digest,
        'run_status': snapshot.status,
        'address': address, 'turn_id': turn[1],
        'token_threshold': token_threshold, 'context_hard_cap': context_hard_cap,
        'command_prefix': command_prefix,
    }
    path = directory / 'supervisor-wakeup.json'
    if path.is_symlink():
        return {'state': 'unknown', 'reason': 'supervisor wakeup receipt is a symlink'}
    try:
        with interprocess_lock(directory / '.supervisor-wakeup.lock'):
            previous = supervisor._read_json_file(path)
            cached = previous.get('result') if isinstance(previous, dict) else None
            tokens = supervisor._session_tokens(supervisor._read_json_file(session_record))
            baseline = address.get('baseline_context')
            warning = tokens is not None and tokens >= supervisor.DEFAULT_ROTATE_SOFT_CAP
            rotate = tokens is not None and (
                tokens >= context_hard_cap or (warning and type(baseline) is int
                    and tokens - baseline >= token_threshold))
            if (isinstance(previous, dict) and previous.get('binding') == binding
                    and isinstance(cached, dict) and cached.get('state') in {'work', 'rotate'}
                    and previous.get('phase') == {'work': 'service_mailbox', 'rotate': 'handoff'}[cached['state']]
                    and tokens is not None and cached.get('prepare_handoff') == warning
                    and (cached.get('state') == 'rotate') == rotate):
                return {**cached, 'tokens': tokens, 'wakeup': {
                    'turn_id': turn[1], 'phase': previous['phase'], 'reused': True}}
            result = compute()
            if result.get('state') not in {'work', 'idle', 'rotate'} or result.get('handoff_status') == 'unknown':
                return result
            phase = {'work': 'service_mailbox', 'idle': 'complete', 'rotate': 'handoff'}[result['state']]
            _atomic_bytes(path, json.dumps({'binding': binding, 'phase': phase, 'result': result},
                ensure_ascii=False, sort_keys=True).encode('utf-8'))
            return {**result, 'wakeup': {'turn_id': turn[1], 'phase': phase, 'reused': False}}
    except (OSError, ValueError, TimeoutError) as error:
        return {'state': 'unknown', 'reason': 'supervisor wakeup checkpoint unavailable: ' + str(error)[:200]}
