"""Parse the fixed Micron quarterly directory as inert, bounded source data.

No network, script execution, settings, adapters, or task queues are involved.
Directory labels identify periods, not publication dates or verified headlines.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

MICRON_DIRECTORY_URL = 'https://investors.micron.com/financials/quarterly-results/default.aspx'
PARSER_VERSION = 'micron-quarterly-directory-v1'
MAX_DIRECTORY_BYTES = 2 * 1024 * 1024
MAX_JSON_CHARS = 128 * 1024
MAX_JSON_DEPTH = 12
MAX_INLINE_SCRIPTS = 256
MAX_YEARS = 32
MAX_LINK_CHARS = 2048
_CATEGORIES = {'Press release', 'Earnings presentation', 'Prepared remarks', 'Form 10-Q / 10K'}
_HOST = 'investors.micron.com'
_IDENTIFIER = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]*')


class MicronDirectoryError(ValueError):
    """Stable local reason; callers may map every code to invalid_feed."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self.active = None
        self.external = False

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            attrs = dict(attrs)
            self.active = []
            self.external = ('src' in attrs or (attrs.get('type') or '').lower()
                             not in {'', 'text/javascript', 'application/javascript'})

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_data(self, data):
        if self.active is not None and not self.external:
            self.active.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.active is not None:
            if not self.external:
                self.scripts.append(''.join(self.active))
                if len(self.scripts) > MAX_INLINE_SCRIPTS:
                    raise MicronDirectoryError('directory_script_limit')
            self.active = None


def _value_offsets(script):
    """Find the bare configuration key outside strings/comments; never run JS."""
    offset = 0
    while offset < len(script):
        char = script[offset]
        if char in "\"'`":
            quote = char
            offset += 1
            while offset < len(script):
                if script[offset] == '\\':
                    offset += 2
                elif script[offset] == quote:
                    offset += 1
                    break
                else:
                    offset += 1
            continue
        if script.startswith('//', offset):
            end = script.find('\n', offset + 2)
            offset = len(script) if end < 0 else end + 1
            continue
        if script.startswith('/*', offset):
            end = script.find('*/', offset + 2)
            if end < 0:
                raise MicronDirectoryError('directory_script_malformed')
            offset = end + 2
            continue
        if char == '/':
            # Treat an unreviewed slash expression conservatively as a regex
            # literal. The observed widget configuration contains no division.
            # Text inside a regex must never become a directory property.
            offset += 1
            escaped, bracket = False, False
            while offset < len(script):
                char = script[offset]
                offset += 1
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '[':
                    bracket = True
                elif char == ']':
                    bracket = False
                elif char == '/' and not bracket:
                    break
            else:
                raise MicronDirectoryError('directory_script_malformed')
            continue
        token = _IDENTIFIER.match(script, offset)
        if token:
            offset = token.end()
            if token.group() == 'documentCategoryData':
                while offset < len(script) and script[offset].isspace():
                    offset += 1
                if offset < len(script) and script[offset] == ':':
                    offset += 1
                    while offset < len(script) and script[offset].isspace():
                        offset += 1
                    yield offset
            continue
        offset += 1


def _unique_object(pairs):
    value = {}
    for key, entry in pairs:
        if key in value:
            raise MicronDirectoryError('directory_duplicate_json_key')
        value[key] = entry
    return value


def _constant(_value):
    raise MicronDirectoryError('directory_json_invalid')


def _json_value(script, start):
    if start >= len(script) or script[start] != '[':
        raise MicronDirectoryError('directory_value_not_json')
    stack, quoted, escaped = [], False, False
    end = None
    for offset in range(start, min(len(script), start + MAX_JSON_CHARS + 1)):
        if offset - start >= MAX_JSON_CHARS:
            raise MicronDirectoryError('directory_json_too_large')
        char = script[offset]
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in '[{':
            stack.append(char)
            if len(stack) > MAX_JSON_DEPTH:
                raise MicronDirectoryError('directory_json_too_deep')
        elif char in ']}':
            if not stack or stack.pop() != ( '[' if char == ']' else '{'):
                raise MicronDirectoryError('directory_json_invalid')
            if not stack:
                end = offset + 1
                break
    if end is None:
        raise MicronDirectoryError('directory_json_invalid')
    suffix = script[end:].lstrip()
    if not suffix or suffix[0] not in ',}':
        raise MicronDirectoryError('directory_value_not_json')
    try:
        return json.loads(script[start:end], object_pairs_hook=_unique_object, parse_constant=_constant)
    except MicronDirectoryError:
        raise
    except (ValueError, RecursionError):
        raise MicronDirectoryError('directory_json_invalid') from None


def _directory_data(text):
    parser = _InlineScripts()
    try:
        parser.feed(text)
        parser.close()
    except (RecursionError, AssertionError):
        raise MicronDirectoryError('directory_html_invalid') from None
    if parser.active is not None:
        raise MicronDirectoryError('directory_html_invalid')
    matches = [(script, offset) for script in parser.scripts if 'documentCategoryData' in script
               for offset in _value_offsets(script)]
    if not matches:
        raise MicronDirectoryError('directory_data_missing')
    if len(matches) != 1:
        raise MicronDirectoryError('directory_data_ambiguous')
    return _json_value(*matches[0])


def _link(value, *, announcement):
    """Normalize references without upgrading HTTP or admitting another host."""
    if (not isinstance(value, str) or not value or len(value) > MAX_LINK_CHARS
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or any(char in value for char in '\\<>"')):
        return None, False, 'invalid_link'
    try:
        url = urljoin(MICRON_DIRECTORY_URL, value)
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            return None, False, 'invalid_link_scheme'
        if parsed.username is not None or parsed.password is not None:
            return None, False, 'link_credentials_forbidden'
        if parsed.port not in (None, 443 if parsed.scheme == 'https' else 80):
            return None, False, 'link_port_forbidden'
        decoded = parsed.path
        for _ in range(3):
            decoded = unquote(decoded)
        if ('\\' in decoded or any(ord(char) <= 32 or ord(char) == 127 for char in decoded)
                or any(part in {'.', '..'} for part in decoded.split('/'))):
            return None, False, 'link_path_forbidden'
        normalized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ''))
        if parsed.scheme != 'https':
            return normalized, False, 'insecure_scheme'
        if parsed.hostname != _HOST:
            return normalized, False, 'host_not_registered'
        if announcement and (not re.fullmatch(r'/news/press-release/\d{4}/[A-Za-z0-9._~-]+(?:/default\.aspx)?', parsed.path)
                             or parsed.query):
            return normalized, False, 'announcement_path_unreviewed'
        return normalized, True, None
    except ValueError:
        return None, False, 'invalid_link'


def _observed_at(value):
    if value is None:
        return None
    try:
        if isinstance(value, datetime):
            stamp = value
        elif type(value) in (int, float) and math.isfinite(value):
            stamp = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str) and len(value) <= 80:
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        else:
            raise ValueError
        if stamp.tzinfo is None:
            raise ValueError
        return stamp.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except (ValueError, OverflowError, OSError):
        raise MicronDirectoryError('directory_observation_time_invalid') from None


def parse_micron_directory(raw, *, directory_url=MICRON_DIRECTORY_URL, observed_at=None, max_bytes=MAX_DIRECTORY_BYTES):
    """Return accepted announcements, rejected links and unread attachment refs.

    All publication/modification times remain None. observed_at, if provided,
    identifies only the local observation. This function never creates tasks.
    """
    if directory_url != MICRON_DIRECTORY_URL:
        raise MicronDirectoryError('directory_url_not_allowed')
    if not isinstance(raw, (bytes, bytearray)) or type(max_bytes) is not int or max_bytes < 1:
        raise MicronDirectoryError('directory_input_invalid')
    if len(raw) > min(max_bytes, MAX_DIRECTORY_BYTES):
        raise MicronDirectoryError('directory_too_large')
    raw = bytes(raw)
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise MicronDirectoryError('directory_encoding_invalid') from None
    data = _directory_data(text)
    if not isinstance(data, list) or not 1 <= len(data) <= len(_CATEGORIES):
        raise MicronDirectoryError('directory_schema_invalid')
    digest = hashlib.sha256(raw).hexdigest()
    observation = _observed_at(observed_at)
    announcements, rejected, attachments = [], [], []
    categories, periods, seen_links = set(), set(), {}
    for category_index, category in enumerate(data):
        if (not isinstance(category, dict) or not {'category', 'data'} <= set(category)
                or set(category) - {'category', 'data', 'tag'} or not isinstance(category.get('category'), str)
                or category['category'] not in _CATEGORIES
                or not isinstance(category.get('data'), list) or len(category['data']) > MAX_YEARS
                or ('tag' in category and (not isinstance(category['tag'], str) or len(category['tag']) > 80))):
            raise MicronDirectoryError('directory_schema_invalid')
        label = category['category']
        if label in categories:
            raise MicronDirectoryError('directory_category_ambiguous')
        categories.add(label)
        years = set()
        for year_index, yearly in enumerate(category['data']):
            if (not isinstance(yearly, dict) or set(yearly) != {'year', 'documents'}
                    or type(yearly.get('year')) is not int or not 1900 <= yearly['year'] <= 2100
                    or not isinstance(yearly.get('documents'), list) or len(yearly['documents']) > 4):
                raise MicronDirectoryError('directory_schema_invalid')
            year = yearly['year']
            if year in years:
                raise MicronDirectoryError('directory_period_ambiguous')
            years.add(year)
            quarters = set()
            for document_index, document in enumerate(yearly['documents']):
                if (not isinstance(document, dict) or set(document) != {'text', 'link'}
                        or not isinstance(document.get('text'), str) or not re.fullmatch(r'Q[1-4]', document['text'])
                        or not isinstance(document.get('link'), str) or len(document['link']) > MAX_LINK_CHARS):
                    raise MicronDirectoryError('directory_schema_invalid')
                quarter = document['text']
                if quarter in quarters:
                    raise MicronDirectoryError('directory_period_ambiguous')
                quarters.add(quarter)
                path = f'documentCategoryData[{category_index}].data[{year_index}].documents[{document_index}]'
                url, allowed, reason = _link(document['link'], announcement=label == 'Press release')
                period = f'FY{year}-{quarter}'
                record = {'category': label, 'fiscal_year': year, 'quarter': quarter,
                          'fiscal_quarter': int(quarter[1]), 'reference_period': period,
                          'period_basis': 'directory_year_and_quarter_labels', 'source_label': quarter,
                          'observed_url': document['link'], 'url': url, 'url_source': 'directory_json_link',
                          'directory_url': directory_url, 'directory_sha256': digest,
                          'json_path': path, 'url_json_path': path + '.link', 'observed_at': observation,
                          'published': None, 'source_updated_at': None,
                          'published_time_source': 'unknown_directory_listing'}
                if label == 'Press release':
                    periods.add((year, quarter))
                    record.update(title=f'Micron {period} press release (directory listing)',
                                  title_basis='directory_label', allowed=allowed)
                    if allowed:
                        if url in seen_links:
                            raise MicronDirectoryError('directory_link_ambiguous')
                        seen_links[url] = period
                        record.update(identity=url)
                        announcements.append(record)
                    else:
                        record.update(reason=reason)
                        rejected.append(record)
                else:
                    record.update(allowed=allowed, registered_host_allowed=allowed,
                                  read=False, status='not_read', download_attempted=False,
                                  reason=reason or 'not_requested')
                    attachments.append(record)
    if 'Press release' not in categories:
        raise MicronDirectoryError('directory_press_releases_missing')
    return {'schema': PARSER_VERSION, 'directory_url': directory_url, 'directory_sha256': digest,
            'directory_bytes': len(raw), 'observed_at': observation, 'publication_time_known': False,
            'coverage': {'kind': 'directory_snapshot', 'years': sorted({year for year, _ in periods}),
                         'listed_periods': [f'FY{year}-{quarter}' for year, quarter in sorted(periods)],
                         'continuous_coverage': False, 'latest_publication': None,
                         'latest_announcement_verified': False},
            'announcements': announcements, 'rejected_announcements': rejected, 'attachments': attachments,
            'counts': {'announcements_seen': len(announcements) + len(rejected),
                       'announcements_accepted': len(announcements), 'announcements_rejected': len(rejected),
                       'attachment_references': len(attachments), 'attachment_reads': 0}}
