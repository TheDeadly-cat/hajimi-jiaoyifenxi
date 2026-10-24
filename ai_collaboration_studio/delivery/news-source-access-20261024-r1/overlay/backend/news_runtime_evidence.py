"""Bounded operational evidence for the local news worker, without payloads."""
from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone

HEARTBEAT_SECONDS = 30
HEARTBEAT_GAP_SECONDS = 90
RETENTION_SECONDS = 30 * 86400
MAX_EVIDENCE_ROWS = 100000


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z') if value is not None else None


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS monitor_sessions(
            id TEXT PRIMARY KEY, started REAL NOT NULL, heartbeat REAL NOT NULL,
            stopped REAL, state TEXT NOT NULL, stop_reason TEXT);
        CREATE INDEX IF NOT EXISTS monitor_sessions_started ON monitor_sessions(started);
        CREATE TABLE IF NOT EXISTS monitor_gaps(
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            kind TEXT NOT NULL, started REAL NOT NULL, ended REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS monitor_gaps_ended ON monitor_gaps(ended);
        CREATE TABLE IF NOT EXISTS poll_runs(
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, source_id TEXT NOT NULL,
            started REAL NOT NULL, finished REAL, outcome TEXT NOT NULL,
            http_status INTEGER, parsed_items INTEGER NOT NULL DEFAULT 0,
            new_items INTEGER NOT NULL DEFAULT 0, updated_items INTEGER NOT NULL DEFAULT 0,
            error_code TEXT);
        CREATE INDEX IF NOT EXISTS poll_runs_started ON poll_runs(started);
        CREATE INDEX IF NOT EXISTS poll_runs_source_started ON poll_runs(source_id,started);
        CREATE INDEX IF NOT EXISTS poll_runs_outcome ON poll_runs(outcome);
    ''')
    # Nullable additive columns preserve the distinction between old evidence
    # and checks for which these fields were actually observed.
    columns = {row[1] for row in db.execute('PRAGMA table_info(poll_runs)')}
    for name in ('trigger_kind', 'schedule_basis', 'diagnostics'):
        if name not in columns:
            db.execute(f'ALTER TABLE poll_runs ADD COLUMN {name} TEXT')


def add_gap(db, session_id, kind, started, ended):
    if ended > started:
        db.execute('INSERT INTO monitor_gaps(session_id,kind,started,ended) VALUES(?,?,?,?)',
                   (session_id, kind, started, ended))


def interrupt_pending(db, now):
    # An unfinished request could have reached its source.  Its outcome is unknown.
    db.execute("UPDATE poll_runs SET finished=?,outcome='interrupted_unknown',error_code='completion_unconfirmed' WHERE outcome='started'", (now,))
    db.execute("UPDATE sources SET status='interrupted_unknown',error=? WHERE status='fetching'",
               ('上次检查的完成状态未确认；已有消息保留，下一次检查将重新获取当前订阅。',))


def begin_session(db, now):
    session_id = uuid.uuid4().hex
    prior = db.execute('SELECT * FROM monitor_sessions ORDER BY started DESC,rowid DESC LIMIT 1').fetchone()
    if prior is not None:
        clean = prior['state'] == 'stopped'
        add_gap(db, session_id, 'clean_restart' if clean else 'unclean_restart',
                prior['stopped'] if clean else prior['heartbeat'], now)
    db.execute("UPDATE monitor_sessions SET state='interrupted_unknown',stop_reason='completion_unconfirmed' WHERE state='running'")
    interrupt_pending(db, now)
    db.execute("INSERT INTO monitor_sessions(id,started,heartbeat,state) VALUES(?,?,?,'running')", (session_id, now, now))
    if db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()[0] != 'true':
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('monitor_paused_since',?)", (str(now),))
    prune(db, now, session_id)
    return session_id


def heartbeat(db, session_id, now):
    row = db.execute('SELECT heartbeat FROM monitor_sessions WHERE id=?', (session_id,)).fetchone()
    if row is None:
        raise RuntimeError('monitor_session_missing')
    previous = row['heartbeat']
    if now - previous > HEARTBEAT_GAP_SECONDS:
        add_gap(db, session_id, 'heartbeat_gap', previous, now)
    elif now < previous:
        add_gap(db, session_id, 'clock_changed', now, previous)
    db.execute('UPDATE monitor_sessions SET heartbeat=? WHERE id=?', (now, session_id))


def stop_session(db, session_id, now):
    heartbeat(db, session_id, now)
    db.execute("UPDATE monitor_sessions SET stopped=?,state='stopped',stop_reason='worker_drained' WHERE id=?", (now, session_id))


def record_control(db, session_id, enabled, now):
    row = db.execute("SELECT value FROM settings WHERE key='monitor_paused_since'").fetchone()
    if enabled:
        if row is not None:
            add_gap(db, session_id or '', 'monitor_paused', float(row[0]), now)
            db.execute("DELETE FROM settings WHERE key='monitor_paused_since'")
    else:
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('monitor_paused_since',?)", (str(now),))


def begin_poll(db, session_id, source_id, now, *, trigger_kind=None, schedule_basis=None):
    poll_id = uuid.uuid4().hex
    trigger = trigger_kind if trigger_kind in ('scheduled', 'manual') else None
    db.execute("INSERT INTO poll_runs(id,session_id,source_id,started,outcome,trigger_kind,schedule_basis) VALUES(?,?,?,?,'started',?,?)",
               (poll_id,session_id,source_id,now,trigger,_safe_json(schedule_basis, 'schedule')))
    return poll_id


def finish_poll(db, poll_id, now, outcome, *, http_status=None, parsed_items=0, new_items=0, updated_items=0, error_code=None,
                diagnostics=None, schedule_basis=None):
    result = db.execute('''UPDATE poll_runs SET finished=?,outcome=?,http_status=?,parsed_items=?,new_items=?,updated_items=?,error_code=?,
        diagnostics=?,schedule_basis=COALESCE(?,schedule_basis)
        WHERE id=? AND outcome='started' ''', (now,outcome,http_status,parsed_items,new_items,updated_items,error_code,
            _safe_json(diagnostics, 'transport'),_safe_json(schedule_basis, 'schedule'),poll_id))
    if result.rowcount != 1:
        raise RuntimeError('poll_completion_not_owned')
    # Manual refreshes can outpace the scheduled heartbeat; enforce the bound
    # at completion as well as during periodic maintenance.
    prune(db, now, None)


def _safe_json(value, kind):
    """A second allowlist boundary: injected transports cannot persist secrets."""
    if not isinstance(value, dict):
        return None
    safe = {}
    enums = ({'schema_version': {'news_feed_diagnostics_v1'},
              'route_class': {'direct', 'local_loopback_proxy', 'unknown'},
              'final_host': {'www.federalreserve.gov', 'www.bls.gov', 'investors.seagate.com'},
              'error_kind': {'timeout', 'dns_error', 'tls_error', 'connection_error', 'redirect_blocked',
                             'feed_too_large', 'invalid_feed', 'unsafe_feed', 'transport_error', 'http_error'},
              'phase': {'proxy_configuration', 'request', 'redirect', 'response_headers', 'response_body',
                        'parse', 'complete'}}
             if kind == 'transport' else
             {'decision': {'normal', 'manual', 'retry_after', 'rate_limit_backoff', 'access_denied_backoff',
                           'failure_backoff', 'recovery_probe', 'manual_hold'},
              'retry_after_status': {'missing', 'valid', 'invalid', 'out_of_range'},
              'retry_after_kind': {'seconds', 'date', 'none'}})
    for key, allowed in enums.items():
        candidate = value.get(key)
        if candidate is None or isinstance(candidate, str) and candidate in allowed:
            safe[key] = candidate
    bools = ('effective_bypass', 'conditional_etag', 'conditional_modified_since') if kind == 'transport' else ('manual_hold',)
    for key in bools:
        if value.get(key) is None or isinstance(value.get(key), bool):
            safe[key] = value.get(key)
    ints = ('redirect_count', 'elapsed_ms') if kind == 'transport' else ('cooldown_level', 'access_denied_level', 'recovery_successes')
    for key in ints:
        candidate = value.get(key)
        if type(candidate) is int and 0 <= candidate <= 10**12:
            safe[key] = candidate
    if kind == 'transport':
        status = value.get('response_status')
        if status is None or type(status) is int and 100 <= status <= 599:
            safe['response_status'] = status
    dates = ('response_date',) if kind == 'transport' else ('not_before', 'server_not_before', 'rate_recovery_after')
    for key in dates:
        candidate = value.get(key)
        if candidate is None:
            safe[key] = None
        elif isinstance(candidate, str) and len(candidate) <= 40:
            try:
                parsed = datetime.fromisoformat(candidate.replace('Z', '+00:00'))
                if parsed.tzinfo is not None:
                    safe[key] = parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
            except (ValueError, OverflowError):
                pass
    retry = value.get('retry_after')
    if kind == 'transport' and isinstance(retry, dict):
        safe_retry = {}
        for key, allowed in (('status', {'missing', 'valid', 'invalid', 'out_of_range'}),
                             ('kind', {'seconds', 'date', 'none'})):
            if isinstance(retry.get(key), str) and retry[key] in allowed:
                safe_retry[key] = retry[key]
        for key in ('retry_at', 'delay_seconds'):
            item = retry.get(key)
            if item is None or type(item) in (int, float) and math.isfinite(item) and 0 <= item <= 253402300799:
                safe_retry[key] = item
        if isinstance(retry.get('manual_hold'), bool):
            safe_retry['manual_hold'] = retry['manual_hold']
        safe['retry_after'] = safe_retry
    return json.dumps(safe, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def prune(db, now, current_session):
    # No news item is removed. In-progress evidence is never discarded.
    for table, timestamp, active in (
        ('poll_runs', 'started', "outcome!='started'"),
        ('monitor_sessions', 'started', "state!='running'"),
        ('monitor_gaps', 'ended', '1=1'),
    ):
        db.execute(f'DELETE FROM {table} WHERE {timestamp}<? AND {active}', (now - RETENTION_SECONDS,))
        protected = db.execute(f'SELECT COUNT(*) FROM {table} WHERE NOT ({active})').fetchone()[0]
        db.execute(f'''DELETE FROM {table} WHERE rowid IN
            (SELECT rowid FROM {table} WHERE {active} ORDER BY {timestamp} DESC,rowid DESC LIMIT -1 OFFSET ?)''', (max(0,MAX_EVIDENCE_ROWS-protected),))


def overview(db, session_id, now):
    earliest = db.execute('SELECT MIN(started) FROM monitor_sessions').fetchone()[0]
    session = db.execute('SELECT * FROM monitor_sessions WHERE id=?', (session_id,)).fetchone() if session_id else None
    previous = db.execute('SELECT state FROM monitor_sessions WHERE id!=? ORDER BY started DESC,rowid DESC LIMIT 1', (session_id or '',)).fetchone()
    gap = db.execute('SELECT kind,started,ended FROM monitor_gaps ORDER BY id DESC LIMIT 1').fetchone()
    gap_count = db.execute('SELECT COUNT(*) FROM monitor_gaps WHERE ended>=? AND started<=?', (now-86400,now)).fetchone()[0]
    # A current open pause is visible even before the user resumes.
    paused = db.execute("SELECT value FROM settings WHERE key='monitor_paused_since'").fetchone()
    if paused is not None and float(paused[0]) < now:
        gap = {'kind':'monitor_paused', 'started':float(paused[0]), 'ended':now}
        gap_count += 1
    counts = dict(db.execute('''SELECT COUNT(*) AS started,
        COALESCE(SUM(outcome!='started'),0) AS completed,
        COALESCE(SUM(outcome IN ('success','not_modified')),0) AS successful,
        COALESCE(SUM(outcome='failed'),0) AS failed,
        COALESCE(SUM(outcome='interrupted_unknown'),0) AS interrupted,
        COALESCE(SUM(outcome='not_modified'),0) AS not_modified,
        COALESCE(SUM(new_items),0) AS new_items,COALESCE(SUM(updated_items),0) AS updated_items
        FROM poll_runs WHERE started>=? AND started<=?''', (now-86400,now)).fetchone())
    recent = []
    for row in db.execute('''SELECT p.*,s.name AS source_name FROM poll_runs p JOIN sources s ON s.id=p.source_id
        ORDER BY p.started DESC,p.rowid DESC LIMIT 10'''):
        recent.append({**{k:row[k] for k in ('source_id','source_name','outcome','http_status','new_items','updated_items','error_code','trigger_kind')},
                       'schedule_basis':json.loads(row['schedule_basis']) if row['schedule_basis'] else None,
                       'diagnostics':json.loads(row['diagnostics']) if row['diagnostics'] else None,
                       'started_at':iso(row['started']), 'finished_at':iso(row['finished'])})
    continuity = {'schema_version':'news_continuity_v1','evidence_started_at':iso(earliest),
        'session_started_at':iso(session['started']) if session else None,
        'last_heartbeat_at':iso(session['heartbeat']) if session else None,
        'previous_session_unclean':bool(previous and previous['state'] != 'stopped'),
        'last_gap':{'kind':gap['kind'],'started_at':iso(gap['started']),'ended_at':iso(gap['ended']),
                    'duration_seconds':max(0,round(gap['ended']-gap['started'],3))} if gap else None,
        'unverified_gap_count_24h':gap_count,'continuous_24h_proven':False,
        'limitations':['记录从本次更新后开始，历史总消息数不代表连续抓取时长。',
                        '心跳缺口表示该时段运行无法确认，不是精确停机时长；订阅可能遗漏停机期间消息。',
                        '运行与检查记录保留30天及每类最多10万条；尚未据此验收完整24小时连续运行。']}
    return {'continuity':continuity, 'checks_24h':counts, 'recent_checks':recent}
