"""Reviewed source registry and discovery using the existing market adapters.

Import and registry inspection never request data. New sources are opt-in.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from .news_feed_transport import SOURCES, FeedResponse, FeedValidationError, fetch_feed, feed_proxy_configuration, _SameHostRedirect
from .sec_request_limit import sec_request_slot


def source_registry():
    from .ir_source_catalog import IR_FEEDS
    result = []
    for item in SOURCES:
        result.append({**item, 'kind': 'rss', 'source_kind': 'rss', 'enabled_default': True,
                       'symbols': ['US.STX'] if item['id'] == 'seagate' else [],
                       'hosts': [urlsplit(item['url']).hostname], 'poll_interval': 900,
                       'coverage': '官方机构订阅' if item['id'] != 'seagate' else '希捷官方公司公告',
                       'time_semantics': '来源明确时区的发布时间；未知不补成本机时间',
                       'access_policy': 'HTTPS 固定来源；遵守 403/429、Retry-After 和已有等待期限',
                       'history_capability': 'feed_window_only', 'continuous_coverage': False})
    for symbol, config in IR_FEEDS.items():
        if symbol != 'US.STX':
            result.append({'id': 'ir_' + symbol[3:].lower(), 'name': config['publisher'],
                           'url': config['url'], 'kind': 'rss', 'source_kind': 'company_ir',
                           'symbols': [symbol], 'hosts': sorted(config['hosts']),
                           'enabled_default': False, 'poll_interval': 900,
                           'coverage': symbol + ' 官方 IR 公告与已链接材料',
                           'time_semantics': 'RSS 原始发布时间与来源修订时间分别保存',
                           'access_policy': '固定 HTTPS；继承来源等待期限；附件单独读取',
                           'history_capability': 'feed_window_only', 'continuous_coverage': False})
        result.append({'id': 'sec_' + symbol[3:].lower(), 'name': symbol[3:] + ' SEC 申报',
                       'url': 'https://data.sec.gov/submissions/', 'kind': 'sec', 'source_kind': 'sec',
                       'symbols': [symbol], 'hosts': ['www.sec.gov', 'data.sec.gov'],
                       'enabled_default': False, 'poll_interval': 900,
                       'coverage': symbol + ' 最近 10-K/10-Q/8-K/20-F/40-F/6-K 申报',
                       'time_semantics': '带时区 acceptanceDateTime；仅日期保留 filing_date，不伪造时刻',
                       'access_policy': '需声明 SEC User-Agent；进程共享至少 0.11 秒间隔；遵守来源等待期限',
                       'history_capability': 'submissions_files_paged', 'continuous_coverage': False})
    return result


def source_definition(source_id):
    return next((item for item in source_registry() if item['id'] == source_id), None)


def canonical_url(value):
    parsed = urlsplit(str(value or ''))
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in {'fbclid', 'gclid'}]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ''))


def allowed_url(value, source_id):
    definition = source_definition(source_id)
    if not definition or not isinstance(value, str) or any(ord(c) <= 32 for c in value) or '\\' in value:
        return False
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname not in definition['hosts']
                or parsed.port not in (None, 443) or parsed.username is not None or parsed.password is not None):
            return False
        if definition['kind'] == 'sec':
            return ((parsed.hostname == 'www.sec.gov' and (parsed.path == '/files/company_tickers.json'
                     or parsed.path.startswith('/Archives/edgar/data/')))
                    or (parsed.hostname == 'data.sec.gov' and re.fullmatch(r'/submissions/CIK\d{10}(?:-submissions-\d{3})?\.json', parsed.path) is not None))
        return True
    except ValueError:
        return False


def _declared_agent(value):
    return isinstance(value, str) and 10 <= len(value) <= 300 and '@' in value and not any(ord(c) < 32 for c in value)


class _OfficialRedirect(_SameHostRedirect):
    def __init__(self, origin, source_id):
        super().__init__(origin)
        self.source_id = source_id

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl, self.source_id):
            raise FeedValidationError('redirect_blocked')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_official(url, headers, timeout, max_bytes, *, source_id, sec_user_agent=''):
    """Bounded source-aware GET; redirect checks run before a redirected request."""
    if not allowed_url(url, source_id):
        raise ValueError('article_url_not_allowed')
    definition = source_definition(source_id)
    if definition['kind'] == 'sec' and not _declared_agent(sec_user_agent):
        raise ValueError('sec_user_agent_required')
    timeout = min(20, max(1, timeout))
    max_bytes = min(2 * 1024 * 1024, max(1, max_bytes))
    proxies, _ = feed_proxy_configuration(urllib.request.getproxies())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies), _OfficialRedirect(url, source_id))
    safe_headers = {key: value for key, value in headers.items() if key.lower() in {'if-none-match', 'if-modified-since'}}
    request = urllib.request.Request(url, headers={
        'User-Agent': sec_user_agent if definition['kind'] == 'sec' else 'AI-Collaboration-Studio-Local-News/1.0',
        'Accept': 'text/html,application/xhtml+xml,application/json,application/rss+xml,text/xml,text/plain',
        'Accept-Encoding': 'identity', **safe_headers})

    def download():
        try:
            with opener.open(request, timeout=timeout) as response:
                if not allowed_url(response.geturl(), source_id):
                    raise FeedValidationError('redirect_blocked')
                length = response.headers.get('Content-Length')
                if length and int(length) > max_bytes:
                    raise FeedValidationError('feed_too_large')
                chunks, size, deadline = [], 0, time.monotonic() + timeout
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('article_read_deadline')
                    chunk = response.read1(min(65536, max_bytes + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise FeedValidationError('feed_too_large')
                    chunks.append(chunk)
                return FeedResponse(response.status, b''.join(chunks), dict(response.headers.items()))
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                result = FeedResponse(304, headers=dict(exc.headers.items()))
                exc.close()
                return result
            exc.close()
            raise
    if definition['kind'] == 'sec':
        with sec_request_slot():
            return download()
    return download()


def discover(source, *, transport=None, headers=None, timeout=12, max_bytes=2 * 1024 * 1024, now=None, sec_user_agent='', history_state=None):
    definition = source_definition(source['id'])
    if definition is None:
        raise ValueError('source_not_allowed')
    headers = headers or {}
    now = time.time() if now is None else now

    def get(url, request_headers):
        if not allowed_url(url, source['id']):
            raise ValueError('source_not_allowed')
        if transport is not None and transport is not fetch_feed:
            response = transport(url, request_headers, timeout, max_bytes)
        elif definition['id'] in {item['id'] for item in SOURCES}:
            response = fetch_feed(url, request_headers, timeout, max_bytes)
        else:
            response = fetch_official(url, request_headers, timeout, max_bytes, source_id=source['id'], sec_user_agent=sec_user_agent)
        if len(response.body or b'') > max_bytes:
            raise FeedValidationError('feed_too_large')
        if response.status not in (200, 304):
            error = urllib.error.HTTPError(url, response.status, 'official_source_error', response.headers or {}, None)
            error.close()
            raise error
        return response

    if definition['kind'] != 'sec':
        return {'response': get(definition['url'], headers), 'entries': None}
    if not _declared_agent(sec_user_agent):
        raise ValueError('sec_user_agent_required')
    from .market.sec_edgar import SecEdgarAdapter, SEC_TICKERS_URL, SEC_SUBMISSIONS_URL, SEC_DEFAULT_FORMS
    ticker_response = get(SEC_TICKERS_URL, {})
    ticker_map = json.loads(ticker_response.body)
    symbol = definition['symbols'][0]
    company = next((row for row in ticker_map.values() if isinstance(row, dict) and row.get('ticker') == symbol[3:]), None)
    if not company:
        raise ValueError('sec_cik_not_found')
    cik = f"{int(company['cik_str']):010d}"
    response = get(SEC_SUBMISSIONS_URL.format(cik=cik), headers)
    from .news_sec_history import advance_history
    payload = json.loads(response.body) if response.status == 200 else None
    filings = SecEdgarAdapter._normalize_recent_filings(payload or {}, cik=cik, forms=SEC_DEFAULT_FORMS,
        limit=500, today=datetime.fromtimestamp(now, timezone.utc).date())
    historical, next_history = advance_history(cik,payload,history_state,get,now)
    filings.extend(historical)
    entries = []
    for filing in filings:
        accepted = filing.get('accepted_at')
        published = None
        try:
            stamp = datetime.fromisoformat(str(accepted).replace('Z', '+00:00'))
            if stamp.tzinfo is not None:
                published = stamp.timestamp()
        except (ValueError, TypeError):
            pass
        identity = filing['accession_number']
        entries.append({'id': hashlib.sha256((source['id'] + '\n' + identity).encode()).hexdigest(),
                        'identity': identity, 'title': f"{symbol[3:]} {filing['form']} {filing['description']}".strip(),
                        'url': filing['official_url'], 'excerpt': filing['description'], 'published': published,
                        'source_updated_at': None, 'symbols': [symbol], 'filing_date': filing['filing_date'],
                        'reference_period': filing['report_date'], 'source_time_basis': 'acceptanceDateTime' if published is not None else 'unknown',
                        'discovery_kind':filing.get('discovery_kind','current_feed')})
    # A 304 primary document can still produce a committed historical batch.
    # Preserve original status explicitly; the pipeline processes supplied entries.
    primary_status=response.status
    if historical and response.status == 304:
        response=FeedResponse(200,b'',response.headers)
    return {'response': response, 'entries': entries, 'history_state':next_history,'primary_http_status':primary_status}
