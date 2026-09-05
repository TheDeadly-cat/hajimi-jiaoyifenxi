"""Bounded Micron Q4 recent-30 metadata reader; no article-body ingestion.

``complete`` means every row in this fixed recent-30 query has admitted time
metadata. Initial reads fetch every head. Ordinary reads may reuse independently
verified metadata for at most one hour, with four old identities revalidated per
poll. Progress distinguishes fetched, cached and withheld identities. It does
not claim full publisher history, continuous freshness or pagination coverage.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.request import Request

from ..source_poll_control import SourcePollCancelled, SourcePollDeadlineExceeded, ensure_source_poll_active
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
MICRON_METADATA_CACHE_CAPACITY = 30
MICRON_METADATA_REVALIDATE_PER_POLL = 4
MICRON_METADATA_MAX_AGE_MS = 3_600_000
MICRON_METADATA_REVALIDATION_TIMEOUT_MS = 2_000
MICRON_METADATA_COMMIT_RESERVE_MS = 500
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


def _sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def micron_metadata_cache_policy() -> dict[str, Any]:
    """Fixed in-process policy; no cache data is a persisted checkpoint."""
    return {
        "version": "micron_metadata_cache_policy_v1",
        "parser_version": "micron_newsarticle_head_parser_v1",
        "time_hash_semantics": MICRON_TIME_METADATA_HASH_SEMANTICS,
        "list_binding": "entire_normalized_q4_row_v1",
        "capacity": MICRON_METADATA_CACHE_CAPACITY,
        "revalidate_per_poll": MICRON_METADATA_REVALIDATE_PER_POLL,
        "maximum_age_ms": MICRON_METADATA_MAX_AGE_MS,
        "revalidation_timeout_ms": MICRON_METADATA_REVALIDATION_TIMEOUT_MS,
        "commit_reserve_ms": MICRON_METADATA_COMMIT_RESERVE_MS,
        "storage": "instance_memory_only_cold_on_restart",
    }


def valid_micron_metadata_progress(value: Any, releases: Any, *, complete: bool) -> bool:
    """Check the closed per-identity admission report before partial import."""
    if type(value) is not dict or set(value) != {
        "version", "poll_number", "scope_ids", "requested_ids", "cache_hit_ids", "failed", "next_revalidation_ids",
    } or value.get("version") != "micron_metadata_progress_v1":
        return False
    if type(value["poll_number"]) is not int or not 1 <= value["poll_number"] <= _MAX_EXACT_JSON_INTEGER:
        return False
    def identities(items):
        return (type(items) is list and len(items) <= 30 and
                all(type(item) is int and 1 <= item <= _MAX_EXACT_JSON_INTEGER for item in items) and
                len(items) == len(set(items)))
    for field in ("scope_ids", "requested_ids", "cache_hit_ids", "next_revalidation_ids"):
        if not identities(value[field]):
            return False
    scope = set(value["scope_ids"])
    requested, cached = set(value["requested_ids"]), set(value["cache_hit_ids"])
    if not requested <= scope or not cached <= scope or requested & cached or not set(value["next_revalidation_ids"]) <= scope:
        return False
    failures = value["failed"]
    if type(failures) is not list or len(failures) > 30 or type(releases) is not list or len(releases) > 30:
        return False
    failed_ids, released_ids, urls = [], [], []
    for failure in failures:
        if type(failure) is not dict or set(failure) != {
            "press_release_id", "official_url", "code", "attempt_count", "retry_position",
        }:
            return False
        if (type(failure["press_release_id"]) is not int or not is_micron_detail_url(failure["official_url"])
                or type(failure["code"]) is not str or re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", failure["code"]) is None
                or type(failure["attempt_count"]) is not int or not 0 <= failure["attempt_count"] <= _MAX_EXACT_JSON_INTEGER
                or type(failure["retry_position"]) is not int or not 0 <= failure["retry_position"] <= 30):
            return False
        failed_ids.append(failure["press_release_id"])
        urls.append(failure["official_url"])
    for release in releases:
        if type(release) is not dict or type(release.get("q4_press_release_id")) is not int or not is_micron_detail_url(release.get("official_url")):
            return False
        released_ids.append(release["q4_press_release_id"])
        urls.append(release["official_url"])
    return bool(
        identities(failed_ids) and identities(released_ids)
        and not set(failed_ids) & set(released_ids)
        and set(failed_ids) | set(released_ids) == scope
        and set(released_ids) <= requested | cached and not set(failed_ids) & cached
        and len(urls) == len(set(urls)) and type(complete) is bool and complete == (not failures)
    )


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
    """Read a sealed query with at most 30 heads and a disposable bounded cache."""

    def __init__(
        self,
        *,
        fetch_bytes: Callable[..., bytes] | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        max_workers: int = MICRON_HEAD_MAX_WORKERS,
    ) -> None:
        if fetch_bytes is not None and not callable(fetch_bytes):
            raise TypeError("fetch_bytes must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if monotonic is not None and not callable(monotonic):
            raise TypeError("monotonic must be callable")
        if type(max_workers) is not int or not 1 <= max_workers <= MICRON_HEAD_MAX_WORKERS:
            raise ValueError("max_workers must be a native integer between 1 and 4")
        self._fetch_bytes = fetch_bytes if fetch_bytes is not None else self._default_fetch_bytes
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._max_workers = max_workers
        self._monotonic = monotonic or time.monotonic
        self._metadata_cache: dict[int, dict[str, Any]] = {}
        self._attempts: dict[int, dict[str, Any]] = {}
        self._poll_number = 0
        self._read_lock = threading.Lock()

    @property
    def transport_identity(self) -> Callable[..., bytes]:
        return self._fetch_bytes

    @property
    def max_workers(self) -> int:
        """Expose the configured concurrency for the caller's provenance seal."""
        return self._max_workers

    @property
    def cache_policy(self) -> dict[str, Any]:
        return micron_metadata_cache_policy()

    def _receipt(self) -> datetime:
        value = self._clock()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise MicronIrJsonError("MICRON_IR_TIME_METADATA_INVALID", "receipt clock must return an aware native datetime")
        return value.astimezone(timezone.utc)

    def _monotonic_ms(self) -> int:
        value = self._monotonic()
        if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= _MAX_EXACT_JSON_INTEGER / 1000:
            raise MicronIrJsonError("MICRON_IR_CACHE_INVALID", "cache clock must be a finite non-negative local monotonic value")
        return int(value * 1000)

    def _cache_entry(self, row: dict[str, Any]) -> dict[str, Any] | None:
        entry = self._metadata_cache.get(row["q4_press_release_id"])
        fields = {"row_sha256", "policy_sha256", "published_at", "metadata_date_modified",
                  "time_metadata_sha256", "verified_at_ms", "verified_monotonic_ms", "entry_sha256"}
        if type(entry) is not dict or set(entry) != fields:
            return None
        try:
            if any(type(entry[field]) is not str or re.fullmatch(r"[0-9a-f]{64}", entry[field]) is None
                   for field in ("row_sha256", "policy_sha256", "time_metadata_sha256", "entry_sha256")):
                return None
            if type(entry["published_at"]) is not str or type(entry["metadata_date_modified"]) is not str:
                return None
            if (entry["row_sha256"] != _sha256(row) or entry["policy_sha256"] != _sha256(self.cache_policy)
                    or entry["entry_sha256"] != _sha256({k: v for k, v in entry.items() if k != "entry_sha256"})
                    or type(entry["verified_at_ms"]) is not int or not 0 <= entry["verified_at_ms"] <= _MAX_EXACT_JSON_INTEGER
                    or type(entry["verified_monotonic_ms"]) is not int or not 0 <= entry["verified_monotonic_ms"] <= _MAX_EXACT_JSON_INTEGER):
                return None
            verified = datetime.fromtimestamp(entry["verified_at_ms"] / 1000, tz=timezone.utc)
            published = _time(entry["published_at"], receipt=verified, field="cached datePublished")
            modified = _time(entry["metadata_date_modified"], receipt=verified, field="cached dateModified") if entry["metadata_date_modified"] else ""
            if (published != entry["published_at"] or modified != entry["metadata_date_modified"]
                    or entry["time_metadata_sha256"] != micron_time_metadata_sha256(
                        official_url=row["official_url"], title=row["title"],
                        published_at=published, metadata_date_modified=modified,
                    )):
                return None
        except (TypeError, ValueError, OverflowError, OSError):
            return None
        return entry

    def _remember(self, row: dict[str, Any], projected: dict[str, Any], receipt: datetime) -> None:
        entry = {
            "row_sha256": _sha256(row), "policy_sha256": _sha256(self.cache_policy),
            "published_at": projected["published_at"], "metadata_date_modified": projected["metadata_date_modified"],
            "time_metadata_sha256": projected["time_metadata_sha256"],
            "verified_at_ms": int(receipt.timestamp() * 1000), "verified_monotonic_ms": self._monotonic_ms(),
        }
        entry["entry_sha256"] = _sha256(entry)
        self._metadata_cache[row["q4_press_release_id"]] = entry

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
        require_complete: bool = True,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        if type(require_complete) is not bool:
            raise TypeError("require_complete must be a native boolean")
        controls = {"deadline_monotonic_ms": deadline_monotonic_ms, "cancel_event": cancel_event}
        ensure_source_poll_active(**controls)
        while not self._read_lock.acquire(timeout=0.025):
            ensure_source_poll_active(**controls)
        try:
            return self._read_recent_locked(require_complete=require_complete, controls=controls)
        finally:
            self._read_lock.release()

    def _read_recent_locked(self, *, require_complete: bool, controls: dict[str, Any]) -> dict[str, Any]:
        ensure_source_poll_active(**controls)
        raw = self._fetch_bytes(MICRON_IR_JSON_URL, max_bytes=MICRON_JSON_MAX_BYTES, head_only=False, **controls)
        ensure_source_poll_active(**controls)
        # Validate every list identity and URL before requesting any detail page.
        rows = _list_rows(raw)
        self._poll_number = self._poll_number % _MAX_EXACT_JSON_INTEGER + 1
        ids = {row["q4_press_release_id"] for row in rows}
        if type(self._metadata_cache) is not dict:
            self._metadata_cache = {}
        self._metadata_cache = {identity: value for identity, value in self._metadata_cache.items() if type(identity) is int and identity in ids}
        self._attempts = {identity: value for identity, value in self._attempts.items() if identity in ids}
        if not rows:
            if require_complete:
                return {"releases": [], "complete": True}
            return {"releases": [], "complete": True, "source_errors": [], "metadata_progress": {
                "version": "micron_metadata_progress_v1", "poll_number": self._poll_number,
                "scope_ids": [], "requested_ids": [], "cache_hit_ids": [], "failed": [], "next_revalidation_ids": [],
            }}

        now_ms = int(self._receipt().timestamp() * 1000)
        monotonic_ms = self._monotonic_ms()
        existing = {}
        for row in rows:
            entry = self._cache_entry(row)
            if entry is not None and entry["verified_at_ms"] <= now_ms and entry["verified_monotonic_ms"] <= monotonic_ms:
                existing[row["q4_press_release_id"]] = entry
        primary = rows if require_complete else [row for row in rows if row["q4_press_release_id"] not in existing]
        if not require_complete:
            # Retry an already failed uncached identity after genuinely new or
            # changed identities. Every retained old identity has a separate,
            # bounded revalidation queue and never precedes these new reads.
            primary.sort(key=lambda row: (bool(self._attempts.get(row["q4_press_release_id"], {}).get("code")),))
        old_rows = [row for row in rows if row["q4_press_release_id"] in existing]
        old_rows.sort(key=lambda row: (self._attempts.get(row["q4_press_release_id"], {}).get("poll", 0), row["q4_press_release_id"]))
        revalidate = [] if require_complete else old_rows[:MICRON_METADATA_REVALIDATE_PER_POLL]
        projected, errors, requested_ids = {}, {}, []
        request_lock = threading.Lock()
        revalidation_deadline = 0

        def read_row(row: dict[str, Any], *, revalidation: bool):
            ensure_source_poll_active(**controls)
            call_controls = dict(controls)
            if revalidation:
                call_controls["deadline_monotonic_ms"] = revalidation_deadline
            try:
                ensure_source_poll_active(**call_controls)
                with request_lock:
                    requested_ids.append(row["q4_press_release_id"])
                document = self._fetch_bytes(row["official_url"], max_bytes=MICRON_HEAD_MAX_BYTES, head_only=True, **call_controls)
                receipt = self._receipt()
                ensure_source_poll_active(**call_controls)
            except SourcePollDeadlineExceeded as exc:
                ensure_source_poll_active(**controls)
                if not revalidation:
                    raise
                raise MicronIrJsonError("MICRON_IR_REVALIDATION_TIMEOUT", "old metadata exceeded its bounded revalidation time") from exc
            ensure_source_poll_active(**controls)
            return _project_metadata(document, row, receipt), receipt

        with ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="micron-ir-head") as executor:
            for group, old_revalidation in ((primary, False), (revalidate, True)):
                if old_revalidation:
                    revalidation_deadline = int(time.monotonic() * 1000) + MICRON_METADATA_REVALIDATION_TIMEOUT_MS
                    if controls["deadline_monotonic_ms"]:
                        revalidation_deadline = min(revalidation_deadline, controls["deadline_monotonic_ms"] - MICRON_METADATA_COMMIT_RESERVE_MS)
                for start in range(0, len(group), self._max_workers):
                    ensure_source_poll_active(**controls)
                    batch = group[start:start + self._max_workers]
                    if old_revalidation and controls["deadline_monotonic_ms"] and (
                        controls["deadline_monotonic_ms"] - int(time.monotonic() * 1000)
                        <= MICRON_METADATA_COMMIT_RESERVE_MS + 25
                    ):
                        for row in batch:
                            errors[row["q4_press_release_id"]] = ("MICRON_IR_REVALIDATION_DEFERRED", "old metadata revalidation deferred to retain the poll commit reserve")
                        continue
                    futures = []
                    for row in batch:
                        identity = row["q4_press_release_id"]
                        previous_count = self._attempts.get(identity, {}).get("count", 0)
                        self._attempts[identity] = {"count": min(_MAX_EXACT_JSON_INTEGER, previous_count + 1), "poll": self._poll_number, "code": ""}
                        futures.append((row, executor.submit(read_row, row, revalidation=old_revalidation)))
                    for row, future in futures:
                        identity = row["q4_press_release_id"]
                        try:
                            value, receipt = future.result()
                            self._remember(row, value, receipt)
                            projected[identity] = value
                        except (SourcePollCancelled, SourcePollDeadlineExceeded):
                            raise
                        except Exception as exc:
                            code = exc.code if type(exc) is MicronIrJsonError else "MICRON_IR_METADATA_REQUEST_FAILED"
                            self._attempts[identity]["code"] = code
                            if require_complete:
                                raise
                            errors[identity] = (code, str(exc)[:200])
        ensure_source_poll_active(**controls)
        if require_complete:
            return {"releases": [projected[row["q4_press_release_id"]] for row in rows], "complete": True}

        now_ms = int(self._receipt().timestamp() * 1000)
        monotonic_ms = self._monotonic_ms()
        cached_ids = []
        for row in rows:
            identity = row["q4_press_release_id"]
            if identity in projected or identity in errors:
                continue
            entry = existing.get(identity)
            failure_code = self._attempts.get(identity, {}).get("code")
            if failure_code:
                errors[identity] = (failure_code, "previously failed metadata awaits its next bounded retry")
            elif (entry is None or entry["verified_at_ms"] > now_ms or entry["verified_monotonic_ms"] > monotonic_ms
                  or max(now_ms - entry["verified_at_ms"], monotonic_ms - entry["verified_monotonic_ms"]) > MICRON_METADATA_MAX_AGE_MS):
                errors[identity] = ("MICRON_IR_METADATA_CACHE_EXPIRED", "metadata is outside its bounded verification age")
            else:
                projected[identity] = {**row, **{key: entry[key] for key in ("published_at", "metadata_date_modified", "time_metadata_sha256")}}
                cached_ids.append(identity)
        retry_queue = sorted(existing, key=lambda identity: (self._attempts.get(identity, {}).get("poll", 0), identity))
        failures, source_errors = [], []
        for row in rows:
            identity = row["q4_press_release_id"]
            if identity not in errors:
                continue
            code, message = errors[identity]
            attempt_count = self._attempts.get(identity, {}).get("count", 0)
            retry_position = retry_queue.index(identity) + 1 if identity in retry_queue else 0
            failures.append({"press_release_id": identity, "official_url": row["official_url"],
                             "code": code, "attempt_count": attempt_count, "retry_position": retry_position})
            source_errors.append({**failures[-1], "message": (
                f"Micron ID {identity}: {message}; attempt_count={attempt_count}; retry_position={retry_position}"
            )})
        progress = {
            "version": "micron_metadata_progress_v1", "poll_number": self._poll_number,
            "scope_ids": [row["q4_press_release_id"] for row in rows],
            "requested_ids": requested_ids, "cache_hit_ids": cached_ids,
            "failed": failures, "next_revalidation_ids": retry_queue[:MICRON_METADATA_REVALIDATE_PER_POLL],
        }
        # A degraded poll deliberately cannot advance the durable checkpoint.
        # Put newly validated list identities before cached replay candidates so
        # repeated already-imported items cannot occupy every delivery slot.
        primary_ids = {row["q4_press_release_id"] for row in primary}
        delivery_rows = primary + [row for row in rows if row["q4_press_release_id"] not in primary_ids]
        releases = [projected[row["q4_press_release_id"]] for row in delivery_rows if row["q4_press_release_id"] in projected]
        if not valid_micron_metadata_progress(progress, releases, complete=not failures):
            raise MicronIrJsonError("MICRON_IR_CACHE_INVALID", "metadata cache admission report is inconsistent")
        return {"releases": releases, "complete": not failures,
                "source_errors": source_errors, "metadata_progress": progress}


__all__ = [
    "MICRON_IR_JSON_URL", "MICRON_TIME_METADATA_HASH_SEMANTICS", "MICRON_HEAD_MAX_WORKERS",
    "MicronIrJsonClient", "MicronIrJsonError", "is_micron_detail_url",
    "micron_time_metadata_sha256", "is_micron_declared_wall_time",
    "micron_metadata_cache_policy", "valid_micron_metadata_progress",
]
