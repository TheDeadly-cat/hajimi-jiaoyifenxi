"""Bounded official-article reading. HTML is inert evidence, never instructions."""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
from io import BytesIO
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .news_sources import allowed_url, canonical_url, fetch_official, source_definition
from .news_feed_transport import FeedValidationError

EXTRACTOR_VERSION = 'official-html-segments-v1'
MAX_ARTICLE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 120000
MAX_ATTACHMENTS_READ = 3
_BLOCKS = {'p', 'li', 'h1', 'h2', 'h3', 'h4', 'blockquote', 'tr'}
_HIDDEN = {'script', 'style', 'noscript', 'nav', 'header', 'footer', 'form', 'button', 'svg'}
_CONTENT = re.compile(r'(?:article[-_]?body|news[-_]?release|release[-_]?body|press[-_]?release|main[-_]?content|field[-_]body|node[-_]content|story[-_]body)', re.I)


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


def _source_date(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


class _ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.segments = []
        self.buffer = []
        self.buffer_scoped = False
        self.buffer_kind = 'paragraph'
        self.links = []
        self.active_link = None
        self.has_content = False
        self.closed_content = False
        self.published = None
        self.updated = None
        self.title = []

    def _hidden(self):
        return any(hidden for _, _, hidden in self.stack)

    def _scoped(self):
        return any(content for _, content, _ in self.stack)

    def flush(self):
        text = re.sub(r'\s+', ' ', ''.join(self.buffer)).strip()
        if text:
            self.segments.append({'text': text, 'kind': self.buffer_kind, 'scoped': self.buffer_scoped})
        self.buffer = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            name = (attrs.get('property') or attrs.get('name') or '').lower()
            if name in {'article:published_time', 'datepublished', 'date'}:
                self.published = _source_date(attrs.get('content')) or self.published
            if name in {'article:modified_time', 'datemodified'}:
                self.updated = _source_date(attrs.get('content')) or self.updated
        is_content = tag in {'article', 'main'} or (tag in {'div', 'section'} and _CONTENT.search(attrs.get('id', '') + ' ' + attrs.get('class', '')) is not None)
        hidden = tag in _HIDDEN or 'hidden' in attrs or attrs.get('aria-hidden') == 'true'
        if tag in _BLOCKS or is_content:
            self.flush()
        if tag not in {'meta', 'link', 'img', 'br', 'hr', 'input', 'source', 'wbr'}:
            self.stack.append((tag, is_content, hidden))
        if is_content and not self._hidden():
            self.has_content = True
        if tag == 'br':
            self.buffer.append(' ')
        if tag in {'td', 'th'} and self.buffer:
            self.buffer.append(' | ')
        if tag in _BLOCKS:
            self.buffer_kind = 'table_row' if tag == 'tr' else 'paragraph'
            self.buffer_scoped = self._scoped()
        if tag == 'a' and not self._hidden():
            self.active_link = {'href': attrs.get('href', ''), 'label': '', 'scoped': self._scoped()}
            self.links.append(self.active_link)

    def handle_endtag(self, tag):
        if tag in _BLOCKS or tag in {'article', 'main', 'div', 'section'}:
            self.flush()
        if tag == 'a':
            self.active_link = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                if any(content for _, content, _ in self.stack[index:]):
                    self.closed_content = True
                self.stack = self.stack[:index]
                break
        if tag == 'tr':
            self.buffer_kind = 'paragraph'

    def handle_data(self, data):
        if any(tag == 'title' for tag, _, _ in self.stack):
            self.title.append(data)
            return
        if self._hidden() or any(tag == 'head' for tag, _, _ in self.stack):
            return
        if not self.buffer:
            self.buffer_scoped = self._scoped()
        self.buffer.append(data)
        if self.active_link is not None:
            self.active_link['label'] += data


def extract_article(raw, url, source_id, *, content_type='text/html', now=None):
    """Pure extraction; retain hashes and bounded text, not executable markup."""
    if not isinstance(raw, bytes) or len(raw) > MAX_ARTICLE_BYTES:
        raise ValueError('article_bytes_invalid')
    now = time.time() if now is None else now
    definition = source_definition(source_id)
    if not allowed_url(url, source_id):
        raise ValueError('article_url_not_allowed')
    evidence = {'content_status': 'fetch_failed', 'status': 'fetch_failed', 'url': url,
                'canonical_url': canonical_url(url), 'body_text': '', 'segments': [], 'paragraphs': [], 'tables': [],
                'attachments': [], 'extractor_version': EXTRACTOR_VERSION, 'extraction_version': EXTRACTOR_VERSION,
                'fetched_at': _iso(now), 'published_at': None, 'source_updated_at': None,
                'raw_content_hash': hashlib.sha256(raw).hexdigest(), 'content_hash': None,
                'raw_bytes': len(raw), 'truncated': False, 'symbols': definition['symbols'],
                'full_text_read': False, 'attachment_scope': 'not_read', 'error': None}
    mime = str(content_type or '').split(';', 1)[0].strip().lower()
    if raw.startswith(b'%PDF') or mime == 'application/pdf':
        # Same locked pypdf dependency and 80-page boundary as material_ingest;
        # importing material_ingest would initialize its unrelated global store.
        try:
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(raw), strict=False)
            if reader.is_encrypted or len(reader.pages) > 80:
                raise ValueError('pdf_not_supported')
            page_count, chars, blank_pages = len(reader.pages), 0, []
            for index, page in enumerate(reader.pages):
                if chars >= MAX_TEXT_CHARS:
                    evidence['truncated'] = True
                    break
                page_text = (page.extract_text() or '').strip()
                if not page_text:
                    blank_pages.append(index + 1)
                    continue
                bounded = page_text[:MAX_TEXT_CHARS - chars]
                evidence['truncated'] = evidence['truncated'] or len(bounded) < len(page_text)
                segment = {'id': 'pdf-page-' + str(index + 1), 'text': bounded, 'kind': 'pdf_page'}
                evidence['segments'].append(segment)
                evidence['paragraphs'].append(segment)
                chars += len(bounded)
            evidence['body_text'] = '\n'.join(row['text'] for row in evidence['segments'])
            evidence['page_count'] = page_count
            evidence['pages_without_text'] = blank_pages
            evidence['reading_scope'] = 'pdf_text_only_no_ocr_no_layout_validation'
            evidence['extractor_version'] = evidence['extraction_version'] = EXTRACTOR_VERSION + '+pypdf-' + __import__('pypdf').__version__
            if evidence['body_text']:
                evidence.update(content_status='partial_text', status='partial_text',
                                content_hash=hashlib.sha256(evidence['body_text'].encode('utf-8')).hexdigest())
            else:
                evidence['error'] = 'pdf_no_extractable_text'
        except ImportError:
            evidence['error'] = 'pdf_dependency_unavailable'
        except Exception:
            evidence['error'] = 'pdf_extraction_failed'
        return evidence
    if mime not in {'text/html', 'application/xhtml+xml', 'text/plain'}:
        evidence['error'] = 'unsupported_content_type'
        return evidence
    charset = re.search(r'charset\s*=\s*["\']?([a-zA-Z0-9_-]+)', content_type or '', re.I)
    try:
        text = raw.decode(charset.group(1) if charset else 'utf-8', errors='strict')
        decode_incomplete = False
    except (UnicodeError, LookupError):
        text = raw.decode('utf-8', errors='replace')
        decode_incomplete = True
    parser = _ArticleParser()
    if mime == 'text/plain':
        rows = [{'text': line.strip(), 'kind': 'paragraph'} for line in text.splitlines() if line.strip()]
        complete = True
    else:
        parser.feed(text)
        parser.close()
        parser.flush()
        rows = [segment for segment in parser.segments if segment['scoped']] if parser.has_content else parser.segments
        complete = parser.has_content and parser.closed_content
    joined = ' '.join(row['text'] for row in rows)
    denial = re.search(r'(?:access denied|verify (?:you are|that you are) human|captcha|just a moment|request (?:rejected|blocked)|enable javascript and cookies)', ' '.join(parser.title) + ' ' + joined[:1500], re.I)
    if denial and (len(joined) < 3000 or not parser.has_content):
        evidence.update(content_status='access_restricted', status='access_restricted', error='access_challenge')
        return evidence
    if len(joined.strip()) < 40:
        evidence['error'] = 'empty_or_insufficient_body'
        return evidence
    chars = 0
    for index, row in enumerate(rows):
        remaining = MAX_TEXT_CHARS - chars
        if remaining <= 0:
            evidence['truncated'] = True
            break
        piece = row['text'][:remaining]
        segment = {'id': ('table-row-' if row['kind'] == 'table_row' else 'paragraph-') + str(index + 1),
                   'text': piece, 'kind': row['kind']}
        evidence['segments'].append(segment)
        evidence['tables' if row['kind'] == 'table_row' else 'paragraphs'].append(segment)
        chars += len(piece)
        if len(piece) != len(row['text']):
            evidence['truncated'] = True
    evidence['body_text'] = '\n'.join(row['text'] for row in evidence['segments'])
    evidence['content_hash'] = hashlib.sha256(evidence['body_text'].encode('utf-8')).hexdigest()
    evidence['published_at'], evidence['source_updated_at'] = parser.published, parser.updated
    complete = complete and not evidence['truncated'] and not decode_incomplete
    evidence.update(content_status='full_text' if complete else 'partial_text', status='full_text' if complete else 'partial_text', full_text_read=complete)
    evidence['reading_scope'] = 'recognized_article_container' if parser.has_content else 'plain_text' if mime == 'text/plain' else 'unverified_page_body'
    seen = set()
    for link in parser.links:
        candidate = urljoin(url, link['href'])
        label = re.sub(r'\s+', ' ', link['label']).strip()[:300]
        path = urlsplit(candidate).path.lower()
        if not link['scoped'] or candidate in seen or candidate == url:
            continue
        if (path.endswith(('.pdf', '.txt', '.htm', '.html')) or '/static-files/' in path) and (path.endswith(('.pdf', '.txt')) or '/static-files/' in path or re.search(r'exhibit|attachment|financial|table|附件|财务', label, re.I)):
            seen.add(candidate)
            permitted = allowed_url(candidate, source_id)
            evidence['attachments'].append({'url': candidate, 'label': label, 'status': 'not_read' if permitted else 'access_restricted', 'read': False,
                                            'reason': None if permitted else 'attachment_host_not_allowed'})
    return evidence


def read_article(item, *, transport=None, now=None, include_attachments=False, sec_user_agent=''):
    source_id, url = item.get('source_id'), item.get('url')
    if not allowed_url(url, source_id):
        raise ValueError('article_url_not_allowed')
    now = time.time() if now is None else now

    def get(target):
        if not allowed_url(target, source_id):
            raise ValueError('article_url_not_allowed')
        response = transport(target, {}, 12, MAX_ARTICLE_BYTES) if transport else fetch_official(target, {}, 12, MAX_ARTICLE_BYTES, source_id=source_id, sec_user_agent=sec_user_agent)
        if response.status not in (200, 410):
            error = urllib.error.HTTPError(target, response.status, 'article_fetch_failed', response.headers or {}, None)
            error.close()
            raise error
        return response
    try:
        response = get(url)
    except urllib.error.HTTPError as exc:
        if exc.code != 410:
            raise
        exc.close()
        return {'content_status': 'fetch_failed', 'status': 'fetch_failed', 'url': url, 'canonical_url': canonical_url(url),
                'body_text': '', 'segments': [], 'attachments': [], 'extractor_version': EXTRACTOR_VERSION,
                'fetched_at': _iso(now), 'full_text_read': False, 'error': 'source_http_410', 'http_status': 410}
    if response.status == 410:
        return {'content_status': 'fetch_failed', 'status': 'fetch_failed', 'url': url, 'canonical_url': canonical_url(url),
                'body_text': '', 'segments': [], 'attachments': [], 'extractor_version': EXTRACTOR_VERSION,
                'fetched_at': _iso(now), 'full_text_read': False, 'error': 'source_http_410', 'http_status': 410}
    headers = {key.lower(): value for key, value in (response.headers or {}).items()}
    evidence = extract_article(response.body, url, source_id, content_type=headers.get('content-type', 'text/html'), now=now)
    evidence['http_status'] = response.status
    attempts = 0
    for attachment in evidence['attachments']:
        if not include_attachments or attachment['status'] != 'not_read':
            continue
        if attempts >= MAX_ATTACHMENTS_READ:
            attachment['reason'] = 'attachment_read_limit'
            continue
        attempts += 1
        try:
            attached = get(attachment['url'])
            attached_headers = {key.lower(): value for key, value in (attached.headers or {}).items()}
            result = extract_article(attached.body, attachment['url'], source_id,
                                     content_type=attached_headers.get('content-type', 'text/html'), now=now)
            attachment.update(status=result['content_status'], read=result['content_status'] in {'full_text', 'partial_text'}, evidence=result)
        except urllib.error.HTTPError:
            # A source restriction applies to all its tasks, including attachments.
            raise
        except (ValueError, TimeoutError, OSError, FeedValidationError):
            attachment.update(status='fetch_failed', reason='attachment_read_failed')
    evidence['attachment_scope'] = 'some_read' if any(a['read'] for a in evidence['attachments']) else 'not_read'
    return evidence


def evidence_package(item, *, max_bytes=10000):
    """A prefix of complete located segments; serialization itself obeys UTF-8 cap."""
    evidence = item.get('evidence', item)
    maximum = min(12000, max(1024, int(max_bytes)))
    rows = list(evidence.get('segments') or [])
    package = {'schema': 'official_evidence_package_v1', 'content_hash': evidence.get('content_hash'),
               'url': str(evidence.get('url') or '')[:1500], 'content_status': evidence.get('content_status'),
               'extractor_version': evidence.get('extractor_version'), 'segments': [],
               'selected_segments': 0, 'omitted_segments': len(rows), 'truncated': bool(rows) or bool(evidence.get('truncated')),
               'attachment_scope': evidence.get('attachment_scope', 'not_read')}
    if len(json.dumps(package, ensure_ascii=False).encode('utf-8')) > maximum:
        package['url'] = ''
    for row in rows:
        proposal = {**package, 'segments': package['segments'] + [row], 'selected_segments': len(package['segments']) + 1,
                    'omitted_segments': len(rows) - len(package['segments']) - 1,
                    'truncated': bool(evidence.get('truncated')) or len(package['segments']) + 1 < len(rows)}
        if len(json.dumps(proposal, ensure_ascii=False).encode('utf-8')) > maximum:
            break
        package = proposal
    return package
