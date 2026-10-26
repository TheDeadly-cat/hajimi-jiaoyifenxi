"""A small, independent, local news reader. No model, credentials or trading calls.

Creating an instance never accesses the network. The host owns start/stop; the
user owns enabling continuous polling or requesting a single refresh.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from .path_identity import first_reparse_component
from . import news_runtime_evidence as runtime_evidence


from .news_feed_transport import (
    SOURCES, FeedResponse, FeedValidationError, classify_feed_error, fetch_feed, _SameHostRedirect, _safe_url, parse_retry_after, failure_retry_after,
)


CATEGORIES = (
    {"id": "monetary", "label": "货币政策"},
    {"id": "inflation", "label": "物价与通胀"},
    {"id": "employment", "label": "就业与工资"},
    {"id": "banking", "label": "银行与监管"},
    {"id": "company", "label": "公司公告"},
    {"id": "general", "label": "其他官方消息"},
)
LIMITATIONS = ["仅整理官方订阅中的标题与摘录，未抓取或验证全文。", "摘要和分类由本机规则生成，不含 AI 推断；请打开原文核对。"]
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 12
RATE_RECOVERY_SECONDS = 24 * 3600
APPLICATION_ID = 0x4E455753


def _independent_database_path(path):
    """Inspect the requested path before resolve can hide an alias."""
    requested = Path(path).expanduser()
    if first_reparse_component(requested) is not None:
        raise ValueError("news_database_alias_not_allowed")
    if os.path.lexists(requested):
        metadata = requested.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("news_database_must_be_independent_regular_file")
    return requested.resolve()


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value is not None else None


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "div", "br", "li", "h1", "h2", "h3"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif tag in {"p", "div", "li"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value, limit=12000):
    parser = _PlainText()
    parser.feed(str(value or "")[:100000])
    parser.close()
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()[:limit]


def _tag(element):
    return element.tag.rsplit("}", 1)[-1].lower() if isinstance(element.tag, str) else ""


def _element_text(element):
    if element is None:
        return ""
    return "".join(element.itertext())


def _date(value):
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (ValueError, TypeError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError, OverflowError):
            return None
    if parsed.tzinfo is None:
        return None  # An unqualified local time is not a proven publication time.
    try:
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def parse_feed(body, source):
    if len(body) > MAX_BYTES:
        raise FeedValidationError("feed_too_large")
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", body.replace(b"\x00", b""), re.I):
        raise FeedValidationError("unsafe_feed")
    root = ET.fromstring(body)
    if _tag(root) not in {"rss", "feed", "rdf"}:
        raise FeedValidationError("invalid_feed")
    result = []
    for entry in (element for element in root.iter() if _tag(element) in {"item", "entry"}):
        if len(result) >= 500:
            break
        fields = {}
        for child in entry:
            fields.setdefault(_tag(child), child)
        title = plain_text(_element_text(fields.get("title")), 700)
        if not title:
            continue
        link = ""
        for child in entry:
            if _tag(child) == "link" and child.attrib.get("rel", "alternate") == "alternate":
                link = _safe_url(child.attrib.get("href") or _element_text(child), source["url"])
                if link:
                    break
        published_raw = next((_element_text(fields.get(key)) for key in ("pubdate", "published", "date", "updated") if key in fields), "")
        excerpt = next((plain_text(_element_text(fields.get(key))) for key in ("description", "summary", "encoded", "content") if key in fields), "")
        guid = next((_element_text(fields.get(key)).strip() for key in ("guid", "id") if key in fields), "")[:2000]
        identity = guid or link or hashlib.sha256((title + "\n" + published_raw).encode()).hexdigest()
        result.append({"id": hashlib.sha256((source["id"] + "\n" + identity).encode()).hexdigest(),
                       "identity": identity, "title": title, "url": link,
                       "excerpt": excerpt, "published": _date(published_raw)})
    return result


def analyze(title, excerpt, published, now, source_id=""):
    searchable = (title + " " + excerpt).lower()
    rules = (
        ("inflation", ("inflation", "consumer price", "producer price", "cpi", "pce", "通胀", "物价")),
        ("employment", ("employment", "payroll", "labor market", "labour", "unemployment", "wages", "就业", "工资")),
        ("monetary", ("fomc", "monetary", "interest rate", "federal funds", "policy rate", "利率", "货币政策")),
        ("banking", ("bank", "supervision", "regulation", "enforcement", "银行", "监管")),
        ("company", ("financial results", "earnings", "quarterly", "revenue", "dividend", "财报", "营收")),
    )
    category, keywords = ("company" if source_id == "seagate" else "general"), []
    for candidate, terms in rules:
        hits = [term for term in terms if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", searchable) if term.isascii()]
        hits += [term for term in terms if not term.isascii() and term in searchable]
        if hits:
            category, keywords = candidate, hits
            break
    limitations = list(LIMITATIONS)
    if not excerpt:
        limitations.append("订阅未提供摘录，当前仅展示标题。")
    if published is None:
        limitations.append("来源未提供可确认时区的发布时间；发现时间不等于事件发生时间。")
    elif published > now:
        limitations.append("来源标注的发布时间在未来，请核对原文；未计入最近 24 小时已发布消息。")
    summary = excerpt[:280] + ("…" if len(excerpt) > 280 else "") if excerpt else title
    return {"summary": summary, "category": category, "keywords": keywords, "limitations": limitations,
            "importance": "attention" if keywords else "normal"}


class NewsPlatform:
    def __init__(self, db_path, *, transport=None, clock=time.time, poll_interval=900):
        self.path = _independent_database_path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _independent_database_path(db_path)
        self.transport = transport or fetch_feed
        self.clock = clock
        self.poll_interval = max(60, int(poll_interval))
        self._maximum_cooldown_level = 1
        while self.poll_interval * 2 ** self._maximum_cooldown_level < 6 * 3600:
            self._maximum_cooldown_level += 1
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._worker_exit_confirmed = False
        self._refresh_pending = False
        self._refresh_remaining = set()
        self._inflight = set()
        self._manual_inflight = set()
        self._pause_epoch = 0
        self._session_id = None
        self._last_heartbeat = None
        self._last_pruned = None
        self._pending_recovery = False
        self._pending_cooldowns = {}
        self._worker_failures = 0
        self._worker_error = None
        self._worker_error_at = None
        self._next_recovery = None
        self._initialize()

    @contextmanager
    def _db(self):
        with self._lock:
            connection = sqlite3.connect(self.path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

    def _initialize(self):
        with self._db() as db:
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if app_id != APPLICATION_ID and (app_id != 0 or tables):
                raise ValueError("news_database_must_be_independent")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO settings VALUES('enabled','false');
                CREATE TABLE IF NOT EXISTS sources(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'never',
                    last_success REAL, last_attempt REAL, next_poll REAL NOT NULL DEFAULT 0,
                    error TEXT, failures INTEGER NOT NULL DEFAULT 0, etag TEXT, last_modified TEXT);
                CREATE TABLE IF NOT EXISTS items(
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
                    identity TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
                    excerpt TEXT NOT NULL, published REAL, discovered REAL NOT NULL, updated REAL NOT NULL,
                    read INTEGER NOT NULL DEFAULT 0, analysis TEXT NOT NULL,
                    UNIQUE(source_id,identity));
                CREATE INDEX IF NOT EXISTS items_discovered ON items(discovered DESC);
                CREATE INDEX IF NOT EXISTS items_published ON items(published DESC);
            """)
            for source in SOURCES:
                db.execute("INSERT INTO sources(id,name,url) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url", tuple(source.values()))
            columns = {row[1] for row in db.execute('PRAGMA table_info(sources)')}
            additions = {
                'not_before': 'REAL NOT NULL DEFAULT 0',
                'cooldown_level': 'INTEGER NOT NULL DEFAULT 0',
                'recovery_successes': 'INTEGER NOT NULL DEFAULT 0',
                'access_denied_level': 'INTEGER NOT NULL DEFAULT 0',
                'access_recovery_successes': 'INTEGER NOT NULL DEFAULT 0',
                'cooldown_reason': 'TEXT',
                'manual_hold': 'INTEGER NOT NULL DEFAULT 0',
                'refresh_queued': 'INTEGER NOT NULL DEFAULT 0',
                'rate_recovery_not_before': 'REAL NOT NULL DEFAULT 0',
            }
            for name, declaration in additions.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE sources ADD COLUMN {name} {declaration}')
            if 'not_before' not in columns:
                # Existing failed-check deadlines remain binding after migration.
                # No old receipt is rewritten or given invented HTTP diagnostics.
                db.execute("UPDATE sources SET not_before=MAX(0,next_poll),cooldown_reason='http_error' WHERE failures>0 OR status='error'")
            if 'rate_recovery_not_before' not in columns:
                # Legacy pressure has no reliable last-429 timestamp. Start a
                # conservative wait without rewriting any historical receipt.
                db.execute("UPDATE sources SET rate_recovery_not_before=MAX(not_before,?) WHERE cooldown_level>0",
                           (self.clock() + RATE_RECOVERY_SECONDS,))
            self._sync_refresh_state(db)
            runtime_evidence.initialize(db)

    def _ensure_session(self):
        with self._lock:
            if self._session_id is not None:
                return
            now = self.clock()
            with self._db() as db:
                session_id = runtime_evidence.begin_session(db, now)
            self._session_id = session_id
            self._last_heartbeat = self._last_pruned = now

    def _record_tick(self):
        self._ensure_session()
        self._restore_pending_cooldowns()
        now = self.clock()
        due = self._last_heartbeat is None or now < self._last_heartbeat or now - self._last_heartbeat >= runtime_evidence.HEARTBEAT_SECONDS
        if due or self._pending_recovery:
            with self._db() as db:
                if self._pending_recovery:
                    runtime_evidence.interrupt_pending(db, now)
                if due:
                    runtime_evidence.heartbeat(db, self._session_id, now)
                if self._last_pruned is None or now - self._last_pruned >= 3600:
                    runtime_evidence.prune(db, now, self._session_id)
            self._pending_recovery = False
            if due:
                self._last_heartbeat = now
            if self._last_pruned is None or now - self._last_pruned >= 3600:
                self._last_pruned = now

    def _restore_pending_cooldowns(self):
        """Commit known safety deadlines before another transport can run.

        Only bounded scheduling scalars are retained after a failed local
        transaction. Its old receipt stays interrupted/unknown; this recovery
        must never fabricate a completed HTTP result or a successful probe.
        """
        with self._lock:
            if not self._pending_cooldowns:
                return
            with self._db() as db:
                runtime_evidence.interrupt_pending(db, self.clock())
                for source_id, pending in self._pending_cooldowns.items():
                    db.execute('''UPDATE sources SET not_before=MAX(not_before,?),next_poll=MAX(next_poll,?),
                        cooldown_level=MAX(cooldown_level,?),access_denied_level=MAX(access_denied_level,?),
                        recovery_successes=0,access_recovery_successes=0,cooldown_reason=?,
                        rate_recovery_not_before=MAX(rate_recovery_not_before,?),
                        manual_hold=MAX(manual_hold,?),refresh_queued=CASE WHEN ? THEN 0 ELSE refresh_queued END WHERE id=?''',
                        (pending['not_before'],pending['not_before'],pending['cooldown_level'],pending['access_denied_level'],
                         pending['cooldown_reason'],pending.get('rate_recovery_not_before',0),int(pending['manual_hold']),int(pending['manual_hold']),source_id))
            # A failed commit leaves this map intact, so even a direct _fetch
            # entry must retry persistence, never the remote source.
            self._pending_cooldowns.clear()

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            if self._thread is not None:
                # A dead thread cannot vouch for the previous session's ending.
                self._session_id = None
            self._stop.clear()
            self._worker_exit_confirmed = False
            self._thread = threading.Thread(target=self._loop, name="official-news-poller", daemon=True)
            self._thread.start()

    def stop(self, timeout=20):
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0, timeout))
        drained = not (thread and thread.is_alive())
        if drained and self._session_id is not None:
            self._restore_pending_cooldowns()
            with self._db() as db:
                runtime_evidence.interrupt_pending(db, self.clock())
                if thread is None or self._worker_exit_confirmed:
                    runtime_evidence.stop_session(db, self._session_id, self.clock())
                else:
                    db.execute("UPDATE monitor_sessions SET state='interrupted_unknown',stop_reason='worker_exit_unconfirmed' WHERE id=?", (self._session_id,))
            self._session_id = None
        return drained

    def control(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("enabled_must_be_boolean")
        with self._db() as db:
            db.execute("UPDATE settings SET value=? WHERE key='enabled'", (json.dumps(enabled),))
            runtime_evidence.record_control(db, self._session_id, enabled, self.clock())
            if not enabled:
                self._refresh_pending = False
                self._refresh_remaining.clear()
                db.execute("UPDATE sources SET refresh_queued=0")
                self._pause_epoch += 1
        if enabled and self._thread is not None and not self._thread.is_alive():
            self.start()
        self._wake.set()
        return self.overview()

    def _sync_refresh_state(self, db):
        self._refresh_remaining = {row[0] for row in db.execute('SELECT id FROM sources WHERE refresh_queued=1 AND enabled=1 AND manual_hold=0')}
        self._refresh_pending = bool(self._refresh_remaining)

    def refresh(self):
        # A durable single pending bit coalesces repeated clicks. A request
        # already in flight satisfies clicks received while it is running.
        with self._db() as db:
            self._sync_refresh_state(db)
            if not self._refresh_pending and not self._manual_inflight:
                for source in SOURCES:
                    if source['id'] not in self._inflight:
                        db.execute('UPDATE sources SET refresh_queued=1 WHERE id=? AND enabled=1 AND manual_hold=0', (source['id'],))
                self._sync_refresh_state(db)
        if self._thread is not None and not self._thread.is_alive():
            self.start()
        self._wake.set()
        return self.overview()

    def _loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            delay = 1
            try:
                self._cycle()
            except Exception as exc:
                # A transient local database error must not silently kill polling.
                # No exception text, source payload, credential or URL is retained.
                with self._lock:
                    self._pending_recovery = True
                    self._worker_failures += 1
                    self._worker_error = 'database_unavailable' if isinstance(exc, sqlite3.Error) else 'worker_operation_failed'
                    self._worker_error_at = self.clock()
                    delay = min(30, 2 ** min(self._worker_failures - 1, 5))
                    self._next_recovery = self.clock() + delay
            else:
                with self._lock:
                    self._worker_failures = 0
                    self._worker_error = None
                    self._next_recovery = None
            self._wake.wait(delay)
        self._worker_exit_confirmed = True

    def _cycle(self):
        self._record_tick()
        with self._lock:
            pause_epoch = self._pause_epoch
        with self._db() as db:
            rows = db.execute('SELECT * FROM sources WHERE enabled=1 ORDER BY id').fetchall()
        for source in rows:
            if self._stop.is_set():
                break
            with self._db() as db:
                if pause_epoch != self._pause_epoch:
                    break
                enabled = db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()[0] == 'true'
                current = db.execute('SELECT * FROM sources WHERE id=?', (source['id'],)).fetchone()
            force = bool(current['refresh_queued'])
            if (force or enabled) and (force or current['next_poll'] <= self.clock()):
                self._fetch(current, trigger_kind='manual' if force else 'scheduled', pause_epoch=pause_epoch)

    def _pressure_delay(self, level):
        return min(6 * 3600, self.poll_interval * 2 ** min(max(1, level), self._maximum_cooldown_level))

    def _schedule_basis(self, source, decision, *, retry=None, not_before=None):
        return {'decision':decision, 'not_before':_iso(source['not_before'] if not_before is None else not_before),
                'cooldown_level':source['cooldown_level'], 'access_denied_level':source['access_denied_level'],
                'recovery_successes':source['recovery_successes'], 'manual_hold':bool(source['manual_hold']),
                'rate_recovery_successes':source['recovery_successes'],
                'access_recovery_successes':source['access_recovery_successes'],
                'retry_after_status':retry['status'] if retry else None,
                'retry_after_kind':retry['kind'] if retry else None,
                'server_not_before':_iso(retry['retry_at']) if retry and retry['retry_at'] is not None else None,
                'rate_recovery_after':_iso(source['rate_recovery_not_before']) if source['rate_recovery_not_before'] else None}

    def _fetch(self, source, *, trigger_kind='manual', pause_epoch=None):
        self._ensure_session()
        self._restore_pending_cooldowns()
        # Re-read authoritative state under the same lock as controls and
        # reservation. Neither stale source objects nor manual refresh bypass
        # durable cooldowns, and the transport runs outside this lock.
        with self._lock:
            with self._db() as db:
                source = db.execute('SELECT * FROM sources WHERE id=?', (source['id'],)).fetchone()
                started = self.clock()
                if (source is None or not source['enabled'] or source['id'] in self._inflight
                        or source['manual_hold'] or source['not_before'] > started
                        or pause_epoch is not None and (pause_epoch != self._pause_epoch or self._stop.is_set())):
                    return False
                # A manual queue entry is acknowledged only in the same commit
                # as its completed receipt. A local write failure therefore
                # preserves the request while completed sources stay consumed.
                db.execute("UPDATE sources SET status='fetching',last_attempt=? WHERE id=?", (started, source['id']))
                decision = 'recovery_probe' if source['cooldown_level'] or source['access_denied_level'] else trigger_kind if trigger_kind == 'manual' else 'normal'
                poll_id = runtime_evidence.begin_poll(db, self._session_id, source['id'], started,
                    trigger_kind=trigger_kind, schedule_basis=self._schedule_basis(source, decision))
                self._sync_refresh_state(db)
            self._inflight.add(source['id'])
            if trigger_kind == 'manual':
                self._manual_inflight.add(source['id'])
        try:
            self._fetch_reserved(source, poll_id, started)
        finally:
            with self._lock:
                self._inflight.discard(source['id'])
                self._manual_inflight.discard(source['id'])
        return True

    def _fetch_reserved(self, source, poll_id, started):
        headers = {}
        if source["etag"]:
            headers["If-None-Match"] = source["etag"]
        if source["last_modified"]:
            headers["If-Modified-Since"] = source["last_modified"]
        response_status = None
        response_headers = None
        diagnostics = None
        parsing_feed = False
        try:
            response = self.transport(source["url"], headers, TIMEOUT_SECONDS, MAX_BYTES)
            response_status = response.status
            response_headers = response.headers
            diagnostics = getattr(response, 'diagnostics', None)
            if response.status not in (200, 304):
                raise ValueError(f"http_status_{response.status}")
            parsing_feed = response.status == 200
            entries = parse_feed(response.body, source) if parsing_feed else []
            parsing_feed = False
            now = self.clock()
            safe_headers = {key.lower(): str(value) for key, value in (response.headers or {}).items()}
            def cache_header(name, old):
                value = safe_headers.get(name, old)
                return value if value and len(value) <= 2000 and not any(ord(c) < 32 for c in value) else None
            with self._db() as db:
                new_items = updated_items = 0
                for entry in entries:
                    analysis = analyze(entry["title"], entry["excerpt"], entry["published"], now, source["id"])
                    existing = db.execute('SELECT title,url,excerpt,published FROM items WHERE id=?', (entry['id'],)).fetchone()
                    if existing is None:
                        new_items += 1
                    elif any(existing[key] != entry[key] for key in ('title','url','excerpt','published')):
                        updated_items += 1
                    db.execute("""INSERT INTO items(id,source_id,identity,title,url,excerpt,published,discovered,updated,analysis)
                        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                        title=excluded.title,url=excluded.url,excerpt=excluded.excerpt,published=excluded.published,
                        updated=CASE WHEN items.title!=excluded.title OR items.url!=excluded.url OR items.excerpt!=excluded.excerpt OR items.published IS NOT excluded.published THEN excluded.updated ELSE items.updated END,
                        analysis=excluded.analysis""", (entry["id"], source["id"], entry["identity"], entry["title"], entry["url"], entry["excerpt"], entry["published"], now, now, json.dumps(analysis, ensure_ascii=False)))
                level, recovery = source['cooldown_level'], source['recovery_successes']
                rate_recovery_at = source['rate_recovery_not_before']
                access_level, access_recovery = source['access_denied_level'], source['access_recovery_successes']
                # Only completed successful probes at/after their deadline
                # count; one 200 or 304 never erases recent rate-limit pressure.
                if started >= source['not_before']:
                    if level:
                        recovery = min(3, recovery + 1)
                        if recovery >= 3 and now >= rate_recovery_at:
                            level, recovery = max(0, level - 1), 0
                            rate_recovery_at = now + RATE_RECOVERY_SECONDS if level else 0
                    if access_level:
                        access_recovery += 1
                        if access_recovery >= 3:
                            access_level, access_recovery = max(0, access_level - 1), 0
                pressure = max(level, access_level)
                delay = self._pressure_delay(pressure) if pressure else self.poll_interval
                not_before = now + delay if pressure else 0
                reason = 'rate_limited' if level else 'access_denied' if access_level else None
                db.execute("""UPDATE sources SET status='ok',last_success=?,next_poll=?,error=NULL,failures=0,etag=?,last_modified=?,
                    cooldown_level=?,recovery_successes=?,access_denied_level=?,access_recovery_successes=?,not_before=?,cooldown_reason=?,rate_recovery_not_before=?,refresh_queued=0 WHERE id=?""",
                    (now, now + delay, cache_header('etag', source['etag']), cache_header('last-modified', source['last_modified']),
                     level,recovery,access_level,access_recovery,not_before,reason,rate_recovery_at,source['id']))
                saved = db.execute('SELECT * FROM sources WHERE id=?', (source['id'],)).fetchone()
                runtime_evidence.finish_poll(db, poll_id, now, 'not_modified' if response.status == 304 else 'success',
                    http_status=response.status, parsed_items=len(entries), new_items=new_items, updated_items=updated_items,
                    diagnostics=diagnostics, schedule_basis=self._schedule_basis(saved, 'recovery_probe' if pressure else 'normal'))
        except sqlite3.Error:
            # If the persistence transaction fails its pending receipt remains unknown;
            # it must not be promoted to a transport failure or a success.
            with self._lock:
                pressure = max(source['cooldown_level'], source['access_denied_level'])
                if response_status in (200, 304) and pressure:
                    # An uncommitted recovery probe cannot advance the success
                    # streak or justify another probe immediately. Keep the old
                    # pressure, not the tentative lowered level in that rollback.
                    self._pending_cooldowns[source['id']] = {
                        'not_before':max(source['not_before'],self.clock() + self._pressure_delay(pressure)),
                        'cooldown_level':source['cooldown_level'],
                        'access_denied_level':source['access_denied_level'],
                        'cooldown_reason':'rate_limited' if source['cooldown_level'] else 'access_denied',
                        'manual_hold':bool(source['manual_hold']),
                        'rate_recovery_not_before':source['rate_recovery_not_before']}
                self._pending_recovery = True
            raise
        except Exception as exc:
            now = self.clock()
            failures = source['failures'] + 1
            if isinstance(exc, urllib.error.HTTPError):
                response_status = exc.code
                response_headers = exc.headers
            diagnostics = getattr(exc, 'news_diagnostics', diagnostics)
            error_kind = 'invalid_feed' if isinstance(exc, ET.ParseError) else classify_feed_error(exc)
            if response_status is None and isinstance(diagnostics, dict):
                observed_status = diagnostics.get('response_status')
                if type(observed_status) is int and 100 <= observed_status <= 599:
                    response_status = observed_status
            if parsing_feed and isinstance(exc, (FeedValidationError, ET.ParseError)):
                diagnostics = dict(diagnostics) if isinstance(diagnostics, dict) else {}
                diagnostics.update(error_kind=error_kind, phase='parse',
                                   response_status=response_status)
            retry = failure_retry_after(response_headers, diagnostics, now)
            level, access_level = source['cooldown_level'], source['access_denied_level']
            rate_recovery_at = source['rate_recovery_not_before']
            if response_status == 429:
                level = min(self._maximum_cooldown_level, level + 1)
                rate_recovery_at = max(rate_recovery_at, now + RATE_RECOVERY_SECONDS)
                error_code, reason, decision = 'rate_limited', 'rate_limited', 'rate_limit_backoff'
                error = '本次请求返回 HTTP 429，已保留消息并等待冷却结束；每次提高检查频率至少等待24小时并完成三次成功检查。'
                delay = self._pressure_delay(level)
            elif response_status == 403:
                access_level = min(self._maximum_cooldown_level, access_level + 1)
                error_code, reason, decision = 'access_denied', 'access_denied', 'access_denied_backoff'
                error = '本次请求返回 HTTP 403：访问被拒绝，原因待确认；已有消息保留，将保守等待后检查。持续拒绝时需核查来源访问策略与项目联系信息。'
                delay = self._pressure_delay(access_level)
            else:
                error_code = ('http_error' if response_status not in (None, 200, 304)
                              and error_kind in ('http_error', 'transport_error') else error_kind)
                reason, decision = error_code, 'failure_backoff'
                messages = {
                    'invalid_feed':'来源内容不是有效订阅格式，已保留已有消息。',
                    'unsafe_feed':'来源订阅包含不支持的内容结构，已拒绝解析并保留已有消息。',
                    'feed_too_large':'来源内容超出读取上限，已保留已有消息。',
                    'timeout':'来源连接或读取超时，已保留已有消息并安排稍后重试。',
                    'dns_error':'来源域名解析失败，已保留已有消息。',
                    'tls_error':'来源安全连接验证失败，已保留已有消息。',
                    'connection_error':'来源连接失败，已保留已有消息。',
                    'redirect_blocked':'来源跳转超出允许范围，已保留已有消息。',
                    'proxy_configuration_error':'本机来源代理配置不受支持或无效；未发送来源请求，也未自动改用直连。已有消息和冷却状态保留。',
                }
                error = messages.get(error_code, f'本次请求返回 HTTP {response_status}，已安排稍后重试。'
                                     if error_code == 'http_error' else '本次获取失败，已保留已有消息并安排稍后重试。')
                delay = min(6 * 3600, self.poll_interval * 2 ** min(failures - 1, 10))
            if level or access_level:
                delay = max(delay, self._pressure_delay(max(level, access_level)))
            not_before = max(source['not_before'], now + delay)
            if retry['status'] == 'valid' and retry['retry_at'] is not None:
                # Server deadlines have no local six-hour truncation.
                if retry['retry_at'] > not_before:
                    decision = 'retry_after'
                not_before = max(not_before, retry['retry_at'])
            manual_hold = bool(source['manual_hold'] or retry['manual_hold'])
            if manual_hold:
                decision = 'manual_hold'
                error += ' 等待期限超出可表示范围，已暂停该来源的自动与手动请求，需要人工核查。'
            with self._lock:
                try:
                    with self._db() as db:
                        db.execute("""UPDATE sources SET status='error',error=?,failures=?,next_poll=?,not_before=?,
                            cooldown_level=?,access_denied_level=?,recovery_successes=0,access_recovery_successes=0,
                            cooldown_reason=?,manual_hold=?,rate_recovery_not_before=?,refresh_queued=0 WHERE id=?""",
                            (error,failures,not_before,not_before,level,access_level,reason,int(manual_hold),rate_recovery_at,source['id']))
                        saved = db.execute('SELECT * FROM sources WHERE id=?', (source['id'],)).fetchone()
                        runtime_evidence.finish_poll(db, poll_id, now, 'failed', http_status=response_status, error_code=error_code,
                            diagnostics=diagnostics, schedule_basis=self._schedule_basis(saved, decision, retry=retry))
                except Exception:
                    # The source already returned its result, but neither its
                    # result receipt nor its safety deadline committed. Retain
                    # no payload, raw headers or exception text here.
                    self._pending_cooldowns[source['id']] = {
                        'not_before':not_before,'cooldown_level':level,'access_denied_level':access_level,
                        'cooldown_reason':reason,'manual_hold':manual_hold,
                        'rate_recovery_not_before':rate_recovery_at}
                    self._pending_recovery = True
                    raise

    def overview(self):
        now = self.clock()
        with self._db() as db:
            enabled = db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()[0] == "true"
            sources = [dict(row) for row in db.execute("SELECT * FROM sources ORDER BY id")]
            counts = dict(db.execute("SELECT COUNT(*) AS total,COALESCE(SUM(read=0),0) AS unread,COALESCE(SUM(published>=? AND published<=?),0) AS last_24h,COALESCE(SUM(discovered>=?),0) AS discovered_24h FROM items", (now - 86400, now, now - 86400)).fetchone())
            evidence = runtime_evidence.overview(db, self._session_id, now)
        serialized = []
        for source in sources:
            serialized.append({"id": source["id"], "name": source["name"], "url": source["url"], "enabled": bool(source["enabled"]), "status": source["status"],
                               "last_success_at": _iso(source["last_success"]), "last_attempt_at": _iso(source["last_attempt"]),
                               "next_poll_at": _iso(source["next_poll"]) if enabled and source["next_poll"] and not source["manual_hold"] else None,
                               "error": source["error"], "consecutive_failures": source["failures"],
                               "cooldown_until": _iso(source['not_before']) if source['not_before'] and not source['manual_hold'] else None,
                               "cooldown_active": bool(source['manual_hold'] or source['not_before'] > now),
                               "cooldown_reason": source['cooldown_reason'],
                               "rate_recovery_after": _iso(source['rate_recovery_not_before']) if source['rate_recovery_not_before'] else None,
                               "recovery_pending": bool(source['cooldown_level'] or source['access_denied_level']),
                               "recovery_successes": source['recovery_successes'] if source['cooldown_level'] else source['access_recovery_successes'],
                               "refresh_queued": bool(source['refresh_queued'] and source['enabled'] and not source['manual_hold']), "manual_hold": bool(source['manual_hold']),
                               "stale": source['last_success'] is None or now - source['last_success'] > self.poll_interval + runtime_evidence.HEARTBEAT_GAP_SECONDS,
                               "overdue": bool(enabled and source['enabled'] and not source['manual_hold'] and source['next_poll'] and now > source['next_poll'] + runtime_evidence.HEARTBEAT_GAP_SECONDS),
                               "overdue_seconds": max(0,round(now-source['next_poll'])) if enabled and source['next_poll'] and not source['manual_hold'] else 0})
        successes = [source["last_success"] for source in sources if source["last_success"] is not None]
        next_polls = [source["next_poll"] for source in sources if source["next_poll"] and source["enabled"] and not source["manual_hold"]]
        counts.update(sources_ok=sum(s["status"] == "ok" and not s['stale'] for s in serialized), sources_error=sum(s["status"] == "error" for s in sources))
        alive = bool(self._thread and self._thread.is_alive())
        heartbeat_fresh = self._last_heartbeat is not None and 0 <= now-self._last_heartbeat <= runtime_evidence.HEARTBEAT_GAP_SECONDS
        worker_health = {'status':'stopped' if not alive else 'recovering' if self._worker_failures else 'unresponsive' if not heartbeat_fresh else 'running' if enabled else 'paused',
            'alive':alive,'heartbeat_fresh':heartbeat_fresh,
            'last_error_code':self._worker_error or ('worker_heartbeat_unconfirmed' if alive and not heartbeat_fresh else None),'last_error_at':_iso(self._worker_error_at),
            'consecutive_failures':self._worker_failures,'next_recovery_at':_iso(self._next_recovery),
            'heartbeat_at':evidence['continuity']['last_heartbeat_at']}
        return {"enabled": enabled, "running": bool(self._thread and self._thread.is_alive()), "refresh_pending": any(source["refresh_queued"] for source in serialized),
                "last_success_at": _iso(max(successes)) if successes else None,
                "next_poll_at": _iso(min(next_polls)) if enabled and next_polls else None,
                "counts": counts, "sources": serialized, "categories": list(CATEGORIES),
                "worker_health":worker_health, **evidence,
                "analysis": {"mode": "local_rules", "label": "本机规则整理", "ai_configured": False, "limitations": list(LIMITATIONS)}}

    def _serialize_item(self, row):
        item = dict(row)
        analysis = json.loads(item.pop("analysis"))
        item.pop("identity", None)
        item.update(analysis)
        item["category_label"] = next((c["label"] for c in CATEGORIES if c["id"] == item["category"]), "其他官方消息")
        item["read"] = bool(item["read"])
        for old, new in (("published", "published_at"), ("discovered", "discovered_at"), ("updated", "updated_at")):
            item[new] = _iso(item.pop(old))
        item["analysis_mode"] = "local_rules"
        return item

    def list_items(self, query="", source="", category="", unread_only=False, limit=100, offset=0, importance=""):
        limit, offset = max(1, min(200, int(limit))), max(0, int(offset))
        clauses, args = [], []
        if query:
            escaped = str(query)[:200].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(i.title LIKE ? ESCAPE '\\' OR i.excerpt LIKE ? ESCAPE '\\')")
            args.extend(("%" + escaped + "%", "%" + escaped + "%"))
        if source:
            clauses.append("i.source_id=?")
            args.append(source)
        if category:
            clauses.append("json_extract(i.analysis,'$.category')=?")
            args.append(category)
        if importance:
            clauses.append("json_extract(i.analysis,'$.importance')=?")
            args.append(importance)
        if unread_only:
            clauses.append("i.read=0")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._db() as db:
            total = db.execute("SELECT COUNT(*) FROM items i" + where, args).fetchone()[0]
            rows = db.execute("SELECT i.*,s.name AS source_name FROM items i JOIN sources s ON s.id=i.source_id" + where + " ORDER BY COALESCE(i.published,i.discovered) DESC,i.id LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return {"items": [self._serialize_item(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def get_item(self, item_id):
        with self._db() as db:
            row = db.execute("SELECT i.*,s.name AS source_name FROM items i JOIN sources s ON s.id=i.source_id WHERE i.id=?", (item_id,)).fetchone()
        return self._serialize_item(row) if row else None

    def mark_read(self, item_id, read=True):
        if not isinstance(read, bool):
            raise ValueError("read_must_be_boolean")
        with self._db() as db:
            db.execute("UPDATE items SET read=? WHERE id=?", (int(read), item_id))
        return self.get_item(item_id)
