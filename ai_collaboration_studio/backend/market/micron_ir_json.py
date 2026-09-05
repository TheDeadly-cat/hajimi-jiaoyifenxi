"""Bounded Micron Q4 recent-30 metadata reader; no article-body ingestion.

``complete`` describes every row returned by this one fixed recent-30 query.
It does not claim the publisher's full history or infer pagination semantics.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.request import Request

from ..source_poll_control import ensure_source_poll_active
from .official_http import open_official_https, read_official_https_body


MICRON_IR_JSON_URL = (
    "https://investors.micron.com/feed/PressRelease.svc/GetPressReleaseList"
    "?LanguageId=1&bodyType=0&pressReleaseDateFilter=1"
    "&categoryId=00000000-0000-0000-0000-000000000000"
    "&pageSize=30&pageNumber=0&tagList=&includeTags=true&year=-1&excludeSelection=1"
)
MICRON_TIME_METADATA_HASH_SEMANTICS = "normalized_newsarticle_head_metadata_not_html_body"
MICRON_JSON_MAX_BYTES = 1_000_000
MICRON_HEAD_MAX_BYTES = 128 * 1024
MICRON_HEAD_MAX_WORKERS = 4
_MAX_EXACT_JSON_INTEGER = (1 << 53) - 1
_ORIGIN = "https://investors.micron.com"
_DETAIL_PATH = re.compile(r"/news/press-release/[0-9]{4}/[A-Za-z0-9_-]+/default\.aspx\Z")
_DECLARED_WALL_TIME = re.compile(r"[0-9]{2}/[0-9]{2}/[0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2}\Z")
_RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z"
)


class MicronIrJsonError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def is_micron_detail_url(value: Any) -> bool:
    """Accept only an exact canonical absolute URL in the observed news path."""
    return (
        type(value) is str
        and value.startswith(_ORIGIN + "/")
        and _DETAIL_PATH.fullmatch(value[len(_ORIGIN):]) is not None
    )


def is_micron_declared_wall_time(value: Any) -> bool:
    """Validate Q4's observed wall-time syntax, without assigning any timezone."""
    if type(value) is not str or _DECLARED_WALL_TIME.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%m/%d/%Y %H:%M:%S")
    except ValueError:
        return False
    return True


def micron_time_metadata_sha256(
    *,
    official_url: str,
    title: str,
    published_at: str,
    metadata_date_modified: str = "",
) -> str:
    """Digest selected normalized JSON-LD metadata, never HTML or article text."""
    if not is_micron_detail_url(official_url) or any(
        type(value) is not str for value in (title, published_at, metadata_date_modified)
    ):
        raise MicronIrJsonError("MICRON_IR_TIME_METADATA_INVALID", "metadata hash inputs are invalid")
    basis = {
        "version": "micron_newsarticle_time_metadata_v1",
        "url": official_url,
        "headline": title,
        "datePublished": published_at,
        "dateModified": metadata_date_modified,
    }
    return hashlib.sha256(json.dumps(
        basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _text(value: Any, *, field: str, maximum: int, empty: bool = False) -> str:
    if type(value) is not str:
        raise MicronIrJsonError("MICRON_IR_JSON_INVALID", f"{field} must be a native string")
    normalized = " ".join(unicodedata.normalize("NFC", value).split())
    if (not normalized and not empty) or len(normalized) > maximum:
        raise MicronIrJsonError("MICRON_IR_JSON_INVALID", f"{field} is empty or exceeds its limit")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in normalized):
        raise MicronIrJsonError("MICRON_IR_JSON_INVALID", f"{field} contains unsupported characters")
    return normalized


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MicronIrJsonError("MICRON_IR_JSON_INVALID", "duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise MicronIrJsonError("MICRON_IR_JSON_INVALID", "non-finite JSON number")


def _decode_json(raw: bytes | str) -> Any:
    try:
        text = raw.decode("utf-8-sig") if type(raw) is bytes else raw
        return json.loads(text, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MicronIrJsonError("MICRON_IR_JSON_INVALID", "invalid UTF-8 JSON response") from exc


def _list_rows(raw: bytes) -> list[dict[str, Any]]:
    if type(raw) is not bytes:
        raise MicronIrJsonError("MICRON_IR_JSON_INVALID", "list response must be native bytes")
    if len(raw) > MICRON_JSON_MAX_BYTES:
        raise MicronIrJsonError("MICRON_IR_RESPONSE_TOO_LARGE", "list response exceeds 1 MB")
    envelope = _decode_json(raw)
    if type(envelope) is not dict or set(envelope) != {"GetPressReleaseListResult"}:
        raise MicronIrJsonError("MICRON_IR_JSON_INVALID", "unrecognized Q4 list envelope")
    rows = envelope["GetPressReleaseListResult"]
    if type(rows) is not list or len(rows) > 30:
        raise MicronIrJsonError("MICRON_IR_SCOPE_INCOMPLETE", "fixed recent-30 scope is invalid")
    seen: set[int] = set()
    seen_urls: set[str] = set()
    result = []
    for row in rows:
        if type(row) is not dict:
            raise MicronIrJsonError("MICRON_IR_JSON_INVALID", "Q4 release must be an object")
        identity, revision = row.get("PressReleaseId"), row.get("RevisionNumber")
        if type(identity) is not int or not 1 <= identity <= _MAX_EXACT_JSON_INTEGER or identity in seen:
            raise MicronIrJsonError("MICRON_IR_JSON_INVALID", "release ID is invalid or duplicated")
        if type(revision) is not int or not 0 <= revision <= _MAX_EXACT_JSON_INTEGER:
            raise MicronIrJsonError("MICRON_IR_JSON_INVALID", "revision must be an exact non-negative JSON integer")
        path = row.get("LinkToDetailPage")
        if type(path) is not str or _DETAIL_PATH.fullmatch(path) is None:
            raise MicronIrJsonError("MICRON_IR_LINK_INVALID", "detail link is outside the fixed relative news path")
        url = _ORIGIN + path
        if url in seen_urls:
            raise MicronIrJsonError("MICRON_IR_JSON_INVALID", "different release IDs share one detail URL")
        raw_date = row.get("PressReleaseDate")
        if not is_micron_declared_wall_time(raw_date):
            raise MicronIrJsonError("MICRON_IR_JSON_INVALID", "PressReleaseDate must be a valid MM/DD/YYYY HH:MM:SS wall-time string")
        summary = row.get("ShortDescription")
        result.append({
            "title": _text(row.get("Headline"), field="Headline", maximum=300),
            "official_url": url,
            "summary": _text("" if summary is None else summary, field="ShortDescription", maximum=800, empty=True),
            "q4_press_release_id": identity,
            "q4_revision_number": revision,
            "source_declared_time_raw": raw_date,
            "source_format": "micron_q4_public_json_v1",
            "guid": "",
        })
        seen.add(identity)
        seen_urls.add(url)
    return result


def _head_prefix(raw: bytes) -> bytes:
    if type(raw) is not bytes:
        raise MicronIrJsonError("MICRON_IR_HEAD_INCOMPLETE", "metadata response must be native bytes")
    end = raw.lower().find(b"</head>")
    if end < 0:
        code = "MICRON_IR_RESPONSE_TOO_LARGE" if len(raw) > MICRON_HEAD_MAX_BYTES else "MICRON_IR_HEAD_INCOMPLETE"
        raise MicronIrJsonError(code, "metadata response has no complete bounded head section")
    end += len(b"</head>")
    if end > MICRON_HEAD_MAX_BYTES:
        raise MicronIrJsonError("MICRON_IR_RESPONSE_TOO_LARGE", "head metadata exceeds 128 KiB")
    return raw[:end]


class _HeadOnlyResponse:
    """Adapt a response to the existing cancellable bounded-body reader.

    One small transport chunk may straddle the closing tag. Only the head
    prefix is retained or returned; no body field or linked resource is read.
    Exposing the original socket lets the shared watcher interrupt a read.
    """

    def __init__(self, response: Any) -> None:
        self._response = response

    @property
    def fp(self) -> Any:
        return getattr(self._response, "fp", None)

    def close(self) -> None:
        self._response.close()

    def read(self, maximum: int) -> bytes:
        reader = getattr(self._response, "read1", None)
        if not callable(reader):
            reader = self._response.read
        accumulated = bytearray()
        while len(accumulated) < maximum:
            chunk = reader(min(4096, maximum - len(accumulated)))
            if type(chunk) is not bytes:
                raise MicronIrJsonError("MICRON_IR_HEAD_INCOMPLETE", "metadata stream returned non-bytes")
            if not chunk:
                break
            accumulated.extend(chunk)
            index = accumulated.lower().find(b"</head>")
            if index >= 0:
                return _head_prefix(bytes(accumulated[:index + 7]))
        return _head_prefix(bytes(accumulated))


class _HeadMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.in_head = False
        self.head_count = 0
        self.head_closed = False
        self.in_jsonld = False
        self.script_data: list[str] = []
        self.documents: list[Any] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "head":
            self.head_count += 1
            self.in_head = True
        if tag == "script" and self.in_head:
            types = [value for name, value in attrs if name == "type"]
            if len(types) > 1:
                raise MicronIrJsonError("MICRON_IR_TIME_METADATA_INVALID", "ambiguous script type")
            self.in_jsonld = bool(types and type(types[0]) is str and types[0].lower() == "application/ld+json")
            self.script_data = []
        if tag == "body" and self.in_head:
            raise MicronIrJsonError("MICRON_IR_HEAD_INCOMPLETE", "body began before head ended")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.in_jsonld:
            self.documents.append(_decode_json("".join(self.script_data)))
            self.in_jsonld = False
            self.script_data = []
        if tag == "head":
            self.in_head = False
            self.head_closed = True

    def handle_data(self, data: str) -> None:
        if self.in_head and self.in_jsonld:
            self.script_data.append(data)


def _newsarticles(value: Any, *, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 10:
        raise MicronIrJsonError("MICRON_IR_TIME_METADATA_INVALID", "JSON-LD graph is too deep")
    if type(value) is list:
        return [article for child in value for article in _newsarticles(child, depth=depth + 1)]
    if type(value) is not dict:
        return []
    article_type = value.get("@type")
    is_article = article_type == "NewsArticle" or (
        type(article_type) is list and "NewsArticle" in article_type
    )
    result = [value] if is_article else []
    if "@graph" in value:
        result.extend(_newsarticles(value["@graph"], depth=depth + 1))
    return result


def _time(value: Any, *, receipt: datetime, field: str) -> str:
    if type(value) is not str or _RFC3339.fullmatch(value) is None or value.endswith("-00:00"):
        raise MicronIrJsonError("MICRON_IR_TIME_METADATA_INVALID", f"{field} requires a known RFC3339 offset")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise MicronIrJsonError("MICRON_IR_TIME_METADATA_INVALID", f"{field} is not a valid timestamp") from exc
    if parsed > receipt:
        raise MicronIrJsonError("MICRON_IR_TIME_METADATA_INVALID", f"{field} is after local response receipt")
    base = parsed.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f".{parsed.microsecond:06d}".rstrip("0") if parsed.microsecond else ""
    return f"{base}{fraction}Z"


def _project_metadata(raw: bytes, row: dict[str, Any], receipt: Any) -> dict[str, Any]:
    if type(receipt) is not datetime or receipt.tzinfo is None or receipt.utcoffset() is None:
        raise MicronIrJsonError("MICRON_IR_TIME_METADATA_INVALID", "receipt clock must return an aware native datetime")
    parser = _HeadMetadataParser()
    try:
        parser.feed(_head_prefix(raw).decode("utf-8-sig"))
        parser.close()
    except (UnicodeDecodeError, RecursionError) as exc:
        raise MicronIrJsonError("MICRON_IR_TIME_METADATA_INVALID", "head metadata is not valid UTF-8 HTML") from exc
    if parser.head_count != 1 or not parser.head_closed or parser.in_jsonld:
        raise MicronIrJsonError("MICRON_IR_HEAD_INCOMPLETE", "expected one complete HTML head section")
    articles = [article for document in parser.documents for article in _newsarticles(document)]
    if len(articles) != 1:
        raise MicronIrJsonError("MICRON_IR_TIME_METADATA_INVALID", "expected exactly one NewsArticle")
    article = articles[0]
    entity = article.get("mainEntityOfPage")
    if type(entity) is not dict or type(entity.get("@id")) is not str or entity["@id"] != row["official_url"]:
        raise MicronIrJsonError("MICRON_IR_TIME_METADATA_INVALID", "NewsArticle URL does not match the release")
    if _text(article.get("headline"), field="NewsArticle.headline", maximum=300) != row["title"]:
        raise MicronIrJsonError("MICRON_IR_TIME_METADATA_INVALID", "NewsArticle headline does not match the release")
    received = receipt.astimezone(timezone.utc)
    published = _time(article.get("datePublished"), receipt=received, field="datePublished")
    modified = _time(article["dateModified"], receipt=received, field="dateModified") if "dateModified" in article else ""
    return {
        **row,
        "published_at": published,
        "metadata_date_modified": modified,
        "time_metadata_sha256": micron_time_metadata_sha256(
            official_url=row["official_url"], title=row["title"],
            published_at=published, metadata_date_modified=modified,
        ),
    }


class MicronIrJsonClient:
    """Read a sealed query and at most 30 same-publisher head sections."""

    def __init__(
        self,
        *,
        fetch_bytes: Callable[..., bytes] | None = None,
        clock: Callable[[], datetime] | None = None,
        max_workers: int = MICRON_HEAD_MAX_WORKERS,
    ) -> None:
        if fetch_bytes is not None and not callable(fetch_bytes):
            raise TypeError("fetch_bytes must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if type(max_workers) is not int or not 1 <= max_workers <= MICRON_HEAD_MAX_WORKERS:
            raise ValueError("max_workers must be a native integer between 1 and 4")
        self._fetch_bytes = fetch_bytes if fetch_bytes is not None else self._default_fetch_bytes
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._max_workers = max_workers

    @property
    def transport_identity(self) -> Callable[..., bytes]:
        return self._fetch_bytes

    @property
    def max_workers(self) -> int:
        """Expose the configured concurrency for the caller's provenance seal."""
        return self._max_workers

    @staticmethod
    def _default_fetch_bytes(
        url: str,
        *,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
        max_bytes: int,
        head_only: bool,
    ) -> bytes:
        if type(head_only) is not bool or type(max_bytes) is not int or (
            (head_only and (not is_micron_detail_url(url) or max_bytes != MICRON_HEAD_MAX_BYTES))
            or (not head_only and (url != MICRON_IR_JSON_URL or max_bytes != MICRON_JSON_MAX_BYTES))
        ):
            raise MicronIrJsonError("MICRON_IR_LINK_INVALID", "request is outside the sealed Micron policy")
        request = Request(url, headers={
            "User-Agent": "AI-Collaboration-Studio/0.1 local-read-only-research",
            "Accept": "text/html" if head_only else "application/json",
            "Accept-Encoding": "identity",
        }, method="GET")
        with open_official_https(
            request, allowed_hosts={"investors.micron.com"}, timeout=12,
            url_validator=lambda candidate: type(candidate) is str and candidate == url,
            deadline_monotonic_ms=deadline_monotonic_ms, cancel_event=cancel_event,
        ) as response:
            if not head_only:
                declared = response.headers.get("Content-Length")
                if declared is not None and (not declared.isdecimal() or int(declared) > max_bytes):
                    raise MicronIrJsonError("MICRON_IR_RESPONSE_TOO_LARGE", "list length is invalid or exceeds 1 MB")
            return read_official_https_body(
                _HeadOnlyResponse(response) if head_only else response,
                max_bytes, deadline_seconds=12,
                deadline_monotonic_ms=deadline_monotonic_ms, cancel_event=cancel_event,
            )

    def read_recent(
        self,
        *,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        controls = {"deadline_monotonic_ms": deadline_monotonic_ms, "cancel_event": cancel_event}
        ensure_source_poll_active(**controls)
        raw = self._fetch_bytes(MICRON_IR_JSON_URL, max_bytes=MICRON_JSON_MAX_BYTES, head_only=False, **controls)
        ensure_source_poll_active(**controls)
        # Validate every list identity and URL before requesting any detail page.
        rows = _list_rows(raw)
        if not rows:
            return {"releases": [], "complete": True}

        def read_row(row: dict[str, Any]) -> dict[str, Any]:
            ensure_source_poll_active(**controls)
            document = self._fetch_bytes(row["official_url"], max_bytes=MICRON_HEAD_MAX_BYTES, head_only=True, **controls)
            receipt = self._clock()
            ensure_source_poll_active(**controls)
            return _project_metadata(document, row, receipt)

        releases = []
        # Small batches bound concurrency and do not queue the remaining scope
        # when any current batch fails. No cache or partial result is published.
        with ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="micron-ir-head") as executor:
            for start in range(0, len(rows), self._max_workers):
                futures = [executor.submit(read_row, row) for row in rows[start:start + self._max_workers]]
                releases.extend(future.result() for future in futures)
        ensure_source_poll_active(**controls)
        return {"releases": releases, "complete": True}


__all__ = [
    "MICRON_IR_JSON_URL", "MICRON_TIME_METADATA_HASH_SEMANTICS", "MICRON_HEAD_MAX_WORKERS",
    "MicronIrJsonClient", "MicronIrJsonError", "is_micron_detail_url",
    "micron_time_metadata_sha256", "is_micron_declared_wall_time",
]
