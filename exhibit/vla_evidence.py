"""Bounded, read-only VLA evidence ledger. No robot control or model dependencies."""
import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path

STALE_AFTER = 15
_MAX_TEXT = 4096
_MAX_EVENTS = 1000
_ACTIVITY = {'operation_started', 'observation', 'request_started', 'raw_response', 'request_failed',
             'request_cancelled', 'target', 'recovery_feedback', 'operation_finished', 'heartbeat'}
_NUMERIC = {'sample_time', 'capture_at', 'capture_monotonic', 'rtt_s', 'publish_started',
            'publish_finished', 'server_queue_and_compute_ms', 'server_queue_wait_ms', 'server_compute_ms',
            'fps', 'age_s', 'observed_at'}
_RESULT_BOOL = {'finished', 'stopped', 'ready_for_home', 'success', 'at_observation_pose'}
_ID = re.compile(r'^[A-Za-z0-9_.:@+\-]{1,256}$')
_IMAGE = re.compile(r'^data:image/(?:png|jpeg);base64,[A-Za-z0-9+/=\r\n]+$')
_VECTORS = {'state23', 'request_state23', 'action23', 'target23', 'feedback23'}
_FIELDS = {
    'operation_started': {'mode', 'task', 'model_id', 'legacy'},
    'observation': {'attempt', 'state23', 'capture_at', 'capture_monotonic', 'observation_meta', 'task', 'archive_queued'},
    'request_started': {'attempt', 'state23', 'capture_at', 'capture_monotonic', 'observation_meta', 'task', 'archive_queued'},
    'raw_response': {'attempt', 'chunk', 'request_state23', 'capture_at', 'rtt_s', 'request_timing', 'observation_meta', 'response'},
    'request_failed': {'attempt', 'reason', 'error', 'error_type'},
    'request_cancelled': {'attempt', 'reason'},
    'target': {'chunk', 'step', 'target23', 'feedback23', 'publish_started', 'publish_finished', 'grippers_published'},
    'recovery_feedback': {'feedback23', 'sample_time', 'stamps', 'history_index'},
    'operation_finished': {'mode', 'result_kind', 'result'},
    'status': {'state', 'reason', 'task', 'model_id', 'legacy'},
    'artifact_ready': {'attempt', 'images', 'status', 'reason'},
    'capture_gap': {'attempt', 'reason', 'dropped', 'count'},
    'grasp_detected': {'reason', 'detail', 'value'},
    'release_detected': {'reason', 'detail', 'value'},
    'clamped': {'reason', 'detail', 'value'},
}
_NESTED = {
    'response': {'action23', 'model_id', 'task', 'cot_text', 'inference_timing_ms', 'cot_generation',
                 'server_queue_and_compute_ms', 'server_queue_wait_ms', 'server_compute_ms',
                 'checkpoint_sha256', 'horizon', 'fps', 'fm_steps'},
    'result': {'finished', 'stopped', 'ready_for_home', 'success', 'at_observation_pose', 'reason', 'home_check_error'},
    'observation_meta': {'head_rgb', 'left_wrist_rgb', 'right_wrist_rgb', 'shape', 'dtype', 'width', 'height', 'channels', 'capture_at', 'capture_monotonic', 'status', 'available', 'age_s', 'timing'},
    'images': {'head_rgb', 'left_wrist_rgb', 'right_wrist_rgb'},
    'request_timing': {'started_at', 'finished_at', 'elapsed_s', 'rtt_s', 'capture_s', 'encode_s', 'infer_s', 'total_s', 'request', 'observe_started', 'capture', 'snapshot', 'observe_finished', 'archive_enqueue_started', 'archive_enqueue_finished', 'graph_started', 'graph_finished', 'infer_started', 'infer_finished', 'finished', 'wire'},
    'inference_timing_ms': {'total', 'total_ms', 'inference', 'inference_ms', 'preprocess', 'preprocess_ms', 'forward', 'forward_ms', 'postprocess', 'postprocess_ms', 'cot', 'cot_ms', 'action', 'action_ms', 'infer_ms', 'prefill_ms', 'cot_generate_text_ms', 'generate_action_total_ms', 'fm_action_ms', 'forward_inference_total_ms'},
    'cot_generation': {'enabled', 'tokens', 'duration_ms', 'status', 'generated_tokens_including_stop', 'stop_token_ids', 'stop_reason'},
    'wire': {'encode_started', 'encode_finished', 'send_started', 'send_finished', 'receive_finished', 'decode_finished'},
    'timing': {'snapshot_monotonic', 'snapshot_ros_time', 'receipt_age_s', 'source_age_s', 'source_skew_s', 'max_receipt_age_s', 'max_source_age_s', 'max_source_skew_s'},
}
_SECRET = re.compile(r'(?i)(?:api[_-]?key|authorization|password|secret|token)\s*[:=]\s*\S+')
_PATH = re.compile(r'(?:https?://|wss?://)\S+|(?:[A-Za-z]:[\\/]|(?<!\w)/)\S+')


def _text(value):
    return _PATH.sub('[redacted]', _SECRET.sub('[redacted]', value[:_MAX_TEXT]))


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('VLA numeric field must be finite')
    return value


def _clean(value, key='', depth=0, parent=''):
    if parent == 'result' and key in _RESULT_BOOL and value is not None and not isinstance(value, bool):
        raise ValueError('VLA result flag must be boolean')
    if key in _NUMERIC and value is not None:
        return _number(value)
    if depth > 5:
        return None
    if key in _NESTED and key != 'inference_timing_ms' and value is not None and not isinstance(value, dict):
        raise ValueError('VLA nested field must be an object')
    if key in {'attempt', 'chunk', 'step', 'history_index', 'dropped', 'count'} and value is not None:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000000000:
            raise ValueError('VLA counter must be a bounded nonnegative integer')
    if key == 'action23' and isinstance(value, (list, tuple)) and value and isinstance(value[0], (list, tuple)):
        if len(value) > 256:
            raise ValueError('VLA action horizon exceeds bound')
        return [_clean(row, 'state23', depth + 1) for row in value]
    if key in _VECTORS:
        if not isinstance(value, (list, tuple)) or len(value) != 23:
            raise ValueError('VLA vector must have 23 finite numbers')
        return [_number(x) for x in value]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, str):
        if key in {'head_rgb', 'left_wrist_rgb', 'right_wrist_rgb'} and value.startswith('data:'):
            if len(value) <= 700000 and _IMAGE.fullmatch(value):
                return value
            return 'missing'
        return _text(value)
    if isinstance(value, (list, tuple)):
        return [_clean(v, depth=depth + 1) for v in value[:32]]
    if isinstance(value, dict):
        allowed = _NESTED.get(key)
        if key in {'receipt_age_s', 'source_age_s'}:
            return {k: _number(v) for k, v in list(value.items())[:32]
                    if isinstance(k, str) and re.fullmatch(r'[a-zA-Z0-9_]{1,64}', k) and not re.search(r'(?i)key|secret|token|password|config', k)}
        # Timing/meta numeric fields are useful; arbitrary config keys never persist.
        if allowed is None and key in {'stamps'}:
            allowed = {'head', 'left', 'right', 'head_rgb', 'left_wrist_rgb', 'right_wrist_rgb', 'left_arm', 'right_arm', 'torso'}
        if allowed is None:
            return {}
        return {k: _clean(v, k, depth + 1, key) for k, v in value.items() if k in allowed}
    raise ValueError('Unsupported VLA payload value')


def _event(value):
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError('Unsupported VLA schema')
    out = {'schema_version': 1}
    for key in ('event_id', 'robot_id', 'session_id', 'run_id', 'operation_id', 'kind'):
        val = value.get(key)
        if not isinstance(val, str) or not _ID.fullmatch(val):
            raise ValueError('Invalid VLA event identity')
        out[key] = val
    out['observed_at'] = _number(value.get('observed_at'))
    if 'monotonic' in value:
        out['monotonic'] = _number(value['monotonic'])
    if value.get('request_id') is not None:
        if not isinstance(value['request_id'], str) or not _ID.fullmatch(value['request_id']):
            raise ValueError('Invalid VLA request identity')
        out['request_id'] = value['request_id']
    payload = value.get('payload', {})
    if not isinstance(payload, dict):
        raise ValueError('VLA payload must be an object')
    allowed = _FIELDS.get(out['kind'], {'reason', 'detail', 'value'})
    out['payload'] = {k: _clean(v, k) for k, v in payload.items() if k in allowed}
    if len(json.dumps(out)) > 2200000:
        raise ValueError('VLA event exceeds size bound')
    return out


def _request():
    return dict.fromkeys(('id', 'status', 'attempt', 'chunk', 'capture_at', 'started_at', 'response_at',
        'state23', 'action23', 'rtt_ms', 'cot_text', 'inference_timing_ms', 'server_queue_and_compute_ms', 'images'))


def _state():
    return dict(robot_id=None, session_id=None, run_id=None, operation_id=None, task=None, model_id=None,
        phase='waiting', reason=None, legacy=False, request=_request(),
        execution=dict(issued_steps=0, chunk=None, step=None, target23=None, feedback23=None,
                       feedback_at=None, feedback_kind=None),
        verification=dict(status='pending', run_finished=None, stopped=None, home_success=None,
                          reason=None, evidence=[]),
        stats=dict(events=0, requests=0, responses=0, failed_requests=0, dropped=0), last_event_at=None)


def _activity(event):
    # A late NPZ becoming readable proves an archive is ready, not that its
    # old operation is now executing. It must not select or refresh a run.
    return event['kind'] in _ACTIVITY and not (
        event['kind'] == 'observation'
        and (event['payload'].get('observation_meta') or {}).get('status') == 'archive_ready')


def _project(events):
    state = _state()
    request_operation = None
    terminal_operations = {}
    for e in events:
        p, kind, at = e['payload'], e['kind'], e['observed_at']
        if _activity(e) or kind == 'status':
            for key in ('robot_id', 'session_id', 'run_id', 'operation_id'):
                state[key] = e[key]
            state['last_event_at'] = at
        state['stats']['events'] += 1
        request, execution, verification = state['request'], state['execution'], state['verification']
        if kind == 'operation_started':
            state.update(phase=p.get('mode') or 'started', task=p.get('task', state['task']),
                         model_id=p.get('model_id', state['model_id']), legacy=bool(p.get('legacy', False)))
        elif kind in ('observation', 'request_started'):
            if (kind == 'observation' and not _activity(e)
                    and state['operation_id'] is not None and e['operation_id'] != state['operation_id']):
                continue
            rid = e.get('request_id')
            # An attempt is never inferred from the successful action chunk.
            if not rid:
                continue
            if (request_operation == e['operation_id'] and isinstance(p.get('attempt'), int)
                    and isinstance(request['attempt'], int) and p['attempt'] < request['attempt']):
                continue
            changed = request['id'] != rid
            request_operation = e['operation_id']
            if changed:
                request = _request()
                state['request'] = request
            request.update(id=rid, attempt=p.get('attempt', request['attempt']))
            if p.get('capture_at') is not None:
                request['capture_at'] = p['capture_at']
            if p.get('state23') is not None and request['status'] not in ('responded', 'failed', 'cancelled'):
                request['state23'] = p['state23']
            if kind == 'request_started':
                state['stats']['requests'] += 1
                if request['started_at'] is None or at < request['started_at']:
                    request['started_at'] = at
                if request['status'] in ('responded', 'failed', 'cancelled'):
                    continue
                request.update(status='started', started_at=at, action23=None, response_at=None,
                               cot_text=None, inference_timing_ms=None, rtt_ms=None,
                               server_queue_and_compute_ms=None, chunk=None)
                state['phase'] = 'request_started'
            elif changed or request['status'] in (None, 'observed'):
                request['status'] = 'observed'
                state['phase'] = 'observation'
            state['task'] = p.get('task', state['task'])
        elif kind == 'raw_response':
            state['stats']['responses'] += 1
            if not e.get('request_id'):
                continue
            if request['id'] != e['request_id']:
                if (request_operation == e['operation_id'] and isinstance(p.get('attempt'), int)
                        and isinstance(request['attempt'], int) and p['attempt'] <= request['attempt']):
                    continue
                request = _request()
                state['request'] = request
                request.update(id=e['request_id'], attempt=p.get('attempt'))
                request_operation = e['operation_id']
            if request['status'] in ('failed', 'cancelled'):
                continue
            response = p.get('response') or {}
            if p.get('request_state23') is not None:
                request['state23'] = p['request_state23']
            if p.get('capture_at') is not None:
                request['capture_at'] = p['capture_at']
            request.update(status='responded', response_at=at, chunk=p.get('chunk'),
                           action23=response.get('action23'), cot_text=response.get('cot_text'),
                           inference_timing_ms=response.get('inference_timing_ms'),
                           server_queue_and_compute_ms=response.get('server_queue_and_compute_ms'))
            if p.get('rtt_s') is not None:
                request['rtt_ms'] = p['rtt_s'] * 1000
            state['task'] = response.get('task', state['task'])
            state['model_id'] = response.get('model_id', state['model_id'])
            state['phase'] = 'raw_response'
        elif kind in ('request_failed', 'request_cancelled'):
            if kind == 'request_failed':
                state['stats']['failed_requests'] += 1
            rid = e.get('request_id')
            if rid and request['id'] != rid:
                if (request_operation == e['operation_id'] and isinstance(p.get('attempt'), int)
                        and isinstance(request['attempt'], int) and p['attempt'] <= request['attempt']):
                    continue
                request = _request()
                state['request'] = request
                request.update(id=rid, attempt=p.get('attempt'))
                request_operation = e['operation_id']
            if rid and request['id'] == rid:
                request.update(status='failed' if kind == 'request_failed' else 'cancelled', action23=None,
                               cot_text=None, inference_timing_ms=None, server_queue_and_compute_ms=None)
                state.update(phase=kind, reason=p.get('reason') or p.get('error'))
        elif kind == 'artifact_ready':
            if e.get('request_id') and request['id'] == e['request_id']:
                request['images'] = p.get('images')
        elif kind == 'target':
            execution.update(chunk=p.get('chunk'), step=p.get('step'), target23=p.get('target23'))
            # Even legacy targets are explicit issued-target evidence, not sampled post-motion state.
            execution['issued_steps'] += 1
            if p.get('feedback23') is not None:
                execution.update(feedback23=p['feedback23'], feedback_at=at, feedback_kind='prepublish')
            state['phase'] = 'target'
        elif kind == 'recovery_feedback':
            sample = p.get('sample_time')
            feedback_at = at + sample - e['monotonic'] if sample is not None and e.get('monotonic') is not None else at
            execution.update(feedback23=p.get('feedback23'), feedback_at=feedback_at,
                             feedback_kind='recovery_feedback')
        elif kind == 'operation_finished':
            result = p.get('result') or {}
            if result.get('reason') is not None:
                state['reason'] = result['reason']
            if p.get('mode') == 'run':
                verification.update(run_finished=result.get('finished'), stopped=result.get('stopped'), reason=result.get('reason'))
                state['phase'] = 'run_finished'
                terminal_operations[e['operation_id']] = 'run_finished'
            elif p.get('mode') == 'home':
                verification['home_success'] = result.get('success')
                state['phase'] = 'home_finished'
                terminal_operations[e['operation_id']] = 'home_finished'
            elif p.get('mode') == 'precheck':
                state['phase'] = 'precheck_failed' if p.get('result_kind') == 'CLIENT_ERROR' else 'precheck_finished'
                terminal_operations[e['operation_id']] = state['phase']
            # Neither a finished run nor home success independently verifies task completion.
            verification['evidence'].append(dict(kind=kind, observed_at=at, detail=p))
        elif kind == 'status':
            state.update(phase=p.get('state', state['phase']), reason=p.get('reason', state['reason']),
                         task=p.get('task', state['task']), model_id=p.get('model_id', state['model_id']))
            state['legacy'] = bool(p.get('legacy', state['legacy']))
        elif kind in ('grasp_detected', 'release_detected', 'clamped', 'capture_gap'):
            verification['evidence'].append(dict(kind=kind, observed_at=at, detail=p))
        verification['evidence'] = verification['evidence'][-100:]
    if state['operation_id'] in terminal_operations:
        state['phase'] = terminal_operations[state['operation_id']]
    return state


class VLAStore:
    """Thread-safe SQLite ledger; source offsets commit atomically with event inserts."""
    def __init__(self, path):
        self.path = ':memory:' if str(path) == ':memory:' else str(Path(path).resolve())
        if self.path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS vla_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS vla_run_seq ON vla_events(run_id,seq);
            CREATE TABLE IF NOT EXISTS vla_event_meta (
                event_id TEXT PRIMARY KEY, observed_at REAL NOT NULL, kind TEXT NOT NULL,
                run_id TEXT NOT NULL, request_id TEXT, light TEXT NOT NULL,
                session_id TEXT, activity INTEGER);
            CREATE INDEX IF NOT EXISTS vla_meta_time ON vla_event_meta(observed_at);
            CREATE TABLE IF NOT EXISTS vla_run_starts (
                source_id TEXT NOT NULL, event_id TEXT NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY(source_id,event_id));
            CREATE TABLE IF NOT EXISTS vla_event_sources (
                source_id TEXT NOT NULL, event_id TEXT NOT NULL, PRIMARY KEY(source_id,event_id));
            CREATE TABLE IF NOT EXISTS vla_sources (
                source_id TEXT PRIMARY KEY, cursor INTEGER NOT NULL, health TEXT, received_at REAL NOT NULL);
        ''')
        # Serialize schema upgrades with the receiver/server's separate connection.
        # Stored event bodies and offsets remain unchanged.
        self._db.execute('BEGIN IMMEDIATE')
        try:
            columns = {r[1] for r in self._db.execute('PRAGMA table_info(vla_event_meta)')}
            for name, kind in (('session_id', 'TEXT'), ('activity', 'INTEGER')):
                if name not in columns:
                    self._db.execute('ALTER TABLE vla_event_meta ADD COLUMN %s %s' % (name, kind))
            for (body,) in self._db.execute('SELECT e.body FROM vla_events e LEFT JOIN vla_event_meta m ON e.event_id=m.event_id WHERE m.event_id IS NULL OR m.session_id IS NULL OR m.activity IS NULL').fetchall():
                self._write_meta(json.loads(body))
            self._db.execute('CREATE INDEX IF NOT EXISTS vla_meta_session_activity ON vla_event_meta(session_id,activity,observed_at)')
            self._db.commit()
        except BaseException:
            self._db.rollback()
            self._db.close()
            raise

    def _write_meta(self, event):
        light = dict(event)
        if event['kind'] == 'artifact_ready':
            light['payload'] = {k: v for k, v in event['payload'].items() if k != 'images'}
        self._db.execute('INSERT OR IGNORE INTO vla_event_meta(event_id,observed_at,kind,run_id,request_id,light,session_id,activity) VALUES(?,?,?,?,?,?,?,?)',
                         (event['event_id'], event['observed_at'], event['kind'], event['run_id'], event.get('request_id'), json.dumps(light, ensure_ascii=False), event['session_id'], int(_activity(event))))
        self._db.execute('UPDATE vla_event_meta SET session_id=?,activity=? WHERE event_id=? AND (session_id IS NULL OR activity IS NULL)',
                         (event['session_id'], int(_activity(event)), event['event_id']))

    def ingest(self, events, source_id, cursor, health=None):
        if not isinstance(source_id, str) or not _ID.fullmatch(source_id):
            raise ValueError('Invalid VLA source identity')
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError('Invalid VLA source cursor')
        if not isinstance(events, (list, tuple)) or len(events) > _MAX_EVENTS:
            raise ValueError('Invalid VLA event batch')
        cleaned = [_event(e) for e in events]
        h = {}
        if health is not None:
            if not isinstance(health, dict):
                raise ValueError('Invalid VLA health')
            for key in ('observed_at', 'dropped'):
                if key in health:
                    h[key] = None if key == 'observed_at' and health[key] is None else _number(health[key])
            h['legacy'] = bool(health.get('legacy', False))
            for key in ('active_session_id', 'robot_id'):
                if key in health:
                    value = health[key]
                    if value is not None and (not isinstance(value, str) or not _ID.fullmatch(value)):
                        raise ValueError('Invalid VLA health identity')
                    h[key] = value
        with self._lock, self._db:
            for e in cleaned:
                self._write_meta(e)
                self._db.execute('INSERT OR IGNORE INTO vla_events(event_id,run_id,body) VALUES(?,?,?)',
                                 (e['event_id'], e['run_id'], json.dumps(e, ensure_ascii=False, allow_nan=False)))
                self._db.execute('INSERT OR IGNORE INTO vla_event_sources(source_id,event_id) VALUES(?,?)',
                                 (source_id, e['event_id']))
                if e['kind'] == 'operation_started' and e['payload'].get('mode') == 'run':
                    self._db.execute('INSERT OR IGNORE INTO vla_run_starts(source_id,event_id,run_id) VALUES(?,?,?)',
                                     (source_id, e['event_id'], e['run_id']))
            self._db.execute('''INSERT INTO vla_sources(source_id,cursor,health,received_at) VALUES(?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET cursor=MAX(cursor,excluded.cursor),
                    health=excluded.health,received_at=excluded.received_at''',
                (source_id, cursor, json.dumps(h), time.time()))
        return len(cleaned)

    def cursor(self, source_id):
        with self._lock:
            row = self._db.execute('SELECT cursor FROM vla_sources WHERE source_id=?', (source_id,)).fetchone()
            return row[0] if row else 0

    def events(self, run_id, after=0, limit=200):
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
            raise ValueError('Invalid VLA run identity')
        if isinstance(after, bool) or not isinstance(after, int) or after < 0 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError('Invalid VLA pagination')
        with self._lock:
            rows = self._db.execute('SELECT seq,body FROM vla_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?',
                                    (run_id, after, limit)).fetchall()
        result = []
        for seq, body in rows:
            e = json.loads(body)
            e['cursor'] = seq
            result.append(e)
        return {'events': result, 'next_cursor': rows[-1][0] if rows else after}

    def _all(self, source_id=None, run_id=None):
        if source_id is None:
            rows = self._db.execute('SELECT light FROM vla_event_meta ORDER BY observed_at,event_id')
        elif run_id is not None:
            rows = self._db.execute('SELECT e.light FROM vla_event_meta e JOIN vla_event_sources s ON e.event_id=s.event_id WHERE s.source_id=? AND e.run_id=? ORDER BY e.observed_at,e.event_id', (source_id, run_id))
        else:
            rows = self._db.execute('SELECT e.light FROM vla_event_meta e JOIN vla_event_sources s ON e.event_id=s.event_id WHERE s.source_id=? ORDER BY e.observed_at,e.event_id', (source_id,))
        return [json.loads(row[0]) for row in rows]

    def runs(self):
        with self._lock:
            groups = {}
            for e in self._all():
                groups.setdefault(e['run_id'], []).append(e)
            return [_project(es) for es in reversed(list(groups.values()))]

    def live(self, now=None):
        now = time.time() if now is None else _number(now)
        with self._lock:
            source = self._db.execute('SELECT health,received_at,source_id FROM vla_sources ORDER BY received_at DESC,rowid DESC LIMIT 1').fetchone()
            health = json.loads(source[0]) if source else {}
            active = None
            selected = health.get('active_session_id')
            if source and ('active_session_id' not in health or selected is not None):
                clause = ' AND m.session_id=?' if selected is not None else ''
                params = (source[2], now + 1) + ((selected,) if selected is not None else ())
                row = self._db.execute('SELECT m.run_id FROM vla_event_meta m JOIN vla_event_sources s ON m.event_id=s.event_id WHERE s.source_id=? AND m.activity=1 AND m.observed_at<=?' + clause + ' ORDER BY m.observed_at DESC,m.event_id DESC LIMIT 1', params).fetchone()
                if row is None:
                    row = self._db.execute("SELECT m.run_id FROM vla_event_meta m JOIN vla_event_sources s ON m.event_id=s.event_id WHERE s.source_id=? AND m.kind='status' AND m.observed_at<=?" + clause + ' ORDER BY m.observed_at DESC,m.event_id DESC LIMIT 1', params).fetchone()
                active = row[0] if row else None
            events = [e for e in self._all(source[2], active) if e['observed_at'] <= now + 1] if source and active else []
            state = _project(events)
            if not events and 'active_session_id' in health:
                state.update(session_id=selected, robot_id=health.get('robot_id'))
            rid = state['request']['id']
            if rid:
                artifact = self._db.execute("SELECT e.body FROM vla_events e JOIN vla_event_meta m ON e.event_id=m.event_id JOIN vla_event_sources s ON e.event_id=s.event_id WHERE s.source_id=? AND m.run_id=? AND m.request_id=? AND m.kind='artifact_ready' ORDER BY m.observed_at DESC,m.event_id DESC LIMIT 1", (source[2], active, rid)).fetchone()
                if artifact:
                    state['request']['images'] = json.loads(artifact[0])['payload'].get('images')
        activity = [e['observed_at'] for e in events if _activity(e) or e['kind'] == 'status']
        age = max(0, now - max(activity)) if activity else None
        if age is not None and health.get('observed_at') is not None:
            age = max(age, max(0, now - min(health['observed_at'], now)))
        connected = age is not None and age <= STALE_AFTER
        state['stats']['dropped'] = health.get('dropped', 0)
        state['legacy'] = bool(health.get('legacy', state['legacy']))
        status = 'connected' if connected else ('stale' if activity else 'waiting')
        if status == 'stale':
            state['phase'] = 'stale'
        return dict(configured=True, connected=connected, status=status, age_s=age, freshness_s=STALE_AFTER, state=state)

    def close(self):
        with self._lock:
            self._db.close()


def unconfigured_state():
    return dict(configured=False, connected=False, status='unconfigured', age_s=None, freshness_s=STALE_AFTER, state=_state())
