"""Versioned, inert HTML evidence. No room, model or monitoring writes.

Network reads use the material reader's public-address pinning and the source
reader's cancellation/deadline control. Only the URL already sealed in an
official inbox event can be fetched; redirects are deliberately not followed.
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .source_inbox_contracts import canonical_sha256
from .source_inbox_service import SourceInboxError, SourceInboxService
from .source_poll_control import ensure_source_poll_active, source_poll_timeout_seconds

FORMAT = "official_document_evidence_v1"
PARSER = "official_html_blocks_v2"
MAX_BYTES = 1_500_000
MAX_CHARS = 50_000
WINDOW_MS = 20 * 60_000
MAX_REQUESTS = 6
RECHECK_MS = 5 * 60_000
RETRY_MANUAL_HOLD = (1 << 63) - 1
MAX_RETRY_TIMESTAMP_MS = 253402300799999  # last millisecond of year 9999
NOT_RESERVED_PREFIX = "document_disposition_"


def retry_after_timestamp(value, *, now_ms):
    """A representable not-before time, or an indefinite manual-review sentinel.

    Never turn a huge, valid delay into a shorter retry due to int/date limits.
    Invalid/missing fields use the existing conservative five-minute fallback.
    """
    fallback = now_ms + RECHECK_MS
    raw = value.strip() if isinstance(value, str) else ""
    if re.fullmatch(r"[0-9]+", raw):
        digits = raw.lstrip("0") or "0"
        maximum = str(max(0, (MAX_RETRY_TIMESTAMP_MS - now_ms) // 1000))
        if len(digits) > len(maximum) or (len(digits) == len(maximum) and digits > maximum):
            return RETRY_MANUAL_HOLD
        return max(fallback, now_ms + int(digits) * 1000)
    try:
        parsed = parsedate_to_datetime(raw)
        # HTTP's obsolete asctime representation also denotes UTC, not local time.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        timestamp = delta.days * 86_400_000 + delta.seconds * 1000
        return max(fallback, timestamp) if timestamp <= MAX_RETRY_TIMESTAMP_MS else RETRY_MANUAL_HOLD
    except (ValueError, TypeError):
        return fallback
    except (OverflowError, OSError):
        return RETRY_MANUAL_HOLD


def _safe_retry_timestamp(value):
    if type(value) is not int or value < 0:
        return RETRY_MANUAL_HOLD
    return value if value <= MAX_RETRY_TIMESTAMP_MS else RETRY_MANUAL_HOLD


def ensure_document_evidence_schema(connection, *, applied_at_ms):
    # Called only by Studio's existing controlled initialization/migration path.
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS source_document_jobs (
            id TEXT PRIMARY KEY, item_id TEXT NOT NULL,
            session_id TEXT NOT NULL, source_host TEXT NOT NULL,
            expires_at INTEGER NOT NULL, status TEXT NOT NULL
                CHECK(status IN ('waiting','fetching','complete','partial','failed','cancelled')),
            requested_at INTEGER NOT NULL, completed_at INTEGER NOT NULL DEFAULT 0,
            retry_at INTEGER NOT NULL DEFAULT 0, error_code TEXT NOT NULL DEFAULT '',
            version_id TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(item_id) REFERENCES source_inbox_items(id)
        );
        CREATE INDEX IF NOT EXISTS idx_document_jobs_item
            ON source_document_jobs(item_id,requested_at);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_document_job_pending
            ON source_document_jobs(item_id) WHERE status IN ('waiting','fetching');
        CREATE TABLE IF NOT EXISTS source_document_versions (
            id TEXT PRIMARY KEY, item_id TEXT NOT NULL, identity_sha256 TEXT NOT NULL,
            record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(item_id,identity_sha256),
            FOREIGN KEY(item_id) REFERENCES source_inbox_items(id)
        );
        CREATE TRIGGER IF NOT EXISTS document_version_no_update
            BEFORE UPDATE ON source_document_versions BEGIN
                SELECT RAISE(ABORT,'document evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS document_version_no_delete
            BEFORE DELETE ON source_document_versions BEGIN
                SELECT RAISE(ABORT,'document evidence is immutable'); END;
    """)
    connection.execute("INSERT OR IGNORE INTO schema_migrations(key,applied_at) VALUES('official_document_evidence_v1',?)", (applied_at_ms,))


def fail(message, code="DOCUMENT_REQUEST_INVALID", status=409):
    return SourceInboxError(message, code=code, status=status)


def bound_source(record):
    """Read verified event provenance; never accept a caller-supplied URL."""
    from .market.micron_ir_json import is_micron_detail_url
    from .source_monitoring.contracts import OFFICIAL_SOURCE_CHANNEL
    item = record["item"]
    if record["source_channel"] != OFFICIAL_SOURCE_CHANNEL:
        raise fail("仅支持自动官方来源中的 NVDA 8-K 和 Micron 公告。", "DOCUMENT_SCOPE_UNSUPPORTED")
    sources = item.get("sources", [])
    url = sources[0].get("url", "") if sources else ""
    sec = item.get("extensions", {}).get("sec_v1", {})
    if item.get("item_type") == "sec_filing" and sec.get("symbol") == "US.NVDA" and (
        sec.get("cik") == "0001045810" and sec.get("form") == "8-K"
    ):
        accession = sec.get("accession_number", "")
        filename = sec.get("primary_document", "")
        expected = f"https://www.sec.gov/Archives/edgar/data/1045810/{accession.replace('-', '')}/{filename}"
        if re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]*\.html?", filename
        ) and url == expected:
            return {"url": url, "kind": "sec", "company": "NVIDIA (NVDA)", "scope": "NVDA 8-K 主 HTML；附件未读取"}
    if item.get("item_type") == "company_ir_release" and any(
        entity.get("kind") == "security" and entity.get("id") == "US.MU"
        for entity in item.get("entities", [])
    ) and is_micron_detail_url(url):
        return {"url": url, "kind": "micron", "company": "Micron (MU)", "scope": "Micron 公告 HTML 正文；附件未读取"}
    raise fail("此事件不属于首批正文读取范围。", "DOCUMENT_SCOPE_UNSUPPORTED")


class DocumentHTMLParser(HTMLParser):
    """Keep article text and table rows, excluding head/nav and active content."""
    VOID = {"br", "hr", "img", "meta", "link", "input", "wbr", "source", "area", "base", "embed"}
    IGNORE = {"head", "script", "style", "noscript", "template", "svg", "nav", "header", "footer", "form", "iframe", "object", "ix:hidden"}
    BLOCK = {"p", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "br"}

    def __init__(self, kind):
        super().__init__(convert_charrefs=True)
        self.kind, self.stack, self.parts, self.links = kind, [], [], []
        self.closed_body = False
        self.saw_article = False

    def active(self):
        return bool(self.stack) and self.stack[-1][1] and not self.stack[-1][2]

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        parent_active = self.stack[-1][1] if self.stack else False
        # Micron's Evergreen page wraps its article in this ASP.NET page form.
        # It grants no article scope itself; ordinary/nested forms stay ignored.
        page_form = self.kind == "micron" and tag == "form" and values.get("id") == "fmForm1" and not parent_active
        ignored = (self.stack[-1][2] if self.stack else False) or (tag in self.IGNORE and not page_form) or (
            "hidden" in values or values.get("aria-hidden") == "true"
            or bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", values.get("style", "") or "", re.I))
        )
        marker = (values.get("class", "") or "") + " " + (values.get("id", "") or "")
        evergreen_body = self.kind == "micron" and "evergreen-news-body" in (values.get("class", "") or "").lower().split()
        article = evergreen_body or tag == "article" or any(key in marker.lower() for key in (
            "field--name-body", "article-body", "article_body", "module_body", "press-release-body",
        ))
        active = parent_active or (tag == "body" if self.kind == "sec" else article)
        if article and not ignored:
            self.saw_article = True
        if active and not ignored:
            if tag in self.BLOCK:
                self.parts.append("\n")
            elif tag in {"td", "th"}:
                self.parts.append("\t")
            if tag == "a" and values.get("href"):
                href = values["href"]
                if not href.startswith("#") and len(self.links) < 100:
                    # Inventory only, not an instruction or a fetch target.
                    self.links.append(href[:1000])
        if tag not in self.VOID:
            self.stack.append((tag, active, ignored))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.active() and tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "body":
            self.closed_body = True
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.active():
            self.parts.append(data)


def extract_document(raw, content_type, source):
    from .material_ingest import decode_text, parse_content_type
    mime, charset = parse_content_type(content_type)
    if mime not in {"text/html", "application/xhtml+xml"}:
        raise fail("正文仅接受 HTML 响应。", "DOCUMENT_NOT_HTML")
    if len(raw) > MAX_BYTES:
        raise fail("正文响应超过 1.5 MB 上限。", "DOCUMENT_TOO_LARGE")
    parser = DocumentHTMLParser(source["kind"])
    parser.feed(decode_text(raw, charset))
    parser.close()
    blocks = [re.sub(r"[^\S\t]+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    blocks = [line for line in blocks if line]
    text = "\n".join(blocks)
    warnings = []
    sufficient = len(text) >= 120 and (
        bool(re.search(r"\bItem\s+[1-9]\.\d{2}\b", text, re.I)) if source["kind"] == "sec" else parser.saw_article
    )
    if not sufficient:
        warnings.append("未定位到足够的公告正文；页头、目录或元数据不能当作完整正文。")
    if not parser.closed_body:
        warnings.append("未读取到 HTML 正文结束标记，内容可能不完整。")
    if len(text) > MAX_CHARS:
        warnings.append("正文超过 50,000 字符，仅保留前部文本；表格可能被截断。")
        text = text[:MAX_CHARS]
        blocks = text.splitlines()
    if source["kind"] == "sec":
        warnings.append("主文件已读取；附件尚未读取；当前证据不完整。关键事实可能仅在 Exhibit 99.1 等附件中。")
    elif parser.links:
        warnings.append("正文内链接及附件尚未读取；不能据此确认附件内容。")
    return {
        "raw_bytes_sha256": hashlib.sha256(raw).hexdigest(),
        "body_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "text": text, "blocks": blocks, "warnings": warnings,
        "status": "partial" if warnings else "complete",
        "scope": source["scope"], "attachment_reading": "not_read",
        "linked_targets_unread": list(dict.fromkeys(parser.links)),
        "body_located": sufficient,
        "coverage": "partial" if warnings else "selected_html_only",
    }


class DocumentFetchError(ValueError):
    def __init__(self, code, retry_at=0):
        super().__init__(code)
        self.code, self.retry_at = code, retry_at


def fetch_document(source, *, deadline_monotonic_ms, cancel_event, resolver=None, transport=None):
    from .config import SEC_USER_AGENT
    from .material_ingest import _resolve_public_http_target, _open_pinned_response, FetchedResource
    from .market.official_http import read_official_https_body
    from .market.micron_ir_json import is_micron_detail_url
    controls = {"deadline_monotonic_ms": deadline_monotonic_ms, "cancel_event": cancel_event}
    ensure_source_poll_active(**controls)
    allowed = (source.get("kind") == "micron" and is_micron_detail_url(source.get("url"))) or (
        source.get("kind") == "sec" and re.fullmatch(
            r"https://www\.sec\.gov/Archives/edgar/data/1045810/[0-9]{18}/[A-Za-z0-9][A-Za-z0-9_-]*\.html?", source.get("url", "")
        )
    )
    if not allowed:
        raise DocumentFetchError("DOCUMENT_SCOPE_UNSUPPORTED")
    user_agent = "AI-Collaboration-Studio/0.1 local-read-only-research"
    if source["kind"] == "sec":
        if not SEC_USER_AGENT or "@" not in SEC_USER_AGENT or any(ord(c) < 32 or ord(c) > 126 for c in SEC_USER_AGENT):
            raise DocumentFetchError("SEC_USER_AGENT_REQUIRED")
        user_agent = SEC_USER_AGENT
    target = _resolve_public_http_target(source["url"], resolver or socket.getaddrinfo)
    timeout = source_poll_timeout_seconds(12, **controls)
    # Exactly one outbound request per reserved job, no retry or redirect crawl.
    response = (transport or _open_pinned_response)(target, target.addresses[0], {
        "Host": target.host_header, "User-Agent": user_agent,
        "Accept": "text/html", "Accept-Encoding": "identity", "Connection": "close",
    }, timeout)
    try:
        ensure_source_poll_active(**controls)
        if response.status == 429 or response.status == 503:
            retry_at = retry_after_timestamp(response.headers.get("Retry-After", ""), now_ms=int(time.time() * 1000))
            code = "DOCUMENT_RETRY_AFTER_UNREPRESENTABLE" if retry_at == RETRY_MANUAL_HOLD else "DOCUMENT_RATE_LIMITED"
            raise DocumentFetchError(code, retry_at)
        if response.status != 200:
            raise DocumentFetchError("DOCUMENT_REDIRECT_REJECTED" if 300 <= response.status < 400 else "DOCUMENT_HTTP_FAILED")
        length = response.headers.get("Content-Length", "")
        if length and (not str(length).isdecimal() or int(length) > MAX_BYTES):
            raise DocumentFetchError("DOCUMENT_TOO_LARGE")
        if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise DocumentFetchError("DOCUMENT_ENCODING_UNSUPPORTED")
        raw = read_official_https_body(getattr(response, "_response", response), MAX_BYTES,
                                      deadline_seconds=12, **controls)
        return FetchedResource(raw, response.headers.get("Content-Type", ""), source["url"])
    finally:
        response.close()


class DocumentEvidenceService:
    def __init__(self, store, *, fetcher=fetch_document, clock=None):
        self.store, self.fetcher = store, fetcher
        self.clock = clock or (lambda: int(time.time() * 1000))

    def item(self, item_id):
        record = SourceInboxService(self.store).get_item(item_id)
        if record is None:
            raise fail("来源事件不存在。", "SOURCE_INBOX_NOT_FOUND", 404)
        return record

    def view(self, item_id):
        record = self.item(item_id)
        try:
            source = bound_source(record)
        except SourceInboxError:
            return {"format": FORMAT, "eligible": False, "status": "unsupported", "versions": [], "job": None}
        with closing(self.store._connect()) as db:
            rows = db.execute("SELECT * FROM source_document_versions WHERE item_id=? ORDER BY created_at,id", (item_id,)).fetchall()
            job = db.execute("SELECT * FROM source_document_jobs WHERE item_id=? ORDER BY requested_at DESC,rowid DESC LIMIT 1", (item_id,)).fetchone()
        versions = []
        for row in rows:
            value = json.loads(row["record_json"])
            if canonical_sha256(value) != row["record_sha256"] or value["item_fingerprint"] != record["server_fingerprint"] or value["request_url"] != source["url"]:
                raise fail("正文证据完整性校验失败。", "DOCUMENT_INTEGRITY_FAILED")
            versions.append(value)
        return {"format": FORMAT, "eligible": True, "source": source, "versions": versions,
                "status": job["status"] if job else "not_fetched", "job": dict(job) if job else None,
                "generation_method": "deterministic_original_excerpt", "direction": "unknown"}

    def request(self, item_id, *, session_id, confirmation, refresh=False, expires_at=0):
        if confirmation is not True or type(refresh) is not bool:
            raise fail("请明确确认读取这一条官方 HTML。")
        record = self.item(item_id)
        source = bound_source(record)
        host = urlsplit(source["url"]).hostname
        now = self.clock()
        with self.store._lock, closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM source_document_jobs WHERE item_id=? ORDER BY requested_at DESC,rowid DESC LIMIT 1", (item_id,)).fetchone()
            if previous:
                if previous["status"] in {"waiting", "fetching"} or not refresh:
                    return dict(previous)
                if now < max(previous["retry_at"], previous["requested_at"] + RECHECK_MS):
                    raise fail("仍在读取间隔或来源限流等待期，请稍后重试。", "DOCUMENT_RETRY_LATER")
            # Persistent cross-session cap also covers manual confirmation/restarts.
            count = db.execute("SELECT COUNT(*) FROM source_document_jobs WHERE requested_at>? AND id NOT GLOB ?", (now - WINDOW_MS, NOT_RESERVED_PREFIX + "*")).fetchone()[0]
            if count >= MAX_REQUESTS:
                raise fail("正文读取在 20 分钟内最多 6 次，请稍后继续。", "DOCUMENT_BUDGET_EXHAUSTED")
            embargo = db.execute("SELECT COALESCE(MAX(retry_at),0) FROM source_document_jobs WHERE source_host=?", (host,)).fetchone()[0]
            if now < embargo:
                raise fail("该来源仍在 Retry-After 等待期，不能换事件绕过限流。", "DOCUMENT_RETRY_LATER")
            job_id = "document_job_" + uuid.uuid4().hex
            db.execute("INSERT INTO source_document_jobs(id,item_id,session_id,source_host,expires_at,status,requested_at) VALUES(?,?,?,?,?,'waiting',?)", (job_id, item_id, session_id, host, expires_at or now + 120_000, now))
            return dict(db.execute("SELECT * FROM source_document_jobs WHERE id=?", (job_id,)).fetchone())

    def record_not_reserved(self, item_id, *, session_id, expires_at, reason):
        """Persist a terminal, event-local disposition; this grants no request.

        Distinct IDs mark records that never reserved budget. All actual job IDs
        still consume their reservation even if cancelled before network I/O.
        """
        codes = {"DOCUMENT_RETRY_LATER": "DOCUMENT_NOT_RESERVED_COOLDOWN",
                 "DOCUMENT_BUDGET_EXHAUSTED": "DOCUMENT_NOT_RESERVED_BUDGET"}
        if reason not in codes:
            raise ValueError("unsupported document disposition")
        source = bound_source(self.item(item_id))
        host = urlsplit(source["url"]).hostname
        with self.store._lock, closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM source_document_jobs WHERE item_id=? ORDER BY requested_at DESC,rowid DESC LIMIT 1", (item_id,)).fetchone()
            if previous:
                return dict(previous)
            now = self.clock()
            retry_at = db.execute("SELECT COALESCE(MAX(retry_at),0) FROM source_document_jobs WHERE source_host=?", (host,)).fetchone()[0]
            job_id = NOT_RESERVED_PREFIX + uuid.uuid4().hex
            db.execute("""INSERT INTO source_document_jobs
                (id,item_id,session_id,source_host,expires_at,status,requested_at,completed_at,retry_at,error_code)
                VALUES(?,?,?,?,?,'cancelled',?,?,?,?)""",
                (job_id, item_id, session_id, host, expires_at, now, now, retry_at, codes[reason]))
            return dict(db.execute("SELECT * FROM source_document_jobs WHERE id=?", (job_id,)).fetchone())

    def _cancel_inadmissible(self, db, job, *, cancel_event, deadline_monotonic_ms):
        """Recheck at execution time; cancellation never refunds a reservation."""
        now = self.clock()
        retry_at = db.execute("SELECT COALESCE(MAX(retry_at),0) FROM source_document_jobs WHERE source_host=?", (job["source_host"],)).fetchone()[0]
        code = ""
        if cancel_event.is_set():
            code = "DOCUMENT_CANCELLED"
        elif now >= job["expires_at"] or int(time.monotonic() * 1000) >= deadline_monotonic_ms:
            code = "DOCUMENT_AUTHORIZATION_EXPIRED"
        elif retry_at == RETRY_MANUAL_HOLD or now < retry_at:
            code = "DOCUMENT_PUBLISHER_COOLDOWN"
        if code:
            db.execute("UPDATE source_document_jobs SET status='cancelled',error_code=?,retry_at=?,completed_at=? WHERE id=?",
                       (code, retry_at, now, job["id"]))
        return bool(code)

    def recover(self):
        # Host owner is held by caller; interrupted work never silently refetches.
        with self.store._lock, closing(self.store._connect()) as db, db:
            db.execute("UPDATE source_document_jobs SET status='cancelled', error_code='DOCUMENT_INTERRUPTED', completed_at=? WHERE status IN ('waiting','fetching')", (self.clock(),))

    def run(self, job_id, *, cancel_event, deadline_monotonic_ms):
        with self.store._lock, closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM source_document_jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["status"] != "waiting":
                return False
            if self._cancel_inadmissible(db, job, cancel_event=cancel_event, deadline_monotonic_ms=deadline_monotonic_ms):
                return False
            db.execute("UPDATE source_document_jobs SET status='fetching' WHERE id=?", (job_id,))
        # All network/parse work is outside the store lock and SQLite transaction.
        try:
            deadline_monotonic_ms = min(deadline_monotonic_ms, int(time.monotonic() * 1000) + max(1, job["expires_at"] - self.clock()))
            record = self.item(job["item_id"])
            source = bound_source(record)
            # Event verification may take time. Recheck the latest cooldown and
            # authorization after it, immediately before the one outbound read.
            with self.store._lock, closing(self.store._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if self._cancel_inadmissible(db, job, cancel_event=cancel_event, deadline_monotonic_ms=deadline_monotonic_ms):
                    return False
            ensure_source_poll_active(cancel_event=cancel_event, deadline_monotonic_ms=deadline_monotonic_ms)
            resource = self.fetcher(source, cancel_event=cancel_event, deadline_monotonic_ms=deadline_monotonic_ms)
            ensure_source_poll_active(cancel_event=cancel_event, deadline_monotonic_ms=deadline_monotonic_ms)
            if resource.final_url != source["url"]:
                raise DocumentFetchError("DOCUMENT_REDIRECT_REJECTED")
            parsed = extract_document(resource.raw, resource.content_type, source)
            identity = canonical_sha256({"item": record["server_fingerprint"], "url": source["url"],
                "raw": parsed["raw_bytes_sha256"], "text": parsed["body_text_sha256"], "parser": PARSER})
            version_id = "document_" + identity
            value = {"format": FORMAT, "id": version_id, "item_id": job["item_id"],
                "item_fingerprint": record["server_fingerprint"], "parser_version": PARSER,
                "request_url": source["url"], "final_url": resource.final_url,
                "published_at": record["item"].get("published_at") or None,
                "modified_at": record["item"].get("extensions", {}).get("company_ir_v2", {}).get("metadata_date_modified") or None,
                "fetched_at": self.clock(), "company": source["company"], **parsed}
            value["paragraphs"] = [{"id": f"{version_id}:p{i + 1:04d}", "text": block} for i, block in enumerate(value.pop("blocks"))]
            with self.store._lock, closing(self.store._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT OR IGNORE INTO source_document_versions VALUES(?,?,?,?,?,?)", (
                    version_id, job["item_id"], identity, json.dumps(value, ensure_ascii=False, sort_keys=True), canonical_sha256(value), self.clock()))
                db.execute("UPDATE source_document_jobs SET status=?,version_id=?,completed_at=? WHERE id=?", (parsed["status"], version_id, self.clock(), job_id))
        except Exception as exc:
            code = getattr(exc, "code", "DOCUMENT_FETCH_OR_PARSE_FAILED")
            if isinstance(exc, TimeoutError):
                code = "DOCUMENT_TIMEOUT"
            if cancel_event.is_set():
                code = "DOCUMENT_CANCELLED"
            with self.store._lock, closing(self.store._connect()) as db, db:
                db.execute("UPDATE source_document_jobs SET status='failed',error_code=?,retry_at=?,completed_at=? WHERE id=?", (code, _safe_retry_timestamp(getattr(exc, "retry_at", 0)), self.clock(), job_id))
        return True


class DocumentEvidenceController:
    """One host-owned enrichment worker, independent of metadata scheduling.

Authorization expires and is never resumed on restart. Construction/start are
inert with respect to network. Pending job recovery requires the host owner.
"""
    def __init__(self, store, *, service=None, network_allowed=False):
        self.store = store
        self.service = service or DocumentEvidenceService(store)
        self.network_allowed = network_allowed
        self.session_id = uuid.uuid4().hex
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.thread = None
        self.auto_until = 0
        self.auto_cursor = 0
        self.auto_remaining = 0
        self.error = ""
        self.has_requests = False

    def start(self):
        self.service.recover()
        self.thread = threading.Thread(target=self._work, name="official-document-evidence", daemon=False)
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return {"network_allowed": self.network_allowed, "enabled": self.auto_until > self.service.clock() and self.auto_remaining > 0,
                "expires_at": self.auto_until, "remaining": self.auto_remaining, "error": self.error,
                "scope": "NVDA 8-K 主 HTML + Micron 公告 HTML", "max_requests": MAX_REQUESTS,
                "window_minutes": 20, "historical_backfill": False, "provider_calls": 0}

    def authorize(self, *, confirmation):
        with self.lock:
            if confirmation is not True or not self.network_allowed or self.stop_event.is_set():
                raise fail("请在联网模式中明确确认正文读取范围。", "DOCUMENT_NETWORK_DISABLED")
            if self.auto_until > self.service.clock():
                return self.snapshot()
            with closing(self.store._connect()) as db:
                self.auto_cursor = db.execute("SELECT COALESCE(MAX(rowid),0) FROM source_inbox_items").fetchone()[0]
            self.auto_until = self.service.clock() + WINDOW_MS
            self.auto_remaining = MAX_REQUESTS
            return self.snapshot()

    def request(self, item_id, *, confirmation, refresh=False):
        with self.lock:
            if not self.network_allowed or self.stop_event.is_set():
                raise fail("正文联网读取尚未启用；请使用联网采集入口。", "DOCUMENT_NETWORK_DISABLED")
            job = self.service.request(item_id, session_id=self.session_id, confirmation=confirmation, refresh=refresh)
            self.has_requests = self.has_requests or job["status"] == "waiting"
            return job

    def cycle(self):
        with self.lock:
            if not self.has_requests and not (self.auto_until > self.service.clock()):
                return
            if self.auto_until > self.service.clock():
                with closing(self.store._connect()) as db:
                    rows = db.execute("SELECT rowid,id FROM source_inbox_items WHERE rowid>? ORDER BY rowid LIMIT 20", (self.auto_cursor,)).fetchall()
                for row in rows:
                    if self.stop_event.is_set() or self.service.clock() >= self.auto_until:
                        break
                    try:
                        if self.auto_remaining <= 0:
                            self.service.record_not_reserved(row["id"], session_id=self.session_id,
                                expires_at=self.auto_until, reason="DOCUMENT_BUDGET_EXHAUSTED")
                            self.auto_cursor = row["rowid"]
                            continue
                        self.service.request(row["id"], session_id=self.session_id, confirmation=True, expires_at=self.auto_until)
                        self.has_requests = True
                        self.auto_remaining -= 1
                    except SourceInboxError as exc:
                        if exc.code in {"DOCUMENT_RETRY_LATER", "DOCUMENT_BUDGET_EXHAUSTED"}:
                            self.service.record_not_reserved(row["id"], session_id=self.session_id,
                                expires_at=self.auto_until, reason=exc.code)
                            self.error = exc.code
                        elif exc.code != "DOCUMENT_SCOPE_UNSUPPORTED":
                            raise
                    # Advance only after a durable job/disposition, or a source
                    # that is explicitly outside this feature's fixed scope.
                    self.auto_cursor = row["rowid"]
            with closing(self.store._connect()) as db:
                jobs = db.execute("SELECT id FROM source_document_jobs WHERE session_id=? AND status='waiting' ORDER BY requested_at,rowid LIMIT ?", (self.session_id, MAX_REQUESTS)).fetchall()
                self.has_requests = bool(jobs)
        for job in jobs:
            if self.stop_event.is_set():
                break
            if self.service.run(job["id"], cancel_event=self.stop_event, deadline_monotonic_ms=int(time.monotonic() * 1000) + 12_000):
                break  # at most one outbound operation per cycle; cancelled heads do not block others

    def _work(self):
        while not self.stop_event.wait(1):
            try:
                self.cycle()
            except Exception:
                self.error = "DOCUMENT_WORKER_FAILED"
                self.network_allowed = False
                self.auto_until = 0
                # Do not repeatedly write/retry when the database is unhealthy.
                return

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(15)
            if self.thread.is_alive():
                return False
        self.service.recover()
        return True
