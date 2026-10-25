from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


_URL_RE = re.compile(r"https?://[^\s<>\]\[(){}\"']+", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9._+-]*", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "spm",
    }
)


def canonicalize_url(value: Any) -> str:
    """Return a stable, fetch-free HTTP(S) URL representation."""

    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    raw = raw.rstrip(".,;:!?\u3002\uff0c\uff1b\uff1a\uff01\uff1f")
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
        port = parsed.port
    except (UnicodeError, ValueError):
        return ""
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    host = hostname if port is None else f"{hostname}:{port}"
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if path != "/":
        path = path.rstrip("/") or "/"
    query_rows = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = unicodedata.normalize("NFKC", key).strip()
        if normalized_key.casefold().startswith("utm_"):
            continue
        if normalized_key.casefold() in _TRACKING_QUERY_KEYS:
            continue
        query_rows.append((normalized_key, unicodedata.normalize("NFKC", item).strip()))
    query = urlencode(sorted(query_rows), doseq=True)
    return urlunsplit((scheme, host, path, query, ""))


def extract_urls(text: Any, declared_urls: Iterable[Any] = ()) -> tuple[str, ...]:
    candidates = [str(item or "") for item in declared_urls]
    candidates.extend(_URL_RE.findall(str(text or "")))
    return tuple(
        sorted(
            {
                canonical
                for candidate in candidates
                if (canonical := canonicalize_url(candidate))
            }
        )
    )


def normalize_text(value: Any) -> str:
    """Normalize presentation noise while retaining semantic words and CJK."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = _URL_RE.sub(" ", text)
    normalized: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        if character.isalnum() or "\u3400" <= character <= "\u9fff":
            normalized.append(character)
        elif category.startswith(("P", "S", "Z", "C")):
            normalized.append(" ")
        else:
            normalized.append(character)
    return " ".join("".join(normalized).split())


def _message_text(message: Any) -> str:
    content = getattr(message, "content_text", None)
    if callable(content):
        content = content()
    if isinstance(content, str):
        return content
    text = str(getattr(message, "text", "") or "").strip()
    caption = str(getattr(message, "caption", "") or "").strip()
    return "\n".join(item for item in (text, caption) if item)


def _message_urls(message: Any) -> Sequence[Any]:
    values = getattr(message, "urls", ())
    return values if isinstance(values, (list, tuple, set, frozenset)) else ()


def content_fingerprint(message: Any) -> str:
    normalized = normalize_text(_message_text(message))
    urls = extract_urls(_message_text(message), _message_urls(message))
    payload = normalized + "\n" + "\n".join(urls)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tokens(value: str) -> tuple[str, ...]:
    tokens = list(_WORD_RE.findall(value))
    for run in _CJK_RE.findall(value):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tuple(tokens)


def simhash64(value: Any) -> int:
    normalized = normalize_text(value)
    tokens = _tokens(normalized)
    if not tokens:
        return 0
    vector = [0] * 64
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        hashed = int.from_bytes(digest, "big", signed=False)
        for bit in range(64):
            vector[bit] += 1 if hashed & (1 << bit) else -1
    output = 0
    for bit, weight in enumerate(vector):
        if weight >= 0:
            output |= 1 << bit
    return output


def hamming_distance(left: int, right: int) -> int:
    if type(left) is not int or type(right) is not int or left < 0 or right < 0:
        raise ValueError("simhash values must be non-negative integers")
    return (left ^ right).bit_count()


def simhash_similarity(left: int, right: int) -> float:
    return max(0.0, min(1.0, 1.0 - hamming_distance(left, right) / 64.0))


def message_similarity(left: Any, right: Any) -> float:
    left_text = normalize_text(_message_text(left))
    right_text = normalize_text(_message_text(right))
    left_urls = set(extract_urls(_message_text(left), _message_urls(left)))
    right_urls = set(extract_urls(_message_text(right), _message_urls(right)))
    if left_text and left_text == right_text and left_urls == right_urls:
        return 1.0
    text_score = simhash_similarity(simhash64(left_text), simhash64(right_text))
    if not left_text or not right_text:
        text_score = 0.0
    url_score = 0.0
    if left_urls and right_urls:
        url_score = len(left_urls & right_urls) / len(left_urls | right_urls)
    return max(text_score, min(1.0, 0.75 * text_score + 0.25 * url_score))


def _identity(message: Any) -> tuple[Any, ...]:
    identity = getattr(message, "identity_key", None)
    if callable(identity):
        identity = identity()
    if isinstance(identity, tuple):
        return identity
    return (
        str(getattr(message, "source", "telegram") or "telegram"),
        getattr(message, "chat_id", None),
        getattr(message, "message_id", None),
    )


@dataclass(frozen=True, slots=True)
class DedupResult:
    duplicate_of: tuple[Any, ...] | None
    duplicate_score: float
    duplicate_reason: str
    content_hash: str
    simhash: int
    normalized_text: str
    normalized_urls: tuple[str, ...]

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None


class Deduplicator:
    """Deterministic two-level deduplication without external models."""

    def __init__(self, *, similarity_threshold: float = 0.86) -> None:
        if not 0.5 <= similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold must be between 0.5 and 1.0")
        self.similarity_threshold = float(similarity_threshold)

    def evaluate(self, message: Any, candidates: Iterable[Any] = ()) -> DedupResult:
        text = _message_text(message)
        normalized = normalize_text(text)
        urls = extract_urls(text, _message_urls(message))
        fingerprint = content_fingerprint(message)
        fingerprint_simhash = simhash64(normalized)
        identity = _identity(message)
        best: tuple[Any, float, str] | None = None
        for candidate in candidates:
            candidate_identity = _identity(candidate)
            if candidate_identity == identity:
                best = (candidate_identity, 1.0, "identity_key")
                break
            candidate_text = _message_text(candidate)
            candidate_urls = extract_urls(candidate_text, _message_urls(candidate))
            if fingerprint == content_fingerprint(candidate):
                score, reason = 1.0, "content_hash"
            elif urls and urls == candidate_urls:
                score, reason = max(0.94, message_similarity(message, candidate)), "canonical_url"
            else:
                score, reason = message_similarity(message, candidate), "simhash"
            if score >= self.similarity_threshold and (best is None or score > best[1]):
                best = (candidate_identity, score, reason)
        return DedupResult(
            duplicate_of=best[0] if best else None,
            duplicate_score=round(best[1], 6) if best else 0.0,
            duplicate_reason=best[2] if best else "",
            content_hash=fingerprint,
            simhash=fingerprint_simhash,
            normalized_text=normalized,
            normalized_urls=urls,
        )


__all__ = [
    "DedupResult",
    "Deduplicator",
    "canonicalize_url",
    "content_fingerprint",
    "extract_urls",
    "hamming_distance",
    "message_similarity",
    "normalize_text",
    "simhash64",
    "simhash_similarity",
]
