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
from .news_feed_transport import FeedValidationError, failure_retry_after

EXTRACTOR_VERSION = 'official-html-segments-v2'
MAX_ARTICLE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 120000
MAX_ATTACHMENTS_READ = 3
MAX_JSON_LD_CHARS = 256000
_BLOCKS = {'p', 'li', 'h1', 'h2', 'h3', 'h4', 'blockquote', 'tr'}
_HIDDEN = {'script', 'style', 'noscript', 'nav', 'header', 'footer', 'button', 'svg', 'textarea', 'select', 'template'}
_CONTENT = re.compile(r'(?<![a-z0-9])(?:article[-_]?body|news[-_]?release|release[-_]?body|press[-_]?release|field[-_]body|node[-_]content|story[-_]body|evergreen-news-body)(?![a-z0-9])', re.I)
_MAIN = re.compile(r'main[-_]?content', re.I)
_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


def _source_date(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _article_dates(documents, meta_dates, url):
    """Read inert Article metadata only when its identity matches this page."""
    pending = [(value, 0, f'script[{i}]') for i, value in enumerate(documents[:64])]
    origins = {key: list(meta_dates[key]) for key in ('published_at', 'source_updated_at')}
    identity_conflicts = []
    visited = 0
    while pending and visited < 256:
        value, depth, path = pending.pop()
        visited += 1
        if depth > 8:
            continue
        if isinstance(value, list):
            pending.extend((entry, depth + 1, f'{path}[{i}]') for i, entry in enumerate(value[:256 - visited]))
            continue
        if not isinstance(value, dict):
            continue
        if '@graph' in value:
            pending.append((value['@graph'], depth + 1, path + '.@graph'))
        kinds = value.get('@type', [])
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not isinstance(kinds, list) or not any(k in ('Article', 'NewsArticle') for k in kinds):
            continue
        identities = []
        for identity_key in ('mainEntityOfPage', 'url', '@id'):
            identity = value.get(identity_key)
            if isinstance(identity, dict):
                identities.extend(identity[k] for k in ('@id', 'url') if isinstance(identity.get(k), str))
            elif isinstance(identity, str):
                identities.append(identity)
        matches = []
        for identity in identities:
            try:
                matches.append(canonical_url(urljoin(url, identity)) == canonical_url(url))
            except ValueError:
                matches.append(False)
        if any(matches) and not all(matches):
            identity_conflicts.append({'path': path, 'identities': identities})
        if not matches or not all(matches):
            continue
        for field, key in [('published_at', 'datePublished'), ('source_updated_at', 'dateModified')]:
            stamp = _source_date(value.get(key))
            if stamp:
                origins[field].append({'value': stamp, 'format': 'json_ld', 'path': path + '.' + key,
                                       'article_url': url})
    result = {}
    for key, sources in origins.items():
        values = {entry['value'] for entry in sources}
        result[key] = {'value': next(iter(values)) if len(values) == 1 and not identity_conflicts else None,
                       'status': 'identity_conflict' if identity_conflicts else 'conflict' if len(values) > 1 else 'confirmed' if values else 'unknown',
                       'sources': sources, 'identity_conflicts': identity_conflicts}
    return result


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
        self.content_scope = 0
        self.closed_scopes = set()
        self.incomplete = False
        self.published = None
        self.updated = None
        self.title = []
        self.json_ld = []
        self.json_buffer = None
        self.json_chars = 0
        self.meta_dates = {'published_at': [], 'source_updated_at': []}
        self.table_count = 0
        self.table_stack = []
        self.row = None
        self.cell = None

    def _hidden(self):
        if any(hidden for _, _, hidden in self.stack):
            return True
        # A layout form may wrap the article. A form inside the article is UI.
        form = max((i for i, (tag, _, _) in enumerate(self.stack) if tag == 'form'), default=-1)
        content = max((i for i, (_, scope, _) in enumerate(self.stack) if scope), default=-1)
        return form >= 0 and form > content

    def _scoped(self):
        return max((content for _, content, _ in self.stack), default=0)

    def flush(self):
        text = re.sub(r'\s+', ' ', ''.join(self.buffer)).strip()
        if text:
            self.segments.append({'text': text, 'kind': self.buffer_kind, 'scoped': self.buffer_scoped})
        self.buffer = []

    def _finish_row(self):
        if self.row is not None:
            for cell in self.row['cells']:
                cell['text'] = re.sub(r'\s+', ' ', cell['text']).strip()
            self.row['text'] = ' | '.join(cell['text'] for cell in self.row['cells'])
            self.segments.append(self.row)
        self.row = self.cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and (attrs.get('type') or '').lower() == 'application/ld+json':
            self.json_buffer = []
            self.json_chars = 0
        if tag == 'meta':
            name = (attrs.get('property') or attrs.get('name') or '').lower()
            if name in {'article:published_time', 'datepublished', 'date'}:
                self.published = _source_date(attrs.get('content')) or self.published
            if name in {'article:modified_time', 'datemodified'}:
                self.updated = _source_date(attrs.get('content')) or self.updated
            field = 'published_at' if name in {'article:published_time', 'datepublished', 'date'} else 'source_updated_at' if name in {'article:modified_time', 'datemodified'} else None
            if field and _source_date(attrs.get('content')):
                self.meta_dates[field].append({'value': _source_date(attrs['content']), 'format': 'meta', 'path': name})
        identity = (attrs.get('id') or '') + ' ' + (attrs.get('class') or '')
        is_content = 2 if tag == 'article' or (tag in {'div', 'section'} and _CONTENT.search(identity)) else 1 if tag == 'main' or (tag in {'div', 'section'} and _MAIN.search(identity)) else 0
        hidden = (tag in _HIDDEN or 'hidden' in attrs or (attrs.get('aria-hidden') or '').lower() == 'true'
                  or (tag == 'form' and (self._scoped() > 0 or attrs.get('role') == 'search'
                                        or re.search(r'search', identity, re.I) is not None)))
        if (tag in _BLOCKS or is_content or tag == 'table') and self.row is None:
            self.flush()
        if tag not in _VOID:
            self.stack.append((tag, is_content, hidden))
        if is_content and not self._hidden():
            self.has_content = True
            self.content_scope = max(self.content_scope, is_content)
        if tag == 'table' and not self._hidden():
            if self.row is not None:
                self.incomplete = True  # Nested tables need a richer layout parser.
                self._finish_row()
            self.table_count += 1
            self.table_stack.append([self.table_count, 0])
        if tag == 'tr' and self.table_stack and not self._hidden():
            if self.row is not None:
                self.incomplete = True
                self._finish_row()
            self.table_stack[-1][1] += 1
            self.row = {'kind': 'table_row', 'scoped': self._scoped(), 'table_id': 'table-' + str(self.table_stack[-1][0]),
                        'row_index': self.table_stack[-1][1], 'cells': []}
        if tag in {'td', 'th'} and self.row is not None and not self._hidden():
            spans = {}
            for key in ('colspan', 'rowspan'):
                try:
                    original_span = int(attrs.get(key, '1'))
                    spans[key] = max(1, min(1000, original_span))
                    self.incomplete = self.incomplete or spans[key] != original_span
                except (ValueError, TypeError):
                    spans[key] = 1
                    self.incomplete = True
            self.cell = {'text': '', 'tag': tag, **spans}
            self.row['cells'].append(self.cell)
        if self.cell is not None and (tag == 'br' or tag in _BLOCKS) and not self._hidden():
            self.cell['text'] += ' '
        elif tag == 'br' and not self._hidden():
            self.buffer.append(' ')
        if tag in _BLOCKS and self.row is None:
            self.buffer_kind = 'table_row' if tag == 'tr' else 'paragraph'
            self.buffer_scoped = self._scoped()
        if tag == 'a' and not self._hidden():
            self.active_link = {'href': attrs.get('href', ''), 'label': '', 'scoped': self._scoped()}
            self.links.append(self.active_link)

    def handle_endtag(self, tag):
        if tag == 'script' and self.json_buffer is not None:
            try:
                if self.json_chars <= MAX_JSON_LD_CHARS and len(self.json_ld) < 64:
                    self.json_ld.append(json.loads(''.join(self.json_buffer)))
            except (ValueError, RecursionError):
                pass
            self.json_buffer = None
        if not self._hidden():
            if tag in {'td', 'th'}:
                self.cell = None
            if tag == 'tr':
                self._finish_row()
            if tag == 'table' and self.table_stack:
                if self.row is not None:
                    self.incomplete = True
                    self._finish_row()
                self.table_stack.pop()
        if self.cell is not None and tag in _BLOCKS:
            self.cell['text'] += ' '
        if self.row is None and (tag in _BLOCKS or tag in {'article', 'main', 'div', 'section'}):
            self.flush()
        if tag == 'a':
            self.active_link = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                if self.stack[index][1] and not self._hidden():
                    self.closed_content = True
                    self.closed_scopes.add(self.stack[index][1])
                if any(content for _, content, _ in self.stack[index + 1:]):
                    self.incomplete = True
                self.stack = self.stack[:index]
                break
        if tag == 'tr':
            self.buffer_kind = 'paragraph'

    def handle_data(self, data):
        if self.json_buffer is not None:
            self.json_chars += len(data)
            if self.json_chars <= MAX_JSON_LD_CHARS:
                self.json_buffer.append(data)
            return
        if any(tag == 'title' for tag, _, _ in self.stack):
            self.title.append(data)
            return
        if self._hidden() or any(tag == 'head' for tag, _, _ in self.stack):
            return
        if self.cell is not None:
            self.cell['text'] += data
        elif self.row is None:
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
        rows = [segment for segment in parser.segments if segment['scoped'] == parser.content_scope] if parser.has_content else parser.segments
        complete = (parser.has_content and parser.content_scope in parser.closed_scopes and not parser.incomplete
                    and not any(scope == parser.content_scope for _, scope, _ in parser.stack)
                    and parser.row is None and not parser.table_stack)
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
        if row['kind'] == 'table_row':
            segment.update({key: row[key] for key in ('table_id', 'row_index') if key in row})
            if len(piece) == len(row['text']):
                segment['cells'] = row.get('cells', [])
            else:
                segment['structure_truncated'] = True
        evidence['segments'].append(segment)
        evidence['tables' if row['kind'] == 'table_row' else 'paragraphs'].append(segment)
        chars += len(piece)
        if len(piece) != len(row['text']):
            evidence['truncated'] = True
    evidence['body_text'] = '\n'.join(row['text'] for row in evidence['segments'])
    evidence['content_hash'] = hashlib.sha256(evidence['body_text'].encode('utf-8')).hexdigest()
    evidence['source_time_evidence'] = _article_dates(parser.json_ld, parser.meta_dates, url)
    evidence['published_at'] = evidence['source_time_evidence']['published_at']['value']
    evidence['source_updated_at'] = evidence['source_time_evidence']['source_updated_at']['value']
    complete = complete and not evidence['truncated'] and not decode_incomplete
    evidence.update(content_status='full_text' if complete else 'partial_text', status='full_text' if complete else 'partial_text', full_text_read=complete)
    evidence['reading_scope'] = 'recognized_article_container' if parser.has_content else 'plain_text' if mime == 'text/plain' else 'unverified_page_body'
    seen = set()
    for link in parser.links:
        candidate = urljoin(url, link['href'])
        label = re.sub(r'\s+', ' ', link['label']).strip()[:300]
        path = urlsplit(candidate).path.lower()
        if link['scoped'] != parser.content_scope or not link['scoped'] or candidate in seen or candidate == url:
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
        if evidence.get('source_restriction'):
            if attachment['status'] == 'not_read':
                attachment['reason'] = 'source_wait_after_attachment_failure'
            continue
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
            if result['content_status'] == 'access_restricted':
                evidence['source_restriction'] = {'http_status': attached.status, 'retry': {},
                                                  'restricted': True, 'origin': 'attachment'}
        except urllib.error.HTTPError as error:
            # Preserve the successful body and earlier attachments. Carry the
            # source-wide wait separately, before another attachment can start.
            retry = failure_retry_after(error.headers, getattr(error, 'news_diagnostics', None), now)
            limited = error.code in {403, 429}
            attachment.update(status='access_restricted' if limited else 'fetch_failed', read=False,
                              http_status=error.code, reason='attachment_http_error')
            if limited or retry.get('status') == 'valid' or retry.get('manual_hold'):
                evidence['source_restriction'] = {'http_status': error.code, 'retry': retry,
                                                  'restricted': False, 'origin': 'attachment'}
            error.close()
        except (ValueError, TimeoutError, OSError, FeedValidationError):
            attachment.update(status='fetch_failed', reason='attachment_read_failed')
    evidence['attachment_scope'] = 'some_read' if any(a['read'] for a in evidence['attachments']) else 'not_read'
    return evidence


def evidence_package(item, *, max_bytes=10000):
    """Complete context groups; the serialized package itself obeys its cap."""
    from .news_evidence_groups import context_groups, summary, select_groups, encoded_size, selected_rows
    evidence = item.get('evidence', item)
    maximum = min(12000, max(0, int(max_bytes)))
    groups = context_groups(item)
    base = {'schema': 'official_evidence_package_v2', 'content_hash': evidence.get('content_hash'),
               'url': str(evidence.get('url') or '')[:1500], 'content_status': evidence.get('content_status'),
               'extractor_version': evidence.get('extractor_version'),
               'attachment_scope': evidence.get('attachment_scope', 'not_read')}
    def render(selected, reasons, detail_limit):
        selection = summary(groups, selected, reasons, detail_limit)
        rows = selected_rows(selected)
        return {**base, **selection, 'segments': rows, 'selected_segments': len(rows),
                'truncated': bool(evidence.get('truncated') or selection['omitted_group_count'])}
    if encoded_size(render([], {}, 0)) > maximum:
        base['url'] = ''
    return select_groups(groups, render, maximum)
